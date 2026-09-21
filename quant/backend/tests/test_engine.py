import asyncio

import pytest

from app.engine import StrategyEngine, SymbolRuntime
from app.events import EventBus
from app.instruments import resolve_instrument
from app.jev_client import JevClient
from app.models import StrategyConfig, Tick


def make_engine(**overrides) -> StrategyEngine:
    params = {
        "mode": "backtest",
        "symbols": ["rb2610.SHFE"],
        "backtest_ticks": 800,
        "decision_interval_ms": 1_000,
    }
    params.update(overrides)
    return StrategyEngine(StrategyConfig(**params), EventBus(), JevClient(api_key=""))


async def run_to_completion(engine: StrategyEngine, timeout: float = 20.0) -> None:
    await engine.start()
    deadline = asyncio.get_running_loop().time() + timeout
    while engine.status not in ("finished", "halted", "error"):
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"回测超时，状态停留在 {engine.status}")
        await asyncio.sleep(0.02)


async def test_backtest_runs_and_flattens():
    engine = make_engine()
    try:
        await run_to_completion(engine)
        snap = engine.snapshot()
        assert snap["status"] == "finished"
        assert snap["stats"]["total_fills"] > 0
        assert snap["positions"] == []  # 结束时强制平仓
        assert snap["equity_curve"]
        assert engine.total_decisions > 0
        assert snap["feeds"][0]["ticks"] == 800
        assert snap["jev"]["mode"] == "mock"
    finally:
        await engine.shutdown()


async def test_trade_logs_carry_jev_trigger_details():
    engine = make_engine()
    try:
        await run_to_completion(engine)
        signal_trades = [t for t in engine.portfolio.recent_trades(500) if t["reason_code"] == "jev_signal"]
        assert signal_trades, "至少应有一笔由 Jev 信号触发的成交"
        trade = signal_trades[0]
        assert "JEV Choice:" in trade["trigger"]
        assert "Prob:" in trade["trigger"] and "Confidence:" in trade["trigger"]
        assert trade["jev_prob"] >= engine.config.buy_threshold or trade["action"].startswith("close")
        assert set(trade["probabilities"]) == {"open_long", "open_short", "close_position", "hold"}
        assert trade["indicators"]["samples"] > 0
    finally:
        await engine.shutdown()


async def test_high_threshold_blocks_entries():
    engine = make_engine(buy_threshold=0.999, close_threshold=0.999, min_confidence=0.99)
    try:
        await run_to_completion(engine)
        opens = [t for t in engine.portfolio.recent_trades(500) if t["action"].startswith("open")]
        assert opens == []
        assert engine.rejected_signals > 0
    finally:
        await engine.shutdown()


async def test_take_profit_and_stop_loss_rules():
    engine = make_engine(take_profit_pct=1.0, stop_loss_pct=0.5)
    symbol = "rb2610.SHFE"
    rt = SymbolRuntime(symbol=symbol, instrument=resolve_instrument(symbol))
    engine.runtimes[symbol] = rt
    try:
        engine.portfolio.open_position(
            symbol, "long", 1, 3_000.0, 0.0,
            trigger="t", reason_code="jev_signal", order_type="ioc", spread=1.0,
        )
        # 上涨 1.2% -> 止盈
        tick = Tick(symbol=symbol, ts=5.0, bid_price_1=3_036.0, ask_price_1=3_037.0)
        assert engine._check_exit_rules(rt, tick) is True
        last = engine.portfolio.recent_trades(1)[0]
        assert last["reason_code"] == "take_profit"
        assert last["realized_pnl"] > 0

        engine.portfolio.open_position(
            symbol, "long", 1, 3_000.0, 10.0,
            trigger="t", reason_code="jev_signal", order_type="ioc", spread=1.0,
        )
        # 下跌 0.7% -> 止损
        tick = Tick(symbol=symbol, ts=15.0, bid_price_1=2_979.0, ask_price_1=2_980.0)
        assert engine._check_exit_rules(rt, tick) is True
        last = engine.portfolio.recent_trades(1)[0]
        assert last["reason_code"] == "stop_loss"
        assert last["realized_pnl"] < 0
        assert "Stop Loss" in last["trigger"]
    finally:
        await engine.shutdown()


