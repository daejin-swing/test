import sys
import threading
import time
from dataclasses import dataclass, field

import pyray as rl

from common.log import cloudlog
from common.params import Params
from ui.camera_feed import CameraFeed
from ui.net_info import get_ip_address
from ui.wifi import WifiManager, WifiNetwork, try_connect_from_qr

FALLBACK_WIDTH = 2160
FALLBACK_HEIGHT = 1080
IP_POLL_INTERVAL = 5.0
NETWORK_POLL_INTERVAL = 15.0

KEYBOARD_ROWS = [
  "1234567890",
  "qwertyuiop",
  "asdfghjkl",
  "zxcvbnm",
]

LIST_X, LIST_Y, LIST_W, LIST_H = 40, 160, 600, 420
ROW_H = 60
DRAG_THRESHOLD = 12  # px of vertical movement before a press is treated as a scroll, not a tap
WHEEL_SCROLL_SPEED = 40


class Screen:
  MAIN = 0
  PASSWORD = 1
  STATUS = 2
  CAMERA = 3


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
  networks: list[WifiNetwork] = field(default_factory=list)
  selected_ssid: str | None = None
  password_buffer: str = ""
  status_message: str = ""
  busy: bool = False
  scroll_offset: float = 0.0
  pointer_was_down: bool = False
  drag_start: tuple[float, float] | None = None
  drag_start_scroll: float = 0.0
  dragging: bool = False
  screen_w: int = FALLBACK_WIDTH
  screen_h: int = FALLBACK_HEIGHT
  camera_starting: bool = False
  camera_texture: object | None = None
  camera_texture_size: tuple[int, int] | None = None
  qr_mode: str = QrMode.SCANNING
  qr_frozen_frame: object | None = None
  qr_bbox: list[tuple[float, float]] | None = None
  qr_invalid_until: float = 0.0
  qr_connecting: bool = False


def run_async(fn) -> None:
  threading.Thread(target=fn, daemon=True).start()


def clamp(value: float, lo: float, hi: float) -> float:
  return max(lo, min(hi, value))


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


def network_row_rect(index: int, scroll_offset: float) -> rl.Rectangle:
  return rl.Rectangle(LIST_X, LIST_Y + index * ROW_H - scroll_offset, LIST_W, ROW_H - 8)


# Both the "QR 접속" button (on Screen.MAIN) and every "< Back" button share this
# same top-right corner spot, since only one of them is ever on screen at a time.
def top_right_button_rect(state: UiState) -> rl.Rectangle:
  return rl.Rectangle(state.screen_w - 220, 40, 180, 70)


def max_scroll(state: UiState) -> float:
  content_h = len(state.networks) * ROW_H
  return max(0.0, content_h - LIST_H)


def refresh_ip(state: UiState, params: Params) -> None:
  ip = get_ip_address()
  if ip != state.ip:
    state.ip = ip
    params.put("IPAddress", ip or "", block=True)


def refresh_networks(state: UiState, wifi: WifiManager) -> None:
  if state.busy:
    return
  state.busy = True

  def _do_scan():
    networks = wifi.scan()
    state.networks = networks
    state.scroll_offset = clamp(state.scroll_offset, 0, max_scroll(state))
    state.busy = False

  run_async(_do_scan)


def start_connect(state: UiState, wifi: WifiManager, params: Params, ssid: str, password: str | None) -> None:
  state.busy = True
  state.screen = Screen.STATUS
  state.status_message = f"connecting to {ssid}..."

  def _do_connect():
    ok, msg = wifi.connect(ssid, password)
    state.status_message = "connected" if ok else f"failed: {msg}"
    params.put_bool("WifiConnected", ok, block=True)
    state.busy = False

  run_async(_do_connect)


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


def start_qr_connect(state: UiState, wifi: WifiManager, params: Params, camera: CameraFeed, qr_data: str) -> None:
  state.qr_connecting = True

  def _do_connect():
    try:
      ok, msg = try_connect_from_qr(qr_data, wifi)
    except Exception:
      cloudlog.exception("wifi_ui: try_connect_from_qr raised")
      ok, msg = False, "internal error"

    state.qr_connecting = False
    if ok:
      params.put_bool("WifiConnected", True, block=True)
      refresh_ip(state, params)
      # Only steal navigation if the user is still on the camera screen --
      # they may have already backed out while this connect attempt ran.
      if state.screen == Screen.CAMERA:
        reset_qr_scan(state, camera)
        state.screen = Screen.MAIN
    else:
      cloudlog.info(f"wifi_ui: QR connect failed: {msg}")
      if state.screen == Screen.CAMERA:
        state.qr_mode = QrMode.INVALID
        state.qr_invalid_until = time.monotonic() + QR_INVALID_DISPLAY_S

  run_async(_do_connect)


