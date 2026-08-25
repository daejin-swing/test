#!/usr/bin/env python3
import os
import sys
import time
import json
import shutil
import threading
from pathlib import Path

from common.config import (
    EVENTS_PENDING_DIR,
    EVENTS_UPLOADED_DIR,
    get_device_id,
    get_api_url,
    ensure_directories,
)
from common.log import cloudlog
from common.params import Params
from common.http_client import post_multipart


class DashcamUploader:
    def __init__(self):
        ensure_directories()
        self.device_id = get_device_id()
        self.params = Params()
        self.exit_event = threading.Event()

    def find_pending_events(self) -> list[str]:
        if not os.path.exists(EVENTS_PENDING_DIR):
            return []
        try:
            return sorted([
                f for f in os.listdir(EVENTS_PENDING_DIR)
                if f.endswith(".mp4") and not f.startswith(".")
            ])
        except Exception as e:
            cloudlog.error(f"Error listing pending events: {e}")
            return []

    def upload_event_file(self, video_fname: str) -> bool:
        video_path = os.path.join(EVENTS_PENDING_DIR, video_fname)
        meta_path = video_path + ".json"

        metadata = {}
        if os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    metadata = json.load(f)
            except Exception as e:
                cloudlog.error(f"Failed to read metadata for {video_fname}: {e}")

        endpoint = f"{get_api_url()}/devices/{self.device_id}/events"
        headers = {
            "X-Device-Id": self.device_id,
        }

        try:
            with open(video_path, "rb") as vf:
                video_data = vf.read()

            files = {
                "video_file": (video_fname, video_data, "video/mp4"),
            }
            fields = {
                "metadata": json.dumps(metadata),
            }

            cloudlog.info(f"Uploading event video {video_fname} to {endpoint}...")
            status, resp_text = post_multipart(endpoint, fields, files, headers=headers, timeout=60)

            if status in (200, 201):
                cloudlog.info(f"Successfully uploaded event video: {video_fname}")
                # Move to uploaded directory
                dst_video = os.path.join(EVENTS_UPLOADED_DIR, video_fname)
                shutil.move(video_path, dst_video)
                if os.path.exists(meta_path):
                    dst_meta = os.path.join(EVENTS_UPLOADED_DIR, os.path.basename(meta_path))
                    shutil.move(meta_path, dst_meta)
                return True
            else:
                cloudlog.warning(f"Server rejected event upload for {video_fname}: HTTP {status} - {resp_text}")
                return False
        except Exception as e:
            cloudlog.debug(f"Network error while uploading {video_fname}: {e}")
            return False

    def step(self):
        pending = self.find_pending_events()
        for video_fname in pending:
            success = self.upload_event_file(video_fname)
            if not success:
                # Stop batch if network fails to avoid fast hammering
                break

    def run(self):
        cloudlog.info("DashcamUploader started")
        while not self.exit_event.is_set():
            self.step()
            self.exit_event.wait(10.0)


def main():
    uploader = DashcamUploader()
    uploader.run()


if __name__ == "__main__":
    main()