async def test_cooldown_blocks_rapid_reentry():
    engine = make_engine(cooldown_sec=30.0, buy_threshold=0.0, min_confidence=0.0)
    symbol = "rb2610.SHFE"
    rt = SymbolRuntime(symbol=symbol, instrument=resolve_instrument(symbol))
    rt.last_open_ts = 100.0
    engine.runtimes[symbol] = rt
    tick = Tick(symbol=symbol, ts=110.0, bid_price_1=3_000.0, ask_price_1=3_001.0, bid_vol_1=500, ask_vol_1=100)
    decision = engine.jev.mock.decide(
        {
            "indicators": {"rsi_14": 75, "macd_histogram": 3.0, "price_change_1m_pct": 1.0, "ma_gap_pct": 0.8, "samples": 100},
            "orderbook": {"spread": 1},
            "current_position": {"side": "none", "unrealized_pnl_pct": 0.0},
        },
        ["open_long", "open_short", "close_position", "hold"],
    )
    outcome = await engine._act_on_decision(rt, tick, decision, {})
    assert outcome["executed"] is False
    assert "冷却" in outcome["reason"]
    await engine.shutdown()


async def test_spread_guard_blocks_entry():
    engine = make_engine(max_spread=1.0, buy_threshold=0.0, min_confidence=0.0)
    symbol = "rb2610.SHFE"
    rt = SymbolRuntime(symbol=symbol, instrument=resolve_instrument(symbol))
    engine.runtimes[symbol] = rt
    tick = Tick(symbol=symbol, ts=1.0, bid_price_1=3_000.0, ask_price_1=3_010.0)
    decision = engine.jev.mock.decide(
        {
            "indicators": {"rsi_14": 80, "macd_histogram": 4.0, "price_change_1m_pct": 1.5, "ma_gap_pct": 1.0, "samples": 100},
            "orderbook": {"spread": 10},
            "current_position": {"side": "none", "unrealized_pnl_pct": 0.0},
        },
        ["open_long", "open_short", "close_position", "hold"],
    )
    outcome = await engine._act_on_decision(rt, tick, decision, {})
    assert outcome["executed"] is False
    assert "价差" in outcome["reason"]
    await engine.shutdown()


async def test_pause_resume_and_reset():
    engine = make_engine(mode="paper", backtest_ticks=100)
    try:
        await engine.start()
        await asyncio.sleep(0.3)
        assert engine.status == "running"
        assert (await engine.pause())["status"] == "paused"
        ticks = engine.runtimes["rb2610.SHFE"].ticks
        await asyncio.sleep(0.8)
        assert engine.runtimes["rb2610.SHFE"].ticks - ticks <= 1  # 暂停后基本不再推进
        assert (await engine.resume())["status"] == "running"
        await engine.reset()
        assert engine.status == "idle"
        assert engine.portfolio.equity == engine.config.initial_capital
        assert engine.runtimes == {}
    finally:
        await engine.shutdown()


async def test_flatten_closes_open_positions():
    engine = make_engine()
    symbol = "rb2610.SHFE"
    rt = SymbolRuntime(symbol=symbol, instrument=resolve_instrument(symbol))
    rt.last_tick = Tick(symbol=symbol, ts=1.0, bid_price_1=3_100.0, ask_price_1=3_101.0)
    engine.runtimes[symbol] = rt
    engine.portfolio.open_position(
        symbol, "long", 1, 3_000.0, 0.0,
        trigger="t", reason_code="jev_signal", order_type="ioc", spread=1.0,
    )
    result = await engine.flatten()
    assert result["closed"] == 1
    assert engine.portfolio.open_positions() == []
    assert engine.portfolio.recent_trades(1)[0]["reason_code"] == "flatten"
    await engine.shutdown()


async def test_apply_config_hot_update_without_restart():
    engine = make_engine()
    try:
        result = await engine.apply_config({"buy_threshold": 0.9, "take_profit_pct": 2.0})
        assert result["restarted"] is False
        assert set(result["changed"]) == {"buy_threshold", "take_profit_pct"}
        assert engine.config.buy_threshold == 0.9
    finally:
        await engine.shutdown()


async def test_apply_config_restarts_when_symbols_change():
    engine = make_engine(mode="paper")
    try:
        await engine.start()
        await asyncio.sleep(0.1)
        result = await engine.apply_config({"symbols": ["AAPL"]})
        assert result["restarted"] is True
        assert engine.config.symbols == ["AAPL"]
        await asyncio.sleep(0.1)
        assert set(engine.runtimes) <= {"AAPL"}
    finally:
        await engine.shutdown()


async def test_events_published_for_decisions_and_trades():
    engine = make_engine()
    seen: list[str] = []

    async def collect() -> None:
        async for frame in engine.bus.stream(replay=0):
            if frame.startswith(":"):
                continue
            seen.append(frame.split("event: ", 1)[1].split("\n", 1)[0])
            if len(seen) > 40:
                return

    task = asyncio.create_task(collect())
    try:
        await run_to_completion(engine)
        await asyncio.sleep(0.05)
        assert "decision" in seen
        assert "trade" in seen
    finally:
        task.cancel()
        await engine.shutdown()