def select_network(state: UiState, wifi: WifiManager, params: Params, net: WifiNetwork) -> None:
  state.selected_ssid = net.ssid
  state.password_buffer = ""
  is_open = net.security in ("", "--")
  if is_open or net.saved:
    # nmcli reconnects saved profiles without a password; open networks need none either
    start_connect(state, wifi, params, net.ssid, None)
  else:
    state.screen = Screen.PASSWORD


def handle_main_input(state: UiState, wifi: WifiManager, params: Params, camera: CameraFeed, pointer: Pointer) -> None:
  if pointer.just_pressed and rl.check_collision_point_rec(rl.Vector2(pointer.x, pointer.y), top_right_button_rect(state)):
    state.screen = Screen.CAMERA
    state.drag_start = None
    reset_qr_scan(state, camera)
    start_camera(state, camera)
    return

  wheel = rl.get_mouse_wheel_move()
  if wheel:
    state.scroll_offset = clamp(state.scroll_offset - wheel * WHEEL_SCROLL_SPEED, 0, max_scroll(state))

  if pointer.just_pressed:
    state.drag_start = (pointer.x, pointer.y)
    state.drag_start_scroll = state.scroll_offset
    state.dragging = False
  elif pointer.down and state.drag_start is not None:
    dy = pointer.y - state.drag_start[1]
    if abs(dy) > DRAG_THRESHOLD:
      state.dragging = True
      state.scroll_offset = clamp(state.drag_start_scroll - dy, 0, max_scroll(state))
  elif pointer.just_released:
    if state.drag_start is not None and not state.dragging:
      if LIST_Y <= pointer.y <= LIST_Y + LIST_H:
        for i, net in enumerate(state.networks):
          row_rect = network_row_rect(i, state.scroll_offset)
          if rl.check_collision_point_rec(rl.Vector2(pointer.x, pointer.y), row_rect):
            select_network(state, wifi, params, net)
            break
    state.drag_start = None
    state.dragging = False


def handle_password_input(state: UiState, wifi: WifiManager, params: Params, pointer: Pointer) -> None:
  if not pointer.just_pressed:
    return

  if rl.check_collision_point_rec(rl.Vector2(pointer.x, pointer.y), top_right_button_rect(state)):
    state.screen = Screen.MAIN
    return

  key_w, key_h = 90, 90
  start_x, start_y = 40, 300
  for row_idx, row in enumerate(KEYBOARD_ROWS):
    for col_idx, ch in enumerate(row):
      key_rect = rl.Rectangle(start_x + col_idx * key_w, start_y + row_idx * key_h, key_w - 6, key_h - 6)
      if rl.check_collision_point_rec(rl.Vector2(pointer.x, pointer.y), key_rect):
        state.password_buffer += ch
        return

  connect_rect = rl.Rectangle(start_x, start_y + len(KEYBOARD_ROWS) * key_h + 20, 300, 80)
  if rl.check_collision_point_rec(rl.Vector2(pointer.x, pointer.y), connect_rect):
    if state.selected_ssid is not None:
      start_connect(state, wifi, params, state.selected_ssid, state.password_buffer)


def handle_status_input(state: UiState, pointer: Pointer) -> None:
  if not state.busy and pointer.just_pressed:
    state.screen = Screen.MAIN


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
      start_qr_connect(state, wifi, params, camera, qr.data)
  elif state.qr_mode == QrMode.INVALID:
    if time.monotonic() >= state.qr_invalid_until:
      reset_qr_scan(state, camera)


