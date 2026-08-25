#!/usr/bin/env python3
import os
import sys
import time
import json
import asyncio
import threading
from datetime import datetime

try:
    import websockets
except ImportError:
    websockets = None

from common.config import get_device_id, get_ws_url
from common.log import cloudlog
from common.params import Params


class LiveStreamerDaemon:
    def __init__(self):
        self.device_id = get_device_id()
        self.params = Params()
        self.is_streaming = False
        self.stream_task = None

    async def handle_signaling_message(self, ws, raw_msg: str):
        try:
            msg = json.loads(raw_msg)
            action = msg.get("action")

            if action == "start_live":
                cloudlog.info("Received start_live request from server")
                self.params.put_bool("IsLiveStreaming", True)
                self.is_streaming = True
                # Send acknowledgement / offer
                ack = {
                    "action": "live_started",
                    "device_id": self.device_id,
                    "timestamp": time.time(),
                }
                await ws.send(json.dumps(ack))

            elif action == "stop_live":
                cloudlog.info("Received stop_live request from server")
                self.params.put_bool("IsLiveStreaming", False)
                self.is_streaming = False
                ack = {
                    "action": "live_stopped",
                    "device_id": self.device_id,
                    "timestamp": time.time(),
                }
                await ws.send(json.dumps(ack))

            elif action in ("offer", "answer", "ice_candidate"):
                cloudlog.info(f"WebRTC signaling exchange: {action}")
                # Forward or process SDP if using native WebRTC peer connection

        except Exception as e:
            cloudlog.error(f"Error handling signaling message: {e}")

    async def run(self):
        cloudlog.info("LiveStreamer signaling daemon started")
        ws_base = get_ws_url()
        uri = f"{ws_base}/webrtc/signaling/{self.device_id}"

        while True:
            if websockets is None:
                cloudlog.warning("websockets package not installed. Live streaming signaling idle.")
                await asyncio.sleep(10)
                continue

            try:
                cloudlog.info(f"Connecting live signaling WebSocket to {uri}...")
                async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                    cloudlog.info("Live signaling WebSocket connected")
                    async for message in ws:
                        await self.handle_signaling_message(ws, message)
            except Exception as e:
                cloudlog.debug(f"Live signaling connection error: {e}")
                self.params.put_bool("IsLiveStreaming", False)
                self.is_streaming = False
                await asyncio.sleep(5.0)


def main():
    daemon = LiveStreamerDaemon()
    asyncio.run(daemon.run())


if __name__ == "__main__":
    main()

