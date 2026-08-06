import sys
import threading
import time
from dataclasses import dataclass

import pyray as rl

from common.log import cloudlog
from common.params import Params
from ui.camera_feed import CameraFeed
from ui.net_info import get_ip_address
from ui.ssh_keys import install_keys_from_url
from ui.wifi import WifiManager, try_connect_from_qr

FALLBACK_WIDTH = 2160
FALLBACK_HEIGHT = 1080
IP_POLL_INTERVAL = 5.0


class Screen:
  MAIN = 0
  CAMERA = 1


class QrPurpose:
  WIFI = "wifi"
  SSH = "ssh"


class QrMode:
  SCANNING = "scanning"
  PROCESSING = "processing"
  INVALID = "invalid"


QR_INVALID_DISPLAY_S = 2.0


@dataclass
class Pointer:
  x: float
  y: float
  down: bool
  just_pressed: bool
  just_released: bool


@dataclass
class UiState:
  screen: int = Screen.MAIN
  ip: str | None = None
  connected_ssid: str | None = None
  wifi_status_busy: bool = False
  pointer_was_down: bool = False
  screen_w: int = FALLBACK_WIDTH
  screen_h: int = FALLBACK_HEIGHT
  camera_starting: bool = False
  camera_texture: object | None = None
  camera_texture_size: tuple[int, int] | None = None
  qr_purpose: str = QrPurpose.WIFI
  qr_mode: str = QrMode.SCANNING
  qr_frozen_frame: object | None = None
  qr_bbox: list[tuple[float, float]] | None = None
  qr_invalid_until: float = 0.0
  qr_connecting: bool = False


def run_async(fn) -> None:
  threading.Thread(target=fn, daemon=True).start()


def get_pointer(state: UiState) -> Pointer:
  # Touch and mouse are unified here: some raylib backends deliver touchscreen
  # taps only as touch events, not synthesized mouse clicks, so both are checked.
  if rl.get_touch_point_count() > 0:
    pos = rl.get_touch_position(0)
    down = True
  else:
    pos = rl.get_mouse_position()
    down = rl.is_mouse_button_down(rl.MOUSE_BUTTON_LEFT)

  just_pressed = down and not state.pointer_was_down
  just_released = state.pointer_was_down and not down
  state.pointer_was_down = down
  return Pointer(x=pos.x, y=pos.y, down=down, just_pressed=just_pressed, just_released=just_released)


# Every "< Back" button (shown one at a time, on Screen.CAMERA) uses this same
# top-right corner spot.
def top_right_button_rect(state: UiState) -> rl.Rectangle:
  return rl.Rectangle(state.screen_w - 220, 40, 180, 70)


# Screen.MAIN's two QR buttons stack in that same top-right corner.
def qr_button_rect(state: UiState, index: int) -> rl.Rectangle:
  return rl.Rectangle(state.screen_w - 260, 40 + index * 90, 220, 70)


def refresh_wifi_status(state: UiState, wifi: WifiManager, params: Params) -> None:
  ip = get_ip_address()
  if ip != state.ip:
    state.ip = ip
    params.put("IPAddress", ip or "", block=True)

  if state.wifi_status_busy:
    return
  state.wifi_status_busy = True

  def _do_check():
    state.connected_ssid = wifi.current_connection()
    state.wifi_status_busy = False

  run_async(_do_check)


def start_camera(state: UiState, camera: CameraFeed) -> None:
  if state.camera_starting or camera.client is not None:
    return
  state.camera_starting = True

  def _do_start():
    try:
      camera.start()
    except Exception:
      cloudlog.exception("camera_feed: start() raised")
    finally:
      state.camera_starting = False

  run_async(_do_start)


def reset_qr_scan(state: UiState, camera: CameraFeed) -> None:
  state.qr_mode = QrMode.SCANNING
  state.qr_frozen_frame = None
  state.qr_bbox = None
  camera.set_scan_enabled(True)


