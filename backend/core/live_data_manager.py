"""
实时行情管理（按 source 独立运行）。

- SourceLiveDataManager: 单一 source 的行情运行态（一个网关 + 一套缓存）。
- MultiSourceLiveDataManager: 对外路由层，按请求 source 选择对应运行态。

SourceLiveDataManager 方法说明：
公开方法：
  start            启动当前数据网关；连接失败时不启动轮询线程，返回是否真正启动。
  stop             停止网关并清空订阅集合。
  get_data_source  返回当前数据源标识。
  set_data_source  切换数据源并迁移原订阅标的，返回 (成功标记, 信息)。
  subscribe        按需订阅标的，网关失败时回滚本地订阅标记。
  get_quote        获取最新报价，并按需补充历史与缓存字段。

私有方法：
  _on_tick             处理单条 tick，写入实时或预热缓冲。
  _minute_context      计算 tick 的基准时间与 minute 字符串。
  _time_offset         按市场/来源返回 minute 展示时区偏移。
  _create_gateway      按 source 创建网关并绑定 tick 回调。
  _clear_runtime_state 清空订阅、tick、缓存与失败退避状态。
  _apply_cached        将命中的缓存字段写回响应数据。
  _next_retry_delay    按连续失败次数返回退避秒数（指数递增并封顶）。
  _mark_success        拉取成功：写缓存、刷时间戳并重置失败计数。
  _mark_failure        拉取失败：刷时间戳并进入指数退避，避免越限流打得越猛。
  _update_ohlc_cache   刷新/复用 OHLC 缓存（TTL + 失败退避）。
  _update_depth_cache  刷新/复用五档缓存（TTL + 失败退避）。
  _parse_depth_rows    将五档原始行标准化为 [price, volume]。

MultiSourceLiveDataManager 方法说明：
公开方法：
  normalize_source  归一化 source（非法值默认 yfinance）。
  start             启动全部 source 的网关。
  stop              停止全部 source 的网关。
  subscribe         按 source 将订阅请求路由到对应运行态。
  get_quote         按 source 获取行情（支持 include_history）。

私有方法：
  _pick_manager     选择并返回目标 source 的运行态管理器。
"""

import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, Tuple

from deltafq.live.event_engine import EVENT_TICK, EventEngine
from deltafq.live.gateway_registry import create_data_gateway

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float, minimum: float) -> float:
    """读取环境变量浮点配置，非法或非正值回退默认值。"""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning(f"Invalid float for {name}: {raw!r}, fallback to {default}")
        return default
    return value if value >= minimum else default


