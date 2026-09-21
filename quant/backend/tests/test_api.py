import time

import pytest
from fastapi.testclient import TestClient

from app.server import app


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c
        c.post("/api/control/action", json={"action": "reset"})


def test_health_and_default_config(client):
    health = client.get("/api/health").json()
    assert health["ok"] is True
    body = client.get("/api/config").json()
    assert body["config"]["mode"] in ("backtest", "paper")
    assert any(p["market"] == "cn_future" for p in body["presets"])
    assert any(p["market"] == "us_equity" for p in body["presets"])


def test_snapshot_shape(client):
    snap = client.get("/api/snapshot").json()
    for key in ("status", "stats", "positions", "equity_curve", "jev", "engine", "config", "feeds"):
        assert key in snap
    assert snap["stats"]["win_rate"] == 0.0
    assert snap["jev"]["mode"] in ("live", "mock")


def test_instruments_endpoint(client):
    data = client.get("/api/instruments", params={"symbols": "AAPL,rb2610.SHFE"}).json()["instruments"]
    assert [i["market"] for i in data] == ["us_equity", "cn_future"]
    assert data[1]["multiplier"] == 10.0


def test_config_update_validates_and_applies(client):
    resp = client.post("/api/control/config", json={"buy_threshold": 0.8, "stop_loss_pct": 1.2})
    assert resp.status_code == 200
    assert resp.json()["config"]["buy_threshold"] == 0.8

    bad = client.post("/api/control/config", json={"buy_threshold": 1.7})
    assert bad.status_code == 422

    unknown = client.post("/api/control/config", json={"not_a_field": 1})
    assert unknown.status_code == 422

    empty_symbols = client.post("/api/control/config", json={"symbols": []})
    assert empty_symbols.status_code == 422


def test_backtest_run_produces_trade_logs(client):
    client.post(
        "/api/control/config",
        json={"mode": "backtest", "symbols": ["rb2610.SHFE"], "backtest_ticks": 600, "initial_capital": 500_000},
    )
    assert client.post("/api/control/action", json={"action": "start"}).status_code == 200

    deadline = time.time() + 30
    while time.time() < deadline:
        snap = client.get("/api/snapshot").json()
        if snap["status"] in ("finished", "halted"):
            break
        time.sleep(0.1)
    else:  # pragma: no cover
        pytest.fail("回测未在 30 秒内结束")

    snap = client.get("/api/snapshot").json()
    assert snap["stats"]["initial_capital"] == 500_000
    assert snap["stats"]["total_fills"] > 0
    assert snap["equity_curve"]

    logs = client.get("/api/trade_logs", params={"limit": 50}).json()
    assert logs["count"] > 0
    first = logs["trades"][0]
    assert first["symbol"] == "rb2610.SHFE"
    assert first["action"] in ("open_long", "open_short", "close_long", "close_short")
    assert first["trigger"]
    assert "stats" in logs

    decisions = client.get("/api/decisions", params={"limit": 5}).json()
    assert decisions["count"] > 0
    top = decisions["decisions"][0]
    assert set(top["probabilities"]) == {"open_long", "open_short", "close_position", "hold"}
    assert "thresholds" in top and "outcome" in top
    assert top["state"]["orderbook"]["spread"] >= 0


def test_control_actions_lifecycle(client):
    client.post("/api/control/config", json={"mode": "paper", "symbols": ["AAPL"]})
    assert client.post("/api/control/action", json={"action": "start"}).json()["status"] == "running"
    assert client.post("/api/control/action", json={"action": "pause"}).json()["status"] == "paused"
    assert client.post("/api/control/action", json={"action": "resume"}).json()["status"] == "running"
    assert client.post("/api/control/action", json={"action": "flatten"}).status_code == 200
    assert client.post("/api/control/action", json={"action": "reset"}).json()["status"] == "idle"
    assert client.post("/api/control/action", json={"action": "explode"}).status_code == 422