def start_qr_action(state: UiState, wifi: WifiManager, params: Params, camera: CameraFeed, qr_data: str) -> None:
  state.qr_connecting = True

  def _do_action():
    try:
      if state.qr_purpose == QrPurpose.WIFI:
        ok, msg = try_connect_from_qr(qr_data, wifi)
      else:
        ok, msg = install_keys_from_url(qr_data)
    except Exception:
      cloudlog.exception("wifi_ui: QR action raised")
      ok, msg = False, "internal error"

    state.qr_connecting = False
    if ok:
      if state.qr_purpose == QrPurpose.WIFI:
        params.put_bool("WifiConnected", True, block=True)
        refresh_wifi_status(state, wifi, params)
      # Only steal navigation if the user is still on the camera screen --
      # they may have already backed out while this action ran.
      if state.screen == Screen.CAMERA:
        reset_qr_scan(state, camera)
        state.screen = Screen.MAIN
    else:
      cloudlog.info(f"wifi_ui: QR action failed: {msg}")
      if state.screen == Screen.CAMERA:
        state.qr_mode = QrMode.INVALID
        state.qr_invalid_until = time.monotonic() + QR_INVALID_DISPLAY_S

  run_async(_do_action)


def handle_main_input(state: UiState, wifi: WifiManager, params: Params, camera: CameraFeed, pointer: Pointer) -> None:
  if not pointer.just_pressed:
    return
  pos = rl.Vector2(pointer.x, pointer.y)

  if rl.check_collision_point_rec(pos, qr_button_rect(state, 0)):
    state.qr_purpose = QrPurpose.WIFI
  elif rl.check_collision_point_rec(pos, qr_button_rect(state, 1)):
    state.qr_purpose = QrPurpose.SSH
  else:
    return

  state.screen = Screen.CAMERA
  reset_qr_scan(state, camera)
  start_camera(state, camera)


def handle_camera_input(state: UiState, camera: CameraFeed, pointer: Pointer) -> None:
  if pointer.just_pressed and rl.check_collision_point_rec(rl.Vector2(pointer.x, pointer.y), top_right_button_rect(state)):
    camera.set_scan_enabled(False)
    state.screen = Screen.MAIN


def update_camera_screen(state: UiState, wifi: WifiManager, params: Params, camera: CameraFeed) -> None:
  if state.qr_mode == QrMode.SCANNING:
    qr = camera.latest_qr
    if qr is not None and not state.qr_connecting:
      state.qr_frozen_frame = camera.get_latest_frame()
      state.qr_bbox = qr.points
      state.qr_mode = QrMode.PROCESSING
      camera.set_scan_enabled(False)
      start_qr_action(state, wifi, params, camera, qr.data)
  elif state.qr_mode == QrMode.INVALID:
    if time.monotonic() >= state.qr_invalid_until:
      reset_qr_scan(state, camera)


def handle_input(state: UiState, wifi: WifiManager, params: Params, camera: CameraFeed) -> None:
  pointer = get_pointer(state)
  if state.screen == Screen.MAIN:
    handle_main_input(state, wifi, params, camera, pointer)
  elif state.screen == Screen.CAMERA:
    handle_camera_input(state, camera, pointer)
    update_camera_screen(state, wifi, params, camera)


def draw_main_screen(state: UiState) -> None:
  wifi_btn = qr_button_rect(state, 0)
  rl.draw_rectangle(int(wifi_btn.x), int(wifi_btn.y), int(wifi_btn.width), int(wifi_btn.height), rl.DARKBLUE)
  rl.draw_text("Scan WiFi QR", int(wifi_btn.x) + 15, int(wifi_btn.y) + 20, 22, rl.WHITE)

  ssh_btn = qr_button_rect(state, 1)
  rl.draw_rectangle(int(ssh_btn.x), int(ssh_btn.y), int(ssh_btn.width), int(ssh_btn.height), rl.DARKBLUE)
  rl.draw_text("Add SSH Key", int(ssh_btn.x) + 15, int(ssh_btn.y) + 20, 22, rl.WHITE)

  rl.draw_text(f"IP: {state.ip or 'not connected'}", 40, 40, 40, rl.WHITE)
  status = f"Connected: {state.connected_ssid}" if state.connected_ssid else "Not connected"
  rl.draw_text(status, 40, 100, 24, rl.LIGHTGRAY)


