import os
import urllib.request
from urllib.parse import urlparse

from common.log import cloudlog

AUTHORIZED_KEYS_PATH = os.path.expanduser("~/.ssh/authorized_keys")


def is_valid_key_url(data: str) -> bool:
  """True if `data` looks like an http(s) URL (vs. e.g. a WiFi QR's WIFI: string)."""
  try:
    parsed = urlparse(data)
  except ValueError:
    return False
  return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def install_keys_from_url(url: str, authorized_keys_path: str = AUTHORIZED_KEYS_PATH,
                           timeout: float = 10.0) -> tuple[bool, str]:
  """Fetch `url` (e.g. https://github.com/<user>.keys), keep only lines that look like
  an actual public key (start with "ssh-"), and append any not already present to
  authorized_keys_path. Append-only: never removes existing keys."""
  if not is_valid_key_url(url):
    return False, "not a URL"
  try:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
      body = resp.read().decode("utf-8", errors="replace")
  except Exception as e:
    cloudlog.exception("ssh_keys: failed to fetch key URL")
    return False, str(e)

  keys = [line.strip() for line in body.splitlines() if line.strip().startswith("ssh-")]
  if not keys:
    return False, "no valid keys found at URL"

  os.makedirs(os.path.dirname(authorized_keys_path), mode=0o700, exist_ok=True)
  existing = set()
  if os.path.exists(authorized_keys_path):
    with open(authorized_keys_path) as f:
      existing = {line.strip() for line in f if line.strip()}

  new_keys = [k for k in keys if k not in existing]
  if new_keys:
    with open(authorized_keys_path, "a") as f:
      for k in new_keys:
        f.write(k + "\n")
    os.chmod(authorized_keys_path, 0o600)

  cloudlog.event("ssh keys installed", url=url, added=len(new_keys))
  return True, f"added {len(new_keys)} key(s)" if new_keys else "keys already installed"
