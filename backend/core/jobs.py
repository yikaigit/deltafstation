"""后台任务执行器。

为什么不用 Celery / RQ
----------------------
本系统是**单机单用户**工具，任务都是「串行访问免费数据源」的类型：
baostock 是全局单连接（并发无意义），东财需要限速（并发会被封）。
引入消息队列只会增加部署负担。这里用 ``ThreadPoolExecutor(max_workers=1)``
串行执行，配合 SQLite 的 ``job_runs`` 记录状态，足够且可观测。

任务类型
--------
| kind | 说明 | 参数 |
|---|---|---|
| update | 每日增量更新 | trade_date?, with_fundamental? |
| backfill | 全量回填 | start?, end?, valuation_years?, with_fundamental? |
| score | 每日打分（P2 实现） | — |
| evaluate | 因子评价（P5 实现） | — |

取消
----
``cancel()`` 只设置标志位，由进度回调在每个标的之间检查并抛 ``JobCancelled``。
正在进行的单次网络请求不会被中断（无法安全打断 requests/baostock）。
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional

from .config import get_config
from .meta_db import get_meta_db

logger = logging.getLogger(__name__)


class JobCancelled(RuntimeError):
    """用户取消任务。"""


class JobFailed(RuntimeError):
    """任务失败（保留原始异常链）。"""


class JobRunner:
    """单线程串行任务执行器。"""

    def __init__(self, max_workers: int = 1):
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="deltaf-job")
        self._cancelled: Dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    # ---------- 提交 ----------

    def submit(self, kind: str, params: Optional[dict] = None) -> str:
        """创建 job_runs 记录并提交执行，立即返回 job_id。"""
        db = get_meta_db()
        params = params or {}
        job_id = db.create_job(kind=kind, params=params)
        cancel_event = threading.Event()
        with self._lock:
            self._cancelled[job_id] = cancel_event
        self._pool.submit(self._run, job_id, kind, params, cancel_event)
        logger.info("任务已提交 %s kind=%s params=%s", job_id, kind, params)
        return job_id

    def cancel(self, job_id: str) -> bool:
        """请求取消。返回 False 表示任务不存在或已结束。"""
        with self._lock:
            event = self._cancelled.get(job_id)
        if event is None:
            return False
        event.set()
        return True

    def is_running(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._cancelled

    def shutdown(self, wait: bool = False) -> None:
        self._pool.shutdown(wait=wait)

    # ---------- 执行 ----------

    def _run(self, job_id: str, kind: str, params: dict, cancel_event: threading.Event) -> None:
        db = get_meta_db()
        db.update_job(job_id, status="running", progress=0.0, message="开始执行")

        def progress(ratio: float, message: str) -> None:
            if cancel_event.is_set():
                raise JobCancelled("用户取消")
            fields: Dict[str, Any] = {"message": message}
            if ratio is not None and ratio >= 0:
                fields["progress"] = max(0.0, min(1.0, float(ratio)))
            db.update_job(job_id, **fields)

        try:
            handler = self._handler(kind)
            result = handler(params, progress)
            db.update_job(
                job_id,
                status="success",
                progress=1.0,
                message=_summarize(kind, result),
                log=_dumps(result),
            )
            logger.info("任务完成 %s", job_id)
        except JobCancelled:
            db.update_job(job_id, status="cancelled", message="用户取消")
            logger.info("任务已取消 %s", job_id)
        except Exception as exc:  # 任务失败不应拖垮进程
            logger.exception("任务失败 %s", job_id)
            db.update_job(
                job_id,
                status="failed",
                message=f"{type(exc).__name__}: {exc}",
                log=_dumps({"error": str(exc), "type": type(exc).__name__}),
            )
        finally:
            with self._lock:
                self._cancelled.pop(job_id, None)

    def _handler(self, kind: str) -> Callable[[dict, Callable], dict]:
        if kind == "update":
            return self._run_update
        if kind == "backfill":
            return self._run_backfill
        if kind == "score":
            return self._run_score
        if kind == "evaluate":
            raise NotImplementedError("evaluate 任务将在 P5 阶段实现")
        raise ValueError(f"未知任务类型：{kind}")

    # ---------- 具体任务 ----------

    @staticmethod
    def _run_update(params: dict, progress: Callable) -> dict:
        from .ingest import get_ingest

        # params.code 存在时走「单只补拉」分支：只碰这一只票的行情与复权因子。
        # 不能拿 update_daily 凑合 —— 那是「把 300 只全部对齐到今天」的语义，
        # 补一只小票会顺带重刷整个池子，慢几十倍还容易在盘中被涨跌停卡住。
        code = (params.get("code") or "").strip()
        if code:
            return get_ingest().fill_symbol(
                code=code, start=params.get("start"), progress=progress
            )

        return get_ingest().update_daily(
            trade_date=params.get("trade_date"),
            with_fundamental=bool(params.get("with_fundamental", False)),
            progress=progress,
        )

    @staticmethod
    def _run_backfill(params: dict, progress: Callable) -> dict:
        from .ingest import get_ingest

        return get_ingest().backfill_all(
            start=params.get("start"),
            end=params.get("end"),
            valuation_years=int(params.get("valuation_years", 5)),
            with_fundamental=bool(params.get("with_fundamental", True)),
            progress=progress,
        )

    @staticmethod
    def _run_score(params: dict, progress: Callable) -> dict:
        """打分任务。

        ``params.full=True`` 走全历史重算（改因子后用），否则单日增量。
        """
        from .scorer import get_scorer

        scorer = get_scorer()
        version = params.get("version")
        if params.get("full"):
            return scorer.score_range(
                version=version,
                start=params.get("start"),
                end=params.get("end"),
                progress=progress,
            )
        return scorer.score_day(
            trade_date=params.get("trade_date"),
            version=version,
            progress=progress,
        )


# ---------- 结果摘要 ----------


def _summarize(kind: str, result: Any) -> str:
    """把任务结果压成一行人类可读的说明，写进 job_runs.message。"""
    if not isinstance(result, dict):
        return "完成"
    if result.get("skipped"):
        return f"跳过：{result.get('reason', '')}"
    parts = []
    if kind == "update":
        parts.append(f"{result.get('trade_date', '')} 更新完成")
        bars = result.get("bars") or {}
        if isinstance(bars, dict):
            parts.append(f"行情 {bars.get('rows_written', 0)} 行")
            if bars.get("failures"):
                parts.append(f"失败 {len(bars['failures'])} 只")
        quality = result.get("quality") or {}
        if isinstance(quality, dict) and quality.get("degraded"):
            parts.append("数据质量降级")
        if result.get("pending"):
            parts.append("当日行情未到位")
    elif kind == "backfill":
        parts.append(f"回填 {result.get('start', '')} ~ {result.get('end', '')}")
        parts.append(f"{result.get('codes', 0)} 只标的")
        bars = result.get("bars") or {}
        if isinstance(bars, dict):
            parts.append(f"行情 {bars.get('rows_written', 0)} 行")
        val = result.get("valuation") or {}
        if isinstance(val, dict):
            parts.append(f"估值 {val.get('rows', 0)} 行")
    elif kind == "score":
        if result.get("mode") == "full":
            parts.append(f"全量打分 {result.get('start')} ~ {result.get('end')}")
        else:
            parts.append(f"{result.get('trade_date', '')} 打分")
        parts.append(f"{result.get('codes', 0)} 只")
        if result.get("rows") is not None:
            parts.append(f"写入 {result.get('rows')} 行")
        dist = result.get("distribution") or {}
        if dist:
            parts.append("强烈看多 " + str(dist.get("强烈看多", 0)))
    return "，".join(parts) or "完成"


def _dumps(result: Any) -> str:
    import json

    try:
        return json.dumps(result, ensure_ascii=False, default=str)[:20000]
    except Exception:
        return str(result)[:20000]


_runner: Optional[JobRunner] = None


def get_runner() -> JobRunner:
    global _runner
    if _runner is None:
        _runner = JobRunner()
    return _runner