def draw_camera_screen(state: UiState, camera: CameraFeed) -> None:
  if state.qr_mode == QrMode.INVALID:
    text = "INVALID CODE"
    font_size = 60
    text_w = rl.measure_text(text, font_size)
    rl.draw_text(text, int((state.screen_w - text_w) / 2), int(state.screen_h / 2 - font_size / 2), font_size, rl.RED)
    back = top_right_button_rect(state)
    rl.draw_rectangle(int(back.x), int(back.y), int(back.width), int(back.height), rl.MAROON)
    rl.draw_text("< Back", int(back.x) + 25, int(back.y) + 20, 24, rl.WHITE)
    return

  # While processing a detected QR, freeze on the exact frame it was found in
  # (and its bounding box) instead of continuing to show the live, scanning feed.
  if state.qr_mode == QrMode.PROCESSING:
    frame = state.qr_frozen_frame
    bbox = state.qr_bbox
  else:
    frame = camera.get_latest_frame()
    bbox = camera.latest_qr.points if camera.latest_qr is not None else None

  if frame is not None:
    h, w = frame.shape[0], frame.shape[1]
    if state.camera_texture is None or state.camera_texture_size != (w, h):
      if state.camera_texture is not None:
        rl.unload_texture(state.camera_texture)
      img = rl.Image(frame, w, h, 1, rl.PIXELFORMAT_UNCOMPRESSED_R8G8B8)
      state.camera_texture = rl.load_texture_from_image(img)
      state.camera_texture_size = (w, h)
    else:
      # rl.ffi.from_buffer() returns a __CDataFromBuf cdata that pyray's generic
      # wrapper doesn't recognize as cdata for void* params; cast it to a plain
      # pointer first so it's passed through as-is.
      pixels_ptr = rl.ffi.cast("void *", rl.ffi.from_buffer(frame))
      rl.update_texture(state.camera_texture, pixels_ptr)
    # draw_texture() is 1:1 pixel scale from the top-left, so a camera frame
    # larger than the screen would only show its top-left corner -- scale the
    # whole frame to fit the screen instead, preserving aspect ratio (letterboxed).
    tex = state.camera_texture
    scale = min(state.screen_w / tex.width, state.screen_h / tex.height)
    dest_w, dest_h = tex.width * scale, tex.height * scale
    dest_x, dest_y = (state.screen_w - dest_w) / 2, (state.screen_h - dest_h) / 2
    src_rec = rl.Rectangle(0, 0, tex.width, tex.height)
    dest_rec = rl.Rectangle(dest_x, dest_y, dest_w, dest_h)
    rl.draw_texture_pro(tex, src_rec, dest_rec, rl.Vector2(0, 0), 0, rl.WHITE)

    if bbox:
      # bbox points are in frame pixel coordinates; map them into the same
      # scaled/letterboxed space the video itself was just drawn into.
      pts = [(dest_x + px * scale, dest_y + py * scale) for px, py in bbox]
      for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        rl.draw_line_ex(rl.Vector2(x1, y1), rl.Vector2(x2, y2), 4, rl.GREEN)

    if state.qr_mode == QrMode.PROCESSING:
      rl.draw_text("connecting...", 40, 40, 32, rl.WHITE)
  elif state.camera_starting:
    rl.draw_text("starting camera...", 40, 40, 32, rl.LIGHTGRAY)
  else:
    rl.draw_text("camera feed not connected yet", 40, 40, 32, rl.LIGHTGRAY)

  back = top_right_button_rect(state)
  rl.draw_rectangle(int(back.x), int(back.y), int(back.width), int(back.height), rl.MAROON)
  rl.draw_text("< Back", int(back.x) + 25, int(back.y) + 20, 24, rl.WHITE)


def main() -> None:
  params = Params()
  if params.get_bool("DisableUI"):
    cloudlog.warning("UI disabled by the DisableUI param")
    sys.exit(0)

  wifi = WifiManager()
  camera = CameraFeed()
  state = UiState()

  rl.set_trace_log_level(rl.LOG_DEBUG)
  rl.init_window(0, 0, "wifi-setup")
  state.screen_w = rl.get_screen_width() or FALLBACK_WIDTH
  state.screen_h = rl.get_screen_height() or FALLBACK_HEIGHT
  rl.set_target_fps(15)

  refresh_wifi_status(state, wifi, params)
  last_ip_poll = time.monotonic()

  while not rl.window_should_close():
    rl.poll_input_events()
    now = time.monotonic()
    if now - last_ip_poll > IP_POLL_INTERVAL:
      refresh_wifi_status(state, wifi, params)
      last_ip_poll = now

    handle_input(state, wifi, params, camera)

    rl.begin_drawing()
    rl.clear_background(rl.BLACK)
    if state.screen == Screen.MAIN:
      draw_main_screen(state)
    elif state.screen == Screen.CAMERA:
      draw_camera_screen(state, camera)
    rl.end_drawing()

  camera.stop()
  rl.close_window()


if __name__ == "__main__":
  main()
