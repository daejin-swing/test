import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from ui.ssh_keys import install_keys_from_url, is_valid_key_url


class TestIsValidKeyUrl(unittest.TestCase):
  def test_https_url(self):
    self.assertTrue(is_valid_key_url("https://github.com/someuser.keys"))

  def test_http_url(self):
    self.assertTrue(is_valid_key_url("http://example.com/keys"))

  def test_wifi_qr_is_not_a_url(self):
    self.assertFalse(is_valid_key_url("WIFI:S:MyHomeWifi;T:WPA;P:hunter2;;"))

  def test_other_scheme_rejected(self):
    self.assertFalse(is_valid_key_url("ftp://example.com/keys"))


def _mock_response(body: str):
  resp = MagicMock()
  resp.read.return_value = body.encode("utf-8")
  resp.__enter__.return_value = resp
  resp.__exit__.return_value = False
  return resp


class TestInstallKeysFromUrl(unittest.TestCase):
  def setUp(self):
    self.tmpdir = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmpdir.cleanup)
    self.keys_path = os.path.join(self.tmpdir.name, ".ssh", "authorized_keys")

  def test_rejects_non_url(self):
    ok, msg = install_keys_from_url("WIFI:S:foo;;", authorized_keys_path=self.keys_path)
    self.assertFalse(ok)
    self.assertFalse(os.path.exists(self.keys_path))

  @patch("urllib.request.urlopen")
  def test_installs_only_valid_key_lines(self, mock_urlopen):
    body = "ssh-ed25519 AAAAC3abc user@host\n<html>404 not found</html>\nssh-rsa AAAAB3xyz other@host\n"
    mock_urlopen.return_value = _mock_response(body)

    ok, msg = install_keys_from_url("https://github.com/someuser.keys", authorized_keys_path=self.keys_path)
    self.assertTrue(ok)
    with open(self.keys_path) as f:
      lines = [line.strip() for line in f if line.strip()]
    self.assertEqual(lines, ["ssh-ed25519 AAAAC3abc user@host", "ssh-rsa AAAAB3xyz other@host"])

  @patch("urllib.request.urlopen")
  def test_no_valid_keys_in_body(self, mock_urlopen):
    mock_urlopen.return_value = _mock_response("<html>404 not found</html>\n")
    ok, msg = install_keys_from_url("https://github.com/someuser.keys", authorized_keys_path=self.keys_path)
    self.assertFalse(ok)
    self.assertFalse(os.path.exists(self.keys_path))

  @patch("urllib.request.urlopen")
  def test_keeps_existing_keys_and_dedups_on_rerun(self, mock_urlopen):
    body = "ssh-ed25519 AAAAC3abc user@host\n"
    mock_urlopen.return_value = _mock_response(body)
    install_keys_from_url("https://github.com/someuser.keys", authorized_keys_path=self.keys_path)

    other_body = "ssh-rsa AAAAB3xyz other@host\n"
    mock_urlopen.return_value = _mock_response(other_body)
    ok, msg = install_keys_from_url("https://github.com/otheruser.keys", authorized_keys_path=self.keys_path)
    self.assertTrue(ok)

    with open(self.keys_path) as f:
      lines = {line.strip() for line in f if line.strip()}
    self.assertEqual(lines, {"ssh-ed25519 AAAAC3abc user@host", "ssh-rsa AAAAB3xyz other@host"})

    # Re-running with the same body should add nothing new.
    mock_urlopen.return_value = _mock_response(other_body)
    ok, msg = install_keys_from_url("https://github.com/otheruser.keys", authorized_keys_path=self.keys_path)
    self.assertTrue(ok)
    self.assertEqual(msg, "keys already installed")
    with open(self.keys_path) as f:
      lines = [line.strip() for line in f if line.strip()]
    self.assertEqual(len(lines), 2)

  @patch("urllib.request.urlopen", side_effect=OSError("network unreachable"))
  def test_fetch_failure(self, mock_urlopen):
    ok, msg = install_keys_from_url("https://github.com/someuser.keys", authorized_keys_path=self.keys_path)
    self.assertFalse(ok)
    self.assertFalse(os.path.exists(self.keys_path))


if __name__ == "__main__":
  unittest.main()
