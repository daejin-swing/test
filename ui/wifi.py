import re
import subprocess
from dataclasses import dataclass

from common.log import cloudlog


@dataclass
class WifiNetwork:
  ssid: str
  signal: int
  security: str
  saved: bool = False


class WifiManager:
  def scan(self, rescan: bool = True) -> list[WifiNetwork]:
    cmd = ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list"]
    if rescan:
      cmd += ["--rescan", "yes"]
    try:
      raw = subprocess.check_output(cmd, encoding="utf8", stderr=subprocess.STDOUT)
    except (subprocess.CalledProcessError, FileNotFoundError):
      cloudlog.exception("wifi.scan failed")
      return []

    saved = set(self.list_saved())
    networks = self._parse_scan_output(raw)
    for n in networks:
      n.saved = n.ssid in saved
    return networks

  def list_saved(self) -> list[str]:
    try:
      raw = subprocess.check_output(
        ["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"],
        encoding="utf8", stderr=subprocess.STDOUT,
      )
    except (subprocess.CalledProcessError, FileNotFoundError):
      cloudlog.exception("wifi.list_saved failed")
      return []
    return self._parse_saved_output(raw)

  def connect(self, ssid: str, password: str | None) -> tuple[bool, str]:
    cmd = ["nmcli", "dev", "wifi", "connect", ssid]
    if password:
      cmd += ["password", password]
    try:
      output = subprocess.check_output(cmd, encoding="utf8", stderr=subprocess.STDOUT)
      cloudlog.event("wifi connect succeeded", ssid=ssid)
      return True, output.strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
      output = e.output if isinstance(e, subprocess.CalledProcessError) else str(e)
      cloudlog.event("wifi connect failed", ssid=ssid, error=output)
      return False, output.strip()

  def forget(self, ssid: str) -> None:
    try:
      subprocess.check_output(["nmcli", "connection", "delete", ssid], encoding="utf8", stderr=subprocess.STDOUT)
    except (subprocess.CalledProcessError, FileNotFoundError):
      cloudlog.exception("wifi.forget failed")

  def current_connection(self) -> str | None:
    try:
      raw = subprocess.check_output(
        ["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"],
        encoding="utf8", stderr=subprocess.STDOUT,
      )
    except (subprocess.CalledProcessError, FileNotFoundError):
      cloudlog.exception("wifi.current_connection failed")
      return None
    for line in raw.splitlines():
      parts = line.split(":", 1)
      if len(parts) == 2 and parts[0] == "yes":
        return parts[1]
    return None

  def is_connected(self) -> bool:
    return self.current_connection() is not None

  def _parse_scan_output(self, raw: str) -> list[WifiNetwork]:
    networks = []
    for line in raw.splitlines():
      if not line.strip():
        continue
      parts = line.split(":")
      if len(parts) < 3:
        continue
      ssid, signal, security = parts[0], parts[1], ":".join(parts[2:])
      if not ssid:
        continue
      try:
        signal_val = int(signal)
      except ValueError:
        signal_val = 0
      networks.append(WifiNetwork(ssid=ssid, signal=signal_val, security=security))
    return networks

  def _parse_saved_output(self, raw: str) -> list[str]:
    names = []
    for line in raw.splitlines():
      if not line.strip():
        continue
      parts = line.split(":")
      if len(parts) < 2:
        continue
      name, conn_type = parts[0], parts[1]
      if conn_type == "802-11-wireless":
        names.append(name)
    return names


def parse_wifi_qr(data: str) -> tuple[str, str | None] | None:
  """Parse the standard WIFI:S:<ssid>;T:<WPA|WEP|nopass>;P:<password>;H:<bool>;;
  string phones generate for "share network" QR codes. Returns (ssid, password)
  or None if `data` isn't a recognizable WiFi QR code."""
  if not data.startswith("WIFI:"):
    return None

  body = data[len("WIFI:"):]
  fields: dict[str, str] = {}
  for part in re.split(r'(?<!\\);', body):
    if not part:
      continue
    m = re.match(r'^([A-Za-z]):(.*)$', part, re.S)
    if not m:
      continue
    key, value = m.group(1).upper(), m.group(2)
    fields[key] = re.sub(r'\\(.)', r'\1', value)  # unescape \; \, \: \\

  ssid = fields.get("S")
  if not ssid:
    return None
  security = fields.get("T", "").upper()
  password = fields.get("P") or None
  if security in ("", "NOPASS"):
    password = None
  return ssid, password


def try_connect_from_qr(qr_data: str, wifi: WifiManager) -> tuple[bool, str]:
  parsed = parse_wifi_qr(qr_data)
  if parsed is None:
    return False, "not a WiFi QR code"
  ssid, password = parsed
  return wifi.connect(ssid, password)
