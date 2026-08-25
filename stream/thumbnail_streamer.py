#!/usr/bin/env python3
import os
import sys
import time
import json
import base64
import asyncio
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
    from PIL import Image, ImageDraw
except ImportError:
    Image = None

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


def generate_fallback_svg_base64(device_id: str, speed: float, steer: float, brake: bool) -> str:
    """Generates a pure-string SVG image base64 when no image libraries are installed."""
    now_str = datetime.now().strftime("%H:%M:%S")
    brake_color = "#f43f5e" if brake else "#64748b"
    brake_text = "BRAKE ON" if brake else "BRAKE OFF"
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180" viewBox="0 0 320 180">
      <rect width="320" height="180" fill="#090d16"/>
      <circle cx="160" cy="90" r="70" fill="none" stroke="#1e293b" stroke-width="4"/>
      <text x="160" y="30" font-family="monospace" font-size="12" fill="#38bdf8" text-anchor="middle" font-weight="bold">{device_id}</text>
      <text x="160" y="50" font-family="monospace" font-size="10" fill="#94a3b8" text-anchor="middle">Live Dashboard [{now_str}]</text>
      <text x="160" y="95" font-family="monospace" font-size="28" fill="#ffffff" text-anchor="middle" font-weight="bold">{int(speed)}</text>
      <text x="160" y="112" font-family="monospace" font-size="10" fill="#64748b" text-anchor="middle">km/h</text>
      <text x="50" y="155" font-family="monospace" font-size="11" fill="#a855f7">Steer: {steer:.1f}&deg;</text>
      <rect x="200" y="142" width="80" height="18" rx="4" fill="{brake_color}"/>
      <text x="240" y="155" font-family="monospace" font-size="9" fill="#ffffff" text-anchor="middle" font-weight="bold">{brake_text}</text>
    </svg>"""
    return base64.b64encode(svg.encode("utf-8")).decode("utf-8")


class ThumbnailStreamer:
    def __init__(self):
        self.device_id = get_device_id()
        self.params = Params()
        self.vipc_client = None
        self.width = 1280
        self.height = 720

    def init_visionipc(self) -> bool:
        if VisionIpcClient is None:
            return False
        try:
            self.vipc_client = VisionIpcClient("camerad", VisionStreamType.VISION_STREAM_ROAD, True)
            if self.vipc_client.connect(False):
                self.width = self.vipc_client.width
                self.height = self.vipc_client.height
                cloudlog.info(f"ThumbnailStreamer connected to VisionIPC ({self.width}x{self.height})")
                return True
        except Exception as e:
            cloudlog.debug(f"ThumbnailStreamer VisionIPC failed: {e}")
        self.vipc_client = None
        return False

    def capture_thumbnail_base64(self) -> tuple[str, str]:
        """Captures a frame and returns (image_base64, mime_type)."""
        frame = None

        # 1. Try VisionIPC frame
        if self.vipc_client and self.vipc_client.is_connected() and np is not None:
            buf = self.vipc_client.recv()
            if buf is not None and cv2 is not None:
                try:
                    yuv = np.frombuffer(buf.data, dtype=np.uint8).reshape((self.height * 3 // 2, self.width))
                    frame = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_NV12)
                except Exception as e:
                    cloudlog.debug(f"YUV convert error: {e}")

        # 2. OpenCV fallback frame
        if frame is not None and cv2 is not None:
            thumb = cv2.resize(frame, (320, 180), interpolation=cv2.INTER_AREA)
            quality = int(self.params.get("ThumbnailQuality") or DEFAULT_THUMBNAIL_QUALITY)
            _, enc_img = cv2.imencode(".jpg", thumb, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
            return base64.b64encode(enc_img).decode("utf-8"), "image/jpeg"

        if frame is None and cv2 is not None and np is not None:
            frame = np.zeros((180, 320, 3), dtype=np.uint8)
            now_str = datetime.now().strftime("%H:%M:%S")
            cv2.putText(frame, f"LIVE [{now_str}]", (20, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
            cv2.putText(frame, f"Dev: {self.device_id[:12]}", (20, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
            cv2.putText(frame, f"Speed: {float(self.params.get('VehicleSpeedKph') or 0):.0f} km/h", (20, 130), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 200, 255), 2)
            _, enc_img = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 50])
            return base64.b64encode(enc_img).decode("utf-8"), "image/jpeg"

        # 3. Pure SVG Fallback (Zero dependencies)
        telemetry = self.get_telemetry()
        svg_b64 = generate_fallback_svg_base64(
            self.device_id,
            telemetry["speed_kph"],
            telemetry["steering_angle_deg"],
            telemetry["brake_pressed"],
        )
        return svg_b64, "image/svg+xml"

    def get_telemetry(self) -> dict:
        return {
            "speed_kph": float(self.params.get("VehicleSpeedKph") or 0.0),
            "steering_angle_deg": float(self.params.get("SteeringAngleDeg") or 0.0),
            "brake_pressed": self.params.get_bool("BrakePressed"),
            "gas_pressed": self.params.get_bool("GasPressed"),
            "gear": self.params.get("GearSelected") or "D",
        }

    async def push_via_http(self, payload: dict):
        """HTTP Fallback for thumbnail push when websockets is unavailable."""
        endpoint = f"{get_api_url()}/devices/{self.device_id}/thumbnail"
        try:
            post_json(endpoint, payload, timeout=2)
        except Exception:
            pass

    async def run(self):
        cloudlog.info("ThumbnailStreamer started")
        self.init_visionipc()

        ws_base = get_ws_url()
        uri = f"{ws_base}/stream/thumbnail/{self.device_id}"

        while True:
            # If websockets is not installed, use HTTP push loop
            if websockets is None:
                fps = float(self.params.get("ThumbnailFPS") or DEFAULT_THUMBNAIL_FPS)
                interval = 1.0 / max(0.1, fps)
                t0 = time.monotonic()
                img_b64, mime_type = self.capture_thumbnail_base64()
                msg = {
                    "type": "thumbnail",
                    "device_id": self.device_id,
                    "timestamp": time.time(),
                    "telemetry": self.get_telemetry(),
                    "image_base64": img_b64,
                    "mime_type": mime_type,
                }
                await self.push_via_http(msg)
                elapsed = time.monotonic() - t0
                await asyncio.sleep(max(0.1, interval - elapsed))
                continue

            try:
                cloudlog.info(f"Connecting thumbnail WebSocket to {uri}...")
                async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                    cloudlog.info("Thumbnail WebSocket connected")
                    while True:
                        fps = float(self.params.get("ThumbnailFPS") or DEFAULT_THUMBNAIL_FPS)
                        interval = 1.0 / max(0.1, fps)

                        t0 = time.monotonic()
                        img_b64, mime_type = self.capture_thumbnail_base64()
                        msg = {
                            "type": "thumbnail",
                            "device_id": self.device_id,
                            "timestamp": time.time(),
                            "telemetry": self.get_telemetry(),
                            "image_base64": img_b64,
                            "mime_type": mime_type,
                        }
                        await ws.send(json.dumps(msg))

                        elapsed = time.monotonic() - t0
                        await asyncio.sleep(max(0.05, interval - elapsed))
            except Exception as e:
                cloudlog.debug(f"Thumbnail WebSocket connection error: {e}")
                # Try HTTP push once on WS failure
                img_b64, mime_type = self.capture_thumbnail_base64()
                msg = {
                    "type": "thumbnail",
                    "device_id": self.device_id,
                    "timestamp": time.time(),
                    "telemetry": self.get_telemetry(),
                    "image_base64": img_b64,
                    "mime_type": mime_type,
                }
                await self.push_via_http(msg)
                await asyncio.sleep(3.0)


def main():
    streamer = ThumbnailStreamer()
    asyncio.run(streamer.run())


if __name__ == "__main__":
    main()
