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


class Screen:
  MAIN = 0
  PASSWORD = 1
  STATUS = 2


@dataclass
class UiState:
  screen: int = Screen.MAIN
  ip: str | None = None
  networks: list[WifiNetwork] = field(default_factory=list)
  selected_ssid: str | None = None
  password_buffer: str = ""
  status_message: str = ""
  busy: bool = False


def run_async(fn) -> None:
  threading.Thread(target=fn, daemon=True).start()


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


def handle_main_input(state: UiState, wifi: WifiManager, params: Params) -> None:
  if rl.is_mouse_button_pressed(rl.MOUSE_BUTTON_LEFT):
    mouse = rl.get_mouse_position()
    row_h = 60
    start_y = 160
    for i, net in enumerate(state.networks):
      row_rect = rl.Rectangle(40, start_y + i * row_h, 600, row_h - 8)
      if rl.check_collision_point_rec(mouse, row_rect):
        state.selected_ssid = net.ssid
        state.password_buffer = ""
        is_open = net.security in ("", "--")
        if is_open or net.saved:
          # nmcli reconnects saved profiles without a password; open networks need none either
          start_connect(state, wifi, params, net.ssid, None)
        else:
          state.screen = Screen.PASSWORD
        break


def handle_password_input(state: UiState, wifi: WifiManager, params: Params) -> None:
  key_w, key_h = 90, 90
  start_x, start_y = 40, 300
  for row_idx, row in enumerate(KEYBOARD_ROWS):
    for col_idx, ch in enumerate(row):
      key_rect = rl.Rectangle(start_x + col_idx * key_w, start_y + row_idx * key_h, key_w - 6, key_h - 6)
      if rl.is_mouse_button_pressed(rl.MOUSE_BUTTON_LEFT) and rl.check_collision_point_rec(rl.get_mouse_position(), key_rect):
        state.password_buffer += ch

  connect_rect = rl.Rectangle(start_x, start_y + len(KEYBOARD_ROWS) * key_h + 20, 300, 80)
  if rl.is_mouse_button_pressed(rl.MOUSE_BUTTON_LEFT) and rl.check_collision_point_rec(rl.get_mouse_position(), connect_rect):
    if state.selected_ssid is not None:
      start_connect(state, wifi, params, state.selected_ssid, state.password_buffer)


def handle_input(state: UiState, wifi: WifiManager, params: Params) -> None:
  if state.screen == Screen.MAIN:
    handle_main_input(state, wifi, params)
  elif state.screen == Screen.PASSWORD:
    handle_password_input(state, wifi, params)


def draw_main_screen(state: UiState) -> None:
  rl.draw_text(f"IP: {state.ip or 'not connected'}", 40, 40, 40, rl.WHITE)
  rl.draw_text("Networks (tap to connect):", 40, 100, 24, rl.LIGHTGRAY)
  row_h = 60
  start_y = 160
  for i, net in enumerate(state.networks):
    label = f"{net.ssid}  ({net.signal}%)  {'saved' if net.saved else net.security}"
    rl.draw_rectangle(40, start_y + i * row_h, 600, row_h - 8, rl.DARKGRAY)
    rl.draw_text(label, 50, start_y + i * row_h + 15, 20, rl.WHITE)


def draw_password_screen(state: UiState) -> None:
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
    if rl.is_mouse_button_pressed(rl.MOUSE_BUTTON_LEFT):
      state.screen = Screen.MAIN


def main() -> None:
  params = Params()
  if params.get_bool("DisableUI"):
    cloudlog.warning("UI disabled by the DisableUI param")
    sys.exit(0)

  wifi = WifiManager()
  state = UiState()

  rl.init_window(0, 0, "wifi-setup")
  width = rl.get_screen_width() or FALLBACK_WIDTH
  height = rl.get_screen_height() or FALLBACK_HEIGHT
  rl.set_target_fps(15)

  refresh_ip(state, params)
  refresh_networks(state, wifi)
  last_ip_poll = time.monotonic()
  last_network_poll = time.monotonic()

  while not rl.window_should_close():
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
    rl.end_drawing()

  rl.close_window()


if __name__ == "__main__":
  main()
