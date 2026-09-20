"""
MEDFLOW API.

Run it:      uvicorn app.main:app --reload
Docs:        http://localhost:8000/docs
Live feed:   ws://localhost:8000/ws/runs/{run_id}?speed=30
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from starlette.concurrency import run_in_threadpool

from . import sim as engine
from .schemas import (AdvanceIn, BenchmarkIn, RunCreate, SurgeIn, WhatIfIn)
from .store import store

app = FastAPI(
    title="MEDFLOW API",
    version="1.0.0",
    description=(
        "Hospital resource allocation simulator. Create a run, advance it, and "
        "compare scheduling policies over identical patient arrivals."
    ),
)

# Wide open so a static frontend on any port can talk to it during a hackathon.
# Narrow this before anything real.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _capacity(payload) -> Dict[str, int] | None:
    return payload.capacity.to_dict() if payload and payload.capacity else None


def _handle(run_id: str):
    h = store.get(run_id)
    if h is None:
        raise HTTPException(status_code=404, detail=f"No run {run_id}. It may have expired.")
    return h


# --------------------------------------------------------------------------
# meta
# --------------------------------------------------------------------------
@app.get("/api/health", tags=["meta"])
def health() -> Dict[str, Any]:
    return {"status": "ok", "active_runs": store.count()}


@app.get("/api/config", tags=["meta"])
def config() -> Dict[str, Any]:
    """Everything a client needs to build its own UI without hard-coding constants."""
    return {
        "policies": [
            {"key": "fifo", "label": engine.POLICY_LABELS["fifo"],
             "description": "Serves in arrival order and ignores urgency. The baseline."},
            {"key": "triage", "label": engine.POLICY_LABELS["triage"],
             "description": "Urgency only. No aging, so low-acuity patients can starve."},
            {"key": "medflow", "label": engine.POLICY_LABELS["medflow"],
             "description": "Three-tier priority with deadline guarantee, atomic bundle "
                             "acquisition, and reservation with backfill."},
        ],
        "resources": [
            {"key": k, "label": label, "default_capacity": cap}
            for k, label, cap in engine.RESOURCES
        ],
        "triage_levels": [
            {"level": 1, "label": "Resuscitation"},
            {"level": 2, "label": "Emergent"},
            {"level": 3, "label": "Urgent"},
            {"level": 4, "label": "Less urgent"},
            {"level": 5, "label": "Non-urgent"},
        ],
        "target_wait_min": engine.TARGET_WAIT,
        "deadline_min": engine.DEADLINE,
        "horizon_min": engine.HORIZON,
        "day_starts_at": "06:00",
    }


# --------------------------------------------------------------------------
# runs
# --------------------------------------------------------------------------
@app.post("/api/runs", status_code=201, tags=["runs"])
def create_run(payload: RunCreate) -> Dict[str, Any]:
    """Create a simulation. It starts at minute zero and does not advance on its own."""
    s = engine.Sim(
        seed=payload.seed,
        policy=payload.policy,
        load_pct=payload.load_pct,
        capacity=_capacity(payload),
        horizon=payload.horizon_min,
    )
    h = store.create(s)
    return {"run_id": h.run_id, **h.summary(), "snapshot": s.snapshot()}


@app.get("/api/runs", tags=["runs"])
def list_runs() -> Dict[str, Any]:
    return {"runs": store.list()}


@app.get("/api/runs/{run_id}", tags=["runs"])
def get_run(run_id: str,
            queue_limit: int = Query(25, ge=1, le=200),
            log_limit: int = Query(40, ge=0, le=400)) -> Dict[str, Any]:
    h = _handle(run_id)
    with h.lock:
        return {"run_id": run_id, "snapshot": h.sim.snapshot(queue_limit, log_limit)}


@app.post("/api/runs/{run_id}/advance", tags=["runs"])
async def advance(run_id: str, payload: AdvanceIn) -> Dict[str, Any]:
    """Step the clock forward. Returns the state after the last minute run."""
    h = _handle(run_id)

    def work() -> Dict[str, Any]:
        with h.lock:
            ran = h.sim.advance(payload.minutes)
            return {"run_id": run_id, "minutes_advanced": ran,
                    "snapshot": h.sim.snapshot()}

    return await run_in_threadpool(work)


@app.post("/api/runs/{run_id}/surge", tags=["runs"])
def surge(run_id: str, payload: SurgeIn) -> Dict[str, Any]:
    """Inject a mass casualty incident at the current minute."""
    h = _handle(run_id)
    with h.lock:
        h.sim.surge(payload.count)
        return {"run_id": run_id, "injected": payload.count,
                "snapshot": h.sim.snapshot()}


@app.post("/api/runs/{run_id}/reset", tags=["runs"])
def reset(run_id: str) -> Dict[str, Any]:
    """Rewind to minute zero with the same configuration."""
    h = _handle(run_id)
    with h.lock:
        old = h.sim
        h.sim = engine.Sim(old.seed, old.policy, round(old.load * 100),
                           old.capacity, old.horizon)
        return {"run_id": run_id, "snapshot": h.sim.snapshot()}


@app.delete("/api/runs/{run_id}", status_code=204, tags=["runs"])
def delete_run(run_id: str) -> Response:
    if not store.delete(run_id):
        raise HTTPException(status_code=404, detail=f"No run {run_id}")
    return Response(status_code=204)


@app.get("/api/runs/{run_id}/metrics", tags=["runs"])
def metrics(run_id: str) -> Dict[str, Any]:
    h = _handle(run_id)
    with h.lock:
        return {"run_id": run_id, "t": h.sim.t, "metrics": h.sim.metrics()}


@app.get("/api/runs/{run_id}/patients.csv", response_class=PlainTextResponse, tags=["runs"])
def patients_csv(run_id: str) -> PlainTextResponse:
    """Patient-level export: one row per person who has left the department."""
    h = _handle(run_id)
    with h.lock:
        body = h.sim.patients_csv()
    return PlainTextResponse(
        body,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="medflow-{run_id}.csv"'},
    )


# --------------------------------------------------------------------------
# analysis
# --------------------------------------------------------------------------
@app.post("/api/benchmark", tags=["analysis"])
async def benchmark(payload: BenchmarkIn) -> Dict[str, Any]:
    """
    Run every policy over the same seeds and return a comparison table.

    This is the honest version of the claim: arrivals come from a separate
    random stream, so each policy faces the identical patient sequence and the
    only difference is the ordering decision.
    """
    return await run_in_threadpool(
        engine.compare_policies,
        payload.seed, payload.load_pct, payload.policies,
        payload.replications, _capacity(payload),
    )


@app.post("/api/what-if", tags=["analysis"])
async def what_if(payload: WhatIfIn) -> Dict[str, Any]:
    """
    Price a staffing decision. Adds one unit of each resource in turn, re-runs
    the same seeds, and ranks the options by reduction in harm and abandonment.
    """
    deltas: List[Dict[str, int]] | None = (
        [d.model_dump() for d in payload.deltas] if payload.deltas else None
    )
    return await run_in_threadpool(
        engine.what_if,
        payload.seed, payload.load_pct, payload.policy,
        deltas, payload.replications, _capacity(payload),
    )


# --------------------------------------------------------------------------
# live feed
# --------------------------------------------------------------------------
@app.websocket("/ws/runs/{run_id}")
async def live(websocket: WebSocket, run_id: str,
               speed: int = Query(30, ge=1, le=600),
               interval_ms: int = Query(100, ge=40, le=2000)) -> None:
    """
    Streams a snapshot every interval, advancing `speed` simulated minutes each
    time. Send {"action":"pause"}, {"action":"resume"}, {"action":"surge","count":8}
    or {"action":"speed","value":60} to control it.
    """
    await websocket.accept()
    h = store.get(run_id)
    if h is None:
        await websocket.send_json({"error": f"No run {run_id}"})
        await websocket.close(code=1008)
        return

    running = True
    paused = False

    async def receiver() -> None:
        nonlocal running, paused, speed
        try:
            while running:
                msg = await websocket.receive_json()
                action = msg.get("action")
                if action == "pause":
                    paused = True
                elif action == "resume":
                    paused = False
                elif action == "speed":
                    speed = max(1, min(600, int(msg.get("value", speed))))
                elif action == "surge":
                    with h.lock:
                        h.sim.surge(int(msg.get("count", 8)))
                elif action == "stop":
                    running = False
        except (WebSocketDisconnect, RuntimeError, ValueError):
            running = False

    task = asyncio.create_task(receiver())
    try:
        while running:
            if not paused:
                def work() -> Dict[str, Any]:
                    with h.lock:
                        h.sim.advance(speed)
                        return h.sim.snapshot()

                snap = await run_in_threadpool(work)
                await websocket.send_json({"run_id": run_id, "snapshot": snap})
                if snap["finished"]:
                    await websocket.send_json({"run_id": run_id, "event": "finished"})
                    break
            await asyncio.sleep(interval_ms / 1000)
    except WebSocketDisconnect:
        pass
    finally:
        running = False
        task.cancel()
        try:
            await websocket.close()
        except RuntimeError:
            pass
