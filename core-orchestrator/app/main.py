"""FastAPI app: REST API (/api/v1/*) and the server-rendered dashboard (/)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .config import Settings, load_settings
from .monitor import Monitor
from .service import Orchestrator, PolicyRejected
from .state_lock import LockTimeout, make_store
from .traffic_manager import DryRunRunner, SubprocessRunner, TcError, TrafficManager
from .ui import router as ui_router

log = logging.getLogger("orchestrator")


class IntentRequest(BaseModel):
    intent: Literal["idle", "4k_stream", "cloud_gaming"]
    slice: Literal["embb_slice", "urllc_slice"] | None = Field(
        default=None, description="target slice; defaults by intent (idle = all commercial slices)"
    )
    rate_mbps: int | None = Field(default=None, ge=1, le=100_000, description="override the guaranteed rate")
    ceiling_mbps: int | None = Field(
        default=None, ge=1, le=100_000,
        description="most the slice may use when others are idle (default: the whole commercial budget)",
    )
    latency_ms: int | None = Field(default=None, ge=0, le=1000, description="override the profile delay")
    on_exceed: Literal["reject", "scale_down"] | None = None


def build_orchestrator(settings: Settings) -> Orchestrator:
    if settings.dry_run:
        runner = DryRunRunner()
        traffic = TrafficManager(
            runner, interface_resolver=lambda name: f"dry-{name.split('_')[0]}", verify_iface=False
        )
    else:
        traffic = TrafficManager(SubprocessRunner(), sys_class_net=settings.sys_class_net)
    return Orchestrator(settings, make_store(settings.redis_url), traffic)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        orch = build_orchestrator(settings)
        orch.start()  # flushes stale tc rules, applies defaults
        monitor = Monitor(orch)
        app.state.orch = orch
        app.state.monitor = monitor
        task = asyncio.create_task(monitor.run())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            orch.stop()  # fail-safe: never leave containers throttled

    app = FastAPI(
        title="Intent-Based Network Slice Orchestrator",
        version="1.0.0",
        description="Intent-driven bandwidth/latency slices with a hard 80% commercial cap.",
        lifespan=lifespan,
    )
    app.state.settings = settings

    def orch_of() -> Orchestrator:
        return app.state.orch

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "dry_run": settings.dry_run}

    @app.get("/api/v1/stats")
    def stats() -> dict:
        return orch_of().snapshot()

    @app.post("/api/v1/intent")
    def intent(req: IntentRequest) -> dict:
        try:
            return orch_of().apply_intent(
                req.intent, req.slice, req.rate_mbps, req.latency_ms, req.on_exceed, req.ceiling_mbps
            )
        except PolicyRejected as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except TcError as exc:
            raise HTTPException(status_code=502, detail=f"kernel error: {exc}") from exc
        except LockTimeout as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/v1/congestion/simulate")
    def simulate() -> dict:
        try:
            return orch_of().simulate_congestion()
        except (TcError, LockTimeout) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/v1/reset")
    def reset() -> dict:
        try:
            return orch_of().reset()
        except (TcError, LockTimeout) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    app.include_router(ui_router)
    return app


app = create_app()
