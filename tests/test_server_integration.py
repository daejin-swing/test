#!/usr/bin/env python3
import os
import sys
import json
import time
import shutil
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from common.config import (
    LOG_ROOT,
    CRASH_LOG_ROOT,
    EVENTS_PENDING_DIR,
    EVENTS_UPLOADED_DIR,
    get_device_id,
    ensure_directories,
)
from common.params import Params
from common.log import save_crash_dump
from agent.agentd import AgentDaemon
from agent.log_sender import LogSender
from dashcam.uploader import DashcamUploader


class TestServerIntegration(unittest.TestCase):
    def setUp(self):
        self.params = Params()
        self.params.put("ServerApiUrl", "http://mock-server.local/api/v1")
        ensure_directories()

    @patch("agent.agentd.post_json")
    def test_01_agent_heartbeat_and_config_sync(self, mock_post_json):
        mock_post_json.return_value = (
            200,
            {
                "status": "success",
                "config_version": 42,
                "config": {
                    "target_branch": "v2.5-prod",
                    "storage": {"min_free_bytes_gb": 6.0},
                },
            },
        )

        agent = AgentDaemon()
        agent.heartbeat_step()

        self.assertTrue(mock_post_json.called)
        call_args = mock_post_json.call_args
        endpoint = call_args[0][0]
        payload = call_args[0][1]

        self.assertIn("/heartbeat", endpoint)
        self.assertIn("software", payload)
        self.assertIn("system", payload)

        # Verify config was updated in Params
        self.assertEqual(self.params.get("UpdaterTargetBranch"), "v2.5-prod")
        self.assertEqual(float(self.params.get("MinFreeDiskGB")), 6.0)
        self.assertEqual(int(self.params.get("AppliedConfigVersion")), 42)

    @patch("agent.log_sender.post_json")
    def test_02_crash_and_app_log_upload(self, mock_post_json):
        mock_post_json.return_value = (200, {"status": "accepted"})

        # 1. Create crash dump
        try:
            raise RuntimeError("Integration Crash Test")
        except Exception:
            exc_type, exc_val, exc_tb = sys.exc_info()
            save_crash_dump(exc_type, exc_val, exc_tb)

        sender = LogSender()
        sent = sender.send_crash_logs()
        self.assertTrue(sent)
        self.assertTrue(mock_post_json.called)

        # Verify crash file was removed after successful upload
        crash_files = [f for f in os.listdir(CRASH_LOG_ROOT) if f.endswith(".json")]
        self.assertEqual(len(crash_files), 0)

    @patch("dashcam.uploader.post_multipart")
    def test_03_dashcam_event_upload_and_directory_move(self, mock_post_multipart):
        mock_post_multipart.return_value = (201, '{"status": "uploaded"}')

        # Create a pending event file
        test_video = os.path.join(EVENTS_PENDING_DIR, "evt-123_video.mp4")
        test_meta = test_video + ".json"
        with open(test_video, "wb") as f:
            f.write(b"mock_mp4_binary_stream_data")
        with open(test_meta, "w") as f:
            json.dump({"event_id": "evt-123", "g_force": 2.1}, f)

        uploader = DashcamUploader()
        uploader.step()

        self.assertTrue(mock_post_multipart.called)

        # Check files were moved to uploaded directory
        self.assertFalse(os.path.exists(test_video))
        self.assertTrue(os.path.exists(os.path.join(EVENTS_UPLOADED_DIR, "evt-123_video.mp4")))
        self.assertTrue(os.path.exists(os.path.join(EVENTS_UPLOADED_DIR, "evt-123_video.mp4.json")))

        # Cleanup
        shutil.rmtree(EVENTS_UPLOADED_DIR)
        ensure_directories()


if __name__ == "__main__":
    unittest.main()

