"""模拟账户、撮合成交与绩效统计。

资金模型对美股与期货统一：
* 开仓冻结 ``名义价值 * 保证金比例``（美股比例为 1.0，即全额买入）；
* 平仓释放保证金并结算盈亏；
* ``权益 = 可用资金 + 占用保证金 + 浮动盈亏``。
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Any, Iterable

from .instruments import Instrument, resolve_instrument
from .models import Position, TradeLog

EQUITY_CURVE_MAX = 4_000
TRADE_LOG_MAX = 2_000


@dataclass(slots=True)
class OpenLot:
    """持仓的资金占用信息。"""

    margin: float = 0.0
    commission: float = 0.0


class Portfolio:
    """单账户多标的模拟持仓与绩效统计。"""

    def __init__(
        self,
        initial_capital: float = 1_000_000.0,
        commission_rate: float = 0.0002,
    ) -> None:
        self.initial_capital = initial_capital
        self.commission_rate = commission_rate
        self.reset()

    # ------------------------------------------------------------------ #
    def reset(self, initial_capital: float | None = None) -> None:
        if initial_capital is not None:
            self.initial_capital = initial_capital
        self.cash = self.initial_capital
        self.positions: dict[str, Position] = {}
        self.lots: dict[str, OpenLot] = {}
        self.instruments: dict[str, Instrument] = {}
        self.marks: dict[str, float] = {}
        self.trades: deque[TradeLog] = deque(maxlen=TRADE_LOG_MAX)
        self.equity_curve: deque[tuple[float, float]] = deque(maxlen=EQUITY_CURVE_MAX)
        self.closed_pnls: list[float] = []
        self.closed_pnl_pcts: list[float] = []
        self.total_commission = 0.0
        self.realized_pnl = 0.0
        self.peak_equity = self.initial_capital
        self.max_drawdown_pct = 0.0
        self._trade_seq = 0
        self._last_curve_ts = 0.0

    # ------------------------------------------------------------------ #
    def instrument(self, symbol: str) -> Instrument:
        inst = self.instruments.get(symbol)
        if inst is None:
            inst = resolve_instrument(symbol)
            self.instruments[symbol] = inst
        return inst

    def position(self, symbol: str) -> Position:
        pos = self.positions.get(symbol)
        if pos is None:
            pos = Position(symbol=symbol)
            self.positions[symbol] = pos
        return pos

    def mark(self, symbol: str, price: float) -> None:
        self.marks[symbol] = price

    # ------------------------------------------------------------------ #
    @property
    def margin_used(self) -> float:
        return sum(lot.margin for lot in self.lots.values())

    @property
    def unrealized_pnl(self) -> float:
        total = 0.0
        for symbol, pos in self.positions.items():
            if not pos.is_open:
                continue
            mark = self.marks.get(symbol, pos.avg_price)
            inst = self.instrument(symbol)
            direction = 1.0 if pos.side == "long" else -1.0
            total += (mark - pos.avg_price) * pos.qty * inst.multiplier * direction
        return total

    @property
    def equity(self) -> float:
        return self.cash + self.margin_used + self.unrealized_pnl

    @property
    def total_return_pct(self) -> float:
        if self.initial_capital <= 0:
            return 0.0
        return (self.equity - self.initial_capital) / self.initial_capital * 100.0

    def can_afford(self, symbol: str, qty: float, price: float) -> tuple[bool, str]:
        inst = self.instrument(symbol)
        need = inst.margin(price, qty) + inst.notional(price, qty) * self.commission_rate
        if need > self.cash:
            return False, f"可用资金不足（需 {need:,.2f}，剩余 {self.cash:,.2f}）"
        return True, ""

    # ------------------------------------------------------------------ #
    def open_position(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        ts: float,
        *,
        trigger: str,
        reason_code: str,
        order_type: str,
        spread: float,
        jev_action: str = "",
        jev_prob: float = 0.0,
        jev_confidence: float = 0.0,
        jev_source: str = "",
        probabilities: dict[str, float] | None = None,
        indicators: dict[str, float] | None = None,
    ) -> TradeLog:
        inst = self.instrument(symbol)
        notional = inst.notional(price, qty)
        commission = notional * self.commission_rate
        margin = inst.margin(price, qty)

        self.cash -= margin + commission
        self.total_commission += commission

        pos = self.position(symbol)
        pos.side = "long" if side == "long" else "short"
        pos.qty = qty
        pos.avg_price = price
        pos.opened_at = ts
        pos.peak_pnl_pct = 0.0
        self.lots[symbol] = OpenLot(margin=margin, commission=commission)
        self.mark(symbol, price)

        return self._record(
            ts=ts,
            symbol=symbol,
            action="open_long" if side == "long" else "open_short",
            side="buy" if side == "long" else "sell",
            price=price,
            qty=qty,
            amount=notional,
            commission=commission,
            realized_pnl=0.0,
            realized_pnl_pct=0.0,
            trigger=trigger,
            reason_code=reason_code,
            order_type=order_type,
            spread=spread,
            jev_action=jev_action,
            jev_prob=jev_prob,
            jev_confidence=jev_confidence,
            jev_source=jev_source,
            probabilities=probabilities or {},
            indicators=indicators or {},
        )

    def close_position(
        self,
        symbol: str,
        price: float,
        ts: float,
        *,
        trigger: str,
        reason_code: str,
        order_type: str,
        spread: float,
        jev_action: str = "",
        jev_prob: float = 0.0,
        jev_confidence: float = 0.0,
        jev_source: str = "",
        probabilities: dict[str, float] | None = None,
        indicators: dict[str, float] | None = None,
    ) -> TradeLog | None:
        pos = self.positions.get(symbol)
        if pos is None or not pos.is_open:
            return None

        inst = self.instrument(symbol)
        direction = 1.0 if pos.side == "long" else -1.0
        gross = (price - pos.avg_price) * pos.qty * inst.multiplier * direction
        notional = inst.notional(price, pos.qty)
        commission = notional * self.commission_rate
        lot = self.lots.pop(symbol, OpenLot())
        net = gross - commission - lot.commission

        self.cash += lot.margin + gross - commission
        self.total_commission += commission
        self.realized_pnl += net
        self.closed_pnls.append(net)
        cost_basis = inst.margin(pos.avg_price, pos.qty) or 1.0
        pnl_pct = net / cost_basis * 100.0
        self.closed_pnl_pcts.append(pnl_pct)

        action = "close_long" if pos.side == "long" else "close_short"
        side = "sell" if pos.side == "long" else "buy"
        qty = pos.qty
        pos.side = "none"
        pos.qty = 0.0
        pos.avg_price = 0.0
        pos.opened_at = 0.0
        pos.peak_pnl_pct = 0.0
        self.mark(symbol, price)

        return self._record(
            ts=ts,
            symbol=symbol,
            action=action,
            side=side,
            price=price,
            qty=qty,
            amount=notional,
            commission=commission,
            realized_pnl=net,
            realized_pnl_pct=pnl_pct,
            trigger=trigger,
            reason_code=reason_code,
            order_type=order_type,
            spread=spread,
            jev_action=jev_action,
            jev_prob=jev_prob,
            jev_confidence=jev_confidence,
            jev_source=jev_source,
            probabilities=probabilities or {},
            indicators=indicators or {},
        )

    def _record(self, **kwargs: Any) -> TradeLog:
        self._trade_seq += 1
        trade = TradeLog(id=self._trade_seq, equity_after=self.equity, **kwargs)
        self.trades.append(trade)
        self.sample_equity(kwargs["ts"], force=True)
        return trade

    # ------------------------------------------------------------------ #
    def sample_equity(self, ts: float, force: bool = False, min_gap: float = 1.0) -> None:
        """把权益写入资金曲线（默认至少间隔 1 秒采样一次）。"""
        if not force and self.equity_curve and (ts - self._last_curve_ts) < min_gap:
            return
        equity = self.equity
        self.equity_curve.append((round(ts, 3), round(equity, 4)))
        self._last_curve_ts = ts
        if equity > self.peak_equity:
            self.peak_equity = equity
        elif self.peak_equity > 0:
            dd = (self.peak_equity - equity) / self.peak_equity * 100.0
            self.max_drawdown_pct = max(self.max_drawdown_pct, dd)

    # ------------------------------------------------------------------ #
    def sharpe(self, periods_per_year: float = 252.0 * 6.5 * 60.0) -> float:
        """基于资金曲线收益率序列的夏普比率（无风险利率取 0）。"""
        curve = [e for _, e in self.equity_curve]
        if len(curve) < 3:
            return 0.0
        rets: list[float] = []
        for prev, cur in zip(curve, curve[1:]):
            if prev > 0:
                rets.append(cur / prev - 1.0)
        if len(rets) < 2:
            return 0.0
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        std = math.sqrt(var)
        if std == 0:
            return 0.0
        return mean / std * math.sqrt(periods_per_year)

    def stats(self) -> dict[str, Any]:
        wins = [p for p in self.closed_pnls if p > 0]
        losses = [p for p in self.closed_pnls if p <= 0]
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        closed = len(self.closed_pnls)
        return {
            "initial_capital": round(self.initial_capital, 2),
            "equity": round(self.equity, 2),
            "cash": round(self.cash, 2),
            "margin_used": round(self.margin_used, 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "unrealized_pnl": round(self.unrealized_pnl, 2),
            "total_return_pct": round(self.total_return_pct, 4),
            "total_commission": round(self.total_commission, 2),
            "closed_trades": closed,
            "total_fills": self._trade_seq,
            "win_trades": len(wins),
            "loss_trades": len(losses),
            "win_rate": round(len(wins) / closed * 100.0, 2) if closed else 0.0,
            "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
            "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
            "profit_factor": (round(gross_win / gross_loss, 3) if gross_loss > 0 else (99.0 if wins else 0.0)),
            "payoff_ratio": round(
                (gross_win / len(wins)) / (gross_loss / len(losses)), 3
            )
            if wins and losses and gross_loss > 0
            else 0.0,
            "max_drawdown_pct": round(self.max_drawdown_pct, 3),
            "sharpe": round(self.sharpe(), 3),
        }

    def open_positions(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for symbol, pos in self.positions.items():
            if not pos.is_open:
                continue
            inst = self.instrument(symbol)
            mark = self.marks.get(symbol, pos.avg_price)
            direction = 1.0 if pos.side == "long" else -1.0
            out.append(
                {
                    "symbol": symbol,
                    "side": pos.side,
                    "qty": pos.qty,
                    "avg_price": round(pos.avg_price, 4),
                    "mark_price": round(mark, 4),
                    "market": inst.market,
                    "currency": inst.currency,
                    "opened_at": pos.opened_at,
                    "unrealized_pnl": round(
                        (mark - pos.avg_price) * pos.qty * inst.multiplier * direction, 2
                    ),
                    "unrealized_pnl_pct": round(pos.unrealized_pnl_pct(mark), 4),
                    "margin": round(self.lots.get(symbol, OpenLot()).margin, 2),
                }
            )
        return out

    def recent_trades(self, limit: int = 200, symbol: str | None = None) -> list[dict[str, Any]]:
        items: Iterable[TradeLog] = reversed(self.trades)
        rows = [t.to_dict() for t in items if symbol is None or t.symbol == symbol]
        return rows[:limit]
