import socket
import unittest
from unittest.mock import patch

from ui.net_info import _ip_via_udp_connect
from ui.wifi import WifiManager, parse_wifi_qr


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

  @patch("subprocess.check_output")
  def test_active_wifi_connection_name_ignores_non_prefixed_ssid(self, mock_check_output):
    # This device's wifi profiles are named "openpilot connection <ssid>", not
    # the bare ssid -- the active-connection lookup must return that real name
    # rather than assume it matches the ssid string.
    mock_check_output.return_value = (
      "lo:loopback\n"
      "lte:gsm\n"
      "openpilot connection SWING_GUEST:802-11-wireless\n"
    )
    self.assertEqual(self.wifi._active_wifi_connection_name(), "openpilot connection SWING_GUEST")

  @patch("subprocess.check_output")
  def test_active_wifi_connection_name_none_when_no_wifi_active(self, mock_check_output):
    mock_check_output.return_value = "lo:loopback\n"
    self.assertIsNone(self.wifi._active_wifi_connection_name())


class TestParseWifiQr(unittest.TestCase):
  def test_wpa_network(self):
    result = parse_wifi_qr("WIFI:S:MyHomeWifi;T:WPA;P:hunter2;;")
    self.assertEqual(result, ("MyHomeWifi", "hunter2"))

  def test_open_network(self):
    result = parse_wifi_qr("WIFI:S:OpenCafe;T:nopass;;")
    self.assertEqual(result, ("OpenCafe", None))

  def test_missing_type_treated_as_open(self):
    result = parse_wifi_qr("WIFI:S:OpenCafe;;")
    self.assertEqual(result, ("OpenCafe", None))

  def test_escaped_characters(self):
    result = parse_wifi_qr("WIFI:S:My\\;Home\\:Wifi;T:WPA;P:pass\\\\word;;")
    self.assertEqual(result, ("My;Home:Wifi", "pass\\word"))

  def test_fields_in_any_order(self):
    result = parse_wifi_qr("WIFI:P:hunter2;T:WPA;S:MyHomeWifi;;")
    self.assertEqual(result, ("MyHomeWifi", "hunter2"))

  def test_not_a_wifi_qr(self):
    self.assertIsNone(parse_wifi_qr("https://example.com"))

  def test_missing_ssid(self):
    self.assertIsNone(parse_wifi_qr("WIFI:T:WPA;P:hunter2;;"))


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
