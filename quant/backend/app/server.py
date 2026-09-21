"""FastAPI 服务：控制面板 API + SSE 实时推送 + 前端静态资源托管。"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .engine import StrategyEngine
from .events import EventBus
from .instruments import resolve_instrument
from .jev_client import JevClient
from .models import ConfigPatch, ControlAction, StrategyConfig

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("jev.server")

FRONTEND_DIST = Path(
    os.getenv("QUANT_FRONTEND_DIST", Path(__file__).resolve().parents[2] / "frontend" / "dist")
)

PRESET_SYMBOLS = [
    {"symbol": "AAPL", "name": "Apple", "market": "us_equity"},
    {"symbol": "TSLA", "name": "Tesla", "market": "us_equity"},
    {"symbol": "NVDA", "name": "NVIDIA", "market": "us_equity"},
    {"symbol": "SPY", "name": "S&P 500 ETF", "market": "us_equity"},
    {"symbol": "rb2610.SHFE", "name": "螺纹钢 2610", "market": "cn_future"},
    {"symbol": "m2610.DCE", "name": "豆粕 2610", "market": "cn_future"},
    {"symbol": "i2610.DCE", "name": "铁矿石 2610", "market": "cn_future"},
    {"symbol": "cu2610.SHFE", "name": "沪铜 2610", "market": "cn_future"},
    {"symbol": "MA2610.CZCE", "name": "甲醇 2610", "market": "cn_future"},
]


def _default_config() -> StrategyConfig:
    cfg = StrategyConfig()
    symbols = os.getenv("QUANT_SYMBOLS", "").strip()
    if symbols:
        cfg.symbols = [s.strip() for s in symbols.split(",") if s.strip()]
    if os.getenv("QUANT_MODE", "").strip() in ("backtest", "paper"):
        cfg.mode = os.getenv("QUANT_MODE", "backtest").strip()  # type: ignore[assignment]
    return cfg


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config = _default_config()
    bus = EventBus()
    jev = JevClient(model=config.jev_model)
    engine = StrategyEngine(config, bus, jev)
    app.state.bus = bus
    app.state.jev = jev
    app.state.engine = engine
    log.info(
        "JEV 量化引擎就绪 | Jev=%s | 模式=%s | 标的=%s",
        "live" if jev.live else "mock(未配置 TYPESAFE_API_KEY)",
        config.mode,
        ",".join(config.symbols),
    )
    if os.getenv("QUANT_AUTOSTART", "").lower() in ("1", "true", "yes"):
        await engine.start()
    try:
        yield
    finally:
        await engine.shutdown()


app = FastAPI(
    title="JEV 量化交易回测与模拟盘系统",
    version=__version__,
    description="基于 TypeSafe Jev 决策大模型的美股 / 国内商品期货策略引擎",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        o.strip()
        for o in os.getenv(
            "QUANT_CORS_ORIGINS",
            "http://localhost:5173,http://127.0.0.1:5173,http://localhost:8080,http://127.0.0.1:8080",
        ).split(",")
        if o.strip()
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


def get_engine() -> StrategyEngine:
    engine: StrategyEngine | None = getattr(app.state, "engine", None)
    if engine is None:  # pragma: no cover - lifespan 未执行时的兜底
        raise HTTPException(status_code=503, detail="引擎尚未初始化")
    return engine


# --------------------------------------------------------------------------- #
# 只读接口
# --------------------------------------------------------------------------- #
@app.get("/api/health")
async def health() -> dict[str, Any]:
    engine = get_engine()
    return {
        "ok": True,
        "version": __version__,
        "status": engine.status,
        "jev_live": engine.jev.live,
        "frontend_dist": FRONTEND_DIST.exists(),
    }


@app.get("/api/snapshot")
async def snapshot() -> dict[str, Any]:
    """当前资产、持仓、收益率、指标与引擎运行状态。"""
    return get_engine().snapshot()


@app.get("/api/trade_logs")
async def trade_logs(
    limit: int = Query(default=200, ge=1, le=2_000),
    symbol: str | None = Query(default=None),
) -> dict[str, Any]:
    """历史成交明细：标的、买卖方向、价格数量、触发条件（含 Jev 概率与置信度）。"""
    engine = get_engine()
    rows = engine.portfolio.recent_trades(limit=limit, symbol=symbol)
    return {"count": len(rows), "trades": rows, "stats": engine.portfolio.stats()}


@app.get("/api/decisions")
async def decisions(limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    """最近的 Jev 决策过程（输入状态 + 概率分布 + 是否触发交易）。"""
    engine = get_engine()
    items = list(engine.decisions)[-limit:]
    items.reverse()
    return {"count": len(items), "decisions": items}


@app.get("/api/config")
async def get_config() -> dict[str, Any]:
    engine = get_engine()
    return {"config": engine.config.model_dump(), "presets": PRESET_SYMBOLS}


@app.get("/api/instruments")
async def instruments(symbols: str = Query(default="")) -> dict[str, Any]:
    wanted = [s.strip() for s in symbols.split(",") if s.strip()] or [p["symbol"] for p in PRESET_SYMBOLS]
    return {
        "instruments": [
            {
                "symbol": inst.symbol,
                "market": inst.market,
                "tick_size": inst.tick_size,
                "multiplier": inst.multiplier,
                "ref_price": inst.ref_price,
                "margin_rate": inst.margin_rate,
                "currency": inst.currency,
            }
            for inst in (resolve_instrument(s) for s in wanted)
        ]
    }


# --------------------------------------------------------------------------- #
# 控制接口
# --------------------------------------------------------------------------- #
@app.post("/api/control/config")
async def update_config(patch: ConfigPatch) -> dict[str, Any]:
    """控制面板热更新参数（阈值 / 止盈止损 / 初始资金 / 标的池 / 模式 …）。"""
    engine = get_engine()
    updates = patch.model_dump(exclude_none=True)
    if not updates:
        return {"changed": [], "restarted": False, "config": engine.config.model_dump()}
    try:
        merged = engine.config.model_copy(update=updates)
        StrategyConfig.model_validate(merged.model_dump())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"参数非法: {exc}") from exc
    return await engine.apply_config(updates)


@app.post("/api/control/action")
async def control_action(cmd: ControlAction) -> dict[str, Any]:
    """引擎控制：start / pause / resume / reset / flatten。"""
    engine = get_engine()
    handlers = {
        "start": engine.start,
        "pause": engine.pause,
        "resume": engine.resume,
        "reset": engine.reset,
        "flatten": engine.flatten,
    }
    result = await handlers[cmd.action]()
    return {"action": cmd.action, **result}


# --------------------------------------------------------------------------- #
# SSE
# --------------------------------------------------------------------------- #
@app.get("/api/events")
async def events(replay: int = Query(default=20, ge=0, le=300)) -> StreamingResponse:
    """Server-Sent Events：实时推送每一次 Jev 决策、成交与状态变化。"""
    bus: EventBus = app.state.bus
    return StreamingResponse(
        bus.stream(replay=replay),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# --------------------------------------------------------------------------- #
# 前端静态资源（构建产物存在时托管，否则给出提示）
# --------------------------------------------------------------------------- #
if FRONTEND_DIST.exists():
    assets = FRONTEND_DIST / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(FRONTEND_DIST / "index.html")

    @app.get("/{path:path}")
    async def spa(path: str) -> FileResponse:
        candidate = (FRONTEND_DIST / path).resolve()
        if candidate.is_file() and candidate.is_relative_to(FRONTEND_DIST.resolve()):
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")

else:

    @app.get("/")
    async def index_placeholder() -> JSONResponse:
        return JSONResponse(
            {
                "message": "后端已就绪。前端构建产物不存在，请先执行 `npm install && npm run build`，"
                "或用 `npm run dev` 启动 Vite 开发服务器（默认 http://localhost:5173）。",
                "api_docs": "/docs",
                "sse": "/api/events",
            }
        )
