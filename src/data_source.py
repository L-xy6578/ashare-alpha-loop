"""
三角闭环选股体系 — 数据源接口层

行情获取与知识库解耦：知识库只消费 KlineBar，不关心数据从哪来。

实现：
1. StaticDataSource   — 外部注入（连接器/AI 工具拉取后注入）
2. AkshareDataSource  — 独立环境通用实现（pip install akshare，不依赖任何 AI 软件）

用法:
    from src.data_source import KlineBar, StaticDataSource, AkshareDataSource

    # 静态注入（用外部连接器拉取后喂入）
    src = StaticDataSource()
    src.set_symbol("sh600535", [KlineBar("2026-07-29", 15.78, 15.95, 16.10, 15.78)])
    bars = src.daily_kline("sh600535", "2026-07-29", "2026-07-31")

    # akshare 自动拉取（换电脑/换工具后依然可用）
    src = AkshareDataSource()
    bars = src.daily_kline("sh600535", "2026-07-29", "2026-07-31")
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterable

from .logger import get_logger

log = get_logger(__name__)


# ============================================================
# 数据模型
# ============================================================

@dataclass(frozen=True)
class KlineBar:
    """一根日K线"""

    date: str        # YYYY-MM-DD
    open: float      # 开盘价
    close: float     # 收盘价
    high: float      # 最高价
    low: float       # 最低价
    volume: float | None = None  # 成交量（手），P1-3：契约字段，可为空
    amount: float | None = None  # 成交额（元）


# ============================================================
# 抽象接口
# ============================================================

class DataSource(ABC):
    """行情数据源统一接口"""

    adjust: str = "unknown"   # 复权口径：qfq=前复权 / raw=不复权（#84）

    def adjust_for(self, symbol: str) -> str:
        """某标的的复权口径（指数无复权 → "none"，个股沿用本数据源口径）

        供 Collector / 闭环按标的落库时标注，避免指数被当成 qfq 个股。
        """
        return INDEX_ADJUST if is_index_symbol(symbol) else self.adjust

    @abstractmethod
    def daily_kline(
        self,
        symbol: str,
        start: str | None = None,
        end: str | None = None,
    ) -> list[KlineBar]:
        """返回按日期升序的日K线

        Args:
            symbol: 标的代码，如 "sh600535"、"sz002366"、"sz399006"
            start:  起始日期 YYYY-MM-DD（含），None 表示不限
            end:    截止日期 YYYY-MM-DD（含），None 表示不限
        """


# ============================================================
# 实现 1：静态注入（外部工具场景）
# ============================================================

class StaticDataSource(DataSource):
    """外部注入的数据源：AI 通过 westock-mcp/tdx-connector 拉取后写入

    模拟引擎/知识库无需感知数据来源，本类仅做缓存、排序与日期过滤。
    """

    def __init__(self, bars_by_symbol: dict[str, Iterable[KlineBar]] | None = None):
        self._data: dict[str, list[KlineBar]] = {
            sym: sorted(bars, key=lambda b: b.date)
            for sym, bars in (bars_by_symbol or {}).items()
        }
        self.adjust = "unknown"

    def set_symbol(self, symbol: str, bars: Iterable[KlineBar]) -> None:
        """注入/覆盖某标的的 K 线数据"""
        bars = sorted(bars, key=lambda b: b.date)
        self._data[symbol] = bars
        log.info("数据源注入 %s: %d 根K线", symbol, len(bars))

    def daily_kline(
        self,
        symbol: str,
        start: str | None = None,
        end: str | None = None,
    ) -> list[KlineBar]:
        bars = self._data.get(symbol, [])
        if start:
            bars = [b for b in bars if b.date >= start]
        if end:
            bars = [b for b in bars if b.date <= end]
        return list(bars)


# ============================================================
# 实现 2：akshare（独立环境，不依赖任何外部工具）
# ============================================================

# 常见指数代码（走指数接口；其余按个股接口处理）
_INDEX_CODES = {
    "sh000001", "sh000016", "sh000300", "sh000905",  # 上证指数 / 上证50 / 沪深300 / 中证500
    "sz399001", "sz399006",                          # 深证成指 / 创业板指
}

# 指数落库口径：指数不存在复权，adjust 记 "none" 以与个股 qfq/raw 区分，
# 避免被"技术因子只认 tencent/qfq"的门禁误判（指数只作基准，不参与个股评分）。
INDEX_ADJUST = "none"


def is_index_symbol(symbol: str) -> bool:
    """指数代码判定（上证 sh000xxx / 深证 sz399xxx）"""
    s = (symbol or "").lower()
    code = s[2:] if len(s) >= 6 and s[:2] in ("sh", "sz", "bj") else s
    return (s.startswith("sh") and code.startswith("000")) or (
        s.startswith("sz") and code.startswith("399"))


def _pure_code(symbol: str) -> str:
    """sh600535 -> 600535；已是纯数字则原样返回"""
    if len(symbol) >= 6 and symbol[:2] in ("sh", "sz", "bj"):
        return symbol[2:]
    return symbol


class AkshareDataSource(DataSource):
    """akshare 开源数据源（需 pip install akshare）

    优点：完全开源免费、不依赖任何 AI 软件，换电脑/换工具后依然可用。
    说明：akshare 接口字段随版本可能微调；拉取失败会记录日志并抛出异常。
    """

    adjust = "raw"   # stock_zh_a_hist adjust="" 即不复权

    def daily_kline(
        self,
        symbol: str,
        start: str | None = None,
        end: str | None = None,
    ) -> list[KlineBar]:
        import akshare as ak  # 延迟导入：未安装 akshare 的环境也能用其他数据源

        if symbol in _INDEX_CODES:
            df = ak.stock_zh_index_daily_em(symbol=symbol)
        else:
            df = ak.stock_zh_a_hist(
                symbol=_pure_code(symbol),
                period="daily",
                start_date=(start or "19700101").replace("-", ""),
                end_date=(end or "20991231").replace("-", ""),
                adjust="",
            )

        # 统一列名（akshare 返回中文列名）
        df = df.rename(
            columns={"日期": "date", "开盘": "open", "收盘": "close", "最高": "high", "最低": "low"}
        )

        # 日期过滤
        mask = [True] * len(df)
        if start:
            mask = [m and (str(d) >= start) for m, d in zip(mask, df["date"])]
        if end:
            mask = [m and (str(d) <= end) for m, d in zip(mask, df["date"])]
        df = df[mask]

        bars = [
            KlineBar(str(row.date), float(row.open), float(row.close), float(row.high), float(row.low))
            for row in df.itertuples(index=False)
        ]
        log.info("akshare 拉取 %s: %d 根K线", symbol, len(bars))
        return bars


# ============================================================
# 实现 3：腾讯公开行情 HTTP 接口（纯标准库，零依赖）
# ============================================================

class TencentHttpDataSource(DataSource):
    """腾讯公开行情 HTTP 接口（纯标准库 urllib，零依赖）

    不依赖任何 AI 软件、无需 pip 安装：换电脑/换工具后，只要有 Python 就能拉日K。
    接口：web.ifzq.gtimg.cn/appstock/app/newfqkline/get（前复权）
    注：旧路径 fqkline 自 2026-10-31 起被腾讯 WAF 拦截（HTTP 501 + 跳转页），改用 newfqkline

    用法:
        src = TencentHttpDataSource()
        bars = src.daily_kline("sh600535", "2026-07-29", "2026-07-31")
    """

    adjust = "qfq"   # 腾讯 newfqkline qfq=前复权（仅个股；指数无复权，见 adjust_for）

    _BASE_URL = "https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get"
    # 同源多 host（均为腾讯前复权）：主域限流/501 时切换镜像，避免回退到不复权源
    # 2026-11-05：旧 fqkline 路径被 WAF 拦成 501，全部改用 newfqkline（实测三 host 均通）
    _BASE_URLS = (
        "https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get",
        "https://ifzq.gtimg.cn/appstock/app/newfqkline/get",
        "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
    )

    @classmethod
    def _get_payload(cls, params: str) -> dict:
        """依次尝试腾讯同源 host（前复权），全部失败抛出最后一个异常"""
        import json
        import urllib.request

        last_exc: Exception | None = None
        for base in cls._BASE_URLS:
            try:
                with urllib.request.urlopen(f"{base}?param={params}", timeout=15) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except Exception as exc:
                last_exc = exc
        raise last_exc or RuntimeError("腾讯行情接口不可用")

    @staticmethod
    def _norm_date(d: str | None) -> str:
        """YYYYMMDD -> YYYY-MM-DD；已是横杠格式则原样返回

        腾讯接口要求日期带横杠（如 2026-07-04），否则返回 param error。
        """
        if not d:
            return ""
        if "-" in d:
            return d
        if len(d) == 8 and d.isdigit():
            return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        return d

    def daily_kline(
        self,
        symbol: str,
        start: str | None = None,
        end: str | None = None,
    ) -> list[KlineBar]:
        s = self._norm_date(start) or "1900-01-01"
        e = self._norm_date(end) or "2099-12-31"
        # 注：param 末位必须带 qfq，腾讯才返回日K（指数实测不带该参数只返回 version）；
        # 指数返回的是 day 节点原始点位（不存在复权），落库口径由 adjust_for 标为 "none"。
        params = f"{symbol},day,{s},{e},640,qfq"
        payload = self._get_payload(params)

        data_node = payload.get("data", {})
        # 接口偶发 data 整体为 list（历史故障：'list' object has no attribute 'get'）
        node = data_node.get(symbol, {}) if isinstance(data_node, dict) else {}
        if isinstance(node, list):
            rows = node
        else:
            rows = (node.get("qfqday") or node.get("day") or []) if isinstance(node, dict) else []

        bars = []
        for row in rows:
            date, open_p, close, high, low = row[0], row[1], row[2], row[3], row[4]
            # 腾讯接口日K行：date,open,close,high,low,[volume,...]（P1-3 补 volume）
            volume = float(row[5]) if len(row) > 5 and row[5] not in (None, "") else None
            if start and str(date) < start:
                continue
            if end and str(date) > end:
                continue
            bars.append(KlineBar(str(date), float(open_p), float(close),
                                 float(high), float(low), volume=volume))

        # 兜底：带日期参数时腾讯可能漏掉"当日未结算"K线（如收盘后不久），
        # 此时用无日期参数再拉一次并合并（确保增量采集能拿到当日真实数据）。
        if end and (not bars or bars[-1].date < end):
            try:
                payload2 = self._get_payload(f"{symbol},day,,,640,qfq")
                data_node2 = payload2.get("data", {})
                node2 = data_node2.get(symbol, {}) if isinstance(data_node2, dict) else {}
                rows2 = node2 if isinstance(node2, list) else (
                    node2.get("qfqday") or node2.get("day") or []
                ) if isinstance(node2, dict) else []
                known = {b.date for b in bars}
                for row in rows2:
                    date = str(row[0])
                    if date in known or str(date) < (start or "0000-01-01") or str(date) > end:
                        continue
                    volume = float(row[5]) if len(row) > 5 and row[5] not in (None, "") else None
                    bars.append(KlineBar(date, float(row[1]), float(row[2]),
                                         float(row[3]), float(row[4]), volume=volume))
                bars.sort(key=lambda b: b.date)
                if rows2:
                    log.info("腾讯HTTP兜底补拉 %s 当日K线（原始区间漏当日）", symbol)
            except Exception:
                log.warning("腾讯HTTP兜底补拉 %s 失败，维持原始数据", symbol)

        log.info("腾讯HTTP拉取 %s: %d 根K线（volume非空%d）",
                 symbol, len(bars), sum(1 for b in bars if b.volume))
        return bars


# ============================================================
# 估值快照（五因子"估值水平"数据源，东财公开接口）
# ============================================================

def _parse_tencent_quote(raw: str) -> dict:
    """解析腾讯 qt.gtimg.cn 报价串：提取 PE(TTM, idx39) 与 PB(idx46)"""
    parts = raw.split("~")
    def _num(idx: int):
        try:
            v = float(parts[idx])
            return v if v else None
        except (IndexError, TypeError, ValueError):
            return None
    pe, pb = _num(39), _num(46)
    return {"pe": pe, "pb": pb}


def fetch_valuation_pe_pb(symbol: str) -> dict:
    """多源实时估值（东财优先，失败重试后腾讯兜底）：返回 {"pe": float|None, "pb": float|None}

    东财: push2.eastmoney.com/api/qt/stock/get
        f162=市盈率(动态, ×100), f167=市净率(×100)
    腾讯兜底: qt.gtimg.cn  idx39=PE(TTM), idx46=PB（实测与东财 PB 一致）
    说明: 两源都失败返回 {}，调用方视为"数据缺失"→ 评分不推荐（管住手）。
    """
    import json
    import urllib.request

    market = {"sh": "1", "sz": "0", "bj": "0"}.get((symbol or "")[:2].lower())
    if not market or len(symbol) < 3:
        return {}
    secid = f"{market}.{symbol[2:]}"
    url = ("https://push2.eastmoney.com/api/qt/stock/get"
           f"?secid={secid}&fields=f43,f57,f58,f162,f167")
    for attempt in (1, 2):      # 东财重试 1 次（接口不稳定）
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8")).get("data") or {}

            def _val(x, scale: float = 100.0):
                try:
                    v = float(x) / scale
                    return v if v else None
                except (TypeError, ValueError):
                    return None

            pe, pb = _val(data.get("f162")), _val(data.get("f167"))
            if pe is not None or pb is not None:
                return {"pe": pe, "pb": pb}
        except Exception:
            if attempt == 1:
                continue

    # 腾讯兜底（PE-TTM / PB）
    try:
        with urllib.request.urlopen(f"https://qt.gtimg.cn/q={symbol}", timeout=8) as resp:
            raw = resp.read().decode("gbk", errors="replace")
        quote = _parse_tencent_quote(raw)
        if quote["pe"] is not None or quote["pb"] is not None:
            log.info("估值接口腾讯兜底 %s: %s", symbol, quote)
            return quote
    except Exception:
        pass

    log.warning("估值接口失败 %s（东财+腾讯均不可用，按数据缺失处理，不推荐）", symbol)
    return {}


# ============================================================
# 实现 4：通达信协议（pytdx，TCP 7709）— 第三数据源
# ============================================================

class TdxDataSource(DataSource):
    """通达信协议数据源（pytdx，可选依赖）

    价值：真实官方行情，与腾讯/东财构成双源交叉验证；当日K线兜底
    （腾讯日K收盘后可能延迟，通达信 TCP 直连不封 IP、无需限流）。
    懒加载：未安装 pytdx 时 daily_kline 返回 [] 并告警，不阻塞其他源。

    用法:
        src = TdxDataSource()
        bars = src.daily_kline("sh600019", "2026-08-01", "2026-08-05")
    """

    adjust = "raw"   # 通达信返回不复权真实成交价

    _DEFAULT_SERVERS = [
        ("115.238.56.198", 7709),
        ("119.147.212.81", 7709),
        ("124.160.88.183", 7709),
        ("218.108.98.244", 7709),
        ("218.75.126.9", 7709),
        ("180.153.18.170", 7709),
    ]
    _MARKET = {"sh": 1, "sz": 0, "bj": 0}

    @staticmethod
    def _is_index(symbol: str) -> bool:
        """指数判定：上证指数(sh000xxx)/深证指数(sz399xxx)走 get_index_bars"""
        s = (symbol or "").lower()
        code = s[2:] if s[:2] in ("sh", "sz", "bj") else s
        return (s.startswith("sh") and code.startswith("000")) or (
            s.startswith("sz") and code.startswith("399"))

    def __init__(self, servers: list | None = None, timeout: int = 8):
        self.servers = servers or self._DEFAULT_SERVERS
        self.timeout = timeout
        self._api = None

    def _connect(self) -> bool:
        """连接通达信服务器（多服务器重试）"""
        from pytdx.hq import TdxHq_API

        if self._api is None:
            self._api = TdxHq_API(heartbeat=True)
        else:
            self._api.disconnect()
        for host, port in self.servers:
            try:
                if self._api.connect(host, port, time_out=self.timeout):
                    log.info("通达信连接成功 %s:%s", host, port)
                    return True
            except Exception:
                continue
        return False

    def daily_kline(
        self,
        symbol: str,
        start: str | None = None,
        end: str | None = None,
    ) -> list[KlineBar]:
        import importlib.util

        if importlib.util.find_spec("pytdx") is None:
            log.warning("pytdx 未安装，通达信源不可用（pip install pytdx）")
            return []
        market = self._MARKET.get((symbol or "")[:2].lower())
        if market is None or len(symbol) < 3:
            return []
        code = symbol[2:]
        if not self._connect():
            log.warning("通达信服务器连接失败 %s", symbol)
            return []
        try:
            # 日K线 category=9；一次最多 800 根（约 3 年），按日期过滤
            if self._is_index(symbol):
                bars = self._api.get_index_bars(9, market, code, 0, 800) or []
            else:
                bars = self._api.get_security_bars(9, market, code, 0, 800) or []
        except Exception as e:
            log.warning("通达信拉取失败 %s: %s", symbol, e)
            bars = []
        finally:
            self._api.disconnect()
        out = []
        for b in bars:
            d = str(b.get("datetime", ""))[:10]
            if start and d < start:
                continue
            if end and d > end:
                continue
            out.append(KlineBar(
                d,
                float(b["open"]), float(b["close"]),
                float(b["high"]), float(b["low"]),
                volume=float(b.get("vol") or 0),
            ))
        return out
