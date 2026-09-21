import app.datafeed as datafeed
from app.datafeed import CsvFeed, SyntheticFeed, build_feed


async def collect(feed, limit=50):
    out = []
    async for tick in feed.stream():
        out.append(tick)
        if len(out) >= limit:
            break
    return out


async def test_synthetic_feed_is_deterministic_and_well_formed():
    a = await collect(SyntheticFeed("rb2610.SHFE", limit=60, seed=42))
    b = await collect(SyntheticFeed("rb2610.SHFE", limit=60, seed=42))
    assert [t.last_price for t in a] == [t.last_price for t in b]
    assert len(a) == 50
    for tick in a:
        assert tick.ask_price_1 > tick.bid_price_1
        assert tick.spread > 0
        assert tick.bid_vol_1 > 0 and tick.ask_vol_1 > 0
        assert tick.bid_price_1 < tick.mid < tick.ask_price_1


async def test_synthetic_feed_respects_limit():
    ticks = [t async for t in SyntheticFeed("AAPL", limit=25).stream()]
    assert len(ticks) == 25
    assert all(t.symbol == "AAPL" for t in ticks)


async def test_csv_feed_reads_orderbook_columns(tmp_path):
    path = tmp_path / "rb2610.SHFE.csv"
    path.write_text(
        "timestamp,bid,ask,bid_vol,ask_vol,last,volume\n"
        "2026-09-21 10:30:00,3449,3450,85,120,3450,205\n"
        "2026-09-21 10:30:01,3450,3451,90,110,3451,200\n",
        encoding="utf-8",
    )
    ticks = [t async for t in CsvFeed("rb2610.SHFE", path).stream()]
    assert len(ticks) == 2
    assert ticks[0].bid_price_1 == 3449 and ticks[0].ask_price_1 == 3450
    assert ticks[0].bid_vol_1 == 85 and ticks[0].ask_vol_1 == 120
    assert ticks[1].ts > ticks[0].ts


async def test_csv_feed_synthesizes_orderbook_from_close_only(tmp_path):
    path = tmp_path / "AAPL.csv"
    path.write_text("date,close\n2026-09-18,230.5\n2026-09-19,232.0\n", encoding="utf-8")
    ticks = [t async for t in CsvFeed("AAPL", path).stream()]
    assert len(ticks) == 2
    assert ticks[0].ask_price_1 > ticks[0].bid_price_1
    assert abs(ticks[0].mid - 230.5) < 0.02


def test_build_feed_prefers_csv_for_backtest(tmp_path, monkeypatch):
    monkeypatch.setattr(datafeed, "DATA_DIR", tmp_path)
    (tmp_path / "m2610.DCE.csv").write_text("ts,close\n1,3000\n", encoding="utf-8")
    feed = build_feed("m2610.DCE", "backtest", limit=10)
    assert isinstance(feed, CsvFeed)
    assert feed.source.startswith("csv:")
    # 无 CSV 时回落到可复现的合成历史行情
    assert isinstance(build_feed("AAPL", "backtest", limit=10), SyntheticFeed)


def test_build_feed_paper_mode_without_credentials_is_simulated(monkeypatch):
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    monkeypatch.delenv("TQ_USER", raising=False)
    feed = build_feed("AAPL", "paper")
    assert isinstance(feed, SyntheticFeed)
    assert feed.source == "synthetic"
    assert feed.interval > 0  # 实时推送节奏
    assert feed.limit == 0  # 不限长度
