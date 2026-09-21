"""系统内部与 API 层共用的数据结构。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

Side = Literal["long", "short", "none"]
Action = Literal["open_long", "open_short", "close_position", "hold"]

ACTIONS: tuple[Action, ...] = ("open_long", "open_short", "close_position", "hold")


# --------------------------------------------------------------------------- #
# 行情
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Tick:
    """一笔盘口快照（回测与模拟盘统一使用）。"""

    symbol: str
    ts: float  # epoch 秒（float，保留毫秒精度）
    bid_price_1: float
    ask_price_1: float
    bid_vol_1: float = 0.0
    ask_vol_1: float = 0.0
    last_price: float | None = None
    volume: float = 0.0

    @property
    def mid(self) -> float:
        return (self.bid_price_1 + self.ask_price_1) / 2.0

    @property
    def spread(self) -> float:
        return round(self.ask_price_1 - self.bid_price_1, 8)

    @property
    def price(self) -> float:
        """用于指标计算的参考价：优先最新成交价，否则中间价。"""
        return self.last_price if self.last_price is not None else self.mid


@dataclass(slots=True)
class Position:
    symbol: str
    side: Side = "none"
    qty: float = 0.0
    avg_price: float = 0.0
    opened_at: float = 0.0
    # 开仓时记录的极值，用于移动止盈（保留扩展位）
    peak_pnl_pct: float = 0.0

    @property
    def is_open(self) -> bool:
        return self.side != "none" and self.qty > 0

    def unrealized_pnl(self, mark: float) -> float:
        if not self.is_open:
            return 0.0
        direction = 1.0 if self.side == "long" else -1.0
        return (mark - self.avg_price) * self.qty * direction

    def unrealized_pnl_pct(self, mark: float) -> float:
        if not self.is_open or self.avg_price == 0:
            return 0.0
        direction = 1.0 if self.side == "long" else -1.0
        return (mark - self.avg_price) / self.avg_price * 100.0 * direction


@dataclass(slots=True)
class JevDecision:
    """Jev `Choice` 原语的解析结果。"""

    probabilities: dict[str, float]
    confidence: float
    source: Literal["jev", "mock", "error"] = "mock"
    model: str = "jev-latest"
    reasoning: str = ""
    latency_ms: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def top_action(self) -> Action:
        if not self.probabilities:
            return "hold"
        best = max(self.probabilities.items(), key=lambda kv: kv[1])[0]
        return best  # type: ignore[return-value]

    @property
    def top_prob(self) -> float:
        if not self.probabilities:
            return 0.0
        return max(self.probabilities.values())


@dataclass(slots=True)
class TradeLog:
    """一笔成交记录（前端交易明细表直接消费）。"""

    id: int
    ts: float
    symbol: str
    action: str  # open_long / open_short / close_long / close_short
    side: str  # buy / sell
    price: float
    qty: float
    amount: float
    commission: float
    realized_pnl: float
    realized_pnl_pct: float
    equity_after: float
    trigger: str  # 人类可读的触发条件
    jev_action: str
    jev_prob: float
    jev_confidence: float
    jev_source: str
    spread: float
    order_type: str
    reason_code: str  # jev_signal / take_profit / stop_loss / flatten
    probabilities: dict[str, float] = field(default_factory=dict)
    indicators: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)


# --------------------------------------------------------------------------- #
# 控制面板参数
# --------------------------------------------------------------------------- #
class StrategyConfig(BaseModel):
    """运行时可热更新的策略参数（POST /api/control/config）。"""

    mode: Literal["backtest", "paper"] = "backtest"
    symbols: list[str] = Field(default_factory=lambda: ["AAPL", "rb2610.SHFE"])

    initial_capital: float = Field(default=1_000_000.0, gt=0)
    order_qty: float = Field(default=10.0, gt=0, description="单次下单数量（股/手）")
    order_type: Literal["ioc", "maker"] = "ioc"
    maker_timeout_ticks: int = Field(default=20, ge=1, le=5_000)

    # Jev 决策阈值
    buy_threshold: float = Field(default=0.70, ge=0.0, le=1.0)
    close_threshold: float = Field(default=0.60, ge=0.0, le=1.0)
    min_confidence: float = Field(default=0.60, ge=0.0, le=1.0)

    # 风控
    take_profit_pct: float = Field(default=1.5, ge=0.0, le=100.0)
    stop_loss_pct: float = Field(default=0.8, ge=0.0, le=100.0)
    max_spread: float = Field(default=5.0, ge=0.0, description="超过该价差不下单")
    cooldown_sec: float = Field(default=30.0, ge=0.0, description="同标的两次开仓最小间隔")
    max_trades_per_min: int = Field(default=30, ge=1, le=10_000)
    max_daily_loss_pct: float = Field(default=10.0, ge=0.0, le=100.0)

    # 成本
    commission_rate: float = Field(default=0.0002, ge=0.0, le=0.01)
    slippage_ticks: float = Field(default=0.0, ge=0.0)

    # 运行节奏
    decision_interval_ms: int = Field(default=3_000, ge=0, le=600_000)
    replay_speed: float = Field(default=0.0, ge=0.0, description="0=全速回测，>0 为倍速")
    backtest_ticks: int = Field(default=3_000, ge=10, le=500_000)

    # Jev 接入
    jev_enabled: bool = True
    jev_model: str = "jev-latest"

    @field_validator("symbols")
    @classmethod
    def _non_empty_symbols(cls, v: list[str]) -> list[str]:
        cleaned = [s.strip() for s in v if s and s.strip()]
        if not cleaned:
            raise ValueError("symbols 不能为空")
        if len(cleaned) > 20:
            raise ValueError("最多同时运行 20 个标的")
        return cleaned


class ConfigPatch(BaseModel):
    """控制面板的部分更新请求，未提供的字段保持原值。"""

    model_config = {"extra": "forbid"}

    mode: Literal["backtest", "paper"] | None = None
    symbols: list[str] | None = None
    initial_capital: float | None = Field(default=None, gt=0)
    order_qty: float | None = Field(default=None, gt=0)
    order_type: Literal["ioc", "maker"] | None = None
    maker_timeout_ticks: int | None = Field(default=None, ge=1, le=5_000)
    buy_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    close_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    min_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    take_profit_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    stop_loss_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    max_spread: float | None = Field(default=None, ge=0.0)
    cooldown_sec: float | None = Field(default=None, ge=0.0)
    max_trades_per_min: int | None = Field(default=None, ge=1, le=10_000)
    max_daily_loss_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    commission_rate: float | None = Field(default=None, ge=0.0, le=0.01)
    slippage_ticks: float | None = Field(default=None, ge=0.0)
    decision_interval_ms: int | None = Field(default=None, ge=0, le=600_000)
    replay_speed: float | None = Field(default=None, ge=0.0)
    backtest_ticks: int | None = Field(default=None, ge=10, le=500_000)
    jev_enabled: bool | None = None
    jev_model: str | None = None


class ControlAction(BaseModel):
    action: Literal["start", "pause", "resume", "reset", "flatten"]


def now_ms() -> float:
    return time.time()
