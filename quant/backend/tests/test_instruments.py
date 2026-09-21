from app.instruments import resolve_instrument


def test_resolve_cn_future():
    inst = resolve_instrument("rb2610.SHFE")
    assert inst.market == "cn_future"
    assert inst.multiplier == 10.0
    assert inst.tick_size == 1.0
    assert inst.margin_rate < 1.0
    assert inst.currency == "CNY"


def test_resolve_us_equity():
    inst = resolve_instrument("AAPL")
    assert inst.market == "us_equity"
    assert inst.multiplier == 1.0
    assert inst.margin_rate == 1.0
    assert inst.currency == "USD"


def test_notional_and_margin():
    fut = resolve_instrument("m2610.DCE")
    assert fut.notional(3000.0, 2) == 3000.0 * 2 * fut.multiplier
    assert fut.margin(3000.0, 2) < fut.notional(3000.0, 2)
    eq = resolve_instrument("TSLA")
    assert eq.margin(200.0, 10) == 2000.0


def test_round_price_to_tick():
    inst = resolve_instrument("i2610.DCE")  # tick 0.5
    assert inst.round_price(780.26) == 780.5
    assert resolve_instrument("AAPL").round_price(232.1234) == 232.12
