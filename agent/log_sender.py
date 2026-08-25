#!/usr/bin/env python3
import os
import sys
import time
import json
import threading
from pathlib import Path

from common.config import LOG_ROOT, CRASH_LOG_ROOT, get_device_id, get_api_url, DEFAULT_LOG_UPLOAD_INTERVAL_SEC
from common.log import cloudlog
from common.params import Params
from common.http_client import post_json

OFFSET_FILE = os.path.join(LOG_ROOT, ".log_offset")


class LogSender:
    def __init__(self):
        self.device_id = get_device_id()
        self.api_url = get_api_url()
        self.params = Params()
        self.exit_event = threading.Event()

    def get_headers(self) -> dict:
        return {
            "X-Device-Id": self.device_id,
        }

    def send_crash_logs(self) -> bool:
        """Finds and sends any crash dumps immediately."""
        if not os.path.exists(CRASH_LOG_ROOT):
            return False

        has_sent_any = False
        for fname in os.listdir(CRASH_LOG_ROOT):
            if not fname.endswith(".json"):
                continue

            fpath = os.path.join(CRASH_LOG_ROOT, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    crash_payload = json.load(f)

                endpoint = f"{get_api_url()}/devices/{self.device_id}/logs"
                status, resp = post_json(endpoint, crash_payload, headers=self.get_headers(), timeout=5)
                if status in (200, 201):
                    cloudlog.info(f"Crash log {fname} successfully uploaded")
                    os.remove(fpath)
                    has_sent_any = True
                else:
                    cloudlog.warning(f"Failed to upload crash log {fname}: HTTP {status}")
            except Exception as e:
                cloudlog.debug(f"Network error while sending crash log {fname}: {e}")

        return has_sent_any

    def send_app_logs(self):
        """Sends incremental records from app.log."""
        app_log_path = os.path.join(LOG_ROOT, "app.log")
        if not os.path.exists(app_log_path):
            return

        last_offset = 0
        if os.path.exists(OFFSET_FILE):
            try:
                with open(OFFSET_FILE, "r") as f:
                    last_offset = int(f.read().strip())
            except Exception:
                last_offset = 0

        current_size = os.path.getsize(app_log_path)
        if current_size < last_offset:
            # File was rotated/truncated
            last_offset = 0

        if current_size == last_offset:
            return

        entries = []
        new_offset = last_offset

        try:
            with open(app_log_path, "r", encoding="utf-8") as f:
                f.seek(last_offset)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        entries.append(entry)
                    except json.JSONDecodeError:
                        continue
                new_offset = f.tell()

            if entries:
                payload = {
                    "device_id": self.device_id,
                    "log_type": "app",
                    "entries": entries,
                }
                endpoint = f"{get_api_url()}/devices/{self.device_id}/logs"
                status, resp = post_json(endpoint, payload, headers=self.get_headers(), timeout=5)
                if status in (200, 201):
                    with open(OFFSET_FILE, "w") as f:
                        f.write(str(new_offset))
                else:
                    cloudlog.debug(f"Failed to upload app logs: HTTP {status}")
        except Exception as e:
            cloudlog.debug(f"Network error while sending app logs: {e}")

    def run(self):
        cloudlog.info("LogSender started")
        while not self.exit_event.is_set():
            # 1. First priority: Check crash logs
            self.send_crash_logs()

            # 2. Incremental app logs
            self.send_app_logs()

            interval = float(self.params.get("LogUploadIntervalSec") or DEFAULT_LOG_UPLOAD_INTERVAL_SEC)
            self.exit_event.wait(interval)


def main():
    sender = LogSender()
    sender.run()


if __name__ == "__main__":
    main()

