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
    DEFAULT_THUMBNAIL_FPS,
    DEFAULT_THUMBNAIL_QUALITY,
)
from common.log import cloudlog
from common.params import Params


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

    def capture_thumbnail_jpeg(self) -> str | None:
        """Captures a frame and returns a Base64-encoded low-res JPEG string."""
        frame = None
        if self.vipc_client and self.vipc_client.is_connected() and np is not None:
            buf = self.vipc_client.recv()
            if buf is not None and cv2 is not None:
                yuv = np.frombuffer(buf.data, dtype=np.uint8).reshape((self.height * 3 // 2, self.width))
                frame = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_NV12)

        if frame is None and np is not None:
            frame = np.zeros((360, 640, 3), dtype=np.uint8)
            now_str = datetime.now().strftime("%H:%M:%S")
            if cv2 is not None:
                cv2.putText(frame, f"Thumbnail [{now_str}]", (40, 100), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
                cv2.putText(frame, f"Dev: {self.device_id}", (40, 160), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)

        if frame is not None and cv2 is not None:
            # Resize to thumbnail dimension (e.g. 320x180)
            thumb = cv2.resize(frame, (320, 180), interpolation=cv2.INTER_AREA)
            quality = int(self.params.get("ThumbnailQuality") or DEFAULT_THUMBNAIL_QUALITY)
            encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), quality]
            _, enc_img = cv2.imencode(".jpg", thumb, encode_param)
            return base64.b64encode(enc_img).decode("utf-8")

        return None

    def get_telemetry(self) -> dict:
        return {
            "speed_kph": float(self.params.get("VehicleSpeedKph") or 0.0),
            "steering_angle_deg": float(self.params.get("SteeringAngleDeg") or 0.0),
            "brake_pressed": self.params.get_bool("BrakePressed"),
            "gas_pressed": self.params.get_bool("GasPressed"),
            "gear": self.params.get("GearSelected") or "D",
        }

    async def run(self):
        cloudlog.info("ThumbnailStreamer started")
        self.init_visionipc()

        ws_base = get_ws_url()
        uri = f"{ws_base}/stream/thumbnail/{self.device_id}"

        while True:
            if websockets is None:
                cloudlog.warning("websockets package not installed. Thumbnail streaming idle.")
                await asyncio.sleep(10)
                continue

            try:
                cloudlog.info(f"Connecting thumbnail WebSocket to {uri}...")
                async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                    cloudlog.info("Thumbnail WebSocket connected")
                    while True:
                        fps = float(self.params.get("ThumbnailFPS") or DEFAULT_THUMBNAIL_FPS)
                        interval = 1.0 / max(0.1, fps)

                        t0 = time.monotonic()
                        jpeg_b64 = self.capture_thumbnail_jpeg()
                        if jpeg_b64:
                            msg = {
                                "type": "thumbnail",
                                "device_id": self.device_id,
                                "timestamp": time.time(),
                                "telemetry": self.get_telemetry(),
                                "image_base64": jpeg_b64,
                            }
                            await ws.send(json.dumps(msg))

                        elapsed = time.monotonic() - t0
                        await asyncio.sleep(max(0.05, interval - elapsed))
            except Exception as e:
                cloudlog.debug(f"Thumbnail WebSocket connection error: {e}")
                await asyncio.sleep(5.0)


def main():
    streamer = ThumbnailStreamer()
    asyncio.run(streamer.run())


if __name__ == "__main__":
    main()

