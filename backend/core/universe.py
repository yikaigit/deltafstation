"""标的池（universe）：元信息、HS300 成分快照、行业映射。

三个数据来源拼装
----------------
1. baostock ``query_hs300_stocks`` —— 当前成分与名称，同时把 ``updateDate`` 落成
   **不可变快照**（``lake/index_members/hs300_YYYYMMDD.parquet``）。上线越久快照越多，
   将来可用「历史上每个快照日的成分」做近似 point-in-time 池，缓解幸存者偏差。
2. baostock ``query_stock_basic`` —— 上市日 / 退市日。
3. 东财估值横截面 ``BOARD_NAME`` —— 行业分类（比 baostock 的行业更细、更新更快）。

注意：baostock 只返回**当前**成分。用当前成分回测 2008 年起的区间存在幸存者偏差，
必须在报告与 UI 中披露（见规格 §4.7）。
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Sequence

import pandas as pd

from .codes import board_of, normalize_bs_code
from .config import Config, get_config
from .lake import Lake, get_lake
from . import providers

logger = logging.getLogger(__name__)

DEFAULT_INDEX = "hs300"

UNIVERSE_COLUMNS = [
    "code", "name", "board", "industry_l1", "list_date",
    "delist_date", "is_st", "in_hs300", "updated_at",
]


class Universe:
    """标的池构建与查询。"""

    def __init__(self, cfg: Optional[Config] = None, lake: Optional[Lake] = None):
        self.cfg = cfg or get_config()
        self.lake = lake or get_lake()

    # ---------- 读取 ----------

    def load(self) -> pd.DataFrame:
        frame = self.lake.read("universe")
        if frame.empty:
            return pd.DataFrame(columns=UNIVERSE_COLUMNS)
        return frame

    def codes(self, in_hs300_only: bool = True, exclude_st: Optional[bool] = None) -> List[str]:
        """标的代码列表。"""
        frame = self.load()
        if frame.empty:
            return []
        if in_hs300_only and "in_hs300" in frame.columns:
            frame = frame[frame["in_hs300"].fillna(False).astype(bool)]
        if exclude_st is None:
            exclude_st = bool(self.cfg.get("universe.exclude_st", True))
        if exclude_st and "is_st" in frame.columns:
            frame = frame[~frame["is_st"].fillna(False).astype(bool)]
        return sorted(frame["code"].astype(str).tolist())

    def industry_map(self) -> Dict[str, str]:
        frame = self.load()
        if frame.empty or "industry_l1" not in frame.columns:
            return {}
        pairs = frame[["code", "industry_l1"]].dropna()
        return dict(zip(pairs["code"], pairs["industry_l1"]))

    def name_map(self) -> Dict[str, str]:
        frame = self.load()
        if frame.empty or "name" not in frame.columns:
            return {}
        pairs = frame[["code", "name"]].dropna()
        return dict(zip(pairs["code"], pairs["name"]))

    def count(self) -> int:
        return len(self.load())

    # ---------- 刷新 ----------

    def refresh(
        self,
        index: str = DEFAULT_INDEX,
        with_basic: bool = True,
        industry_from_em: bool = True,
        on_progress=None,
    ) -> dict:
        """刷新标的池：成分快照 → 元信息 → 行业。

        Args:
            index: 目前只支持 hs300
            with_basic: 是否逐只拉 baostock 基础信息（300 次调用，约 1~2 分钟）
            industry_from_em: 是否用东财当日估值横截面补行业

        Returns:
            统计字典（新增/更新/成分日/行业覆盖等）
        """
        if index != DEFAULT_INDEX:
            raise ValueError(f"暂只支持 {DEFAULT_INDEX}，收到 {index}")

        def report(message: str) -> None:
            if on_progress:
                on_progress(-1.0, message)
            logger.info(message)

        # 1. 成分快照
        report("拉取沪深300 成分")
        members = providers.get_baostock().hs300_stocks()
        if members.empty:
            raise RuntimeError("沪深300 成分为空，数据源异常")
        snapshot_date = pd.Timestamp(members["snapshot_date"].iloc[0])
        self.lake.write_index_members(snapshot_date, index, members["code"].tolist())
        report(f"成分快照 {snapshot_date.date()}，共 {len(members)} 只")

        codes = members["code"].tolist()
        base = pd.DataFrame({"code": codes, "name": members["name"].values})
        base["board"] = base["code"].map(board_of)
        base["in_hs300"] = True
        base["list_date"] = pd.NaT
        base["delist_date"] = pd.NaT
        base["is_st"] = False
        base["industry_l1"] = None

        # 2. 基础信息（上市日/退市日）
        if with_basic:
            report(f"拉取 {len(codes)} 只标的基础信息")
            basics = []
            provider = providers.get_baostock()
            with provider.session():
                for i, code in enumerate(codes, 1):
                    try:
                        info = provider.stock_basic(code)
                    except Exception as exc:  # 单只失败不中断
                        logger.warning("拉取 %s 基础信息失败：%s", code, exc)
                        info = {}
                    if info:
                        basics.append(info)
                    if on_progress and i % 25 == 0:
                        on_progress(i / len(codes) * 0.7, f"基础信息 {i}/{len(codes)}")
            if basics:
                info_frame = pd.DataFrame(basics)[["code", "list_date", "delist_date"]]
                base = base.drop(columns=["list_date", "delist_date"]).merge(
                    info_frame, on="code", how="left"
                )

        # 3. 行业（东财当日横截面）
        if industry_from_em:
            report("用东财估值横截面补行业")
            try:
                latest = self.lake.latest_date("valuation")
                # 不能直接用 today()：东财只在交易日有横截面数据，
                # 周末/节假日跑会拿到空表，行业覆盖率直接掉成 0（实测 300 只全空）。
                trade_date = latest or self._recent_trading_day()
                industry = self._industry_from_em(trade_date)
                if industry:
                    base["industry_l1"] = base["code"].map(industry)
            except Exception as exc:
                logger.warning("东财行业获取失败，行业留空：%s", exc)

        # 4. ST 标记（来自最近一个交易日的行情，若已回填）
        base["is_st"] = self._st_from_bars(base["code"].tolist())

        base["updated_at"] = pd.Timestamp.now().normalize()
        base = base[UNIVERSE_COLUMNS]
        total = self.lake.write("universe", base)

        industry_covered = int(base["industry_l1"].notna().sum())
        result = {
            "index": index,
            "snapshot_date": snapshot_date.strftime("%Y-%m-%d"),
            "symbols": len(base),
            "total_rows": total,
            "industry_covered": industry_covered,
            "industry_missing": len(base) - industry_covered,
            "list_date_covered": int(base["list_date"].notna().sum()),
        }
        report(
            f"标的池更新完成：{result['symbols']} 只，行业覆盖 "
            f"{industry_covered}/{len(base)}"
        )
        return result

    # ---------- 辅助 ----------

    def _recent_trading_day(self) -> str:
        """最近一个交易日（YYYY-MM-DD）。日历不可用时退化为今天。"""
        try:
            from .calendar import get_calendar

            day = get_calendar().latest_trading_day()
            if day is not None:
                return pd.Timestamp(day).strftime("%Y-%m-%d")
        except Exception as exc:  # 日历不可用不该阻断标的池刷新
            logger.debug("取交易日失败，退化为今天：%s", exc)
        return pd.Timestamp.today().strftime("%Y-%m-%d")

    def _industry_from_em(self, trade_date: str) -> Dict[str, str]:
        """从数据湖已有的估值表取行业；没有则实时拉一次。

        实时拉取时若指定日无数据（非交易日），自动往前回退最多 7 天 ——
        东财的横截面只覆盖交易日，周末/节假日直接查会拿到空表。
        """
        if self.lake.exists("valuation"):
            latest = self.lake.latest_date("valuation")
            if latest:
                frame = self.lake.read("valuation", columns=["code", "industry"], start=latest, end=latest)
                if not frame.empty and "industry" in frame.columns:
                    frame = frame.dropna(subset=["industry"])
                    if not frame.empty:
                        return dict(zip(frame["code"], frame["industry"]))
        provider = providers.get_eastmoney()
        day = pd.Timestamp(trade_date)
        for back in range(7):
            probe = (day - pd.Timedelta(days=back)).strftime("%Y-%m-%d")
            snapshot = provider.valuation_snapshot(probe)
            if snapshot.empty:
                continue
            snapshot = snapshot.dropna(subset=["industry"])
            if not snapshot.empty:
                logger.info("行业取自 %s 的估值横截面", probe)
                return dict(zip(snapshot["code"], snapshot["industry"]))
        return {}

    def _st_from_bars(self, codes: Sequence[str]) -> List[bool]:
        """从最近一个交易日的行情取 ST 标记（未回填时全 False）。"""
        blank = [False] * len(codes)
        if not self.lake.exists("bars"):
            return blank
        try:
            latest = self.lake.latest_date("bars")
            if not latest:
                return blank
            frame = self.lake.read("bars", columns=["code", "is_st"], start=latest, end=latest)
            if frame.empty or "is_st" not in frame.columns:
                return blank
            mapping = dict(zip(frame["code"], frame["is_st"].fillna(False).astype(bool)))
            return [bool(mapping.get(c, False)) for c in codes]
        except Exception:
            return blank

    # ---------- 成分快照 ----------

    def snapshots(self, index: str = DEFAULT_INDEX) -> List[str]:
        folder = self.lake.dataset_path("universe").parent / "index_members"
        if not folder.exists():
            return []
        return sorted(p.stem.split("_", 1)[1] for p in folder.glob(f"{index}_*.parquet"))

    def members_on(self, snapshot_date: str, index: str = DEFAULT_INDEX) -> List[str]:
        """读取某日成分快照。"""
        path = (
            self.lake.dataset_path("universe").parent
            / "index_members"
            / f"{index}_{str(snapshot_date).replace('-', '')}.parquet"
        )
        if not path.exists():
            return []
        frame = pd.read_parquet(path)
        return frame["code"].astype(str).tolist()


def get_universe() -> Universe:
    return Universe(get_config(), get_lake())
