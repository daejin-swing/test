import socket
import unittest
from unittest.mock import patch

from ui.net_info import _ip_via_udp_connect
from ui.wifi import WifiManager


class TestWifiParsing(unittest.TestCase):
  def setUp(self):
    self.wifi = WifiManager()

  def test_parse_scan_output(self):
    raw = (
      "MyHomeWifi:78:WPA2\n"
      "OpenCafe:55:--\n"
      "\n"
      "Neighbor\\:Wifi:30:WPA2\n"
    )
    networks = self.wifi._parse_scan_output(raw)
    self.assertEqual(len(networks), 3)
    self.assertEqual(networks[0].ssid, "MyHomeWifi")
    self.assertEqual(networks[0].signal, 78)
    self.assertEqual(networks[0].security, "WPA2")
    self.assertEqual(networks[1].ssid, "OpenCafe")
    self.assertEqual(networks[1].security, "--")

  def test_parse_scan_output_skips_blank_ssid(self):
    raw = ":40:WPA2\nRealSSID:60:WPA2\n"
    networks = self.wifi._parse_scan_output(raw)
    self.assertEqual([n.ssid for n in networks], ["RealSSID"])

  def test_parse_saved_output(self):
    raw = (
      "MyHomeWifi:802-11-wireless\n"
      "eth0:802-3-ethernet\n"
      "OfficeWifi:802-11-wireless\n"
    )
    saved = self.wifi._parse_saved_output(raw)
    self.assertEqual(saved, ["MyHomeWifi", "OfficeWifi"])


class TestNetInfo(unittest.TestCase):
  def test_ip_via_udp_connect_real(self):
    ip = _ip_via_udp_connect()
    if ip is not None:
      socket.inet_aton(ip)  # raises if not a valid IPv4 address

  @patch("socket.socket")
  def test_ip_via_udp_connect_no_route(self, mock_socket_cls):
    mock_socket_cls.return_value.__enter__.side_effect = OSError("network unreachable")
    self.assertIsNone(_ip_via_udp_connect())


if __name__ == "__main__":
  unittest.main()
