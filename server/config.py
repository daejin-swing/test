import os
from pathlib import Path

# Server Base Directory
BASE_DIR = Path(__file__).resolve().parent

# Network
HOST = os.getenv("SERVER_HOST", "0.0.0.0")
PORT = int(os.getenv("SERVER_PORT", "8000"))

# Storage Paths
STORAGE_DIR = BASE_DIR / "storage"
VIDEOS_DIR = STORAGE_DIR / "videos"
LOGS_DIR = STORAGE_DIR / "logs"
DB_PATH = STORAGE_DIR / "server.db"

# Ensure storage directories exist
os.makedirs(STORAGE_DIR, exist_ok=True)
os.makedirs(VIDEOS_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

# Default Device Configuration
DEFAULT_DEVICE_CONFIG = {
    "target_branch": "master",
    "force_update": False,
    "storage": {
        "min_free_bytes_gb": 5.0,
        "normal_retention_hours": 24,
        "event_max_files": 100,
        "delete_uploaded_events_first": True,
    },
    "logging": {
        "level": "INFO",
        "upload_interval_sec": 30,
        "max_log_size_mb": 10,
    },
    "event_trigger": {
        "g_sensor_threshold": 1.5,
        "hard_brake_mps2": -4.0,
        "pre_event_seconds": 20,
        "post_event_seconds": 10,
    },
    "stream": {
        "thumbnail_fps": 1.0,
        "thumbnail_quality": 40,
        "live_fps": 30,
        "live_bitrate_kbps": 2000,
    },
}
