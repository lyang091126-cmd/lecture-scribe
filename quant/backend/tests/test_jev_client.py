import httpx
import pytest

from app.jev_client import JevClient, MockJevModel
from app.models import ACTIONS


def _state(**indicators):
    base = {
        "rsi_14": 50.0,
        "macd_histogram": 0.0,
        "price_change_1m_pct": 0.0,
        "price_change_5m_pct": 0.0,
        "ma_gap_pct": 0.0,
        "imbalance": 0.0,
        "samples": 200,
    }
    base.update(indicators)
    return {
        "symbol": "rb2610.SHFE",
        "orderbook": {"bid_price_1": 3449, "ask_price_1": 3450, "spread": 1},
        "indicators": base,
        "current_position": {"side": "none", "unrealized_pnl_pct": 0.0},
    }


def test_mock_probabilities_normalized():
    decision = MockJevModel().decide(_state(), list(ACTIONS))
    assert set(decision.probabilities) == set(ACTIONS)
    assert abs(sum(decision.probabilities.values()) - 1.0) < 1e-5  # 概率保留 6 位小数，允许舍入误差
    assert 0.0 <= decision.confidence <= 1.0
    assert decision.source == "mock"


def test_mock_is_bullish_on_positive_momentum():
    decision = MockJevModel().decide(
        _state(rsi_14=72, macd_histogram=2.4, price_change_1m_pct=0.6, ma_gap_pct=0.5, imbalance=0.4),
        list(ACTIONS),
    )
    assert decision.top_action == "open_long"
    assert decision.probabilities["open_long"] > decision.probabilities["open_short"]


def test_mock_is_bearish_on_negative_momentum():
    decision = MockJevModel().decide(
        _state(rsi_14=25, macd_histogram=-2.0, price_change_1m_pct=-0.7, ma_gap_pct=-0.5, imbalance=-0.5),
        list(ACTIONS),
    )
    assert decision.top_action == "open_short"


def test_mock_never_opens_when_holding():
    state = _state(rsi_14=75, ma_gap_pct=0.8)
    state["current_position"] = {"side": "long", "unrealized_pnl_pct": 1.2}
    decision = MockJevModel().decide(state, list(ACTIONS))
    assert decision.top_action in ("hold", "close_position")


def test_cold_start_prefers_hold():
    decision = MockJevModel().decide(_state(samples=5, ma_gap_pct=0.9), list(ACTIONS))
    assert decision.top_action == "hold"


async def test_client_without_key_falls_back_to_mock():
    client = JevClient(api_key="")
    assert client.live is False
    decision = await client.choose(_state(), ACTIONS)
    assert decision.source == "mock"
    assert "TYPESAFE_API_KEY" in (decision.error or "")


@pytest.mark.parametrize(
    "body",
    [
        {"choice": {"probabilities": {"open_long": 0.78, "open_short": 0.08, "close_position": 0.04, "hold": 0.10}, "confidence": 0.85}},
        {"probabilities": {"open_long": 78, "open_short": 8, "close_position": 4, "hold": 10}, "confidence": 0.85},
        {"choice": {"options": [{"label": "open_long", "probability": 0.78}, {"label": "open_short", "probability": 0.08}, {"label": "close_position", "probability": 0.04}, {"label": "hold", "probability": 0.10}], "confidence": 0.85}},
    ],
)
def test_parse_supported_response_shapes(body):
    decision = JevClient(api_key="k")._parse(body, ACTIONS)
    assert decision.source == "jev"
    assert decision.top_action == "open_long"
    assert abs(sum(decision.probabilities.values()) - 1.0) < 1e-5  # 概率保留 6 位小数，允许舍入误差
    assert decision.confidence == 0.85


def test_parse_single_selected_option():
    decision = JevClient(api_key="k")._parse({"selected": "hold"}, ACTIONS)
    assert decision.top_action == "hold"
    assert decision.probabilities["hold"] == 1.0


def test_parse_rejects_unknown_shape():
    with pytest.raises(ValueError):
        JevClient(api_key="k")._parse({"unexpected": True}, ACTIONS)


async def test_live_call_parses_choice(monkeypatch):
    client = JevClient(api_key="test-key", base_url="https://jev.test", max_retries=0)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = request.read().decode()
        assert "jev-latest" in payload and "open_long" in payload
        return httpx.Response(
            200,
            json={"choice": {"probabilities": {"open_long": 0.9, "hold": 0.1}, "confidence": 0.77, "reasoning": "trend up"}},
        )

    client._client = httpx.AsyncClient(base_url="https://jev.test", transport=httpx.MockTransport(handler))
    decision = await client.choose(_state(), ACTIONS)
    assert decision.source == "jev"
    assert decision.top_action == "open_long"
    assert decision.reasoning == "trend up"
    assert decision.latency_ms >= 0
    await client.aclose()


async def test_live_call_degrades_to_mock_on_server_error():
    client = JevClient(api_key="test-key", base_url="https://jev.test", max_retries=0)
    client._client = httpx.AsyncClient(
        base_url="https://jev.test",
        transport=httpx.MockTransport(lambda request: httpx.Response(500, text="boom")),
    )
    decision = await client.choose(_state(), ACTIONS)
    assert decision.source == "mock"
    assert "降级" in (decision.error or "")
    assert client.failures >= 1
    await client.aclose()
