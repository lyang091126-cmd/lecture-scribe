"""标的元信息：区分美股与国内商品期货的报价单位、合约乘数与保证金。"""

from __future__ import annotations

from dataclasses import dataclass

CN_EXCHANGES = {"SHFE", "DCE", "CZCE", "INE", "CFFEX", "GFEX"}

# 常见商品期货的近似参数（合约乘数 / 最小变动价位 / 参考价）
_FUTURE_PRESETS: dict[str, tuple[float, float, float]] = {
    "rb": (10.0, 1.0, 3450.0),  # 螺纹钢
    "hc": (10.0, 1.0, 3400.0),  # 热卷
    "i": (100.0, 0.5, 780.0),  # 铁矿石
    "m": (10.0, 1.0, 3100.0),  # 豆粕
    "y": (10.0, 2.0, 8200.0),  # 豆油
    "p": (10.0, 2.0, 8600.0),  # 棕榈油
    "cu": (5.0, 10.0, 78000.0),  # 铜
    "al": (5.0, 5.0, 20500.0),  # 铝
    "au": (1000.0, 0.02, 560.0),  # 黄金
    "ag": (15.0, 1.0, 8300.0),  # 白银
    "ru": (10.0, 5.0, 15500.0),  # 橡胶
    "MA": (10.0, 1.0, 2450.0),  # 甲醇
    "TA": (5.0, 2.0, 5600.0),  # PTA
    "SA": (20.0, 1.0, 1450.0),  # 纯碱
    "FG": (20.0, 1.0, 1300.0),  # 玻璃
}

_EQUITY_PRESETS: dict[str, float] = {
    "AAPL": 232.0,
    "TSLA": 268.0,
    "NVDA": 178.0,
    "MSFT": 425.0,
    "AMZN": 196.0,
    "SPY": 585.0,
    "QQQ": 505.0,
    "META": 612.0,
    "GOOGL": 172.0,
}


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str
    market: str  # "us_equity" | "cn_future"
    tick_size: float
    multiplier: float  # 合约乘数（美股为 1）
    ref_price: float  # 合成行情与回测的起始参考价
    margin_rate: float  # 保证金比例（美股按全额 1.0）
    currency: str

    @property
    def is_future(self) -> bool:
        return self.market == "cn_future"

    def round_price(self, price: float) -> float:
        if self.tick_size <= 0:
            return round(price, 4)
        return round(round(price / self.tick_size) * self.tick_size, 8)

    def notional(self, price: float, qty: float) -> float:
        """一笔头寸的名义价值。"""
        return price * qty * self.multiplier

    def margin(self, price: float, qty: float) -> float:
        """占用资金：期货按保证金，美股按全额。"""
        return self.notional(price, qty) * self.margin_rate


def _future_root(code: str) -> str:
    root = ""
    for ch in code:
        if ch.isdigit():
            break
        root += ch
    return root


def resolve_instrument(symbol: str) -> Instrument:
    """根据代码推断标的类型。

    * ``rb2610.SHFE`` / ``m2610.DCE`` → 国内商品期货
    * ``AAPL`` / ``TSLA`` → 美股
    """
    raw = symbol.strip()
    code, _, exch = raw.partition(".")
    if exch.upper() in CN_EXCHANGES or (not exch and _future_root(code) and any(c.isdigit() for c in code)):
        root = _future_root(code)
        mult, tick, ref = _FUTURE_PRESETS.get(root, _FUTURE_PRESETS.get(root.upper(), (10.0, 1.0, 3000.0)))
        return Instrument(
            symbol=raw,
            market="cn_future",
            tick_size=tick,
            multiplier=mult,
            ref_price=ref,
            margin_rate=0.12,
            currency="CNY",
        )
    ref = _EQUITY_PRESETS.get(code.upper(), 100.0)
    return Instrument(
        symbol=raw,
        market="us_equity",
        tick_size=0.01,
        multiplier=1.0,
        ref_price=ref,
        margin_rate=1.0,
        currency="USD",
    )
