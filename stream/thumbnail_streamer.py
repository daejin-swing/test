#!/usr/bin/env python3
import os
import sys
import time
import json
import base64
import asyncio
import threading
from datetime import datetime, UTC

try:
    import cv2
except ImportError:
    cv2 = None

try:
    import numpy as np
except ImportError:
    np = None

try:
    import websockets
except ImportError:
    websockets = None

try:
    from msgq.visionipc import VisionIpcClient, VisionStreamType
except ImportError:
    VisionIpcClient = None
    VisionStreamType = None

from common.config import (
    get_device_id,
    get_ws_url,
    get_api_url,
    DEFAULT_THUMBNAIL_FPS,
    DEFAULT_THUMBNAIL_QUALITY,
)
from common.log import cloudlog
from common.params import Params
from common.http_client import post_json


def generate_fallback_svg_base64(device_id: str, label: str, speed: float, steer: float, brake: bool) -> str:
    """Generates a pure-string SVG image base64 when no image libraries are installed."""
    now_str = datetime.now().strftime("%H:%M:%S")
    brake_color = "#f43f5e" if brake else "#64748b"
    brake_text = "BRAKE ON" if brake else "BRAKE OFF"
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180" viewBox="0 0 320 180">
      <rect width="320" height="180" fill="#090d16"/>
      <rect x="10" y="10" width="300" height="160" rx="8" fill="#0f172a" stroke="#1e293b" stroke-width="1.5"/>
      <text x="160" y="32" font-family="monospace" font-size="12" fill="#38bdf8" text-anchor="middle" font-weight="bold">{device_id}</text>
      <text x="160" y="48" font-family="monospace" font-size="10" fill="#94a3b8" text-anchor="middle">[{label}] {now_str}</text>
      <text x="160" y="96" font-family="monospace" font-size="28" fill="#ffffff" text-anchor="middle" font-weight="bold">{int(speed)}</text>
      <text x="160" y="112" font-family="monospace" font-size="10" fill="#64748b" text-anchor="middle">km/h</text>
      <text x="50" y="150" font-family="monospace" font-size="11" fill="#a855f7">Steer: {steer:.1f}&deg;</text>
      <rect x="200" y="137" width="80" height="18" rx="4" fill="{brake_color}"/>
      <text x="240" y="150" font-family="monospace" font-size="9" fill="#ffffff" text-anchor="middle" font-weight="bold">{brake_text}</text>
    </svg>"""
    return base64.b64encode(svg.encode("utf-8")).decode("utf-8")


class DualThumbnailStreamer:
    def __init__(self):
        self.device_id = get_device_id()
        self.params = Params()

        # Dual VisionIPC clients: Road & Wide Road
        self.vipc_road = None
        self.vipc_wide = None

        self.width_road = 1280
        self.height_road = 720
        self.width_wide = 1280
        self.height_wide = 720

    def init_visionipc(self):
        if VisionIpcClient is None:
            return

        cloudlog.debug("Initializing VisionIPC clients for dual cameras...")
        # 1. Road Camera Client
        try:
            if self.vipc_road is None:
                self.vipc_road = VisionIpcClient("camerad", VisionStreamType.VISION_STREAM_ROAD, True)
            if not self.vipc_road.is_connected():                
                if self.vipc_road.connect(False):
                    if self.vipc_road.width and self.vipc_road.height:
                        self.width_road = int(self.vipc_road.width)
                        self.height_road = int(self.vipc_road.height)
                    cloudlog.info(f"Connected to Road Camera VIPC ({self.width_road}x{self.height_road})")
                else:
                    self.vipc_road = None
                    cloudlog.debug("Failed to connect to Road Camera VIPC")
            else:
                cloudlog.debug("Road Camera VIPC already connected")
        except Exception as e:
            cloudlog.debug(f"Road VIPC connect error: {e}")
            self.vipc_road = None

        # 2. Wide Road Camera Client
        try:
            if self.vipc_wide is None:
                self.vipc_wide = VisionIpcClient("camerad", VisionStreamType.VISION_STREAM_WIDE_ROAD, True)
            if not self.vipc_wide.is_connected():
                if self.vipc_wide.connect(False):
                    if self.vipc_wide.width and self.vipc_wide.height:
                        self.width_wide = int(self.vipc_wide.width)
                        self.height_wide = int(self.vipc_wide.height)
                    cloudlog.info(f"Connected to Wide Road Camera VIPC ({self.width_wide}x{self.height_wide})")
                else:
                    cloudlog.debug("Failed to connect to Wide Road Camera VIPC")
                    self.vipc_wide = None
            else:
                cloudlog.debug("Wide Road Camera VIPC already connected")
        except Exception as e:
            cloudlog.debug(f"Wide VIPC connect error: {e}")
            self.vipc_wide = None

    def encode_single_stream(self, vipc_client, label: str, default_w: int, default_h: int) -> tuple[str, str]:
        """Encodes one camera stream to base64 with dynamic buffer resolution handling."""
        w = default_w
        h = default_h
        frame = None

        if vipc_client and vipc_client.is_connected() and np is not None:
            buf = vipc_client.recv(timeout_ms=200)
            #if buf is None:
            #    cloudlog.debug("buf is NONE!!!!!")
            if buf is not None and cv2 is not None:
                # Read actual buffer metadata directly
                cloudlog.debug(f"width: {getattr(buf, "width", None)}, height: {getattr(buf, "height", None)}")
                buf_w = getattr(buf, "width", None) or vipc_client.width or w
                buf_h = getattr(buf, "height", None) or vipc_client.height or h
                buf_stride = getattr(buf, "stride", None) or buf_w

                w, h = int(buf_w), int(buf_h)
                stride = int(buf_stride)

                try:
                    yuv = np.frombuffer(buf.data, dtype=np.uint8).reshape((h * 3 // 2, stride))
                    bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_NV12)
                    if stride > w:
                        bgr = bgr[:, :w]
                    frame = bgr
                except Exception as e:
                    cloudlog.debug(f"YUV convert error for {label}: {e}")
        else:
            cloudlog.debug(f"VIPC client connection: {vipc_client.is_connected() if vipc_client else 'N/A'}")
        if frame is not None and cv2 is not None:
            thumb = cv2.resize(frame, (320, 180), interpolation=cv2.INTER_AREA)
            quality = int(self.params.get("ThumbnailQuality") or DEFAULT_THUMBNAIL_QUALITY)
            _, enc_img = cv2.imencode(".jpg", thumb, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
            return base64.b64encode(enc_img).decode("utf-8"), "image/jpeg"

        # Mock frame with OpenCV
        # if frame is None and cv2 is not None and np is not None:
        #     frame = np.zeros((180, 320, 3), dtype=np.uint8)
        #     now_str = datetime.now().strftime("%H:%M:%S")
        #     color = (0, 200, 255) if label.startswith("ROAD") else (255, 100, 200)
        #     cv2.putText(frame, f"CAM [{label}] {now_str}", (15, 45), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2)
        #     cv2.putText(frame, f"Dev: {self.device_id[:10]}", (15, 85), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
        #     cv2.putText(frame, f"Speed: {float(self.params.get('VehicleSpeedKph') or 0):.0f} km/h", (15, 125), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
        #     _, enc_img = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 50])
        #     return base64.b64encode(enc_img).decode("utf-8"), "image/jpeg"

        # Pure SVG Fallback
        tel = self.get_telemetry()
        return generate_fallback_svg_base64(self.device_id, label, tel["speed_kph"], tel["steering_angle_deg"], tel["brake_pressed"]), "image/svg+xml"

    def get_telemetry(self) -> dict:
        return {
            "speed_kph": float(self.params.get("VehicleSpeedKph") or 0.0),
            "steering_angle_deg": float(self.params.get("SteeringAngleDeg") or 0.0),
            "brake_pressed": self.params.get_bool("BrakePressed"),
            "gas_pressed": self.params.get_bool("GasPressed"),
            "gear": self.params.get("GearSelected") or "D",
        }

    def build_payload(self) -> dict:
        road_b64, road_mime = self.encode_single_stream(self.vipc_road, "ROAD (Main)", self.width_road, self.height_road)
        wide_b64, wide_mime = self.encode_single_stream(self.vipc_wide, "WIDE (Wide Road)", self.width_wide, self.height_wide)

        return {
            "type": "thumbnail",
            "device_id": self.device_id,
            "timestamp": time.time(),
            "telemetry": self.get_telemetry(),
            "image_base64": road_b64,         # Main / default
            "image_road_base64": road_b64,    # Road Cam
            "image_wide_base64": wide_b64,    # Wide Cam
            "mime_type": road_mime,
        }

    async def push_via_http(self, payload: dict):
        endpoint = f"{get_api_url()}/devices/{self.device_id}/thumbnail"
        try:
            post_json(endpoint, payload, timeout=2)
        except Exception:
            pass

    async def run(self):
        cloudlog.info("DualThumbnailStreamer started")
        self.init_visionipc()

        ws_base = get_ws_url()
        uri = f"{ws_base}/stream/thumbnail/{self.device_id}"

        last_vipc_retry = time.monotonic()

        while True:
            # Periodically re-check VIPC connections
            if time.monotonic() - last_vipc_retry > 5.0:
                if not (self.vipc_road and self.vipc_road.is_connected()) or not (self.vipc_wide and self.vipc_wide.is_connected()):
                    self.init_visionipc()
                last_vipc_retry = time.monotonic()

            fps = float(self.params.get("ThumbnailFPS") or DEFAULT_THUMBNAIL_FPS)
            interval = 1.0 / max(0.1, fps)

            if websockets is None:
                t0 = time.monotonic()
                payload = self.build_payload()
                await self.push_via_http(payload)
                elapsed = time.monotonic() - t0
                await asyncio.sleep(max(0.1, interval - elapsed))
                continue

            try:
                cloudlog.info(f"Connecting dual thumbnail WebSocket to {uri}...")
                async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                    cloudlog.info("Dual thumbnail WebSocket connected")
                    while True:
                        t0 = time.monotonic()
                        payload = self.build_payload()
                        await ws.send(json.dumps(payload))
                        elapsed = time.monotonic() - t0
                        await asyncio.sleep(max(0.05, interval - elapsed))
            except Exception as e:
                cloudlog.debug(f"Thumbnail WebSocket connection error: {e}")
                # Fallback to HTTP POST
                payload = self.build_payload()
                await self.push_via_http(payload)
                await asyncio.sleep(3.0)


def main():
    streamer = DualThumbnailStreamer()
    asyncio.run(streamer.run())


if __name__ == "__main__":
    main()
