"""行情数据源：历史回放（CSV / 合成）与实时盘口（Alpaca / TqSdk / 合成）。

所有数据源统一产出 :class:`~app.models.Tick` 的异步迭代流，引擎无需关心来源。
未配置任何数据源 Key 时使用 **可复现的合成盘口**（按标的代码播种），
保证离线环境下回测与模拟盘都能跑通，并在事件里明确标注 ``source``。
"""

from __future__ import annotations

import asyncio
import csv
import logging
import math
import os
import random
import time
from pathlib import Path
from typing import AsyncIterator, Protocol

from .instruments import Instrument, resolve_instrument
from .models import Tick

log = logging.getLogger("jev.datafeed")

DATA_DIR = Path(os.getenv("QUANT_DATA_DIR", Path(__file__).resolve().parents[2] / "data"))


class DataFeed(Protocol):
    symbol: str
    source: str

    def stream(self) -> AsyncIterator[Tick]: ...


# --------------------------------------------------------------------------- #
# 合成盘口（默认离线数据源）
# --------------------------------------------------------------------------- #
class SyntheticFeed:
    """几何布朗运动 + 日内趋势段的合成 Tick 流。

    回测模式下按 ``limit`` 条一次性推演；模拟盘模式下按 ``interval`` 实时推送。
    同一 ``symbol`` + ``seed`` 产生完全相同的序列，便于参数对比实验。
    """

    def __init__(
        self,
        symbol: str,
        limit: int = 3_000,
        interval: float = 0.0,
        seed: int | None = None,
        start_ts: float | None = None,
        instrument: Instrument | None = None,
    ) -> None:
        self.symbol = symbol
        self.instrument = instrument or resolve_instrument(symbol)
        self.limit = limit
        self.interval = interval
        self.source = "synthetic"
        self._seed = seed if seed is not None else (abs(hash(symbol)) % 100_000)
        self._start_ts = start_ts if start_ts is not None else time.time() - limit * 0.5

    async def stream(self) -> AsyncIterator[Tick]:
        rng = random.Random(self._seed)
        inst = self.instrument
        price = inst.ref_price
        # 趋势段：每 120~400 个 tick 切换一次漂移方向，制造可被动量策略捕捉的行情
        drift = rng.uniform(-1.0, 1.0) * 2e-5
        seg_left = rng.randint(120, 400)
        vol = 4e-4 if inst.is_future else 6e-4
        ts = self._start_ts
        emitted = 0

        while self.limit <= 0 or emitted < self.limit:
            if seg_left <= 0:
                drift = rng.uniform(-1.0, 1.0) * 2.5e-5
                seg_left = rng.randint(120, 400)
            seg_left -= 1

            price *= math.exp(drift + rng.gauss(0.0, vol))
            price = max(price, inst.tick_size * 10)
            mid = inst.round_price(price)
            half = inst.tick_size * (1 if rng.random() < 0.75 else 2) / 2.0
            bid = inst.round_price(mid - half)
            ask = inst.round_price(mid + half)
            if ask <= bid:
                ask = inst.round_price(bid + inst.tick_size)

            base_vol = 120 if inst.is_future else 800
            skew = 1.0 + 0.6 * math.tanh(drift * 4e4)
            bid_vol = max(1.0, round(rng.gauss(base_vol * skew, base_vol * 0.35)))
            ask_vol = max(1.0, round(rng.gauss(base_vol / skew, base_vol * 0.35)))

            ts += 0.5 if self.interval <= 0 else self.interval
            emitted += 1
            yield Tick(
                symbol=self.symbol,
                ts=ts if self.interval <= 0 else time.time(),
                bid_price_1=bid,
                ask_price_1=ask,
                bid_vol_1=bid_vol,
                ask_vol_1=ask_vol,
                last_price=mid,
                volume=bid_vol + ask_vol,
            )
            if self.interval > 0:
                await asyncio.sleep(self.interval)
            elif emitted % 500 == 0:
                await asyncio.sleep(0)  # 让出事件循环，避免阻塞 SSE 推送