def handle_input(state: UiState, wifi: WifiManager, params: Params, camera: CameraFeed) -> None:
  pointer = get_pointer(state)
  if state.screen == Screen.MAIN:
    handle_main_input(state, wifi, params, camera, pointer)
  elif state.screen == Screen.PASSWORD:
    handle_password_input(state, wifi, params, pointer)
  elif state.screen == Screen.STATUS:
    handle_status_input(state, pointer)
  elif state.screen == Screen.CAMERA:
    handle_camera_input(state, camera, pointer)
    update_camera_screen(state, wifi, params, camera)


def draw_main_screen(state: UiState) -> None:
  qr = top_right_button_rect(state)
  rl.draw_rectangle(int(qr.x), int(qr.y), int(qr.width), int(qr.height), rl.DARKBLUE)
  rl.draw_text("QR 접속", int(qr.x) + 35, int(qr.y) + 20, 24, rl.WHITE)

  rl.draw_text(f"IP: {state.ip or 'not connected'}", 40, 40, 40, rl.WHITE)
  rl.draw_text("Networks (tap to connect, drag to scroll):", 40, 100, 24, rl.LIGHTGRAY)

  rl.begin_scissor_mode(LIST_X, LIST_Y, LIST_W, LIST_H)
  for i, net in enumerate(state.networks):
    rect = network_row_rect(i, state.scroll_offset)
    if rect.y + rect.height < LIST_Y or rect.y > LIST_Y + LIST_H:
      continue
    label = f"{net.ssid}  ({net.signal}%)  {'saved' if net.saved else net.security}"
    rl.draw_rectangle(int(rect.x), int(rect.y), int(rect.width), int(rect.height), rl.DARKGRAY)
    rl.draw_text(label, int(rect.x) + 10, int(rect.y) + 15, 20, rl.WHITE)
  rl.end_scissor_mode()


def draw_password_screen(state: UiState) -> None:
  back = top_right_button_rect(state)
  rl.draw_rectangle(int(back.x), int(back.y), int(back.width), int(back.height), rl.MAROON)
  rl.draw_text("< Back", int(back.x) + 25, int(back.y) + 20, 24, rl.WHITE)

  rl.draw_text(f"SSID: {state.selected_ssid}", 40, 40, 32, rl.WHITE)
  rl.draw_text(f"Password: {'*' * len(state.password_buffer)}", 40, 100, 28, rl.LIGHTGRAY)

  key_w, key_h = 90, 90
  start_x, start_y = 40, 300
  for row_idx, row in enumerate(KEYBOARD_ROWS):
    for col_idx, ch in enumerate(row):
      x = start_x + col_idx * key_w
      y = start_y + row_idx * key_h
      rl.draw_rectangle(x, y, key_w - 6, key_h - 6, rl.DARKGRAY)
      rl.draw_text(ch, x + 30, y + 25, 24, rl.WHITE)

  connect_y = start_y + len(KEYBOARD_ROWS) * key_h + 20
  rl.draw_rectangle(start_x, connect_y, 300, 80, rl.DARKGREEN)
  rl.draw_text("Connect", start_x + 80, connect_y + 25, 28, rl.WHITE)


def draw_status_screen(state: UiState) -> None:
  rl.draw_text(state.status_message, 40, 40, 32, rl.WHITE)
  if not state.busy:
    rl.draw_text("tap anywhere to go back", 40, 100, 20, rl.LIGHTGRAY)


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

  refresh_ip(state, params)
  refresh_networks(state, wifi)
  last_ip_poll = time.monotonic()
  last_network_poll = time.monotonic()

  while not rl.window_should_close():
    rl.poll_input_events()
    now = time.monotonic()
    if now - last_ip_poll > IP_POLL_INTERVAL:
      refresh_ip(state, params)
      last_ip_poll = now
    if now - last_network_poll > NETWORK_POLL_INTERVAL and state.screen == Screen.MAIN:
      refresh_networks(state, wifi)
      last_network_poll = now

    handle_input(state, wifi, params, camera)

    rl.begin_drawing()
    rl.clear_background(rl.BLACK)
    if state.screen == Screen.MAIN:
      draw_main_screen(state)
    elif state.screen == Screen.PASSWORD:
      draw_password_screen(state)
    elif state.screen == Screen.STATUS:
      draw_status_screen(state)
    elif state.screen == Screen.CAMERA:
      draw_camera_screen(state, camera)
    rl.end_drawing()

  camera.stop()
  rl.close_window()


if __name__ == "__main__":
  main()
