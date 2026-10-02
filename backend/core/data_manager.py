"""
数据管理模块

数据源策略（见 docs/data-source-and-adjustment.md）
-------------------------------------------------
* 历史数据只走 **baostock**（白名单 {baostock, miniqmt}，默认 baostock）。
  yfinance 已被 IP 级 429 封禁，且它默认返回前复权价，与 baostock 的不复权
  口径混写会造出假跳空，故已从白名单移除。
* **不做跨源降级**。一次拉取失败就是失败，不换源重试——换源会把不同复权口径
  的数据拼进同一个文件，且不会报错。
* 每次落盘都写 ``data/meta/{symbol}.json`` 记录 source；合并本地历史前校验
  source 一致，不一致直接拒绝合并（需 force_refresh 全量重拉）。
"""
import json
import logging
import os
from datetime import datetime, timedelta

import pandas as pd

from deltafq.data import DataFetcher

from .adjustment import BS_LOCK, AdjustmentStore, normalize_bs_code

logger = logging.getLogger(__name__)

#: 允许的数据源（yfinance 已移除，详见模块 docstring）
SUPPORTED_SOURCES = {"baostock", "miniqmt"}
#: 未显式指定时使用的数据源
DEFAULT_SOURCE = "baostock"


class DataManager:
    """数据管理器"""

    # 拉取串行锁：baostock 依赖全局单连接（每次拉取都 login/logout），
    # 并发调用会互相踩连接并触发服务端异常，故用类级锁串行化所有拉取。
    # 与 adjustment.BS_LOCK 是同一把锁，避免两把锁互相打架。
    _FETCH_LOCK = BS_LOCK

    def __init__(self, data_folder):
        self.data_folder = data_folder
        self.raw_folder = os.path.join(data_folder, 'raw')
        self.meta_folder = os.path.join(data_folder, 'meta')

        # 确保文件夹存在
        os.makedirs(self.raw_folder, exist_ok=True)
        os.makedirs(self.meta_folder, exist_ok=True)
        self.adjust_store = AdjustmentStore(data_folder)

    @staticmethod
    def _canonicalize_symbol(symbol: str) -> str:
        """统一标的代码：baostock 保持 sh./sz. 小写，其余大写。"""
        s = (symbol or "").strip()
        if len(s) >= 3 and s[:3].lower() in ("sh.", "sz."):
            return s.lower()
        return s.upper()

    def _resolve_period_range(self, period):
        """将 period 转为 (start_date, end_date)。"""
        end_date = datetime.now().date()
        p = (period or "").strip().lower()
        if not p:
            return None, None
        if p == "max":
            return (end_date - timedelta(days=365 * 20)), end_date
        if p.endswith("y") and p[:-1].isdigit():
            return (end_date - timedelta(days=365 * int(p[:-1]))), end_date
        if p.endswith("mo") and p[:-2].isdigit():
            return (end_date - timedelta(days=30 * int(p[:-2]))), end_date
        if p.endswith("m") and p[:-1].isdigit():
            return (end_date - timedelta(days=30 * int(p[:-1]))), end_date
        if p.endswith("d") and p[:-1].isdigit():
            return (end_date - timedelta(days=int(p[:-1]))), end_date
        raise ValueError(f"Unsupported period: {period}")
    
    def save_data(self, df, filename, index=False):
        """保存数据到CSV文件"""
        filepath = os.path.join(self.raw_folder, filename)
        df.to_csv(filepath, index=index)
        return filepath
    
    def find_latest_file(self, symbol):
        """查找指定股票代码的最新文件"""
        symbol_u = self._canonicalize_symbol(symbol).upper()
        candidates = []
        for filename in os.listdir(self.raw_folder):
            if filename.lower().endswith('.csv') and filename.upper().startswith(symbol_u):
                filepath = os.path.join(self.raw_folder, filename)
                stat = os.stat(filepath)
                candidates.append((stat.st_mtime, filename))
        
        if candidates:
            candidates.sort(reverse=True)
            return candidates[0][1]
        return None
    
    def get_file_info(self, symbol):
        """获取指定股票代码的文件信息"""
        latest_file = self.find_latest_file(symbol)
        if not latest_file:
            return None
        
        df = pd.read_csv(os.path.join(self.raw_folder, latest_file), nrows=1)
        return {
            'id': latest_file,
            'filename': latest_file,
            'source': 'local',
            'columns': list(df.columns)
        }
    
    # ==================== 来源标记（防止跨源混合写入） ====================

    def _meta_path(self, symbol):
        return os.path.join(self.meta_folder, f"{normalize_bs_code(symbol)}.json")

    def load_meta(self, symbol):
        """读取落盘元数据；不存在返回 None。"""
        path = self._meta_path(symbol)
        if not os.path.exists(path):
            return None
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return None

    def save_meta(self, symbol, meta):
        """写入落盘元数据。"""
        path = self._meta_path(symbol)
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Failed to write meta for {symbol}: {e}")
        return path

    def check_source_conflict(self, symbol, source):
        """返回与本次 source 冲突的历史 source；无冲突返回 None。

        首次落盘（无 meta）视为 unknown，不冲突，由本次写入确立来源。
        """
        meta = self.load_meta(symbol)
        if not meta:
            return None
        prev = (meta.get('source') or '').strip().lower()
        if prev and prev != source:
            return prev
        return None

    def _standardize_dataframe(self, df):
        """标准化数据框格式"""
        if 'Date' not in df.columns:
            df = df.reset_index().rename(columns={df.index.name or 'index': 'Date'})
        df['Date'] = pd.to_datetime(df['Date']).dt.strftime('%Y-%m-%d')
        # baostock 对停牌日返回**空字符串**成交量（实测 sh.688981 2025-09-01~09-08），
        # 直接落盘会变成 NaN 并污染下游计算。统一填 0，停牌行本身由
        # 校验规则 R2.6（OHLC 四值全等且成交量为 0）识别并在清洗阶段剔除。
        if 'Volume' in df.columns:
            vol = pd.to_numeric(df['Volume'], errors='coerce')
            if vol.isna().any():
                vol = vol.fillna(0)
            # adapter 返回的 Volume 是 float64；直接落盘会写成 1.2e+08 丢精度。
            # 全部为整数时转 int64。
            if len(vol) and vol.notna().all() and (vol % 1 == 0).all():
                vol = vol.astype('int64')
            df['Volume'] = vol
        return df
    
    def _get_file_start_date(self, filename):
        """从文件中获取起始日期"""
        try:
            df_dates = pd.read_csv(os.path.join(self.raw_folder, filename), usecols=['Date'])
            if not df_dates.empty:
                return pd.to_datetime(df_dates['Date']).min().date()
        except:
            pass
        return (datetime.now() - timedelta(days=365*20)).date()
    
    def _merge_with_local(self, df, latest_file):
        """用新数据覆盖同日期旧行，保留本地其余历史。

        早期实现直接用本次拉取结果覆盖 ``SYMBOL.csv``：若用户请求一个更窄的区间，
        已有的长历史会被静默截断（例如先拉全量、再拉单日，文件只剩一行）。
        """
        filepath = os.path.join(self.raw_folder, latest_file)
        try:
            old = pd.read_csv(filepath)
        except Exception:
            return df
        if old.empty or 'Date' not in old.columns:
            return df

        old = self._standardize_dataframe(old)
        merged = pd.concat([old, df], ignore_index=True)
        # keep='last'：新数据在后，同日期以新数据为准（可修正历史脏数据）
        merged = merged.drop_duplicates(subset=['Date'], keep='last')
        merged = merged.sort_values('Date').reset_index(drop=True)
        # 列序以本次结果为准，本地多出的列（如 Adj Close）保留在末尾
        columns = list(df.columns) + [c for c in old.columns if c not in df.columns]
        return merged.reindex(columns=columns)

    def _get_file_date_range(self, filename):
        """从文件中获取日期范围"""
        try:
            df = pd.read_csv(os.path.join(self.raw_folder, filename), usecols=['Date'])
            if not df.empty:
                df['Date'] = pd.to_datetime(df['Date'])
                return df['Date'].min().date(), df['Date'].max().date()
        except:
            pass
        return None, None
    
    def fetch_data(self, symbol, start_date=None, end_date=None, period=None, update_existing=True, data_source=None, force_refresh=False, with_adjust=True):
        """
        统一的数据获取方法，支持多种数据源和增量更新
        
        Args:
            symbol: 股票代码
            start_date: 起始日期（datetime.date 或 str）
            end_date: 结束日期（datetime.date 或 str）
            period: 时间周期（如 '1y', 'max'），与 start_date/end_date 二选一
            update_existing: 是否检查并更新已有文件
            data_source: 数据源，可选 baostock/miniqmt，默认 baostock（yfinance 已移除）
            force_refresh: 是否强制刷新（忽略本地范围命中，重新拉取并覆盖）
            with_adjust: 是否同步确保复权事件表落盘（本地已有时不联网）
        
        Returns:
            tuple: (filename, df, status, source) - 文件名、数据框、状态信息、数据源
        """
        symbol = self._canonicalize_symbol(symbol)
        selected_source = (data_source or DEFAULT_SOURCE).strip().lower()
        if selected_source not in SUPPORTED_SOURCES:
            raise ValueError(
                f"Unsupported data source: {data_source}. "
                f"Supported: {sorted(SUPPORTED_SOURCES)}（yfinance 已移除，"
                f"其源已被 IP 级限流且默认返回前复权价，与 baostock 口径不一致）"
            )
        fetcher_source = {"miniqmt": "miniqmt", "baostock": "baostock"}[selected_source]

        # 跨源混合写入防护：本地已有数据来自另一个源时拒绝合并
        if update_existing and not force_refresh:
            conflict = self.check_source_conflict(symbol, selected_source)
            if conflict:
                raise ValueError(
                    f"数据源冲突：{symbol} 本地已有数据来自 '{conflict}'，"
                    f"本次请求为 '{selected_source}'。两者复权口径不同，"
                    f"合并会造出假跳空。请使用 force_refresh=true 以本次数据源全量重拉。"
                )
        
        # 转换日期格式
        if start_date and isinstance(start_date, str):
            start_date = datetime.fromisoformat(start_date).date()
        if end_date and isinstance(end_date, str):
            end_date = datetime.fromisoformat(end_date).date()
        if period:
            start_date, end_date = self._resolve_period_range(period)
        
        # 1. 检查本地文件（如果需要增量更新）
        latest_file = None
        if update_existing and not force_refresh:
            latest_file = self.find_latest_file(symbol)
            
            # 如果指定了日期范围，检查现有文件是否已包含所需日期范围
            if latest_file and start_date and end_date:
                file_start, file_end = self._get_file_date_range(latest_file)
                if file_start and file_end:
                    # 检查文件的日期范围是否包含所需的日期范围
                    if file_start <= start_date and file_end >= end_date:
                        # 日期范围已存在，直接返回文件
                        df = pd.read_csv(os.path.join(self.raw_folder, latest_file))
                        return latest_file, df, "exists", "local"
        
        # 2. 确定时间范围
        if latest_file and update_existing and not (start_date and end_date):
            # 从已有文件获取起始日期，更新到最新（仅在没有指定日期范围时）
            start_date = self._get_file_start_date(latest_file)
            end_date = datetime.now().date()
            status = "updated"
        else:
            # 首次下载或指定了日期范围（需要全量重新下载）
            if not start_date:
                start_date = (datetime.now() - timedelta(days=365*20)).date()
            if not end_date:
                end_date = datetime.now().date()
            status = "downloaded_full"
        
        # 3. 获取数据（串行化，避免并发踩坏 baostock 全局连接）
        try:
            fetcher = DataFetcher(source=fetcher_source)
            with DataManager._FETCH_LOCK:
                df = fetcher.fetch_data(
                    symbol=symbol,
                    start_date=start_date.isoformat(),
                    end_date=end_date.isoformat(),
                    interval='1d'
                )
            source = selected_source
        except Exception as download_error:
            if latest_file:
                # 下载失败，返回已有文件
                df = pd.read_csv(os.path.join(self.raw_folder, latest_file))
                return latest_file, df, "using_local_on_error", "local"
            raise Exception(f"Failed to download data: {str(download_error)}")
        
        if df is None or df.empty:
            if latest_file:
                df = pd.read_csv(os.path.join(self.raw_folder, latest_file))
                return latest_file, df, "using_local_on_error", "local"
            raise ValueError(f"No data found for symbol {symbol}")
            
        # 4. 标准化数据格式
        df = self._standardize_dataframe(df)

        # 5. 合并本地已有历史：窄区间请求只覆盖对应日期，不再把整份历史截断
        #    （force_refresh 表示调用方明确要求重建，此时不做合并）
        filename = f"{symbol}.csv"
        persisted = df
        if update_existing and not force_refresh and latest_file:
            persisted = self._merge_with_local(df, latest_file)

        # 6. 保存文件（统一命名为 SYMBOL.csv；落盘为合并后的全历史，
        #    返回值仍为本次请求区间，保持既有调用方语义不变）
        self.save_data(persisted, filename, index=False)
        
        # 7. 清理旧文件（如果有）
        if latest_file and latest_file != filename:
            try:
                os.remove(os.path.join(self.raw_folder, latest_file))
            except:
                pass

        # 8. 落盘来源标记（跨源混合写入防护的依据）
        dates = pd.to_datetime(persisted['Date'], errors='coerce').dropna()
        self.save_meta(symbol, {
            'symbol': normalize_bs_code(symbol),
            'source': selected_source,
            'schema_version': 1,
            'rows': int(len(persisted)),
            'first_date': dates.min().strftime('%Y-%m-%d') if len(dates) else None,
            'last_date': dates.max().strftime('%Y-%m-%d') if len(dates) else None,
            'last_fetch': datetime.now().isoformat(timespec='seconds'),
        })

        # 9. 确保复权事件表落盘（本地已有时不联网；事件表通常只有几十行）
        if with_adjust and selected_source == 'baostock':
            try:
                self.adjust_store.ensure_events(symbol)
            except Exception as e:
                logger.warning(f"Failed to fetch adjust events for {symbol}: {e}")

        return filename, df, status, source
