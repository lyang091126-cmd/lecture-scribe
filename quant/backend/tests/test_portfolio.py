import pytest

from app.portfolio import Portfolio


def _open(pf, symbol, side, qty, price, ts=0.0):
    return pf.open_position(
        symbol, side, qty, price, ts,
        trigger="t", reason_code="jev_signal", order_type="ioc", spread=1.0,
    )


def _close(pf, symbol, price, ts=1.0):
    return pf.close_position(
        symbol, price, ts,
        trigger="t", reason_code="jev_signal", order_type="ioc", spread=1.0,
    )


def test_equity_long_profit_us_equity():
    pf = Portfolio(100_000.0, commission_rate=0.0)
    _open(pf, "AAPL", "long", 100, 200.0)
    assert pf.cash == pytest.approx(80_000.0)  # 美股全额占用
    pf.mark("AAPL", 210.0)
    assert pf.unrealized_pnl == pytest.approx(1_000.0)
    assert pf.equity == pytest.approx(101_000.0)
    trade = _close(pf, "AAPL", 210.0)
    assert trade is not None
    assert trade.realized_pnl == pytest.approx(1_000.0)
    assert pf.cash == pytest.approx(101_000.0)
    assert pf.position("AAPL").is_open is False


def test_short_profit_uses_multiplier_for_futures():
    pf = Portfolio(1_000_000.0, commission_rate=0.0)
    _open(pf, "rb2610.SHFE", "short", 2, 3_500.0)
    inst = pf.instrument("rb2610.SHFE")
    assert pf.cash == pytest.approx(1_000_000.0 - 3_500.0 * 2 * inst.multiplier * inst.margin_rate)
    trade = _close(pf, "rb2610.SHFE", 3_450.0)
    # 空头下跌 50 点 * 2 手 * 10 乘数 = 1000
    assert trade.realized_pnl == pytest.approx(1_000.0)
    assert pf.equity == pytest.approx(1_001_000.0)


def test_commission_reduces_pnl_on_both_legs():
    pf = Portfolio(100_000.0, commission_rate=0.001)
    _open(pf, "AAPL", "long", 10, 100.0)
    trade = _close(pf, "AAPL", 100.0)
    # 开平各 1 元手续费
    assert trade.realized_pnl == pytest.approx(-2.0)
    assert pf.total_commission == pytest.approx(2.0)


def test_can_afford_rejects_oversized_order():
    pf = Portfolio(1_000.0, commission_rate=0.0)
    ok, why = pf.can_afford("AAPL", 100, 200.0)
    assert ok is False and "资金不足" in why
    assert pf.can_afford("AAPL", 4, 200.0)[0] is True


def test_close_without_position_returns_none():
    pf = Portfolio(10_000.0)
    assert _close(pf, "AAPL", 100.0) is None


def test_stats_win_rate_and_drawdown():
    pf = Portfolio(100_000.0, commission_rate=0.0)
    _open(pf, "AAPL", "long", 10, 100.0, ts=0)
    _close(pf, "AAPL", 110.0, ts=10)  # +100
    _open(pf, "AAPL", "long", 10, 110.0, ts=20)
    _close(pf, "AAPL", 100.0, ts=30)  # -100
    _open(pf, "AAPL", "long", 10, 100.0, ts=40)
    _close(pf, "AAPL", 130.0, ts=50)  # +300

    stats = pf.stats()
    assert stats["closed_trades"] == 3
    assert stats["win_trades"] == 2
    assert stats["win_rate"] == pytest.approx(66.67, abs=0.01)
    assert stats["realized_pnl"] == pytest.approx(300.0)
    assert stats["total_return_pct"] == pytest.approx(0.3, abs=1e-6)
    assert stats["profit_factor"] == pytest.approx(4.0)
    assert stats["max_drawdown_pct"] > 0  # 中间那笔亏损形成回撤
    assert len(pf.equity_curve) >= 6


def test_recent_trades_is_newest_first_and_filterable():
    pf = Portfolio(100_000.0, commission_rate=0.0)
    _open(pf, "AAPL", "long", 1, 100.0, ts=0)
    _close(pf, "AAPL", 101.0, ts=1)
    _open(pf, "TSLA", "short", 1, 200.0, ts=2)
    rows = pf.recent_trades(limit=10)
    assert rows[0]["symbol"] == "TSLA"
    assert [r["id"] for r in rows] == [3, 2, 1]
    assert all(r["symbol"] == "AAPL" for r in pf.recent_trades(limit=10, symbol="AAPL"))


def test_reset_restores_initial_state():
    pf = Portfolio(50_000.0)
    _open(pf, "AAPL", "long", 10, 100.0)
    pf.reset(80_000.0)
    assert pf.cash == 80_000.0
    assert pf.open_positions() == []
    assert pf.stats()["closed_trades"] == 0
