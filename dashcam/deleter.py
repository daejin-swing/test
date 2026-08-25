#!/usr/bin/env python3
import os
import time
import shutil
import threading
from pathlib import Path

from common.config import (
    MEDIA_ROOT,
    NORMAL_DIR,
    EVENTS_UPLOADED_DIR,
    DEFAULT_MIN_FREE_DISK_GB,
    DEFAULT_EVENT_MAX_FILES,
    ensure_directories,
)
from common.log import cloudlog
from common.params import Params


def get_disk_free_gb() -> float:
    try:
        target = MEDIA_ROOT if os.path.exists(MEDIA_ROOT) else "/"
        _, _, free = shutil.disk_usage(target)
        return free / (1024 ** 3)
    except Exception:
        return 999.0


def list_files_by_mtime(directory: str) -> list[str]:
    """Returns absolute file paths sorted from oldest to newest."""
    if not os.path.exists(directory):
        return []
    try:
        files = [
            os.path.join(directory, f)
            for f in os.listdir(directory)
            if not f.startswith(".")
        ]
        return sorted(files, key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0)
    except Exception as e:
        cloudlog.error(f"Error listing files in {directory}: {e}")
        return []


class DashcamDeleter:
    def __init__(self):
        ensure_directories()
        self.params = Params()
        self.exit_event = threading.Event()

    def delete_oldest_normal_file(self) -> bool:
        files = list_files_by_mtime(NORMAL_DIR)
        # Leave at least the current active segment (don't delete if only 1 file)
        if len(files) <= 1:
            return False

        oldest = files[0]
        try:
            cloudlog.info(f"Deleter removing old normal video: {oldest}")
            os.remove(oldest)
            return True
        except OSError as e:
            cloudlog.error(f"Failed to delete {oldest}: {e}")
            return False

    def delete_oldest_uploaded_event(self) -> bool:
        files = list_files_by_mtime(EVENTS_UPLOADED_DIR)
        if not files:
            return False

        oldest = files[0]
        try:
            cloudlog.info(f"Deleter removing uploaded event file: {oldest}")
            os.remove(oldest)
            # If it's an mp4, also remove the json metadata if exists
            if oldest.endswith(".mp4"):
                json_path = oldest + ".json"
                if os.path.exists(json_path):
                    os.remove(json_path)
            return True
        except OSError as e:
            cloudlog.error(f"Failed to delete {oldest}: {e}")
            return False

    def check_max_uploaded_events(self):
        max_files = int(self.params.get("EventMaxFiles") or DEFAULT_EVENT_MAX_FILES)
        files = [f for f in list_files_by_mtime(EVENTS_UPLOADED_DIR) if f.endswith(".mp4")]
        excess = len(files) - max_files
        for i in range(excess):
            self.delete_oldest_uploaded_event()

    def step(self):
        min_free_gb = float(self.params.get("MinFreeDiskGB") or DEFAULT_MIN_FREE_DISK_GB)
        free_gb = get_disk_free_gb()

        # 1. Clean excess uploaded events by count limit
        self.check_max_uploaded_events()

        # 2. Clean if disk space is below threshold
        while free_gb < min_free_gb:
            cloudlog.warning(f"Disk space low ({free_gb:.2f}GB < {min_free_gb:.2f}GB), cleaning oldest files")

            # Priority 1: Delete normal rolling recordings
            deleted = self.delete_oldest_normal_file()

            # Priority 2: Delete already uploaded event backups if still out of space
            if not deleted:
                deleted = self.delete_oldest_uploaded_event()

            if not deleted:
                cloudlog.error("Deleter has no more expendable files to delete!")
                break

            free_gb = get_disk_free_gb()

    def run(self):
        cloudlog.info("DashcamDeleter started")
        while not self.exit_event.is_set():
            self.step()
            self.exit_event.wait(15.0)


def main():
    deleter = DashcamDeleter()
    deleter.run()


if __name__ == "__main__":
    main()

