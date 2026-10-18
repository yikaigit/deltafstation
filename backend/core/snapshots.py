"""打分快照存取。

目录结构：
    data/snapshots/score/version=v1.0.0/trade_date=2026-10-16/part.parquet

每天一个文件、每版一个目录，永不覆盖历史 —— 这是「任意一天的分都能回溯」的物理保证。
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

from .config import Config, get_config

SCORE_COLUMNS = [
    "trade_date", "code", "version", "score_pct", "score_abs", "grade",
    "dim_trend", "dim_momentum", "dim_volume", "dim_risk",
    "dim_valuation", "dim_quality", "data_quality",
]


#: ``read_tail`` 的内存缓存：(版本, 天数) → (落盘时间, 合并后的完整 DataFrame)
#:
#: 为什么需要它：按日一文件的设计让「读最近 250 天」变成「打开 250 个 parquet」，
#: 实测固定开销 ~0.55s，其中 0.5s 纯粹是 250 次 open/parse —— 与要读多少列无关。
#: 个股抽屉是高频交互，这个开销直接体现为点击后转圈。
#: 缓存只按 (版本, days) 维度存整表（7.5 万行 × 十几列 ≈ 十几 MB），
#: 之后按 code 过滤是内存切片（~5ms）。进程内缓存，重启即失效，不会读到旧数据。
_TAIL_CACHE: Dict[Tuple[str, int], Tuple[float, pd.DataFrame]] = {}

#: 缓存有效期（秒）。打分任务写新快照后最多等这么久就能看到。
_TAIL_CACHE_TTL = 120.0


class ScoreStore:
    def __init__(self, cfg: Optional[Config] = None):
        self.cfg = cfg or get_config()
        self.root = self.cfg.paths.snapshots / "score"

    # ---------- 查询 ----------

    def versions(self) -> List[str]:
        if not self.root.exists():
            return []
        return sorted(p.name.split("=", 1)[1] for p in self.root.glob("version=*") if p.is_dir())

    def dates(self, version: str) -> List[str]:
        folder = self.root / f"version={version}"
        if not folder.exists():
            return []
        return sorted(p.name.split("=", 1)[1] for p in folder.glob("trade_date=*") if p.is_dir())

    def latest_date(self, version: Optional[str] = None) -> Optional[str]:
        versions = [version] if version else self.versions()
        dates = [d for v in versions for d in self.dates(v)]
        return max(dates) if dates else None

    def path_for(self, version: str, trade_date: str) -> Path:
        return self.root / f"version={version}" / f"trade_date={trade_date}" / "part.parquet"

    def read(self, version: Optional[str] = None, trade_date: Optional[str] = None) -> pd.DataFrame:
        """读取快照；未指定版本/日期时读取全部（调用方自行过滤）。"""
        if not self.root.exists():
            return pd.DataFrame(columns=SCORE_COLUMNS)

        versions = [version] if version else self.versions()
        frames = []
        for v in versions:
            folder = self.root / f"version={v}"
            if not folder.exists():
                continue
            files = sorted(folder.glob("trade_date=*/part.parquet"))
            if trade_date:
                files = [p for p in files if p.parent.name == f"trade_date={trade_date}"]
            for f in files:
                frames.append(pd.read_parquet(f))
        if not frames:
            return pd.DataFrame(columns=SCORE_COLUMNS)
        return pd.concat(frames, ignore_index=True)

    def read_tail(self, version: str, days: int = 250,
                  codes: Optional[Sequence[str]] = None) -> pd.DataFrame:
        """只读最近 ``days`` 个交易日的快照。

        全量快照按日一文件，``read()`` 要把几千个文件全读进来 —— 个股详情页
        只画最近一年的曲线，没必要为此付出几秒的 I/O。这里按已知日期直接
        构造路径，跳过 ``glob`` 扫描。
        """
        dates = self.dates(version)[-int(days):] if days else self.dates(version)
        if not dates:
            return pd.DataFrame(columns=SCORE_COLUMNS)

        key = (str(version), int(days))
        cached = _TAIL_CACHE.get(key)
        if cached is not None and time.monotonic() - cached[0] < _TAIL_CACHE_TTL:
            full = cached[1]
        else:
            frames = []
            for day in dates:
                path = self.path_for(version, day)
                if path.exists():
                    frames.append(pd.read_parquet(path))
            if not frames:
                return pd.DataFrame(columns=SCORE_COLUMNS)
            full = pd.concat(frames, ignore_index=True)
            # 只保留最近一两份，避免不同 days 参数把内存堆起来
            if len(_TAIL_CACHE) >= 4:
                _TAIL_CACHE.clear()
            _TAIL_CACHE[key] = (time.monotonic(), full)

        if codes is None:
            return full
        return full[full["code"].isin(set(codes))]

    # ---------- 写入 ----------

    def write(self, df: pd.DataFrame, version: str, trade_date: Optional[str] = None) -> Path:
        """写入某一天的打分快照（按 trade_date 覆盖同日，不覆盖其他日期）。"""
        # 新快照落盘 → 缓存必须作废，否则详情页会拿着旧的 250 天窗口
        _TAIL_CACHE.clear()
        if df is None or df.empty:
            raise ValueError("打分快照为空，拒绝写入")
        data = df.copy()
        if "trade_date" not in data.columns:
            if not trade_date:
                raise ValueError("缺少 trade_date 列且未显式指定日期")
            data["trade_date"] = pd.Timestamp(trade_date)
        data["trade_date"] = pd.to_datetime(data["trade_date"]).dt.normalize()
        data["version"] = version

        for day, part in data.groupby("trade_date"):
            path = self.path_for(version, pd.Timestamp(day).strftime("%Y-%m-%d"))
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            part.to_parquet(tmp, index=False, compression="snappy")
            os.replace(tmp, path)
        return self.root / f"version={version}"


def get_score_store() -> ScoreStore:
    return ScoreStore(get_config())
