import os
import json
import time
import shutil
import asyncio
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse

from config import HOST, PORT, VIDEOS_DIR, LOGS_DIR, BASE_DIR
from database import (
    init_db,
    upsert_device_heartbeat,
    update_device_config,
    list_devices,
    get_device,
    insert_logs,
    list_logs,
    insert_event,
    list_events,
)

app = FastAPI(
    title="Device Management & Streaming Server",
    version="1.0.0",
    description="Backend API and Real-Time Streaming Server for Connected Dashcam / OTA",
)

# Enable CORS for web dashboards
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount Static and Storage files
STATIC_DIR = BASE_DIR / "static"
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/storage", StaticFiles(directory=str(BASE_DIR / "storage")), name="storage")


# In-memory latest thumbnail & telemetry cache: device_id -> {image_base64, telemetry, timestamp}
latest_thumbnails: dict[str, dict] = {}


@app.on_event("startup")
def startup_event():
    init_db()


# -------------------------------------------------------------
# WebSocket Connection Hubs
# -------------------------------------------------------------
class ConnectionHub:
    def __init__(self):
        # device_id -> list of subscriber websockets (web clients)
        self.thumbnail_subscribers: dict[str, list[WebSocket]] = {}
        # device_id -> list of signaling websockets (devices & web clients)
        self.signaling_peers: dict[str, list[WebSocket]] = {}

    # --- Thumbnail Streaming ---
    async def subscribe_thumbnail(self, device_id: str, websocket: WebSocket):
        await websocket.accept()
        if device_id not in self.thumbnail_subscribers:
            self.thumbnail_subscribers[device_id] = []
        self.thumbnail_subscribers[device_id].append(websocket)

        # Immediately send the latest cached thumbnail on connect
        if device_id in latest_thumbnails:
            try:
                cached_msg = json.dumps(latest_thumbnails[device_id])
                await websocket.send_text(cached_msg)
            except Exception:
                pass

    def unsubscribe_thumbnail(self, device_id: str, websocket: WebSocket):
        if device_id in self.thumbnail_subscribers:
            if websocket in self.thumbnail_subscribers[device_id]:
                self.thumbnail_subscribers[device_id].remove(websocket)

    async def broadcast_thumbnail(self, device_id: str, message: str):
        # Cache latest thumbnail
        try:
            parsed = json.loads(message)
            latest_thumbnails[device_id] = parsed
        except Exception:
            pass

        if device_id in self.thumbnail_subscribers:
            dead_sockets = []
            for ws in self.thumbnail_subscribers[device_id]:
                try:
                    await ws.send_text(message)
                except Exception:
                    dead_sockets.append(ws)
            for ws in dead_sockets:
                self.unsubscribe_thumbnail(device_id, ws)

    # --- WebRTC Signaling ---
    async def register_signaling(self, device_id: str, websocket: WebSocket):
        await websocket.accept()
        if device_id not in self.signaling_peers:
            self.signaling_peers[device_id] = []
        self.signaling_peers[device_id].append(websocket)

    def unregister_signaling(self, device_id: str, websocket: WebSocket):
        if device_id in self.signaling_peers:
            if websocket in self.signaling_peers[device_id]:
                self.signaling_peers[device_id].remove(websocket)

    async def broadcast_signaling(self, device_id: str, sender_ws: WebSocket, message: str):
        if device_id in self.signaling_peers:
            dead_sockets = []
            for ws in self.signaling_peers[device_id]:
                if ws != sender_ws:
                    try:
                        await ws.send_text(message)
                    except Exception:
                        dead_sockets.append(ws)
            for ws in dead_sockets:
                self.unregister_signaling(device_id, ws)


hub = ConnectionHub()


# -------------------------------------------------------------
# REST API Endpoints
# -------------------------------------------------------------

@app.get("/")
def index():
    return RedirectResponse(url="/static/index.html")


# 1. Heartbeat
@app.post("/api/v1/devices/{device_id}/heartbeat")
def device_heartbeat(device_id: str, payload: dict):
    software = payload.get("software", {})
    system = payload.get("system", {})
    dashcam = payload.get("dashcam", {})
    applied_cfg_ver = payload.get("applied_config_version", 0)

    result = upsert_device_heartbeat(device_id, software, system, dashcam, applied_cfg_ver)
    return {
        "status": "success",
        "server_time": datetime.now(UTC).isoformat(),
        "config_version": result["config_version"],
        "config": result["config"],
    }


