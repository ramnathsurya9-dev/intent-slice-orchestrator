"""Server-rendered dashboard: plain Python + Jinja2, no JavaScript.

Buttons are small HTML forms that POST to these routes; each runs the same orchestrator
call as the REST API and redirects back to "/" with a one-line result message.
"""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .service import Orchestrator, PolicyRejected
from .state_lock import LockTimeout
from .traffic_manager import TcError

router = APIRouter(include_in_schema=False)

REFRESH_SECONDS = 3


def _templates(request: Request) -> Jinja2Templates:
    cache = request.app.state
    if not hasattr(cache, "templates"):
        cache.templates = Jinja2Templates(directory=request.app.state.settings.dashboard_dir)
    return cache.templates


def _back(kind: str, message: str) -> RedirectResponse:
    return RedirectResponse("/?" + urlencode({"kind": kind, "msg": message}), status_code=303)


@router.get("/")
def dashboard(request: Request, msg: str = "", kind: str = "ok"):
    orch: Orchestrator = request.app.state.orch
    snap = orch.snapshot()
    reserve_pct = snap["public_reserve_mbps"] * 100 // snap["capacity_mbps"]
    return _templates(request).TemplateResponse(
        request,
        "dashboard.html",
        {
            "snap": snap,
            "flash": msg[:400],
            "flash_kind": "err" if kind == "err" else "ok",
            "refresh": REFRESH_SECONDS,
            "reserve_pct": reserve_pct,
            "committed_pct": min(snap["commercial_allocated_mbps"] * 100 // snap["capacity_mbps"], 100),
            "free_commercial_pct": max(
                (snap["commercial_budget_mbps"] - snap["commercial_allocated_mbps"])
                * 100 // snap["capacity_mbps"], 0),
        },
    )


@router.post("/ui/intent/{intent}")
def ui_intent(request: Request, intent: str):
    orch: Orchestrator = request.app.state.orch
    try:
        result = orch.apply_intent(intent)
    except PolicyRejected as exc:
        return _back("err", f"Refused: {exc.message}")
    except (ValueError, TcError, LockTimeout) as exc:
        return _back("err", str(exc))
    msg = result["reason"]
    if result["scaled_down"]:
        msg += " | scaled down: " + ", ".join(
            f"{s['slice']} {s['from_mbps']}->{s['to_mbps']}" for s in result["scaled_down"])
    return _back("ok", msg)


@router.post("/ui/simulate")
def ui_simulate(request: Request):
    orch: Orchestrator = request.app.state.orch
    try:
        result = orch.simulate_congestion()
    except (TcError, LockTimeout) as exc:
        return _back("err", str(exc))
    lines = [f"{'OK' if s['allowed'] else 'REFUSED'}: {s['step']}" for s in result["steps"]]
    return _back("ok", " || ".join(lines))


@router.post("/ui/reset")
def ui_reset(request: Request):
    orch: Orchestrator = request.app.state.orch
    try:
        orch.reset()
    except (TcError, LockTimeout) as exc:
        return _back("err", str(exc))
    return _back("ok", "All slices reset to their default profiles.")
