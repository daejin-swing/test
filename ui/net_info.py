import fcntl
import os
import socket
import struct

SIOCGIFADDR = 0x8915


def _ip_via_udp_connect() -> str | None:
  try:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
      s.connect(("8.8.8.8", 80))
      return s.getsockname()[0]
  except OSError:
    return None


def _ip_via_ioctl(ifname: str) -> str | None:
  try:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
      packed = struct.pack('256s', ifname[:15].encode())
      res = fcntl.ioctl(s.fileno(), SIOCGIFADDR, packed)
      return socket.inet_ntoa(res[20:24])
  except OSError:
    return None


def get_ip_address(preferred_ifaces: tuple[str, ...] = ("wlan0", "eth0")) -> str | None:
  ip = _ip_via_udp_connect()
  if ip is not None:
    return ip

  for ifname in preferred_ifaces:
    if not os.path.isdir(f"/sys/class/net/{ifname}"):
      continue
    ip = _ip_via_ioctl(ifname)
    if ip is not None:
      return ip

  return None
