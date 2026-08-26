import sys
import threading
import time
from dataclasses import dataclass

import numpy as np

from common import BASEDIR
from common.log import cloudlog

# camerad itself is launched by launch.sh (see camerad/run_camerad.sh) as its own
# long-running process, same as updater/ota.py and ui/wifi_ui.py. This module only
# connects to the VisionIPC stream it publishes; it never starts/stops the daemon.
# msgq (specifically msgq.visionipc, which is fully self-contained and does not
# depend on cereal) is vendored at msgq_repo/msgq, exposed as `msgq` via the
# msgq -> msgq_repo/msgq symlink at the repo root (matching upstream openpilot's
# own convention for that submodule).


def yuv_to_rgb(y: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
  # ported verbatim from openpilot/system/camerad/snapshot.py
  ul = np.repeat(np.repeat(u, 2).reshape(u.shape[0], y.shape[1]), 2, axis=0).reshape(y.shape)
  vl = np.repeat(np.repeat(v, 2).reshape(v.shape[0], y.shape[1]), 2, axis=0).reshape(y.shape)

  yuv = np.dstack((y, ul, vl)).astype(np.int16)
  yuv[:, :, 1:] -= 128

  m = np.array([
    [1.00000, 1.00000, 1.00000],
    [0.00000, -0.39465, 2.03211],
    [1.13983, -0.58060, 0.00000],
  ])
  rgb = np.dot(yuv, m).clip(0, 255)
  return rgb.astype(np.uint8)


def extract_image(buf) -> np.ndarray:
  # ported verbatim from openpilot/system/camerad/snapshot.py (NV12 -> RGB)
  uv_height = ((buf.height // 2) + 15) // 16 * 16
  uv_plane_size = buf.stride * uv_height

  y = np.array(buf.data[:buf.uv_offset], dtype=np.uint8).reshape((-1, buf.stride))[:buf.height, :buf.width]
  uv_data = buf.data[buf.uv_offset:buf.uv_offset + uv_plane_size]
  u = np.array(uv_data[::2], dtype=np.uint8).reshape((-1, buf.stride // 2))[:buf.height // 2, :buf.width // 2]
  v = np.array(uv_data[1::2], dtype=np.uint8).reshape((-1, buf.stride // 2))[:buf.height // 2, :buf.width // 2]

  return np.ascontiguousarray(yuv_to_rgb(y, u, v))


@dataclass
class QrDetection:
  data: str
  points: list[tuple[float, float]]  # polygon corners, in frame pixel coordinates


def detect_qr(frame: np.ndarray) -> QrDetection | None:
  # lazy: keeps this module importable/testable without zxing-cpp installed.
  # zxing-cpp reads stylized QR codes (rounded/dot modules, dense grids like
  # phone wifi-share codes) far more reliably than cv2's built-in
  # QRCodeDetector, which only recognizes the classic square finder pattern.
  import zxingcpp


  barcodes = zxingcpp.read_barcodes(frame, formats=zxingcpp.BarcodeFormat.QRCode)
  if not barcodes:
    return None
  barcode = barcodes[0]
  pos = barcode.position
  points = [(float(pos.top_left.x), float(pos.top_left.y)),
            (float(pos.top_right.x), float(pos.top_right.y)),
            (float(pos.bottom_right.x), float(pos.bottom_right.y)),
            (float(pos.bottom_left.x), float(pos.bottom_left.y))]
  return QrDetection(data=barcode.text, points=points)


class CameraFeed:
  def __init__(self):
    self.client = None  # msgq.visionipc.VisionIpcClient once connected
    self.latest_frame: np.ndarray | None = None
    self.latest_qr: QrDetection | None = None
    self._scan_enabled = True
    self._thread: threading.Thread | None = None
    self._running = False

  def start(self, timeout_s: float = 15.0) -> bool:
    if self.client is not None:
      return True

    if BASEDIR not in sys.path:
      sys.path.insert(0, BASEDIR)
    from msgq.visionipc import VisionIpcClient, VisionStreamType

    #client = VisionIpcClient("camerad", VisionStreamType.VISION_STREAM_DRIVER, True)
    
    client = VisionIpcClient("camerad", VisionStreamType.VISION_STREAM_ROAD, True)
    deadline = time.monotonic() + timeout_s
    connected = False
    while time.monotonic() < deadline:
      if client.connect(False):
        connected = True
        break
      time.sleep(0.2)

    if not connected:
      cloudlog.warning("camera_feed: VisionIpcClient never connected (is camerad running?)")
      return False

    self.client = client
    self._running = True
    self._thread = threading.Thread(target=self._poll_loop, daemon=True)
    self._thread.start()
    return True

  def _poll_loop(self) -> None:
    while self._running:
      buf = self.client.recv(timeout_ms=200)
      if buf is None:
        continue

      # Decoding is slower than the frame rate, so more than one frame can pile
      # up in the queue between recv() calls -- drain to whatever is newest
      # right now instead of decoding (and displaying) a stale backlog frame.
      while True:
        newer = self.client.recv(timeout_ms=0)
        if newer is None:
          break
        buf = newer

      try:
        self.latest_frame = extract_image(buf)
      except Exception:
        cloudlog.exception("camera_feed: failed to decode frame")
        continue

      if not self._scan_enabled:
        self.latest_qr = None
        continue
      try:
        self.latest_qr = detect_qr(self.latest_frame)
      except Exception:
        cloudlog.exception("camera_feed: failed to run QR detection")

  def get_latest_frame(self) -> np.ndarray | None:
    return self.latest_frame

  def set_scan_enabled(self, enabled: bool) -> None:
    self._scan_enabled = enabled
    if not enabled:
      self.latest_qr = None

  def stop(self) -> None:
    self._running = False
    if self._thread is not None:
      self._thread.join(timeout=2)
      self._thread = None
    self.client = None
