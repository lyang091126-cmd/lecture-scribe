from app.indicators import EMA, MACD, RSI, IndicatorEngine, RollingWindow


def test_rsi_bounds_and_trend():
    rsi = RSI(14)
    for i in range(60):
        value = rsi.update(100 + i)  # 单边上涨
    assert value > 90
    rsi_down = RSI(14)
    for i in range(60):
        value = rsi_down.update(100 - i * 0.5)
    assert value < 10


def test_rsi_neutral_on_first_sample():
    assert RSI().update(100.0) == 50.0


def test_ema_seeds_with_first_value():
    ema = EMA(10)
    assert ema.update(5.0) == 5.0
    assert 5.0 < ema.update(10.0) < 10.0


def test_macd_histogram_sign_follows_trend():
    macd = MACD()
    for i in range(80):
        _, _, hist = macd.update(100 + i)
    assert hist > 0


def test_rolling_window_evicts_old_samples():
    win = RollingWindow(60.0)
    for i in range(200):
        win.update(float(i), 100.0 + i)
    assert win.first is not None
    # 只保留最近 60 秒
    assert win.first >= 100.0 + 199 - 61
    assert win.change_pct(300.0) > 0


def test_indicator_engine_snapshot_shape():
    eng = IndicatorEngine()
    snap = None
    for i in range(50):
        snap = eng.update(float(i), 100.0 + i * 0.1, bid_vol=200, ask_vol=100)
    assert snap is not None
    data = snap.as_dict()
    assert set(data) >= {"rsi_14", "macd_histogram", "price_change_5m_pct", "imbalance", "samples"}
    assert data["samples"] == 50
    assert data["imbalance"] > 0  # 买盘量更大
    eng.reset()
    assert eng.update(0.0, 100.0).samples == 1