class SourceLiveDataManager:
    """单一 source 的实时行情运行态。"""

    # 缓存 TTL（秒）：命中时直接复用，不发起请求。
    OHLC_TTL = _env_float("DFS_LIVE_OHLC_TTL", 60, 1)
    DEPTH_TTL = _env_float("DFS_LIVE_DEPTH_TTL", 3, 1)

    # 失败退避（秒）：连续失败时按 2^n 递增并封顶。
    # 背景：早期实现只在成功时刷新 *_last_update，一旦上游限流，TTL 永不命中，
    # 每次前端轮询都会直接打上游（OHLC 从 1 次/分放大到 12 次/分），形成「越限流打得越猛」的雪崩。
    OHLC_FAIL_BACKOFF = _env_float("DFS_LIVE_OHLC_FAIL_BACKOFF", 60, 1)
    DEPTH_FAIL_BACKOFF = _env_float("DFS_LIVE_DEPTH_FAIL_BACKOFF", 15, 1)
    FAIL_BACKOFF_MAX = _env_float("DFS_LIVE_FAIL_BACKOFF_MAX", 300, 1)
    FAIL_BACKOFF_MAX_POW = 4

    WARMUP_SOURCES = {"yf_warmup", "miniqmt_warmup"}
    REALTIME_MINIQMT_SOURCES = {"miniqmt", "miniqmt_push"}

    def __init__(self, source: str = "yfinance"):
        self.event_engine = EventEngine()
        self.latest_ticks: Dict[str, dict] = {}
        self.history_ticks: Dict[str, list] = {}
        self.subscribed_symbols = set()
        self.ohlc_cache: Dict[str, dict] = {}
        self.ohlc_last_update: Dict[str, float] = {}
        self.depth_cache: Dict[str, dict] = {}
        self.depth_last_update: Dict[str, float] = {}
        self.data_source = (source or "yfinance").strip().lower()
        self._gateway_params = {
            "yfinance": {"interval": _env_float("DFS_LIVE_YF_INTERVAL", 5, 1)},
            "miniqmt": {"interval": _env_float("DFS_LIVE_MINIQMT_INTERVAL", 5, 1), "mode": "poll"},
        }
        self._lock = threading.Lock()
        self.gateway = None

        # 连续失败计数与退避到期时刻（source 切换时随缓存一起清空）。
        self.ohlc_fail_count: Dict[str, int] = {}
        self.depth_fail_count: Dict[str, int] = {}
        self.ohlc_next_retry: Dict[str, float] = {}
        self.depth_next_retry: Dict[str, float] = {}

        # 统一由事件引擎转发 tick，便于后续扩展多个事件消费者。
        self.event_engine.on(EVENT_TICK, self._on_tick)

        # 启动时尝试创建默认网关；失败时允许服务继续启动。
        try:
            self.gateway = self._create_gateway(self.data_source)
        except Exception as e:
            logger.error(f"Failed to create data gateway: {e}")
            self.gateway = None

    # ==================== Public APIs ====================
    def start(self) -> bool:
        """启动当前网关；连接失败时不起轮询线程，返回是否真正启动。

        早期实现忽略 ``connect()`` 的返回值，导致 miniqmt 在 xtquant 缺失时
        仍起后台轮询线程空转刷错误日志。
        """
        if not self.gateway:
            logger.warning(f"Skip starting {self.data_source}: gateway unavailable")
            return False
        try:
            connected = self.gateway.connect()
        except Exception as e:
            logger.error(f"Failed to connect {self.data_source} gateway: {e}")
            return False
        if connected is False:
            logger.warning(f"Skip starting {self.data_source}: gateway connect failed")
            return False
        self.gateway.start()
        return True

    def stop(self):
        """停止当前网关并清空订阅集合。"""
        if self.gateway:
            try:
                self.gateway.stop()
            except Exception as e:
                logger.warning(f"Error stopping {self.data_source} gateway: {e}")
        with self._lock:
            self.subscribed_symbols.clear()

    def get_data_source(self) -> str:
        """返回当前数据源。"""
        with self._lock:
            return self.data_source

    def set_data_source(self, source: str) -> Tuple[bool, str]:
        """切换数据源并迁移原订阅；返回 (成功标记, 信息)。"""
        source = (source or "").strip().lower()
        if source not in self._gateway_params:
            return False, f"Unsupported data source: {source}"

        # Step 1: 记录旧状态并清理运行态缓存。
        with self._lock:
            if source == self.data_source:
                return True, source
            old_gateway = self.gateway
            old_symbols = list(self.subscribed_symbols)
            self._clear_runtime_state()

        # Step 2: 构建并启动新网关，恢复原订阅。
        try:
            new_gateway = self._create_gateway(source)
            if not new_gateway.connect():
                with self._lock:
                    self.subscribed_symbols = set(old_symbols)
                return False, f"Failed to connect data source: {source}"
            new_gateway.start()
            new_gateway.subscribe(old_symbols)
        except Exception as e:
            logger.error(f"Failed to switch data source to {source}: {e}")
            with self._lock:
                self.subscribed_symbols = set(old_symbols)
            return False, str(e)

        # Step 3: 关闭旧网关并提交新网关。
        if old_gateway:
            try:
                old_gateway.stop()
            except Exception as e:
                logger.warning(f"Error stopping old gateway: {e}")

        with self._lock:
            self.gateway = new_gateway
            self.data_source = source
            self.subscribed_symbols = set(old_symbols)
        return True, source

    def subscribe(self, symbols: list):
        """按需订阅 symbols，失败时回滚本地订阅标记。"""
        if not self.gateway:
            return

        # Step 1: 只保留尚未订阅的 symbol，并先乐观写入集合。
        with self._lock:
            new_symbols = [s for s in symbols if s not in self.subscribed_symbols]
            if not new_symbols:
                return
            for s in new_symbols:
                self.subscribed_symbols.add(s)

        # Step 2: 锁外发起订阅，异常时回滚本地标记。
        try:
            self.gateway.subscribe(new_symbols)
        except Exception as e:
            logger.error(f"Gateway subscribe failed for {new_symbols}: {e}")
            with self._lock:
                for s in new_symbols:
                    self.subscribed_symbols.discard(s)

    def get_quote(self, symbol: str, include_history: bool = False):
        """获取最新报价，必要时附加历史与缓存字段。"""
        # Step 1: 先确保目标标的已订阅。
        self.subscribe([symbol])

        # Step 2: 读取最新 tick 快照（无数据直接返回）。
        with self._lock:
            data = self.latest_ticks.get(symbol, {}).copy()
            if not data:
                return None
            data["data_source"] = self.data_source
            if include_history and symbol in self.history_ticks:
                data["history"] = self.history_ticks[symbol]

        # Step 3: 补充缓存行情字段（OHLC + 五档）。
        self._update_ohlc_cache(symbol, data)
        self._update_depth_cache(symbol, data)
        return data

    # ==================== Tick Processing ====================
    def _on_tick(self, tick):
        """处理单条 tick：标准化 minute，并写入对应缓冲区。"""
        with self._lock:
            source = getattr(tick, "source", None)
            ts_base, minute = self._minute_context(tick)
            tick_data = {
                "symbol": tick.symbol,
                "price": tick.price,
                "volume": tick.volume,
                "timestamp": ts_base.isoformat(),
                "minute": minute,
            }
            if source in self.WARMUP_SOURCES:
                self.history_ticks.setdefault(tick.symbol, []).append(tick_data)
                return
            self.latest_ticks[tick.symbol] = tick_data

    def _minute_context(self, tick) -> Tuple[datetime, str]:
        """返回 (基准时间, 分钟字符串)。"""
        symbol = tick.symbol
        source = getattr(tick, "source", None)
        ts_base = tick.timestamp
        if source in self.REALTIME_MINIQMT_SOURCES:
            # 实时 miniqmt 可能返回停滞成交时刻，改用接收时刻驱动 minute。
            ts_base = datetime.now().replace(tzinfo=None)
            offset = 0
        else:
            offset = self._time_offset(symbol, source)
        minute = (ts_base + timedelta(hours=offset)).strftime("%H:%M")
        return ts_base, minute

    @staticmethod
    def _time_offset(symbol: str, source: str) -> int:
        """按市场/来源选择 minute 展示时区偏移。"""
        if source == "miniqmt_warmup" or symbol.endswith((".SS", ".SZ")):
            return 8
        if symbol.endswith("-USD") or "BTC" in symbol or "ETH" in symbol:
            return 0
        return -5

    # ==================== Cache & Gateway Helpers ====================
    def _create_gateway(self, source: str):
        """按 source 创建网关，并将 tick 转发到事件引擎。"""
        source = (source or "").strip().lower()
        if source not in self._gateway_params:
            raise ValueError(f"Unsupported data source: {source}")
        gateway = create_data_gateway(source, **self._gateway_params[source])
        gateway.set_tick_handler(lambda tick: self.event_engine.emit(EVENT_TICK, tick))
        return gateway

    def _clear_runtime_state(self):
        """清空订阅、缓存与失败退避状态（用于切换数据源前）。"""
        self.subscribed_symbols.clear()
        self.latest_ticks.clear()
        self.history_ticks.clear()
        self.ohlc_cache.clear()
        self.ohlc_last_update.clear()
        self.depth_cache.clear()
        self.depth_last_update.clear()
        self.ohlc_fail_count.clear()
        self.depth_fail_count.clear()
        self.ohlc_next_retry.clear()
        self.depth_next_retry.clear()

    @staticmethod
    def _apply_cached(cache: Dict[str, dict], symbol: str, data: dict):
        """存在缓存则写入 data。"""
        cached = cache.get(symbol)
        if cached:
            data.update(cached)

    def _next_retry_delay(self, fail_count: int, base: float) -> float:
        """按连续失败次数返回退避秒数（指数递增并封顶）。"""
        exponent = min(max(fail_count - 1, 0), self.FAIL_BACKOFF_MAX_POW)
        return min(base * (2 ** exponent), self.FAIL_BACKOFF_MAX)

    @staticmethod
    def _mark_success(cache, last_update, fail_count, next_retry, symbol, payload, current_time):
        """拉取成功：写缓存、刷新时间戳并重置失败计数。"""
        cache[symbol] = payload
        last_update[symbol] = current_time
        fail_count.pop(symbol, None)
        next_retry.pop(symbol, None)

    def _mark_failure(self, last_update, fail_count, next_retry, symbol, base, current_time, label):
        """拉取失败：刷新时间戳并进入指数退避，随后由调用方回退旧缓存。"""
        fail_count[symbol] = fail_count.get(symbol, 0) + 1
        last_update[symbol] = current_time
        next_retry[symbol] = current_time + self._next_retry_delay(fail_count[symbol], base)
        logger.warning(
            f"{label} fetch failed for {symbol} "
            f"(consecutive={fail_count[symbol]}, retry after {self._next_retry_delay(fail_count[symbol], base):.0f}s)"
        )

    def _update_ohlc_cache(self, symbol: str, data: dict):
        """刷新/复用 OHLC 缓存（TTL + 失败指数退避）。"""
        current_time = time.time()

        # Step 1: 退避窗口内或 TTL 命中时直接复用缓存。
        if current_time < self.ohlc_next_retry.get(symbol, 0):
            self._apply_cached(self.ohlc_cache, symbol, data)
            return
        if current_time - self.ohlc_last_update.get(symbol, 0) <= self.OHLC_TTL:
            self._apply_cached(self.ohlc_cache, symbol, data)
            return

        # Step 2: TTL 过期时拉新；成功落缓存，失败刷新时间戳并退避。
        ohlc = None
        if self.gateway:
            try:
                ohlc = self.gateway.get_today_ohlc(symbol)
            except Exception as e:
                logger.warning(f"Error fetching OHLC for {symbol}: {e}")
        if ohlc:
            self._mark_success(
                self.ohlc_cache, self.ohlc_last_update, self.ohlc_fail_count,
                self.ohlc_next_retry, symbol, ohlc, current_time,
            )
            data.update(ohlc)
            return
        self._mark_failure(
            self.ohlc_last_update, self.ohlc_fail_count, self.ohlc_next_retry,
            symbol, self.OHLC_FAIL_BACKOFF, current_time, "OHLC",
        )
        self._apply_cached(self.ohlc_cache, symbol, data)

    def _update_depth_cache(self, symbol: str, data: dict):
        """刷新/复用五档缓存（TTL + 失败指数退避）。"""
        if not self.gateway:
            return

        current_time = time.time()

        # Step 1: 退避窗口内或 TTL 命中时直接复用缓存。
        if current_time < self.depth_next_retry.get(symbol, 0):
            self._apply_cached(self.depth_cache, symbol, data)
            return
        if current_time - self.depth_last_update.get(symbol, 0) <= self.DEPTH_TTL:
            self._apply_cached(self.depth_cache, symbol, data)
            return

        # Step 2: TTL 过期时拉新并标准化结构；失败刷新时间戳并退避。
        parsed = None
        try:
            depths = self.gateway.get_depths(symbol, levels=5) or {}
            asks = self._parse_depth_rows(depths.get("asks") or [])
            bids = self._parse_depth_rows(depths.get("bids") or [])
            if asks or bids:
                parsed = {"asks": asks, "bids": bids}
        except Exception as e:
            logger.warning(f"Error fetching depth for {symbol}: {e}")

        if parsed:
            self._mark_success(
                self.depth_cache, self.depth_last_update, self.depth_fail_count,
                self.depth_next_retry, symbol, parsed, current_time,
            )
            data.update(parsed)
            return
        self._mark_failure(
            self.depth_last_update, self.depth_fail_count, self.depth_next_retry,
            symbol, self.DEPTH_FAIL_BACKOFF, current_time, "Depth",
        )
        self._apply_cached(self.depth_cache, symbol, data)

    @staticmethod
    def _parse_depth_rows(rows: list) -> list:
        """将 depth 行标准化为 [price, volume] 列表。"""
        parsed = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            p, v = row.get("price"), row.get("volume")
            if p is None or v is None:
                continue
            parsed.append([float(p), int(v)])
        return parsed


class MultiSourceLiveDataManager:
    """按 source 路由到独立运行态。"""

    SUPPORTED_SOURCES = {"yfinance", "miniqmt"}

    def __init__(self):
        self._managers: Dict[str, SourceLiveDataManager] = {
            "yfinance": SourceLiveDataManager("yfinance"),
            "miniqmt": SourceLiveDataManager("miniqmt"),
        }

    @classmethod
    def normalize_source(cls, source: str = None) -> str:
        s = (source or "").strip().lower()
        return s if s in cls.SUPPORTED_SOURCES else "yfinance"

    def _pick_manager(self, source: str = None) -> SourceLiveDataManager:
        selected = self.normalize_source(source)
        return self._managers[selected]

    def start(self):
        """启动全部 source 网关。"""
        for manager in self._managers.values():
            manager.start()

    def stop(self):
        """停止全部 source 网关。"""
        for manager in self._managers.values():
            manager.stop()

    def subscribe(self, symbols: list, source: str = None):
        self._pick_manager(source).subscribe(symbols)

    def get_quote(self, symbol: str, include_history: bool = False, source: str = None):
        return self._pick_manager(source).get_quote(symbol, include_history=include_history)


live_data_manager = MultiSourceLiveDataManager()
