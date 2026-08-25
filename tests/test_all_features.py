#!/usr/bin/env python3
import os
import sys
import json
import time
import shutil
import unittest
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from common.config import (
    MEDIA_ROOT,
    LOG_ROOT,
    CRASH_LOG_ROOT,
    NORMAL_DIR,
    EVENTS_PENDING_DIR,
    EVENTS_UPLOADED_DIR,
    get_device_id,
    get_api_url,
    get_ws_url,
    ensure_directories,
)
from common.params import Params
from common.log import cloudlog, save_crash_dump
from agent.agentd import AgentDaemon, get_system_metrics
from dashcam.recorder import DashcamRecorder
from dashcam.deleter import DashcamDeleter, list_files_by_mtime


class TestConnectedDashcamAndAgent(unittest.TestCase):
    def setUp(self):
        self.params = Params()
        ensure_directories()

    def test_01_config_hierarchy(self):
        # Default
        device_id = get_device_id()
        self.assertTrue(len(device_id) > 0)

        # Override via params
        self.params.put("ServerApiUrl", "http://custom-api:9000/api/v1")
        self.assertEqual(get_api_url(), "http://custom-api:9000/api/v1")
        self.params.remove("ServerApiUrl")

    def test_02_logging_and_crash_handler(self):
        cloudlog.info("Test info message")
        app_log = os.path.join(LOG_ROOT, "app.log")
        self.assertTrue(os.path.exists(app_log))

        # Test crash dump generation
        try:
            raise ValueError("Test Crash Simulation")
        except Exception:
            exc_type, exc_val, exc_tb = sys.exc_info()
            save_crash_dump(exc_type, exc_val, exc_tb, thread_name="test_thread")

        crash_files = [f for f in os.listdir(CRASH_LOG_ROOT) if f.endswith(".json")]
        self.assertTrue(len(crash_files) > 0)

        # Verify crash payload structure
        crash_path = os.path.join(CRASH_LOG_ROOT, crash_files[0])
        with open(crash_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            self.assertEqual(data["log_type"], "crash")
            self.assertEqual(data["entries"][0]["level"], "CRITICAL")
            self.assertIn("Test Crash Simulation", data["entries"][0]["message"])

        # Cleanup crash file
        os.remove(crash_path)

    def test_03_agent_status_and_config_sync(self):
        agent = AgentDaemon()
        payload = agent.collect_status_payload()

        self.assertIn("software", payload)
        self.assertIn("system", payload)
        self.assertIn("dashcam", payload)
        self.assertIn("applied_config_version", payload)

        # Test applying remote server config
        test_remote_config = {
            "target_branch": "release-v2.0",
            "storage": {
                "min_free_bytes_gb": 4.5,
                "event_max_files": 50,
            },
            "event_trigger": {
                "g_sensor_threshold": 2.0,
            },
        }
        agent.apply_server_config(test_remote_config, config_version=10)

        self.assertEqual(self.params.get("UpdaterTargetBranch"), "release-v2.0")
        self.assertEqual(float(self.params.get("MinFreeDiskGB")), 4.5)
        self.assertEqual(int(self.params.get("EventMaxFiles")), 50)
        self.assertEqual(int(self.params.get("AppliedConfigVersion")), 10)

    def test_04_dashcam_event_preservation(self):
        recorder = DashcamRecorder()

        # Create dummy normal recordings
        f1 = os.path.join(NORMAL_DIR, "20260825_120000.mp4")
        f2 = os.path.join(NORMAL_DIR, "20260825_120100.mp4")
        with open(f1, "w") as f:
            f.write("dummy_video_data_1")
        with open(f2, "w") as f:
            f.write("dummy_video_data_2")

        recorder.previous_filename = f1
        recorder.current_filename = f2

        # Trigger event
        self.params.put_bool("TriggerEvent", True)
        self.params.put("LastEventType", "SHOCK_TEST")
        recorder.handle_event_trigger()

        # Check that events/pending received the files and metadata
        pending_files = os.listdir(EVENTS_PENDING_DIR)
        mp4_files = [f for f in pending_files if f.endswith(".mp4")]
        json_files = [f for f in pending_files if f.endswith(".json")]

        self.assertEqual(len(mp4_files), 2)
        self.assertEqual(len(json_files), 2)

        # Cleanup test files
        shutil.rmtree(NORMAL_DIR)
        shutil.rmtree(EVENTS_PENDING_DIR)
        ensure_directories()

    def test_05_deleter_policy(self):
        deleter = DashcamDeleter()

        # Create files
        f1 = os.path.join(NORMAL_DIR, "vid1.mp4")
        f2 = os.path.join(NORMAL_DIR, "vid2.mp4")
        with open(f1, "w") as f:
            f.write("test")
        time.sleep(0.01)
        with open(f2, "w") as f:
            f.write("test")

        # Deleter should remove oldest normal file (f1)
        deleted = deleter.delete_oldest_normal_file()
        self.assertTrue(deleted)
        self.assertFalse(os.path.exists(f1))
        self.assertTrue(os.path.exists(f2))

        # Cleanup
        shutil.rmtree(NORMAL_DIR)
        ensure_directories()


if __name__ == "__main__":
    unittest.main()

