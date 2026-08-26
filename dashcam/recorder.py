#!/usr/bin/env python3
import os
import sys
import time
import json
import shutil
import threading
from datetime import datetime, UTC
from pathlib import Path

try:
    import cv2
except ImportError:
    cv2 = None

try:
    import numpy as np
except ImportError:
    np = None

try:
    from msgq.visionipc import VisionIpcClient, VisionStreamType
except ImportError:
    VisionIpcClient = None
    VisionStreamType = None

from common.config import (
    NORMAL_DIR,
    EVENTS_PENDING_DIR,
    ensure_directories,
    get_device_id,
)
from common.log import cloudlog
from common.params import Params

SEGMENT_DURATION_SEC = 60  # 1-minute video segments
RECORDING_FPS = 20
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 720


class DashcamRecorder:
    def __init__(self):
        ensure_directories()
        self.params = Params()
        self.exit_event = threading.Event()
        self.current_writer = None
        self.current_filename = None
        self.previous_filename = None
        self.writer_resolution = None  # (width, height) currently opened by VideoWriter
        self.segment_start_time = 0.0

        self.width = DEFAULT_WIDTH
        self.height = DEFAULT_HEIGHT
        self.vipc_client = None

    def init_visionipc(self) -> bool:
        if VisionIpcClient is None:
            return False
        try:
            self.vipc_client = VisionIpcClient("camerad", VisionStreamType.VISION_STREAM_ROAD, True)
            if self.vipc_client.connect(False):
                # Update resolution if already ready
                if self.vipc_client.width and self.vipc_client.height:
                    self.width = int(self.vipc_client.width)
                    self.height = int(self.vipc_client.height)
                cloudlog.info(f"Connected to VisionIPC camerad ({self.width}x{self.height})")
                return True
        except Exception as e:
            cloudlog.debug(f"VisionIPC connection attempt failed: {e}")
        self.vipc_client = None
        return False

    def get_frame(self):
        """Fetch frame from VisionIPC, dynamically discovering actual camera resolution from incoming buffers."""
        if self.vipc_client and self.vipc_client.is_connected() and np is not None:
            buf = self.vipc_client.recv()
            if buf is not None:
                # 1. Dynamically read actual buffer dimensions from the frame itself
                buf_w = getattr(buf, "width", None) or self.vipc_client.width
                buf_h = getattr(buf, "height", None) or self.vipc_client.height
                buf_stride = getattr(buf, "stride", None) or buf_w or self.width

                if buf_w and buf_h:
                    buf_w = int(buf_w)
                    buf_h = int(buf_h)
                    # If actual camera resolution was just discovered or changed, adapt immediately!
                    if (buf_w != self.width or buf_h != self.height):
                        cloudlog.info(f"Dashcam detected actual camera resolution: {self.width}x{self.height} -> {buf_w}x{buf_h}")
                        self.width = buf_w
                        self.height = buf_h
                        # If VideoWriter was opened with initial default size, restart segment with actual size
                        if self.current_writer and self.writer_resolution != (self.width, self.height):
                            self.start_new_segment()

                if cv2 is not None:
                    try:
                        stride = int(buf_stride)
                        h = int(self.height)
                        w = int(self.width)
                        yuv = np.frombuffer(buf.data, dtype=np.uint8).reshape((h * 3 // 2, stride))
                        bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_NV12)
                        if stride > w:
                            bgr = bgr[:, :w]
                        return bgr
                    except Exception as e:
                        cloudlog.debug(f"YUV convert error in recorder: {e}")

        # Fallback dummy frame with timestamp for simulation/headless mode
        w = int(self.width if self.width else DEFAULT_WIDTH)
        h = int(self.height if self.height else DEFAULT_HEIGHT)

        # if np is not None:
        #     frame = np.zeros((h, w, 3), dtype=np.uint8)
        #     now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        #     if cv2 is not None:
        #         cv2.putText(frame, f"Dashcam Live [{now_str}]", (50, 80), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255, 255, 255), 2)
        #         cv2.putText(frame, f"Device: {get_device_id()}", (50, 140), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 0), 2)
        #         cv2.putText(frame, f"Res: {w}x{h}", (50, 200), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 200, 255), 2)
        #     return frame
        return None

    def start_new_segment(self):
        if self.current_writer:
            self.current_writer.release()
            self.previous_filename = self.current_filename

        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.current_filename = os.path.join(NORMAL_DIR, f"{timestamp_str}.mp4")
        self.segment_start_time = time.monotonic()

        w = int(self.width if self.width else DEFAULT_WIDTH)
        h = int(self.height if self.height else DEFAULT_HEIGHT)
        self.writer_resolution = (w, h)

        if cv2 is not None:
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            self.current_writer = cv2.VideoWriter(self.current_filename, fourcc, RECORDING_FPS, (w, h))
            cloudlog.info(f"Started new dashcam segment: {self.current_filename} ({w}x{h})")
        else:
            cloudlog.warning("OpenCV (cv2) not available; video recording is running in mock mode")

    def handle_event_trigger(self):
        """Copies current and previous video segments to events/pending when triggered."""
        if not self.params.get_bool("TriggerEvent"):
            return

        self.params.put_bool("TriggerEvent", False)
        event_id = f"evt-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
        cloudlog.warning(f"Event triggered! Preserving segments for event [{event_id}]")

        target_files = []
        if self.previous_filename and os.path.exists(self.previous_filename):
            target_files.append(self.previous_filename)
        if self.current_filename and os.path.exists(self.current_filename):
            target_files.append(self.current_filename)

        for src_path in target_files:
            base_name = os.path.basename(src_path)
            dst_video = os.path.join(EVENTS_PENDING_DIR, f"{event_id}_{base_name}")
            try:
                shutil.copy2(src_path, dst_video)
                metadata = {
                    "event_id": event_id,
                    "event_type": self.params.get("LastEventType") or "MANUAL_TRIGGER",
                    "occurred_at": datetime.now(UTC).isoformat(),
                    "speed_kph": float(self.params.get("VehicleSpeedKph") or 0.0),
                    "g_force": float(self.params.get("GSensorVal") or 1.0),
                    "video_filename": os.path.basename(dst_video),
                }
                meta_path = os.path.join(EVENTS_PENDING_DIR, f"{event_id}_{base_name}.json")
                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump(metadata, f, indent=2)
                cloudlog.info(f"Preserved event video: {dst_video}")
            except Exception as e:
                cloudlog.error(f"Failed to preserve event video {src_path}: {e}")

    def run(self):
        cloudlog.info("DashcamRecorder started")
        self.params.put_bool("DashcamRecording", True)
        self.init_visionipc()
        self.start_new_segment()

        frame_interval = 1.0 / RECORDING_FPS
        last_vipc_check = time.monotonic()

        while not self.exit_event.is_set():
            loop_start = time.monotonic()

            # Re-check VisionIPC connection periodically if disconnected
            if time.monotonic() - last_vipc_check > 5.0:
                if not self.vipc_client or not self.vipc_client.is_connected():
                    self.init_visionipc()
                last_vipc_check = time.monotonic()

            # 1. Capture and write frame
            frame = self.get_frame()
            if self.current_writer and frame is not None:
                self.current_writer.write(frame)

            # 2. Check for event preservation triggers
            self.handle_event_trigger()

            # 3. Check segment rollover (every 60 seconds)
            if time.monotonic() - self.segment_start_time >= SEGMENT_DURATION_SEC:
                self.start_new_segment()

            # Sleep to maintain FPS
            elapsed = time.monotonic() - loop_start
            sleep_time = max(0.001, frame_interval - elapsed)
            time.sleep(sleep_time)

        if self.current_writer:
            self.current_writer.release()
        self.params.put_bool("DashcamRecording", False)
        cloudlog.info("DashcamRecorder stopped")


def main():
    recorder = DashcamRecorder()
    try:
        recorder.run()
    except KeyboardInterrupt:
        recorder.exit_event.set()


if __name__ == "__main__":
    main()
