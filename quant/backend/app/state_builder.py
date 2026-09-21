"""把 Tick + 指标 + 持仓 组装成给 Jev 的 State JSON。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .indicators import IndicatorSnapshot
from .instruments import Instrument
from .models import Position, Tick


def iso_ms(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_state(
    tick: Tick,
    indicators: IndicatorSnapshot,
    position: Position,
    instrument: Instrument,
    *,
    equity: float = 0.0,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """生成 Jev 决策输入。字段布局与设计文档一致，便于线上/离线模型复用。"""
    mark = tick.mid
    state: dict[str, Any] = {
        "symbol": tick.symbol,
        "market": instrument.market,
        "timestamp": iso_ms(tick.ts),
        "orderbook": {
            "ask_price_1": tick.ask_price_1,
            "ask_vol_1": tick.ask_vol_1,
            "bid_price_1": tick.bid_price_1,
            "bid_vol_1": tick.bid_vol_1,
            "spread": tick.spread,
            "last_price": tick.price,
        },
        "indicators": indicators.as_dict(),
        "current_position": {
            "side": position.side,
            "qty": position.qty,
            "avg_price": round(position.avg_price, 4),
            "unrealized_pnl_pct": round(position.unrealized_pnl_pct(mark), 4),
            "holding_sec": round(tick.ts - position.opened_at, 1) if position.is_open else 0.0,
        },
        "account": {"equity": round(equity, 2), "currency": instrument.currency},
    }
    if extra:
        state.update(extra)
    return state