# --------------------------------------------------------------------------- #
# CSV 历史回放
# --------------------------------------------------------------------------- #
class CsvFeed:
    """从 ``data/<symbol>.csv`` 读取历史 Tick / K 线。

    支持列名（大小写不敏感，缺失自动推导）::

        ts|timestamp|datetime, bid|bid_price_1, ask|ask_price_1,
        bid_vol|bid_vol_1, ask_vol|ask_vol_1, last|close|price, volume
    """

    def __init__(self, symbol: str, path: Path, limit: int = 0, interval: float = 0.0) -> None:
        self.symbol = symbol
        self.path = path
        self.limit = limit
        self.interval = interval
        self.source = f"csv:{path.name}"
        self.instrument = resolve_instrument(symbol)

    @staticmethod
    def _to_ts(value: str, fallback: float) -> float:
        value = (value or "").strip()
        if not value:
            return fallback
        try:
            num = float(value)
            # 毫秒级时间戳
            return num / 1000.0 if num > 1e11 else num
        except ValueError:
            pass
        from datetime import datetime

        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(value.replace("Z", "+0000"), fmt).timestamp()
            except ValueError:
                continue
        return fallback

    async def stream(self) -> AsyncIterator[Tick]:
        inst = self.instrument
        with self.path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            fields = {(f or "").strip().lower(): (f or "") for f in (reader.fieldnames or [])}

            def pick(row: dict[str, str], *names: str) -> str | None:
                for name in names:
                    key = fields.get(name)
                    if key is not None and row.get(key) not in (None, ""):
                        return row[key]
                return None

            ts = time.time() - 86_400
            count = 0
            for row in reader:
                if self.limit and count >= self.limit:
                    break
                ts = self._to_ts(pick(row, "ts", "timestamp", "datetime", "date", "time") or "", ts + 0.5)
                last_raw = pick(row, "last", "last_price", "close", "price")
                bid_raw = pick(row, "bid", "bid_price_1", "bid1")
                ask_raw = pick(row, "ask", "ask_price_1", "ask1")
                if last_raw is None and bid_raw is None:
                    continue
                last = float(last_raw) if last_raw is not None else None
                if bid_raw is not None and ask_raw is not None:
                    bid, ask = float(bid_raw), float(ask_raw)
                else:
                    # 只有 K 线收盘价时，按最小变动价位合成一档买卖盘
                    ref = last or 0.0
                    bid = inst.round_price(ref)
                    ask = inst.round_price(bid + inst.tick_size)
                volume = float(pick(row, "volume", "vol") or 0.0)
                count += 1
                yield Tick(
                    symbol=self.symbol,
                    ts=ts,
                    bid_price_1=bid,
                    ask_price_1=ask,
                    bid_vol_1=float(pick(row, "bid_vol", "bid_vol_1", "bid_volume") or volume or 1.0),
                    ask_vol_1=float(pick(row, "ask_vol", "ask_vol_1", "ask_volume") or volume or 1.0),
                    last_price=last if last is not None else (bid + ask) / 2,
                    volume=volume,
                )
                if self.interval > 0:
                    await asyncio.sleep(self.interval)
                elif count % 500 == 0:
                    await asyncio.sleep(0)


