"""TypeSafe Jev 决策大模型客户端（Choice 原语）+ 本地 Mock 降级。

设计要点
--------
* 未配置 ``TYPESAFE_API_KEY`` 时自动降级为本地动量/均线策略，保证离线可跑通全流程。
* 线上调用失败（超时/5xx/限流）时按指数退避重试，重试耗尽后仍降级到 Mock，
  并在决策结果里标注 ``source="mock"`` 与 ``error``，前端可以直接看到降级原因。
* 返回结构做了容错解析：兼容 ``{"choice": {...}}`` / ``{"probabilities": {...}}`` /
  ``{"options": [{"label": ..., "probability": ...}]}`` 等常见形状。
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import random
import time
from typing import Any, Iterable, Sequence

import httpx

from .models import ACTIONS, JevDecision

log = logging.getLogger("jev.client")

QUESTION = "基于当前盘口和技术指标，下一步交易动作是什么？"
DEFAULT_BASE_URL = "https://api.typesafe.ai"


def _softmax(scores: dict[str, float], temperature: float = 1.0) -> dict[str, float]:
    if not scores:
        return {}
    t = max(temperature, 1e-6)
    peak = max(scores.values())
    exp = {k: math.exp((v - peak) / t) for k, v in scores.items()}
    total = sum(exp.values()) or 1.0
    return {k: v / total for k, v in exp.items()}


def _normalize(raw: dict[str, Any], options: Sequence[str]) -> dict[str, float]:
    """把任意数值字典裁剪到 options 上并归一化。"""
    probs: dict[str, float] = {}
    for opt in options:
        try:
            value = float(raw.get(opt, 0.0))
        except (TypeError, ValueError):
            value = 0.0
        probs[opt] = max(value, 0.0)
    total = sum(probs.values())
    if total <= 0:
        return {opt: 1.0 / len(options) for opt in options}
    return {k: v / total for k, v in probs.items()}


class MockJevModel:
    """离线降级模型：动量 + 均线 + 盘口失衡的可解释打分器。

    输出与线上 Jev 完全一致的 Choice 概率分布，方便无网/无 Key 时开发调试。
    """

    def __init__(self, seed: int | None = 7) -> None:
        self._rng = random.Random(seed)

    def decide(self, state: dict[str, Any], options: Sequence[str]) -> JevDecision:
        started = time.perf_counter()
        ind = state.get("indicators", {})
        book = state.get("orderbook", {})
        pos = state.get("current_position", {})

        rsi = float(ind.get("rsi_14", 50.0))
        hist = float(ind.get("macd_histogram", 0.0))
        chg5 = float(ind.get("price_change_5m_pct", 0.0))
        chg1 = float(ind.get("price_change_1m_pct", 0.0))
        ma_gap = float(ind.get("ma_gap_pct", 0.0))
        imbalance = float(ind.get("imbalance", 0.0))
        samples = int(ind.get("samples", 0))

        side = str(pos.get("side", "none"))
        upnl = float(pos.get("unrealized_pnl_pct", 0.0))

        # 归一化的多空动能，正数看多、负数看空
        momentum = (
            2.4 * math.tanh(ma_gap / 0.25)
            + 1.8 * math.tanh(chg1 / 0.30)
            + 1.2 * math.tanh(chg5 / 0.80)
            + 1.5 * math.tanh(hist / 0.60)
            + 1.0 * imbalance
            + 1.6 * math.tanh((rsi - 50.0) / 18.0)
        )

        scores = {"open_long": 0.0, "open_short": 0.0, "close_position": 0.0, "hold": 0.9}
        if side == "none":
            scores["open_long"] = momentum
            scores["open_short"] = -momentum
            scores["close_position"] = -4.0  # 空仓无可平
        else:
            direction = 1.0 if side == "long" else -1.0
            # 动能反向或浮亏扩大 -> 倾向平仓
            adverse = -momentum * direction
            scores["close_position"] = 1.1 * adverse + 0.55 * max(-upnl, 0.0) + 0.35 * max(upnl, 0.0)
            scores["hold"] = 1.3 - 0.5 * max(adverse, 0.0)
            scores["open_long"] = -4.0
            scores["open_short"] = -4.0

        # 样本不足时压低开仓倾向，避免冷启动乱开
        if samples < 30:
            scores["open_long"] -= 2.5
            scores["open_short"] -= 2.5
            scores["hold"] += 1.5

        probs = _softmax({k: v for k, v in scores.items() if k in options}, temperature=0.85)
        spread = float(book.get("spread", 0.0) or 0.0)
        strength = min(abs(momentum) / 4.0, 1.0)
        confidence = round(min(0.95, 0.45 + 0.45 * strength - min(spread, 3.0) * 0.02), 4)

        top = max(probs, key=probs.get) if probs else "hold"
        reasoning = (
            f"mock(动量={momentum:+.2f}, RSI={rsi:.1f}, MACD柱={hist:+.3f}, "
            f"1m={chg1:+.2f}%, 5m={chg5:+.2f}%, 盘口失衡={imbalance:+.2f}) -> {top}"
        )
        return JevDecision(
            probabilities={k: round(v, 6) for k, v in probs.items()},
            confidence=confidence,
            source="mock",
            model="mock-momentum-v1",
            reasoning=reasoning,
            latency_ms=round((time.perf_counter() - started) * 1000.0, 3),
            raw={"scores": scores},
        )


class JevClient:
    """异步 Jev 客户端；无 Key 或调用失败时透明降级到 :class:`MockJevModel`。"""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = "jev-latest",
        timeout: float = 5.0,
        max_retries: int = 2,
        mock: MockJevModel | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("TYPESAFE_API_KEY", "").strip()
        self.base_url = (base_url or os.getenv("TYPESAFE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.mock = mock or MockJevModel()
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self.calls = 0
        self.failures = 0
        self.last_error: str | None = None

    # -- 生命周期 ----------------------------------------------------------- #
    @property
    def live(self) -> bool:
        """是否具备调用线上 Jev 的条件。"""
        return bool(self.api_key)

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    self._client = httpx.AsyncClient(
                        base_url=self.base_url,
                        timeout=self.timeout,
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json",
                            "User-Agent": "jev-quant/1.0",
                        },
                    )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- 决策 --------------------------------------------------------------- #
    async def choose(
        self,
        state: dict[str, Any],
        options: Iterable[str] = ACTIONS,
        question: str = QUESTION,
    ) -> JevDecision:
        opts = list(options)
        if not self.live:
            decision = self.mock.decide(state, opts)
            decision.error = "TYPESAFE_API_KEY 未配置，已降级为本地动量模型"
            return decision

        payload = {
            "model": self.model,
            "primitive": "choice",
            "question": question,
            "options": opts,
            "state": state,
            "return_probabilities": True,
        }
        started = time.perf_counter()
        last_error: str | None = None

        for attempt in range(self.max_retries + 1):
            try:
                client = await self._http()
                self.calls += 1
                resp = await client.post("/v1/choice", json=payload)
                if resp.status_code in (408, 429) or resp.status_code >= 500:
                    last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    raise httpx.HTTPStatusError(last_error, request=resp.request, response=resp)
                resp.raise_for_status()
                decision = self._parse(resp.json(), opts)
                decision.latency_ms = round((time.perf_counter() - started) * 1000.0, 3)
                self.last_error = None
                return decision
            except Exception as exc:  # noqa: BLE001 - 任何异常都要降级，不能中断热循环
                last_error = last_error or f"{type(exc).__name__}: {exc}"
                self.failures += 1
                if attempt < self.max_retries:
                    await asyncio.sleep(0.25 * (2**attempt))
                    continue
                log.warning("Jev 调用失败，降级本地模型: %s", last_error)

        self.last_error = last_error
        decision = self.mock.decide(state, opts)
        decision.error = f"Jev 调用失败已降级: {last_error}"
        decision.latency_ms = round((time.perf_counter() - started) * 1000.0, 3)
        return decision

    # -- 响应解析 ----------------------------------------------------------- #
    def _parse(self, data: dict[str, Any], options: Sequence[str]) -> JevDecision:
        node: Any = data
        for key in ("choice", "result", "data", "output"):
            if isinstance(node, dict) and isinstance(node.get(key), dict):
                node = node[key]
        if not isinstance(node, dict):
            node = data

        raw_probs: dict[str, Any] | None = None
        for key in ("probabilities", "probs", "distribution", "scores"):
            candidate = node.get(key) if isinstance(node, dict) else None
            if isinstance(candidate, dict):
                raw_probs = candidate
                break

        if raw_probs is None:
            listed = node.get("options") if isinstance(node, dict) else None
            if isinstance(listed, list):
                collected: dict[str, Any] = {}
                for item in listed:
                    if not isinstance(item, dict):
                        continue
                    label = item.get("label") or item.get("option") or item.get("name")
                    value = item.get("probability", item.get("prob", item.get("score")))
                    if label is not None and value is not None:
                        collected[str(label)] = value
                if collected:
                    raw_probs = collected

        if raw_probs is None:
            # 只返回了单一选项：退化为 one-hot
            selected = None
            if isinstance(node, dict):
                selected = node.get("selected") or node.get("answer") or node.get("action")
            if isinstance(selected, str) and selected in options:
                raw_probs = {opt: (1.0 if opt == selected else 0.0) for opt in options}
            else:
                raise ValueError(f"无法从 Jev 响应解析 Choice 概率: {str(data)[:200]}")

        probs = _normalize(raw_probs, options)
        confidence = node.get("confidence", data.get("confidence")) if isinstance(node, dict) else None
        try:
            conf = float(confidence)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            conf = max(probs.values())
        reasoning = ""
        if isinstance(node, dict):
            reasoning = str(node.get("reasoning") or node.get("explanation") or "")

        return JevDecision(
            probabilities={k: round(v, 6) for k, v in probs.items()},
            confidence=round(min(max(conf, 0.0), 1.0), 4),
            source="jev",
            model=str(node.get("model", self.model)) if isinstance(node, dict) else self.model,
            reasoning=reasoning[:500],
            raw=data if isinstance(data, dict) else {},
        )
