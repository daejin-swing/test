import sys
import threading
import time
from dataclasses import dataclass, field

import pyray as rl

from common.log import cloudlog
from common.params import Params
from ui.net_info import get_ip_address
from ui.wifi import WifiManager, WifiNetwork

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
  debug_touch_count: int = 0
  debug_mouse_down: bool = False
  debug_pointer_pos: tuple[float, float] = (0.0, 0.0)
  screen_w: int = FALLBACK_WIDTH
  screen_h: int = FALLBACK_HEIGHT


def run_async(fn) -> None:
  threading.Thread(target=fn, daemon=True).start()


def clamp(value: float, lo: float, hi: float) -> float:
  return max(lo, min(hi, value))


def get_pointer(state: UiState) -> Pointer:
  # Touch and mouse are unified here: some raylib backends deliver touchscreen
  # taps only as touch events, not synthesized mouse clicks, so both are checked.
  touch_count = rl.get_touch_point_count()
  mouse_down = rl.is_mouse_button_down(rl.MOUSE_BUTTON_LEFT)

  if touch_count > 0:
    pos = rl.get_touch_position(0)
    down = True
  else:
    pos = rl.get_mouse_position()
    down = mouse_down

  state.debug_touch_count = touch_count
  state.debug_mouse_down = mouse_down
  state.debug_pointer_pos = (pos.x, pos.y)

  just_pressed = down and not state.pointer_was_down
  just_released = state.pointer_was_down and not down
  state.pointer_was_down = down
  return Pointer(x=pos.x, y=pos.y, down=down, just_pressed=just_pressed, just_released=just_released)


def draw_debug_overlay(state: UiState) -> None:
  text = (
    f"touch={state.debug_touch_count} "
    f"mouse_down={state.debug_mouse_down} "
    f"pos=({state.debug_pointer_pos[0]:.0f},{state.debug_pointer_pos[1]:.0f})"
  )
  rl.draw_text(text, 10, rl.get_screen_height() - 30, 20, rl.YELLOW)


def network_row_rect(index: int, scroll_offset: float) -> rl.Rectangle:
  return rl.Rectangle(LIST_X, LIST_Y + index * ROW_H - scroll_offset, LIST_W, ROW_H - 8)


def password_back_rect(state: UiState) -> rl.Rectangle:
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


def select_network(state: UiState, wifi: WifiManager, params: Params, net: WifiNetwork) -> None:
  state.selected_ssid = net.ssid
  state.password_buffer = ""
  is_open = net.security in ("", "--")
  if is_open or net.saved:
    # nmcli reconnects saved profiles without a password; open networks need none either
    start_connect(state, wifi, params, net.ssid, None)
  else:
    state.screen = Screen.PASSWORD


def handle_main_input(state: UiState, wifi: WifiManager, params: Params, pointer: Pointer) -> None:
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

  if rl.check_collision_point_rec(rl.Vector2(pointer.x, pointer.y), password_back_rect(state)):
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


def handle_input(state: UiState, wifi: WifiManager, params: Params) -> None:
  pointer = get_pointer(state)
  if state.screen == Screen.MAIN:
    handle_main_input(state, wifi, params, pointer)
  elif state.screen == Screen.PASSWORD:
    handle_password_input(state, wifi, params, pointer)
  elif state.screen == Screen.STATUS:
    handle_status_input(state, pointer)


def draw_main_screen(state: UiState) -> None:
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
  back = password_back_rect(state)
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


def main() -> None:
  params = Params()
  if params.get_bool("DisableUI"):
    cloudlog.warning("UI disabled by the DisableUI param")
    sys.exit(0)

  wifi = WifiManager()
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

    handle_input(state, wifi, params)

    rl.begin_drawing()
    rl.clear_background(rl.BLACK)
    if state.screen == Screen.MAIN:
      draw_main_screen(state)
    elif state.screen == Screen.PASSWORD:
      draw_password_screen(state)
    elif state.screen == Screen.STATUS:
      draw_status_screen(state)
    draw_debug_overlay(state)
    rl.end_drawing()

  rl.close_window()


if __name__ == "__main__":
  main()
