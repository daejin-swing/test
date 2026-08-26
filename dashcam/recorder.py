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
    NORMAL_ROAD_DIR,
    NORMAL_WIDE_DIR,
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


class CameraRecorder:
    """Handles VisionIPC capture + segment rollover for a single camera stream."""

    def __init__(self, label: str, stream_type, output_dir: str):
        self.label = label
        self.stream_type = stream_type
        self.output_dir = output_dir

        self.vipc_client = None
        self.width = DEFAULT_WIDTH
        self.height = DEFAULT_HEIGHT

        self.current_writer = None
        self.current_filename = None
        self.previous_filename = None
        self.writer_resolution = None
        self.segment_start_time = 0.0

    def init_visionipc(self) -> bool:
        if VisionIpcClient is None:
            return False
        try:
            self.vipc_client = VisionIpcClient("camerad", self.stream_type, True)
            if self.vipc_client.connect(False):
                if self.vipc_client.width and self.vipc_client.height:
                    self.width = int(self.vipc_client.width)
                    self.height = int(self.vipc_client.height)
                cloudlog.info(f"Connected to {self.label} VisionIPC camerad ({self.width}x{self.height})")
                return True
        except Exception as e:
            cloudlog.debug(f"{self.label} VisionIPC connection attempt failed: {e}")
        self.vipc_client = None
        return False

    def get_frame(self):
        """Fetch frame from VisionIPC, dynamically discovering actual camera resolution from incoming buffers."""
        if self.vipc_client and self.vipc_client.is_connected() and np is not None:
            buf = self.vipc_client.recv()
            if buf is not None:
                buf_w = getattr(buf, "width", None) or self.vipc_client.width
                buf_h = getattr(buf, "height", None) or self.vipc_client.height
                buf_stride = getattr(buf, "stride", None) or buf_w or self.width

                if buf_w and buf_h:
                    buf_w = int(buf_w)
                    buf_h = int(buf_h)
                    if (buf_w != self.width or buf_h != self.height):
                        cloudlog.info(f"{self.label} detected actual camera resolution: {self.width}x{self.height} -> {buf_w}x{buf_h}")
                        self.width = buf_w
                        self.height = buf_h
                        if self.current_writer and self.writer_resolution != (self.width, self.height):
                            self.start_new_segment()

                if cv2 is not None:
                    try:
                        stride = int(buf_stride)
                        h = int(self.height)
                        w = int(self.width)
                        # Y and UV planes are separately row-aligned (32 / 16 rows) by the
                        # ISP, so they aren't simply back-to-back at stride*h -- use the
                        # buffer's own uv_offset to find the UV plane, same as
                        # ui/camera_feed.py's extract_image().
                        uv_offset = getattr(buf, "uv_offset", None) or (stride * h)
                        uv_height = ((h // 2) + 15) // 16 * 16
                        y = np.frombuffer(buf.data[:uv_offset], dtype=np.uint8).reshape(-1, stride)[:h]
                        uv = np.frombuffer(buf.data[uv_offset:uv_offset + stride * uv_height], dtype=np.uint8).reshape(-1, stride)[:h // 2]
                        yuv = np.vstack([y, uv])
                        bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_NV12)
                        if stride > w:
                            bgr = bgr[:, :w]
                        return bgr
                    except Exception as e:
                        cloudlog.debug(f"YUV convert error in {self.label} recorder: {e}")

        return None

    def start_new_segment(self):
        if self.current_writer:
            self.current_writer.release()
            self.previous_filename = self.current_filename

        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.current_filename = os.path.join(self.output_dir, f"{timestamp_str}.mp4")
        self.segment_start_time = time.monotonic()

        w = int(self.width if self.width else DEFAULT_WIDTH)
        h = int(self.height if self.height else DEFAULT_HEIGHT)
        self.writer_resolution = (w, h)

        if cv2 is not None:
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            self.current_writer = cv2.VideoWriter(self.current_filename, fourcc, RECORDING_FPS, (w, h))
            cloudlog.info(f"Started new {self.label} dashcam segment: {self.current_filename} ({w}x{h})")
        else:
            cloudlog.warning(f"OpenCV (cv2) not available; {self.label} recording is running in mock mode")

    def write_frame(self):
        frame = self.get_frame()
        if self.current_writer and frame is not None:
            self.current_writer.write(frame)

    def maybe_rollover(self):
        if time.monotonic() - self.segment_start_time >= SEGMENT_DURATION_SEC:
            self.start_new_segment()

    def close(self):
        if self.current_writer:
            self.current_writer.release()


class DashcamRecorder:
    def __init__(self):
        ensure_directories()
        self.params = Params()
        self.exit_event = threading.Event()

        self.cameras = []
        if VisionStreamType is not None:
            self.cameras.append(CameraRecorder("ROAD", VisionStreamType.VISION_STREAM_ROAD, NORMAL_ROAD_DIR))
            self.cameras.append(CameraRecorder("WIDE", VisionStreamType.VISION_STREAM_WIDE_ROAD, NORMAL_WIDE_DIR))
        else:
            self.cameras.append(CameraRecorder("ROAD", None, NORMAL_ROAD_DIR))
            self.cameras.append(CameraRecorder("WIDE", None, NORMAL_WIDE_DIR))

    def handle_event_trigger(self):
        """Copies current and previous video segments (for every camera) to events/pending when triggered."""
        if not self.params.get_bool("TriggerEvent"):
            return

        self.params.put_bool("TriggerEvent", False)
        event_id = f"evt-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
        cloudlog.warning(f"Event triggered! Preserving segments for event [{event_id}]")

        for cam in self.cameras:
            target_files = []
            if cam.previous_filename and os.path.exists(cam.previous_filename):
                target_files.append(cam.previous_filename)
            if cam.current_filename and os.path.exists(cam.current_filename):
                target_files.append(cam.current_filename)

            for src_path in target_files:
                base_name = os.path.basename(src_path)
                dst_video = os.path.join(EVENTS_PENDING_DIR, f"{event_id}_{cam.label.lower()}_{base_name}")
                try:
                    shutil.copy2(src_path, dst_video)
                    metadata = {
                        "event_id": event_id,
                        "camera": cam.label.lower(),
                        "event_type": self.params.get("LastEventType") or "MANUAL_TRIGGER",
                        "occurred_at": datetime.now(UTC).isoformat(),
                        "speed_kph": float(self.params.get("VehicleSpeedKph") or 0.0),
                        "g_force": float(self.params.get("GSensorVal") or 1.0),
                        "video_filename": os.path.basename(dst_video),
                    }
                    meta_path = os.path.join(EVENTS_PENDING_DIR, f"{event_id}_{cam.label.lower()}_{base_name}.json")
                    with open(meta_path, "w", encoding="utf-8") as f:
                        json.dump(metadata, f, indent=2)
                    cloudlog.info(f"Preserved event video: {dst_video}")
                except Exception as e:
                    cloudlog.error(f"Failed to preserve event video {src_path}: {e}")

    def run(self):
        cloudlog.info("DashcamRecorder started")
        self.params.put_bool("DashcamRecording", True)
        for cam in self.cameras:
            cam.init_visionipc()
            cam.start_new_segment()

        frame_interval = 1.0 / RECORDING_FPS
        last_vipc_check = time.monotonic()

        while not self.exit_event.is_set():
            loop_start = time.monotonic()

            # Re-check VisionIPC connections periodically if disconnected
            if time.monotonic() - last_vipc_check > 5.0:
                for cam in self.cameras:
                    if not cam.vipc_client or not cam.vipc_client.is_connected():
                        cam.init_visionipc()
                last_vipc_check = time.monotonic()

            # 1. Capture and write frames for every camera
            for cam in self.cameras:
                cam.write_frame()

            # 2. Check for event preservation triggers
            self.handle_event_trigger()

            # 3. Check segment rollover (every 60 seconds)
            for cam in self.cameras:
                cam.maybe_rollover()

            # Sleep to maintain FPS
            elapsed = time.monotonic() - loop_start
            sleep_time = max(0.001, frame_interval - elapsed)
            time.sleep(sleep_time)

        for cam in self.cameras:
            cam.close()
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
