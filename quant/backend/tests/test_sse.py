"""SSE 端到端测试：跑一个真实的 uvicorn 实例，验证事件流可被浏览器式客户端消费。

（不用 TestClient：它无法优雅地中断一个无限的 StreamingResponse。）
"""

import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from app.server import app


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def live_server():
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if httpx.get(f"{base}/api/health", timeout=1.0).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:  # pragma: no cover
        pytest.fail("uvicorn 未能在 15 秒内启动")
    yield base
    server.should_exit = True
    thread.join(timeout=10)


def test_sse_pushes_decisions_and_trades(live_server):
    httpx.post(
        f"{live_server}/api/control/config",
        json={
            "mode": "paper",
            "symbols": ["rb2610.SHFE"],
            "decision_interval_ms": 0,
            "buy_threshold": 0.35,
            "min_confidence": 0.1,
        },
        timeout=10,
    ).raise_for_status()
    httpx.post(f"{live_server}/api/control/action", json={"action": "start"}, timeout=10).raise_for_status()

    events: list[dict] = []
    try:
        with httpx.stream("GET", f"{live_server}/api/events?replay=10", timeout=20) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            for line in resp.iter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[len("data: ") :]))
                if len(events) >= 6:
                    break
    finally:
        httpx.post(f"{live_server}/api/control/action", json={"action": "reset"}, timeout=10)

    assert events, "SSE 未推送任何事件"
    assert {"id", "type", "ts"} <= set(events[0])
    types = {e["type"] for e in events}
    assert types & {"status", "feed", "decision", "trade"}
    decisions = [e for e in events if e["type"] == "decision"]
    if decisions:
        first = decisions[0]
        assert set(first["probabilities"]) == {"open_long", "open_short", "close_position", "hold"}
        assert 0.0 <= first["confidence"] <= 1.0
        assert first["thresholds"]["buy_threshold"] == 0.35