# --------------------------------------------------------------------------- #
# 实时行情适配器
# --------------------------------------------------------------------------- #
class AlpacaQuoteFeed:
    """Alpaca 实时报价轮询（美股）。需要 ``APCA_API_KEY_ID`` / ``APCA_API_SECRET_KEY``。"""

    def __init__(self, symbol: str, interval: float = 1.0) -> None:
        self.symbol = symbol
        self.interval = max(interval, 0.2)
        self.source = "alpaca"
        self.instrument = resolve_instrument(symbol)
        self.key = os.getenv("APCA_API_KEY_ID", "").strip()
        self.secret = os.getenv("APCA_API_SECRET_KEY", "").strip()
        self.base = os.getenv("APCA_DATA_URL", "https://data.alpaca.markets").rstrip("/")

    @property
    def available(self) -> bool:
        return bool(self.key and self.secret)

    async def stream(self) -> AsyncIterator[Tick]:
        import httpx

        headers = {"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret}
        url = f"{self.base}/v2/stocks/{self.symbol}/quotes/latest"
        async with httpx.AsyncClient(timeout=5.0, headers=headers) as client:
            while True:
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    q = resp.json().get("quote", {})
                    bid, ask = float(q.get("bp", 0.0)), float(q.get("ap", 0.0))
                    if bid > 0 and ask > 0:
                        yield Tick(
                            symbol=self.symbol,
                            ts=time.time(),
                            bid_price_1=bid,
                            ask_price_1=ask,
                            bid_vol_1=float(q.get("bs", 0.0)) * 100,
                            ask_vol_1=float(q.get("as", 0.0)) * 100,
                            last_price=(bid + ask) / 2,
                        )
                except Exception as exc:  # noqa: BLE001 - 行情中断不应打断引擎
                    log.warning("Alpaca 行情拉取失败 %s: %s", self.symbol, exc)
                await asyncio.sleep(self.interval)


class TqSdkFeed:
    """天勤 TqSdk 实时盘口（国内商品期货）。需要安装 ``tqsdk`` 并配置账号。

    TqSdk 是同步阻塞 API，这里放在独立线程里跑，通过队列桥接到 asyncio。
    """

    def __init__(self, symbol: str, interval: float = 0.0) -> None:
        self.symbol = symbol
        self.interval = interval
        self.source = "tqsdk"
        self.instrument = resolve_instrument(symbol)
        self.user = os.getenv("TQ_USER", "").strip()
        self.password = os.getenv("TQ_PASSWORD", "").strip()

    @property
    def available(self) -> bool:
        if not (self.user and self.password):
            return False
        try:
            import tqsdk  # noqa: F401
        except ImportError:
            return False
        return True

    async def stream(self) -> AsyncIterator[Tick]:
        from tqsdk import TqApi, TqAuth  # type: ignore[import-not-found]

        queue: asyncio.Queue[Tick] = asyncio.Queue(maxsize=2_000)
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()

        def worker() -> None:
            api = TqApi(auth=TqAuth(self.user, self.password))
            try:
                quote = api.get_quote(self.symbol)
                while not stop.is_set():
                    api.wait_update()
                    tick = Tick(
                        symbol=self.symbol,
                        ts=time.time(),
                        bid_price_1=float(quote.bid_price1),
                        ask_price_1=float(quote.ask_price1),
                        bid_vol_1=float(quote.bid_volume1),
                        ask_vol_1=float(quote.ask_volume1),
                        last_price=float(quote.last_price),
                        volume=float(quote.volume or 0.0),
                    )
                    loop.call_soon_threadsafe(lambda t=tick: queue.put_nowait(t) if not queue.full() else None)
            except Exception as exc:  # noqa: BLE001
                log.error("TqSdk 行情线程异常: %s", exc)
            finally:
                api.close()

        import threading

        thread = threading.Thread(target=worker, name=f"tqsdk-{self.symbol}", daemon=True)
        thread.start()
        try:
            while True:
                yield await queue.get()
        finally:
            stop.set()


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #
def build_feed(symbol: str, mode: str, *, limit: int = 3_000, replay_speed: float = 0.0) -> DataFeed:
    """按模式与可用凭证选择数据源。

    * ``backtest``：优先 ``data/<symbol>.csv``，否则合成历史行情。
    * ``paper``：优先真实行情（Alpaca / TqSdk），否则合成实时行情。
    """
    inst = resolve_instrument(symbol)
    if mode == "backtest":
        for name in (f"{symbol}.csv", f"{symbol.replace('.', '_')}.csv", f"{symbol.split('.')[0]}.csv"):
            path = DATA_DIR / name
            if path.exists():
                return CsvFeed(symbol, path, limit=limit, interval=(1.0 / replay_speed if replay_speed > 0 else 0.0))
        return SyntheticFeed(
            symbol,
            limit=limit,
            interval=(1.0 / replay_speed if replay_speed > 0 else 0.0),
            instrument=inst,
        )

    if inst.market == "us_equity":
        alpaca = AlpacaQuoteFeed(symbol)
        if alpaca.available:
            return alpaca
    else:
        tq = TqSdkFeed(symbol)
        if tq.available:
            return tq
    # 无凭证：实时合成盘口（500ms 一跳），前端会标注 "simulated"
    return SyntheticFeed(symbol, limit=0, interval=0.5, instrument=inst, seed=int(time.time()) % 9_973)
