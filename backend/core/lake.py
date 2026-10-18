"""Parquet 数据湖读写。

设计要点（见 docs/system-spec.md §4.2）：
* 行情与估值按 ``year=YYYY/part.parquet`` 分区，便于按年增量写入与回溯；
* 其余数据集（universe / adj_factor / fundamental / calendar）为单文件；
* 写入一律「读旧 → 合并去重（keep last）→ 原子替换」，保证幂等重跑；
* 读取优先用 pyarrow.dataset（按列裁剪 + 分区裁剪），无 pyarrow 时给出明确报错。

不在这里做任何业务计算，只负责存取。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional, Sequence

import pandas as pd

from .config import Config, get_config

#: 数据集注册表：主键与是否按年分区
DATASETS = {
    "bars": {"keys": ["date", "code"], "partition": True},
    "index_bars": {"keys": ["date", "code"], "partition": True},
    "valuation": {"keys": ["date", "code"], "partition": True},
    "adj_factor": {"keys": ["code", "ex_date"], "partition": False},
    "fundamental": {"keys": ["code", "report_date"], "partition": False},
    "universe": {"keys": ["code"], "partition": False},
    "calendar": {"keys": ["date"], "partition": False},
}

#: 需要统一成 datetime64[ns] 的列
DATE_COLUMNS = ("date", "ex_date", "report_date", "notice_date", "list_date", "delist_date", "snapshot_date")


class LakeError(RuntimeError):
    """数据湖读写异常。"""


def _require_pyarrow():
    try:
        import pyarrow  # noqa: F401
        import pyarrow.dataset  # noqa: F401
    except ImportError as exc:  # pragma: no cover - 环境缺依赖时的明确提示
        raise LakeError("需要 pyarrow 才能读写 Parquet，请执行 pip install pyarrow") from exc


def _normalize_dates(df: pd.DataFrame) -> pd.DataFrame:
    """把已知日期列统一为 datetime64[ns]（只处理存在的列）。"""
    for col in DATE_COLUMNS:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.normalize()
    return df


class Lake:
    """Parquet 数据湖。"""

    def __init__(self, cfg: Optional[Config] = None):
        self.cfg = cfg or get_config()
        self.root = self.cfg.paths.lake

    # ---------- 路径 ----------

    def dataset_path(self, name: str) -> Path:
        if name not in DATASETS:
            raise LakeError(f"未注册的数据集: {name}，可用: {sorted(DATASETS)}")
        return self.root / name

    def partition_path(self, name: str, year: int) -> Path:
        return self.dataset_path(name) / f"year={year}" / "part.parquet"

    def file_path(self, name: str) -> Path:
        return self.dataset_path(name) / "part.parquet"

    def exists(self, name: str) -> bool:
        path = self.dataset_path(name)
        if not path.exists():
            return False
        if DATASETS[name]["partition"]:
            return any(path.glob("year=*/part.parquet"))
        return self.file_path(name).exists()

    # ---------- 读 ----------

    def read(
        self,
        name: str,
        columns: Optional[Sequence[str]] = None,
        years: Optional[Iterable[int]] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        filters: Optional[Sequence[tuple]] = None,
    ) -> pd.DataFrame:
        """读取数据集；不存在时返回空 DataFrame。

        ``filters`` 是 ``[(列名, 操作符, 值), ...]``，操作符支持
        ``= / != / > / >= / < / <= / in``（与 pyarrow 的写法一致）。
        它会被下推到 Parquet 层，所以「只读一只票的复权因子」不用把整张
        20 万行的表拉进 pandas —— 实测接口从 0.8s 降到 0.1s 量级。
        """
        if not self.exists(name):
            return pd.DataFrame(columns=list(columns) if columns else None)

        _require_pyarrow()
        import pyarrow.dataset as ds

        spec = DATASETS[name]
        if spec["partition"]:
            paths = sorted(self.dataset_path(name).glob("year=*/part.parquet"))
            if years is not None:
                keep = {int(y) for y in years}
                paths = [p for p in paths if int(p.parent.name.split("=")[1]) in keep]
            if not paths:
                return pd.DataFrame(columns=list(columns) if columns else None)
            dataset = ds.dataset([str(p) for p in paths], format="parquet")
        else:
            dataset = ds.dataset(str(self.file_path(name)), format="parquet")

        expr = None
        if filters:
            import pyarrow.dataset as pds

            field_of = {name: pds.field(name) for name, _, _ in filters}
            for col, op, value in filters:
                f = field_of[col]
                if op == "in":
                    cond = f.isin(list(value))
                elif op == "=":
                    cond = f == value
                elif op == "!=":
                    cond = f != value
                elif op == ">":
                    cond = f > value
                elif op == ">=":
                    cond = f >= value
                elif op == "<":
                    cond = f < value
                elif op == "<=":
                    cond = f <= value
                else:
                    raise LakeError(f"不支持的过滤操作符: {op}")
                expr = cond if expr is None else (expr & cond)
        table = dataset.to_table(
            columns=list(columns) if columns else None,
            filter=expr,
        )
        df = table.to_pandas()
        if "date" in df.columns and (start or end):
            if start:
                df = df[df["date"] >= pd.Timestamp(start)]
            if end:
                df = df[df["date"] <= pd.Timestamp(end)]
        return df.reset_index(drop=True)

    # ---------- 写 ----------

    @staticmethod
    def _upsert(existing: Optional[pd.DataFrame], new: pd.DataFrame, keys: Sequence[str]) -> pd.DataFrame:
        if existing is None or existing.empty:
            out = new.copy()
        else:
            out = pd.concat([existing, new], ignore_index=True)
            out = out.drop_duplicates(subset=list(keys), keep="last")
        sort_keys = [k for k in keys if k in out.columns]
        if sort_keys:
            out = out.sort_values(sort_keys)
        return out.reset_index(drop=True)

    @staticmethod
    def _atomic_write(df: pd.DataFrame, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        df.to_parquet(tmp, index=False, compression="snappy")
        os.replace(tmp, path)

    def write(self, name: str, df: pd.DataFrame) -> int:
        """写入数据集（幂等 upsert），返回写入后的总行数。"""
        if df is None or df.empty:
            return 0
        _require_pyarrow()
        spec = DATASETS[name]
        keys = spec["keys"]
        missing = [k for k in keys if k not in df.columns]
        if missing:
            raise LakeError(f"{name} 缺少主键列 {missing}")

        df = _normalize_dates(df.copy())

        if not spec["partition"]:
            path = self.file_path(name)
            existing = self.read(name) if path.exists() else None
            merged = self._upsert(existing, df, keys)
            self._atomic_write(merged, path)
            return len(merged)

        if "date" not in df.columns:
            raise LakeError(f"{name} 是按年分区的数据集，必须有 date 列")
        total = 0
        for year, part in df.groupby(df["date"].dt.year):
            path = self.partition_path(name, int(year))
            existing = self.read(name, years=[int(year)]) if path.exists() else None
            merged = self._upsert(existing, part, keys)
            self._atomic_write(merged, path)
            total += len(merged)
        return total

    # ---------- 指数成分快照 ----------

    def write_index_members(self, snapshot_date, index_code: str, codes: Sequence[str]) -> Path:
        """写一份成分股快照（按快照日分文件，永不覆盖历史）。"""
        ts = pd.Timestamp(snapshot_date).strftime("%Y%m%d")
        path = self.dataset_path("universe").parent / "index_members" / f"{index_code}_{ts}.parquet"
        df = pd.DataFrame({"snapshot_date": pd.Timestamp(snapshot_date), "index_code": index_code, "code": list(codes)})
        df = _normalize_dates(df)
        self._atomic_write(df, path)
        return path

    def latest_index_snapshot(self, index_code: str) -> pd.DataFrame:
        """读取最新一份成分快照；没有则返回空表。"""
        folder = self.dataset_path("universe").parent / "index_members"
        if not folder.exists():
            return pd.DataFrame(columns=["snapshot_date", "index_code", "code"])
        files = sorted(folder.glob(f"{index_code}_*.parquet"))
        if not files:
            return pd.DataFrame(columns=["snapshot_date", "index_code", "code"])
        return pd.read_parquet(files[-1])

    # ---------- 覆盖情况 ----------

    def latest_date(self, name: str = "bars") -> Optional[str]:
        """数据集的最新日期（只读最后一个分区，避免全量扫描）。"""
        if not self.exists(name):
            return None
        spec = DATASETS[name]
        try:
            if spec["partition"]:
                parts = sorted(self.dataset_path(name).glob("year=*/part.parquet"))
                if not parts:
                    return None
                df = pd.read_parquet(parts[-1], columns=["date"])
            else:
                df = pd.read_parquet(self.file_path(name), columns=["date"])
        except Exception:
            return None
        if df.empty:
            return None
        return pd.Timestamp(df["date"].max()).strftime("%Y-%m-%d")

    def coverage(self, name: str = "bars") -> pd.DataFrame:
        """每只标的的数据覆盖情况：首末日、行数。"""
        df = self.read(name, columns=["date", "code"])
        if df.empty:
            return pd.DataFrame(columns=["code", "first_date", "last_date", "rows"])
        grouped = df.groupby("code")["date"].agg(first_date="min", last_date="max", rows="count")
        return grouped.reset_index().sort_values("code")

    # ---------- SQL ----------

    def sql(self, query: str) -> pd.DataFrame:
        """用 DuckDB 直接查询数据湖（query 中的 ``{lake}`` 会替换为湖根目录）。"""
        try:
            import duckdb
        except ImportError as exc:  # pragma: no cover
            raise LakeError("需要 duckdb 才能执行 SQL，请执行 pip install duckdb") from exc
        return duckdb.sql(query.replace("{lake}", str(self.root).replace("\\", "/"))).df()


def get_lake() -> Lake:
    """便捷入口：使用全局配置构造 Lake。"""
    return Lake(get_config())
