"""增量式技术指标计算（面向 Tick 级热循环，O(1) 更新）。"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field


class EMA:
    """指数移动平均，首个样本作为种子。"""

    __slots__ = ("alpha", "value")

    def __init__(self, period: int) -> None:
        self.alpha = 2.0 / (period + 1.0)
        self.value: float | None = None

    def update(self, x: float) -> float:
        self.value = x if self.value is None else self.value + self.alpha * (x - self.value)
        return self.value


class RSI:
    """Wilder 平滑 RSI。样本不足时返回 50（中性）。"""

    __slots__ = ("period", "_avg_gain", "_avg_loss", "_prev", "_count")

    def __init__(self, period: int = 14) -> None:
        self.period = period
        self._avg_gain = 0.0
        self._avg_loss = 0.0
        self._prev: float | None = None
        self._count = 0

    def update(self, price: float) -> float:
        if self._prev is None:
            self._prev = price
            return 50.0
        change = price - self._prev
        self._prev = price
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        self._count += 1
        if self._count <= self.period:
            # 前 period 个样本用简单平均做种子
            self._avg_gain += (gain - self._avg_gain) / self._count
            self._avg_loss += (loss - self._avg_loss) / self._count
        else:
            k = 1.0 / self.period
            self._avg_gain = self._avg_gain * (1 - k) + gain * k
            self._avg_loss = self._avg_loss * (1 - k) + loss * k
        if self._avg_loss == 0.0:
            return 100.0 if self._avg_gain > 0 else 50.0
        rs = self._avg_gain / self._avg_loss
        return 100.0 - 100.0 / (1.0 + rs)


class MACD:
    """MACD(12, 26, 9)，返回 (dif, dea, histogram)。"""

    __slots__ = ("fast", "slow", "signal")

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9) -> None:
        self.fast = EMA(fast)
        self.slow = EMA(slow)
        self.signal = EMA(signal)

    def update(self, price: float) -> tuple[float, float, float]:
        dif = self.fast.update(price) - self.slow.update(price)
        dea = self.signal.update(dif)
        return dif, dea, (dif - dea) * 2.0


class RollingWindow:
    """按时间长度滚动的价格窗口，用于涨跌幅与波动率。"""

    __slots__ = ("seconds", "_buf")

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self._buf: deque[tuple[float, float]] = deque()

    def update(self, ts: float, price: float) -> None:
        self._buf.append((ts, price))
        cutoff = ts - self.seconds
        while len(self._buf) > 1 and self._buf[0][0] < cutoff:
            self._buf.popleft()

    @property
    def first(self) -> float | None:
        return self._buf[0][1] if self._buf else None

    def change_pct(self, price: float) -> float:
        base = self.first
        if base is None or base == 0:
            return 0.0
        return (price - base) / base * 100.0

    def volatility_pct(self) -> float:
        """窗口内对数收益的标准差（百分比）。"""
        if len(self._buf) < 3:
            return 0.0
        rets: list[float] = []
        prev = self._buf[0][1]
        for _, p in list(self._buf)[1:]:
            if prev > 0 and p > 0:
                rets.append(math.log(p / prev))
            prev = p
        if len(rets) < 2:
            return 0.0
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        return math.sqrt(var) * 100.0


@dataclass
class IndicatorSnapshot:
    rsi_14: float = 50.0
    macd_dif: float = 0.0
    macd_dea: float = 0.0
    macd_histogram: float = 0.0
    price_change_1m_pct: float = 0.0
    price_change_5m_pct: float = 0.0
    ma_fast: float = 0.0
    ma_slow: float = 0.0
    ma_gap_pct: float = 0.0
    volatility_pct: float = 0.0
    imbalance: float = 0.0  # 盘口买卖量失衡 [-1, 1]
    samples: int = 0

    def as_dict(self) -> dict[str, float]:
        return {
            "rsi_14": round(self.rsi_14, 3),
            "macd_dif": round(self.macd_dif, 5),
            "macd_dea": round(self.macd_dea, 5),
            "macd_histogram": round(self.macd_histogram, 5),
            "price_change_1m_pct": round(self.price_change_1m_pct, 4),
            "price_change_5m_pct": round(self.price_change_5m_pct, 4),
            "ma_fast": round(self.ma_fast, 4),
            "ma_slow": round(self.ma_slow, 4),
            "ma_gap_pct": round(self.ma_gap_pct, 4),
            "volatility_pct": round(self.volatility_pct, 4),
            "imbalance": round(self.imbalance, 4),
            "samples": self.samples,
        }


@dataclass
class IndicatorEngine:
    """单标的指标聚合器。"""

    rsi_period: int = 14
    ma_fast_period: int = 12
    ma_slow_period: int = 48
    _rsi: RSI = field(init=False)
    _macd: MACD = field(init=False)
    _ma_fast: EMA = field(init=False)
    _ma_slow: EMA = field(init=False)
    _w1m: RollingWindow = field(init=False)
    _w5m: RollingWindow = field(init=False)
    _samples: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._rsi = RSI(self.rsi_period)
        self._macd = MACD()
        self._ma_fast = EMA(self.ma_fast_period)
        self._ma_slow = EMA(self.ma_slow_period)
        self._w1m = RollingWindow(60.0)
        self._w5m = RollingWindow(300.0)
        self._samples = 0

    def update(
        self,
        ts: float,
        price: float,
        bid_vol: float = 0.0,
        ask_vol: float = 0.0,
    ) -> IndicatorSnapshot:
        self._samples += 1
        rsi = self._rsi.update(price)
        dif, dea, hist = self._macd.update(price)
        ma_fast = self._ma_fast.update(price)
        ma_slow = self._ma_slow.update(price)
        self._w1m.update(ts, price)
        self._w5m.update(ts, price)
        total_vol = bid_vol + ask_vol
        imbalance = (bid_vol - ask_vol) / total_vol if total_vol > 0 else 0.0
        return IndicatorSnapshot(
            rsi_14=rsi,
            macd_dif=dif,
            macd_dea=dea,
            macd_histogram=hist,
            price_change_1m_pct=self._w1m.change_pct(price),
            price_change_5m_pct=self._w5m.change_pct(price),
            ma_fast=ma_fast,
            ma_slow=ma_slow,
            ma_gap_pct=((ma_fast - ma_slow) / ma_slow * 100.0) if ma_slow else 0.0,
            volatility_pct=self._w5m.volatility_pct(),
            imbalance=imbalance,
            samples=self._samples,
        )
