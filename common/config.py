import os
import uuid
from pathlib import Path
from common import BASEDIR
from common.params import Params

# --- Git Default Configurations (Edit here in repository) ---
DEFAULT_SERVER_API_URL = "http://127.0.0.1:8000/api/v1"
DEFAULT_SERVER_WS_URL = "ws://127.0.0.1:8000/ws/v1"

DEFAULT_HEARTBEAT_INTERVAL_SEC = 30
DEFAULT_LOG_UPLOAD_INTERVAL_SEC = 30
DEFAULT_MIN_FREE_DISK_GB = 5.0
DEFAULT_NORMAL_MAX_STORAGE_GB = 20.0
DEFAULT_EVENT_MAX_FILES = 100

DEFAULT_THUMBNAIL_FPS = 1.0
DEFAULT_THUMBNAIL_QUALITY = 40

# --- Directory Constants ---
def get_default_media_root() -> str:
    if os.getenv("MEDIA_ROOT"):
        return os.getenv("MEDIA_ROOT")
    if os.path.exists("/data") and os.access("/data", os.W_OK):
        return "/data/media"
    return os.path.join(BASEDIR, ".data", "media")

MEDIA_ROOT = get_default_media_root()
LOG_ROOT = os.path.join(MEDIA_ROOT, "logs")
CRASH_LOG_ROOT = os.path.join(LOG_ROOT, "crash")

DASHCAM_ROOT = os.path.join(MEDIA_ROOT, "dashcam")
NORMAL_DIR = os.path.join(DASHCAM_ROOT, "normal")
EVENTS_DIR = os.path.join(DASHCAM_ROOT, "events")
EVENTS_PENDING_DIR = os.path.join(EVENTS_DIR, "pending")
EVENTS_UPLOADED_DIR = os.path.join(EVENTS_DIR, "uploaded")


def ensure_directories():
    """Ensure all required media and log directories exist."""
    for d in [LOG_ROOT, CRASH_LOG_ROOT, NORMAL_DIR, EVENTS_PENDING_DIR, EVENTS_UPLOADED_DIR]:
        os.makedirs(d, exist_ok=True)


def get_device_id() -> str:
    """Returns the unique device ID (DongleId), creating one if not present."""
    params = Params()
    dongle_id = params.get("DongleId")
    if not dongle_id:
        dongle_id = f"device-{uuid.uuid4().hex[:12]}"
        params.put("DongleId", dongle_id)
    return str(dongle_id)


def get_api_url() -> str:
    """Hierarchy: ENV -> Params -> Git Default"""
    params = Params()
    return os.getenv("SERVER_API_URL") or params.get("ServerApiUrl") or DEFAULT_SERVER_API_URL


def get_ws_url() -> str:
    """Hierarchy: ENV -> Params -> Git Default"""
    params = Params()
    return os.getenv("SERVER_WS_URL") or params.get("ServerWsUrl") or DEFAULT_SERVER_WS_URL

