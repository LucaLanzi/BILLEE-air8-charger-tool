"""HTTP + WebSocket API for the Air8 charger.

Run:  uv run air8-backend            (real charger over Bluetooth)
      uv run air8-backend --sim      (simulator, no hardware)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Literal, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from . import limits
from .device import BleCharger, ChargerBase, EventBus, SimCharger


class ConnectBody(BaseModel):
    address: str


class JobBody(BaseModel):
    task: Literal["charge", "discharge", "storage", "destroy"]
    chemistry: str
    cells: int = Field(ge=0, le=16)
    current_ma: int
    cutoff_mv: Optional[int] = None
    capacity_limit_mah: int = 0
    confirm_destroy: bool = False


class AlarmBody(BaseModel):
    enabled: bool


class RawBody(BaseModel):
    characteristic: Literal["af01", "af02"] = "af01"
    hex: str


def create_app(simulate: bool = False, allow_raw: bool = False) -> FastAPI:
    bus = EventBus()
    charger: ChargerBase = SimCharger(bus) if simulate else BleCharger(bus)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await charger.disconnect()

    app = FastAPI(title="BILLEE Air8 charger backend", lifespan=lifespan)

    def to_request(body: JobBody) -> limits.JobRequest:
        return limits.JobRequest(**body.model_dump())

    @app.get("/api/health")
    async def health():
        return {"ok": True, "simulated": simulate, "allow_raw": allow_raw}

    @app.get("/api/limits")
    async def get_limits():
        return limits.defaults()

    @app.get("/api/state")
    async def state():
        return charger.state()

    @app.get("/api/scan")
    async def scan(timeout: float = 5.0):
        try:
            return await charger.scan(min(max(timeout, 1.0), 20.0))
        except Exception as e:  # noqa: BLE001 - surface Bluetooth errors to the UI
            raise HTTPException(503, f"Bluetooth scan failed: {e}") from e

    @app.post("/api/connect")
    async def connect(body: ConnectBody):
        try:
            await charger.connect(body.address)
        except Exception as e:  # noqa: BLE001
            await charger.disconnect()
            raise HTTPException(502, f"Could not connect: {e}") from e
        return charger.state()

    @app.post("/api/disconnect")
    async def disconnect():
        await charger.disconnect()
        return charger.state()

    @app.post("/api/job/validate")
    async def validate_job(body: JobBody):
        try:
            task, warnings = limits.validate(to_request(body))
        except limits.JobError as e:
            raise HTTPException(422, str(e)) from e
        from .protocol import work_task_req
        return {"warnings": warnings, "cutoff_mv": task.cutoff_mv,
                "packet": work_task_req(task).hex(" ")}

    @app.post("/api/job/start")
    async def start_job(body: JobBody):
        try:
            task, warnings = limits.validate(to_request(body))
        except limits.JobError as e:
            raise HTTPException(422, str(e)) from e
        if not charger.connected:
            raise HTTPException(409, "Charger is not connected")
        result = await charger.start_job(task)
        bus.publish("log", f"Job sent: {body.task} {body.chemistry} {body.cells or 'auto'}S "
                           f"{body.current_ma} mA, cut-off {task.cutoff_mv}")
        return {"warnings": warnings, **result}

    @app.post("/api/alarm")
    async def alarm(body: AlarmBody):
        if not charger.connected:
            raise HTTPException(409, "Charger is not connected")
        await charger.set_alarm(body.enabled)
        return {"ok": True}

    @app.post("/api/raw")
    async def raw(body: RawBody):
        if not allow_raw:
            raise HTTPException(403, "Raw commands are disabled. Start the backend with --allow-raw.")
        if not charger.connected:
            raise HTTPException(409, "Charger is not connected")
        try:
            data = bytes.fromhex(body.hex.replace(",", " "))
        except ValueError as e:
            raise HTTPException(422, "invalid hex") from e
        if not 1 <= len(data) <= 64:
            raise HTTPException(422, "1-64 bytes")
        await charger.send_raw(body.characteristic, data)
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        q = bus.subscribe()
        try:
            await sock.send_json({"type": "status", "data": charger.state()})
            while True:
                await sock.send_json(await q.get())
        except (WebSocketDisconnect, asyncio.CancelledError, RuntimeError):
            pass
        finally:
            bus.unsubscribe(q)

    return app


def main() -> None:
    ap = argparse.ArgumentParser(description="BILLEE ISDT Air8 charger backend")
    ap.add_argument("--sim", action="store_true", help="use the simulator instead of Bluetooth")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address (default localhost only; anyone who can reach this can start a charge)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--allow-raw", action="store_true",
                    help="enable the raw hex command endpoint (protocol research only)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_app(args.sim, args.allow_raw), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