# 2. Devices List & Details
@app.get("/api/v1/devices")
def get_device_list():
    devices = list_devices()
    for d in devices:
        dev_id = d.get("device_id")
        if dev_id in latest_thumbnails:
            d["latest_thumbnail"] = latest_thumbnails[dev_id].get("image_base64")
            d["latest_telemetry"] = latest_thumbnails[dev_id].get("telemetry")
    return devices


@app.get("/api/v1/devices/{device_id}")
def get_single_device(device_id: str):
    device = get_device(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    if device_id in latest_thumbnails:
        device["latest_thumbnail"] = latest_thumbnails[device_id].get("image_base64")
        device["latest_telemetry"] = latest_thumbnails[device_id].get("telemetry")
    return device


# 3. HTTP Fallback for Thumbnail Push
@app.post("/api/v1/devices/{device_id}/thumbnail")
async def post_thumbnail_http(device_id: str, payload: dict):
    msg_str = json.dumps(payload)
    await hub.broadcast_thumbnail(device_id, msg_str)
    return {"status": "broadcasted"}


# 4. Update Device Config
@app.put("/api/v1/devices/{device_id}/config")
def set_device_config(device_id: str, config_update: dict):
    result = update_device_config(device_id, config_update)
    return {
        "status": "success",
        "device_id": device_id,
        "config_version": result["config_version"],
        "config": result["config"],
    }


# 5. Logs Ingestion & Query
@app.post("/api/v1/devices/{device_id}/logs")
def ingest_logs(device_id: str, payload: dict):
    log_type = payload.get("log_type", "app")
    entries = payload.get("entries", [])
    if entries:
        insert_logs(device_id, log_type, entries)
    return {"status": "success", "received_count": len(entries)}


@app.get("/api/v1/logs")
def get_logs(device_id: str | None = None, level: str | None = None, limit: int = 100):
    return list_logs(device_id, limit, level)


# 6. Events Upload & Query
@app.post("/api/v1/devices/{device_id}/events")
async def upload_event(
    device_id: str,
    metadata: str = Form(...),
    video_file: UploadFile = File(...),
):
    try:
        meta_dict = json.loads(metadata)
    except Exception:
        meta_dict = {}

    event_id = meta_dict.get("event_id") or f"evt-{int(time.time())}"
    filename = f"{device_id}_{video_file.filename}"
    save_path = VIDEOS_DIR / filename

    with open(save_path, "wb") as buffer:
        shutil.copyfileobj(video_file.file, buffer)

    file_size = os.path.getsize(save_path)
    video_url = f"/storage/videos/{filename}"

    insert_event(
        event_id=event_id,
        device_id=device_id,
        event_type=meta_dict.get("event_type", "EVENT"),
        g_force=float(meta_dict.get("g_force", 1.0)),
        speed_kph=float(meta_dict.get("speed_kph", 0.0)),
        location=meta_dict.get("location", {}),
        video_url=video_url,
        video_size_bytes=file_size,
        occurred_at=meta_dict.get("occurred_at") or datetime.now(UTC).isoformat(),
    )

    return {
        "status": "uploaded",
        "event_id": event_id,
        "video_url": video_url,
        "size_bytes": file_size,
    }


@app.get("/api/v1/events")
def get_events(device_id: str | None = None, limit: int = 50):
    return list_events(device_id, limit)


# -------------------------------------------------------------
# WebSocket Endpoints
# -------------------------------------------------------------

# 1. Device pushes 1-FPS Thumbnail
@app.websocket("/ws/v1/stream/thumbnail/{device_id}")
async def ws_thumbnail_ingest(websocket: WebSocket, device_id: str):
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_text()
            await hub.broadcast_thumbnail(device_id, data)
    except WebSocketDisconnect:
        pass


# 2. Web Client subscribes to Device Thumbnail
@app.websocket("/ws/v1/stream/view/{device_id}")
async def ws_thumbnail_view(websocket: WebSocket, device_id: str):
    await hub.subscribe_thumbnail(device_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        hub.unsubscribe_thumbnail(device_id, websocket)


# 3. WebRTC Live Streaming Signaling Hub
@app.websocket("/ws/v1/webrtc/signaling/{device_id}")
async def ws_signaling(websocket: WebSocket, device_id: str):
    await hub.register_signaling(device_id, websocket)
    try:
        while True:
            message = await websocket.receive_text()
            await hub.broadcast_signaling(device_id, websocket, message)
    except WebSocketDisconnect:
        hub.unregister_signaling(device_id, websocket)


if __name__ == "__main__":
    import uvicorn
    print(f"Starting Device Management & Streaming Server on http://{HOST}:{PORT}")
    uvicorn.run("main:app", host=HOST, port=PORT, reload=True)
