#!/usr/bin/env python3
import os
import sys
import time
import json
import socket
import shutil
import subprocess
import threading
from datetime import datetime, UTC
from pathlib import Path

from common import BASEDIR
from common.config import (
    MEDIA_ROOT,
    EVENTS_PENDING_DIR,
    EVENTS_UPLOADED_DIR,
    get_device_id,
    get_api_url,
    DEFAULT_HEARTBEAT_INTERVAL_SEC,
)
from common.log import cloudlog
from common.params import Params
from common.http_client import post_json


def get_default_ip() -> str:
    """Find the device's outbound IP address."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 53))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def get_git_info(path: str) -> tuple[str, str]:
    """Returns (branch, commit_hash)."""
    try:
        branch = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=path, encoding="utf-8").strip()
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path, encoding="utf-8").strip()
        return branch, commit
    except Exception:
        return "unknown", "unknown"


def get_system_metrics() -> dict:
    """Collects CPU, RAM, Disk, Temp, and Uptime metrics."""
    metrics = {
        "cpu_usage_pct": 0.0,
        "memory_used_mb": 0,
        "memory_total_mb": 0,
        "disk_free_gb": 0.0,
        "disk_total_gb": 0.0,
        "temperature_c": 0.0,
        "network_type": "wifi",
        "ip_address": get_default_ip(),
        "uptime_seconds": 0,
    }

    # Disk usage
    try:
        target_path = MEDIA_ROOT if os.path.exists(MEDIA_ROOT) else "/"
        total, used, free = shutil.disk_usage(target_path)
        metrics["disk_free_gb"] = round(free / (1024 ** 3), 2)
        metrics["disk_total_gb"] = round(total / (1024 ** 3), 2)
    except Exception:
        pass

    # Uptime & Memory (Linux /proc or cross-platform fallback)
    if os.path.exists("/proc/uptime"):
        try:
            with open("/proc/uptime") as f:
                metrics["uptime_seconds"] = int(float(f.readline().split()[0]))
        except Exception:
            pass

    if os.path.exists("/proc/meminfo"):
        try:
            mem = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        mem[parts[0].strip()] = int(parts[1].split()[0])
            total_kb = mem.get("MemTotal", 0)
            avail_kb = mem.get("MemAvailable", mem.get("MemFree", 0))
            metrics["memory_total_mb"] = total_kb // 1024
            metrics["memory_used_mb"] = (total_kb - avail_kb) // 1024
        except Exception:
            pass

    # Temperature
    thermal_zones = ["/sys/class/thermal/thermal_zone0/temp", "/sys/class/hwmon/hwmon0/temp1_input"]
    for tz in thermal_zones:
        if os.path.exists(tz):
            try:
                with open(tz) as f:
                    metrics["temperature_c"] = round(float(f.read().strip()) / 1000.0, 1)
                break
            except Exception:
                pass

    return metrics


class AgentDaemon:
    def __init__(self):
        self.device_id = get_device_id()
        self.params = Params()
        self.exit_event = threading.Event()

    def get_version(self) -> str:
        version_file = os.path.join(BASEDIR, "version")
        if os.path.exists(version_file):
            try:
                with open(version_file) as f:
                    return f.read().strip()
            except Exception:
                pass
        return "0.0.1"

    def count_files(self, directory: str) -> int:
        if not os.path.exists(directory):
            return 0
        try:
            return len([f for f in os.listdir(directory) if not f.startswith(".")])
        except Exception:
            return 0

    def collect_status_payload(self) -> dict:
        cur_branch, cur_commit = get_git_info(BASEDIR)
        applied_target = self.params.get("UpdaterTargetBranch") or cur_branch

        self.params.put("GitCommit", cur_commit)

        return {
            "timestamp": datetime.now(UTC).isoformat(),
            "software": {
                "version": self.get_version(),
                "git_branch": cur_branch,
                "git_commit": cur_commit,
                "applied_target_branch": applied_target,
                "updater_state": self.params.get("UpdaterState") or "idle",
                "last_update_time": str(self.params.get("LastUpdateTime") or ""),
            },
            "system": get_system_metrics(),
            "dashcam": {
                "recording": self.params.get_bool("DashcamRecording"),
                "pending_events_count": self.count_files(EVENTS_PENDING_DIR),
                "uploaded_events_count": self.count_files(EVENTS_UPLOADED_DIR),
            },
            "applied_config_version": int(self.params.get("AppliedConfigVersion") or 0),
        }

    def apply_server_config(self, config: dict, config_version: int):
        """Applies remote configurations from server to local Params."""
        if not config:
            return

        cloudlog.info(f"Applying new server config v{config_version}: {config}")

        # 1. Target Branch
        if "target_branch" in config:
            new_target = config["target_branch"]
            current_target = self.params.get("UpdaterTargetBranch")
            if new_target != current_target:
                cloudlog.info(f"Server changed UpdaterTargetBranch: {current_target} -> {new_target}")
                self.params.put("UpdaterTargetBranch", new_target)
                self.params.put_bool("UpdaterFetchAvailable", True)

        # 2. Storage Policies
        storage = config.get("storage", {})
        if "min_free_bytes_gb" in storage:
            self.params.put("MinFreeDiskGB", float(storage["min_free_bytes_gb"]))
        if "event_max_files" in storage:
            self.params.put("EventMaxFiles", int(storage["event_max_files"]))

        # 3. Logging Policies
        logging_cfg = config.get("logging", {})
        if "upload_interval_sec" in logging_cfg:
            self.params.put("LogUploadIntervalSec", int(logging_cfg["upload_interval_sec"]))

        # 4. Event Trigger Policies
        trigger_cfg = config.get("event_trigger", {})
        if "g_sensor_threshold" in trigger_cfg:
            self.params.put("GSensorThreshold", float(trigger_cfg["g_sensor_threshold"]))

        # 5. Stream Policies
        stream_cfg = config.get("stream", {})
        if "thumbnail_fps" in stream_cfg:
            self.params.put("ThumbnailFPS", float(stream_cfg["thumbnail_fps"]))

        self.params.put("AppliedConfigVersion", config_version)

    def heartbeat_step(self):
        payload = self.collect_status_payload()
        endpoint = f"{get_api_url()}/devices/{self.device_id}/heartbeat"
        headers = {
            "X-Device-Id": self.device_id,
        }

        try:
            status, data = post_json(endpoint, payload, headers=headers, timeout=5)
            if status == 200 and data:
                srv_cfg_ver = data.get("config_version", 0)
                cur_cfg_ver = int(self.params.get("AppliedConfigVersion") or 0)
                if srv_cfg_ver > cur_cfg_ver and "config" in data:
                    self.apply_server_config(data["config"], srv_cfg_ver)
            else:
                cloudlog.debug(f"Heartbeat responded with status: {status}")
        except Exception as e:
            cloudlog.debug(f"Heartbeat connection error: {e}")

    def run(self):
        cloudlog.info(f"AgentDaemon started for device [{self.device_id}]")
        while not self.exit_event.is_set():
            self.heartbeat_step()
            interval = float(self.params.get("HeartbeatIntervalSec") or DEFAULT_HEARTBEAT_INTERVAL_SEC)
            self.exit_event.wait(interval)


def main():
    agent = AgentDaemon()
    agent.run()


if __name__ == "__main__":
    main()

