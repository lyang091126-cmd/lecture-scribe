"""事件驱动策略引擎：行情 -> 状态 -> Jev 决策 -> 风控 -> 撮合。

同一套热循环同时服务两种模式：

* ``backtest``：历史 Tick 全速推演，结束后强平并汇总绩效；
* ``paper``：订阅实时盘口，按当前买一/卖一模拟挂单与成交。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .datafeed import build_feed
from .events import EventBus
from .indicators import IndicatorEngine, IndicatorSnapshot
from .instruments import Instrument, resolve_instrument
from .jev_client import JevClient
from .models import ACTIONS, JevDecision, StrategyConfig, Tick
from .portfolio import Portfolio
from .state_builder import build_state

log = logging.getLogger("jev.engine")

# 回测全速推演时，限制事件推送频率，避免压垮 SSE 与浏览器
MAX_DECISION_EVENTS_PER_SEC = 25.0
DECISION_HISTORY = 500


@dataclass(slots=True)
class PendingOrder:
    """Maker 模式下的挂单。"""

    side: str  # "long" | "short"
    limit_price: float
    qty: float
    ticks_left: int
    decision: JevDecision
    trigger: str
    created_ts: float


@dataclass
class SymbolRuntime:
    symbol: str
    instrument: Instrument
    indicators: IndicatorEngine = field(default_factory=IndicatorEngine)
    source: str = "-"
    ticks: int = 0
    last_tick: Tick | None = None
    last_snapshot: IndicatorSnapshot = field(default_factory=IndicatorSnapshot)
    last_decision_ts: float = 0.0
    last_open_ts: float = 0.0
    trade_times: deque[float] = field(default_factory=lambda: deque(maxlen=512))
    pending: PendingOrder | None = None
    jev_calls: int = 0
    finished: bool = False


class StrategyEngine:
    """引擎单例：持有配置、账户、行情任务与事件总线。"""

    def __init__(self, config: StrategyConfig, bus: EventBus, jev: JevClient) -> None:
        self.config = config
        self.bus = bus
        self.jev = jev
        self.portfolio = Portfolio(config.initial_capital, config.commission_rate)
        self.status = "idle"  # idle | running | paused | finished | halted | error
        self.runtimes: dict[str, SymbolRuntime] = {}
        self.decisions: deque[dict[str, Any]] = deque(maxlen=DECISION_HISTORY)
        self.started_at: float = 0.0
        self.finished_at: float = 0.0
        self.total_decisions = 0
        self.rejected_signals = 0
        self.last_error: str | None = None

        self._tasks: list[asyncio.Task[None]] = []
        self._supervisor: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._resume = asyncio.Event()
        self._resume.set()
        self._exec_lock = asyncio.Lock()
        self._last_decision_pub = 0.0

    # ------------------------------------------------------------------ #
    # 控制命令
    # ------------------------------------------------------------------ #
    async def start(self) -> dict[str, Any]:
        if self.status in ("running", "paused"):
            if self.status == "paused":
                return await self.resume()
            return {"status": self.status, "message": "引擎已在运行"}

        await self._cancel_tasks()
        self._stop = asyncio.Event()
        self._resume.set()
        self.portfolio.reset(self.config.initial_capital)
        self.portfolio.commission_rate = self.config.commission_rate
        self.runtimes.clear()
        self.decisions.clear()
        self.total_decisions = 0
        self.rejected_signals = 0
        self.last_error = None
        self.started_at = time.time()
        self.finished_at = 0.0
        self.status = "running"

        self._supervisor = asyncio.create_task(self._run(), name="jev-engine")
        self.bus.publish(
            "status",
            {
                "status": self.status,
                "mode": self.config.mode,
                "symbols": list(self.config.symbols),
                "message": f"引擎启动：{self.config.mode} 模式，{len(self.config.symbols)} 个标的",
            },
        )
        return {"status": self.status, "mode": self.config.mode, "symbols": list(self.config.symbols)}

    async def pause(self) -> dict[str, Any]:
        if self.status != "running":
            return {"status": self.status, "message": "引擎未在运行"}
        self._resume.clear()
        self.status = "paused"
        self.bus.publish("status", {"status": self.status, "message": "引擎已暂停"})
        return {"status": self.status}

    async def resume(self) -> dict[str, Any]:
        if self.status != "paused":
            return {"status": self.status, "message": "引擎未处于暂停状态"}
        self.status = "running"
        self._resume.set()
        self.bus.publish("status", {"status": self.status, "message": "引擎已恢复"})
        return {"status": self.status}

    async def reset(self) -> dict[str, Any]:
        await self._cancel_tasks()
        self.portfolio.reset(self.config.initial_capital)
        self.runtimes.clear()
        self.decisions.clear()
        self.total_decisions = 0
        self.rejected_signals = 0
        self.started_at = 0.0
        self.finished_at = 0.0
        self.last_error = None
        self.status = "idle"
        self.bus.publish("status", {"status": self.status, "message": "已重置账户与运行状态"})
        return {"status": self.status}

    async def flatten(self) -> dict[str, Any]:
        """一键平掉所有持仓（按当前买一/卖一）。"""
        closed = 0
        async with self._exec_lock:
            for symbol, rt in list(self.runtimes.items()):
                if rt.last_tick is None:
                    continue
                closed += 1 if self._force_close(symbol, rt, rt.last_tick, "手动一键平仓", "flatten") else 0
        return {"status": self.status, "closed": closed}

    async def shutdown(self) -> None:
        await self._cancel_tasks()
        await self.jev.aclose()

    async def _cancel_tasks(self) -> None:
        self._stop.set()
        self._resume.set()
        tasks = [t for t in ([self._supervisor] + self._tasks) if t is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()
        self._supervisor = None

    # ------------------------------------------------------------------ #
    # 配置热更新
    # ------------------------------------------------------------------ #
    RESTART_FIELDS = {"mode", "symbols", "initial_capital", "backtest_ticks", "replay_speed", "commission_rate"}

    async def apply_config(self, updates: dict[str, Any]) -> dict[str, Any]:
        changed = {k: v for k, v in updates.items() if getattr(self.config, k, None) != v}
        for key, value in changed.items():
            setattr(self.config, key, value)
        self.portfolio.commission_rate = self.config.commission_rate
        self.jev.model = self.config.jev_model

        needs_restart = bool(self.RESTART_FIELDS & set(changed))
        restarted = False
        if needs_restart and self.status in ("running", "paused"):
            await self._cancel_tasks()
            self.status = "idle"
            await self.start()
            restarted = True
        elif needs_restart:
            # 未在运行时直接按新配置重置账户，下次 start 从干净状态开始
            self.portfolio.reset(self.config.initial_capital)

        self.bus.publish(
            "status",
            {
                "status": self.status,
                "message": f"参数已更新: {', '.join(changed) or '无变化'}"
                + ("（已按新配置重启）" if restarted else ""),
                "changed": list(changed),
            },
        )
        return {"changed": list(changed), "restarted": restarted, "config": self.config.model_dump()}

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #
    async def _run(self) -> None:
        try:
            self._tasks = [
                asyncio.create_task(self._run_symbol(symbol), name=f"feed-{symbol}")
                for symbol in self.config.symbols
            ]
            results = await asyncio.gather(*self._tasks, return_exceptions=True)
            for res in results:
                if isinstance(res, Exception) and not isinstance(res, asyncio.CancelledError):
                    self.last_error = f"{type(res).__name__}: {res}"
                    log.exception("标的任务异常", exc_info=res)
        except asyncio.CancelledError:
            raise
        finally:
            if not self._stop.is_set():
                await self._finish()

    async def _finish(self) -> None:
        """回测行情耗尽：强平 + 汇总。"""
        async with self._exec_lock:
            for symbol, rt in self.runtimes.items():
                if rt.last_tick is not None:
                    self._force_close(symbol, rt, rt.last_tick, "回测结束强制平仓", "flatten")
        self.status = "finished" if self.status != "halted" else "halted"
        self.finished_at = time.time()
        stats = self.portfolio.stats()
        self.bus.publish(
            "status",
            {
                "status": self.status,
                "message": f"运行结束：成交 {stats['total_fills']} 笔，胜率 {stats['win_rate']}%，收益 {stats['total_return_pct']}%",
                "stats": stats,
            },
        )

    async def _run_symbol(self, symbol: str) -> None:
        instrument = resolve_instrument(symbol)
        rt = SymbolRuntime(symbol=symbol, instrument=instrument)
        self.runtimes[symbol] = rt
        feed = build_feed(
            symbol,
            self.config.mode,
            limit=self.config.backtest_ticks,
            replay_speed=self.config.replay_speed,
        )
        rt.source = getattr(feed, "source", "unknown")
        self.bus.publish(
            "feed",
            {
                "symbol": symbol,
                "source": rt.source,
                "market": instrument.market,
                "mode": self.config.mode,
                "message": f"{symbol} 行情源: {rt.source}",
            },
        )
        try:
            async for tick in feed.stream():
                if self._stop.is_set():
                    break
                if not self._resume.is_set():
                    await self._resume.wait()
                    if self._stop.is_set():
                        break
                await self._on_tick(rt, tick)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{symbol}: {type(exc).__name__}: {exc}"
            log.exception("行情流异常 %s", symbol)
            self.bus.publish("error", {"symbol": symbol, "message": self.last_error})
        finally:
            rt.finished = True

    # ------------------------------------------------------------------ #
    # 单 Tick 处理
    # ------------------------------------------------------------------ #
    async def _on_tick(self, rt: SymbolRuntime, tick: Tick) -> None:
        cfg = self.config
        rt.ticks += 1
        rt.last_tick = tick
        rt.last_snapshot = rt.indicators.update(tick.ts, tick.price, tick.bid_vol_1, tick.ask_vol_1)
        self.portfolio.mark(tick.symbol, tick.mid)
        self.portfolio.sample_equity(tick.ts, min_gap=1.0 if cfg.mode == "paper" else 5.0)

        async with self._exec_lock:
            # 1) 止盈止损优先于模型信号
            if self._check_exit_rules(rt, tick):
                return
            # 2) Maker 挂单撮合
            if rt.pending is not None and self._match_pending(rt, tick):
                return
            # 3) 日内最大亏损熔断
            if self._check_circuit_breaker(tick):
                return

        # 4) 决策节流
        interval = cfg.decision_interval_ms / 1000.0
        if interval > 0 and (tick.ts - rt.last_decision_ts) < interval:
            return
        rt.last_decision_ts = tick.ts

        position = self.portfolio.position(tick.symbol)
        state = build_state(
            tick,
            rt.last_snapshot,
            position,
            rt.instrument,
            equity=self.portfolio.equity,
        )

        if cfg.jev_enabled:
            rt.jev_calls += 1
            decision = await self.jev.choose(state, ACTIONS)
        else:
            decision = self.jev.mock.decide(state, list(ACTIONS))
            decision.error = "控制面板已关闭 Jev 调用，使用本地动量模型"

        self.total_decisions += 1
        async with self._exec_lock:
            outcome = await self._act_on_decision(rt, tick, decision, state)
        self._publish_decision(rt, tick, decision, state, outcome)

    # ------------------------------------------------------------------ #
    def _check_exit_rules(self, rt: SymbolRuntime, tick: Tick) -> bool:
        """止盈 / 止损，命中即平仓。"""
        cfg = self.config
        pos = self.portfolio.position(tick.symbol)
        if not pos.is_open:
            return False
        exit_price = tick.bid_price_1 if pos.side == "long" else tick.ask_price_1
        pnl_pct = pos.unrealized_pnl_pct(exit_price)
        pos.peak_pnl_pct = max(pos.peak_pnl_pct, pnl_pct)

        if cfg.take_profit_pct > 0 and pnl_pct >= cfg.take_profit_pct:
            trigger = (
                f"Take Profit: {pnl_pct:+.3f}% ≥ {cfg.take_profit_pct:.2f}% "
                f"(持仓 {pos.side}, 均价 {pos.avg_price:g})"
            )
            self._force_close(tick.symbol, rt, tick, trigger, "take_profit")
            return True
        if cfg.stop_loss_pct > 0 and pnl_pct <= -cfg.stop_loss_pct:
            trigger = (
                f"Stop Loss: {pnl_pct:+.3f}% ≤ -{cfg.stop_loss_pct:.2f}% "
                f"(持仓 {pos.side}, 均价 {pos.avg_price:g})"
            )
            self._force_close(tick.symbol, rt, tick, trigger, "stop_loss")
            return True
        return False

    def _check_circuit_breaker(self, tick: Tick) -> bool:
        cfg = self.config
        if cfg.max_daily_loss_pct <= 0 or self.status == "halted":
            return False
        if self.portfolio.total_return_pct > -cfg.max_daily_loss_pct:
            return False
        trigger = (
            f"风控熔断: 回撤 {self.portfolio.total_return_pct:.2f}% "
            f"触及上限 -{cfg.max_daily_loss_pct:.2f}%"
        )
        # 熔断时把所有标的的持仓一次性平掉，而不只是当前标的
        for other_symbol, other_rt in self.runtimes.items():
            other_tick = tick if other_symbol == tick.symbol else other_rt.last_tick
            if other_tick is not None:
                self._force_close(other_symbol, other_rt, other_tick, trigger, "risk_halt")
        self.status = "halted"
        self.bus.publish(
            "status",
            {
                "status": self.status,
                "message": f"触发最大亏损熔断（{self.portfolio.total_return_pct:.2f}%），已全部平仓并停止开仓",
            },
        )
        return True

    # ------------------------------------------------------------------ #
    async def _act_on_decision(
        self,
        rt: SymbolRuntime,
        tick: Tick,
        decision: JevDecision,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """按 Jev 概率 + 控制面板阈值决定是否下单。返回本次决策的处理结果。"""
        cfg = self.config
        pos = self.portfolio.position(tick.symbol)
        action = decision.top_action
        prob = decision.probabilities.get(action, 0.0)
        indicators = rt.last_snapshot.as_dict()

        def result(executed: bool, reason: str, kind: str = "hold") -> dict[str, Any]:
            if not executed and kind in ("open", "close"):
                self.rejected_signals += 1
            return {"executed": executed, "reason": reason, "kind": kind}

        if action == "hold":
            return result(False, f"模型倾向 hold（{prob:.1%}）", "hold")

        if decision.confidence < cfg.min_confidence:
            return result(
                False,
                f"置信度 {decision.confidence:.2f} < 阈值 {cfg.min_confidence:.2f}",
                "open" if action.startswith("open") else "close",
            )

        # ---- 平仓 ---------------------------------------------------- #
        if action == "close_position":
            if not pos.is_open:
                return result(False, "当前无持仓，忽略 close_position", "hold")
            if prob < cfg.close_threshold:
                return result(False, f"平仓概率 {prob:.1%} < 阈值 {cfg.close_threshold:.1%}", "close")
            trigger = self._trigger_text(decision, action, prob, tick, indicators)
            self._force_close(tick.symbol, rt, tick, trigger, "jev_signal", decision=decision)
            return result(True, "已按 Jev 信号平仓", "close")

        # ---- 开仓 ---------------------------------------------------- #
        side = "long" if action == "open_long" else "short"
        if pos.is_open:
            if pos.side == side:
                return result(False, f"已持有 {side} 仓位，不加仓", "hold")
            if prob < cfg.close_threshold:
                return result(False, f"反向信号 {prob:.1%} 不足以反手（阈值 {cfg.close_threshold:.1%}）", "close")
            trigger = self._trigger_text(decision, action, prob, tick, indicators) + " | 反向信号先平仓"
            self._force_close(tick.symbol, rt, tick, trigger, "jev_signal", decision=decision)
            pos = self.portfolio.position(tick.symbol)

        if self.status == "halted":
            return result(False, "风控熔断中，禁止开仓", "open")
        if prob < cfg.buy_threshold:
            return result(False, f"开仓概率 {prob:.1%} < 阈值 {cfg.buy_threshold:.1%}", "open")
        if cfg.max_spread > 0 and tick.spread > cfg.max_spread:
            return result(False, f"价差 {tick.spread:g} > 上限 {cfg.max_spread:g}", "open")
        if cfg.cooldown_sec > 0 and (tick.ts - rt.last_open_ts) < cfg.cooldown_sec:
            wait = cfg.cooldown_sec - (tick.ts - rt.last_open_ts)
            return result(False, f"防刷冷却中，还需 {wait:.1f}s", "open")

        while rt.trade_times and tick.ts - rt.trade_times[0] > 60.0:
            rt.trade_times.popleft()
        if len(rt.trade_times) >= cfg.max_trades_per_min:
            return result(False, f"每分钟交易上限 {cfg.max_trades_per_min} 已用尽", "open")

        qty = cfg.order_qty
        entry = self._entry_price(tick, side)
        affordable, why = self.portfolio.can_afford(tick.symbol, qty, entry)
        if not affordable:
            return result(False, why, "open")

        trigger = self._trigger_text(decision, action, prob, tick, indicators)

        if cfg.order_type == "maker":
            limit = tick.bid_price_1 if side == "long" else tick.ask_price_1
            rt.pending = PendingOrder(
                side=side,
                limit_price=limit,
                qty=qty,
                ticks_left=cfg.maker_timeout_ticks,
                decision=decision,
                trigger=trigger + f" | Maker 挂单 @{limit:g}",
                created_ts=tick.ts,
            )
            self.bus.publish(
                "order",
                {
                    "symbol": tick.symbol,
                    "side": side,
                    "order_type": "maker",
                    "limit_price": limit,
                    "qty": qty,
                    "trigger": rt.pending.trigger,
                },
            )
            return result(True, f"Maker 挂单 @{limit:g}", "open")

        trade = self.portfolio.open_position(
            tick.symbol,
            side,
            qty,
            entry,
            tick.ts,
            trigger=trigger,
            reason_code="jev_signal",
            order_type="ioc",
            spread=tick.spread,
            jev_action=action,
            jev_prob=prob,
            jev_confidence=decision.confidence,
            jev_source=decision.source,
            probabilities=decision.probabilities,
            indicators=indicators,
        )
        rt.last_open_ts = tick.ts
        rt.trade_times.append(tick.ts)
        self.bus.publish("trade", {"trade": trade.to_dict(), "stats": self.portfolio.stats()})
        return result(True, "已按 Jev 信号开仓", "open")

    # ------------------------------------------------------------------ #
    def _match_pending(self, rt: SymbolRuntime, tick: Tick) -> bool:
        """Maker 挂单撮合：对手价穿越挂单价即成交，超时撤单。"""
        order = rt.pending
        if order is None:
            return False
        filled = (
            tick.ask_price_1 <= order.limit_price
            if order.side == "long"
            else tick.bid_price_1 >= order.limit_price
        )
        if filled:
            rt.pending = None
            ok, why = self.portfolio.can_afford(tick.symbol, order.qty, order.limit_price)
            if not ok:
                self.bus.publish("order", {"symbol": tick.symbol, "status": "rejected", "message": why})
                return False
            trade = self.portfolio.open_position(
                tick.symbol,
                order.side,
                order.qty,
                order.limit_price,
                tick.ts,
                trigger=order.trigger + f" | 挂单成交（等待 {tick.ts - order.created_ts:.1f}s）",
                reason_code="jev_signal",
                order_type="maker",
                spread=tick.spread,
                jev_action=order.decision.top_action,
                jev_prob=order.decision.top_prob,
                jev_confidence=order.decision.confidence,
                jev_source=order.decision.source,
                probabilities=order.decision.probabilities,
                indicators=rt.last_snapshot.as_dict(),
            )
            rt.last_open_ts = tick.ts
            rt.trade_times.append(tick.ts)
            self.bus.publish("trade", {"trade": trade.to_dict(), "stats": self.portfolio.stats()})
            return True

        order.ticks_left -= 1
        if order.ticks_left <= 0:
            rt.pending = None
            self.bus.publish(
                "order",
                {
                    "symbol": tick.symbol,
                    "status": "cancelled",
                    "message": f"Maker 挂单 @{order.limit_price:g} 超时未成交，已撤单",
                },
            )
        return False

    # ------------------------------------------------------------------ #
    def _entry_price(self, tick: Tick, side: str) -> float:
        """IOC 吃单价格（含滑点）。"""
        inst = resolve_instrument(tick.symbol)
        slip = self.config.slippage_ticks * inst.tick_size
        return inst.round_price(tick.ask_price_1 + slip if side == "long" else tick.bid_price_1 - slip)

    def _exit_price(self, tick: Tick, side: str) -> float:
        inst = resolve_instrument(tick.symbol)
        slip = self.config.slippage_ticks * inst.tick_size
        return inst.round_price(tick.bid_price_1 - slip if side == "long" else tick.ask_price_1 + slip)

    def _force_close(
        self,
        symbol: str,
        rt: SymbolRuntime,
        tick: Tick,
        trigger: str,
        reason_code: str,
        decision: JevDecision | None = None,
    ) -> bool:
        pos = self.portfolio.position(symbol)
        if not pos.is_open:
            return False
        price = self._exit_price(tick, pos.side)
        trade = self.portfolio.close_position(
            symbol,
            price,
            tick.ts,
            trigger=trigger,
            reason_code=reason_code,
            order_type="ioc",
            spread=tick.spread,
            jev_action=decision.top_action if decision else "",
            jev_prob=decision.top_prob if decision else 0.0,
            jev_confidence=decision.confidence if decision else 0.0,
            jev_source=decision.source if decision else "risk",
            probabilities=decision.probabilities if decision else {},
            indicators=rt.last_snapshot.as_dict(),
        )
        if trade is None:
            return False
        rt.trade_times.append(tick.ts)
        self.bus.publish("trade", {"trade": trade.to_dict(), "stats": self.portfolio.stats()})
        return True

    @staticmethod
    def _trigger_text(
        decision: JevDecision,
        action: str,
        prob: float,
        tick: Tick,
        indicators: dict[str, float],
    ) -> str:
        return (
            f"JEV Choice: {action}, Prob: {prob * 100:.1f}%, "
            f"Confidence: {decision.confidence:.2f}, Spread: {tick.spread:g}, "
            f"RSI: {indicators.get('rsi_14', 0):.1f}, MACD: {indicators.get('macd_histogram', 0):+.3f}, "
            f"Src: {decision.source}"
        )

    # ------------------------------------------------------------------ #
    def _publish_decision(
        self,
        rt: SymbolRuntime,
        tick: Tick,
        decision: JevDecision,
        state: dict[str, Any],
        outcome: dict[str, Any],
    ) -> None:
        payload = {
            "symbol": tick.symbol,
            "tick_ts": tick.ts,
            "mode": self.config.mode,
            "state": state,
            "probabilities": decision.probabilities,
            "top_action": decision.top_action,
            "top_prob": round(decision.top_prob, 6),
            "confidence": decision.confidence,
            "source": decision.source,
            "model": decision.model,
            "latency_ms": decision.latency_ms,
            "reasoning": decision.reasoning,
            "error": decision.error,
            "thresholds": {
                "buy_threshold": self.config.buy_threshold,
                "close_threshold": self.config.close_threshold,
                "min_confidence": self.config.min_confidence,
            },
            "outcome": outcome,
            "equity": round(self.portfolio.equity, 2),
        }
        self.decisions.append({"ts": time.time(), **payload})

        # 回测全速推演时限流推送；产生成交的决策一定推送
        now = time.perf_counter()
        must_push = outcome.get("executed", False)
        if not must_push and (now - self._last_decision_pub) < (1.0 / MAX_DECISION_EVENTS_PER_SEC):
            return
        self._last_decision_pub = now
        self.bus.publish("decision", payload)

    # ------------------------------------------------------------------ #
    # 快照
    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict[str, Any]:
        stats = self.portfolio.stats()
        return {
            "status": self.status,
            "mode": self.config.mode,
            "symbols": list(self.config.symbols),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_sec": round((self.finished_at or time.time()) - self.started_at, 2) if self.started_at else 0.0,
            "stats": stats,
            "positions": self.portfolio.open_positions(),
            "equity_curve": [{"ts": ts, "equity": eq} for ts, eq in self.portfolio.equity_curve],
            "jev": {
                "live": self.jev.live,
                "model": self.jev.model,
                "calls": self.jev.calls,
                "decisions": self.total_decisions,
                "failures": self.jev.failures,
                "last_error": self.jev.last_error,
                "enabled": self.config.jev_enabled,
                "mode": "live" if (self.jev.live and self.config.jev_enabled) else "mock",
            },
            "engine": {
                "total_decisions": self.total_decisions,
                "rejected_signals": self.rejected_signals,
                "subscribers": self.bus.subscriber_count,
                "dropped_events": self.bus.dropped,
                "last_error": self.last_error,
            },
            "feeds": [
                {
                    "symbol": rt.symbol,
                    "source": rt.source,
                    "market": rt.instrument.market,
                    "ticks": rt.ticks,
                    "jev_calls": rt.jev_calls,
                    "finished": rt.finished,
                    "last_price": rt.last_tick.mid if rt.last_tick else None,
                    "spread": rt.last_tick.spread if rt.last_tick else None,
                    "indicators": rt.last_snapshot.as_dict(),
                    "pending_order": (
                        {
                            "side": rt.pending.side,
                            "limit_price": rt.pending.limit_price,
                            "qty": rt.pending.qty,
                            "ticks_left": rt.pending.ticks_left,
                        }
                        if rt.pending
                        else None
                    ),
                }
                for rt in self.runtimes.values()
            ],
            "config": self.config.model_dump(),
        }
