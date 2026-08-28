#!/usr/bin/env python3
import fcntl
import os
import time
import datetime

try:
    from serial import Serial
except ImportError:
    Serial = None

from common.config import get_device_id, get_api_url
from common.log import cloudlog
from common.params import Params
from common.http_client import post_json

# The GPS fix comes from the LTE modem's built-in GNSS engine (a Quectel
# EG916Q-GL here). We tried reading it via the Qualcomm DIAG protocol
# (system/qcomgpsd/ in openpilot's tree) but that stream never produced a
# single valid CRC-checked frame on this modem/wiring even passively
# listening with zero commands sent -- the framing that code assumes just
# doesn't match this modem's diag output. Quectel modules expose a much
# simpler path for exactly this need: AT+QGPSLOC polls the current fix
# directly as a plain-text AT response, no NMEA port routing or binary
# framing involved. That's what this daemon uses.

AT_PORT = "/dev/modem_at0" if os.path.exists("/dev/modem_at0") else "/dev/ttyUSB2"
AT_LOCK = "/dev/shm/modem.lock"

POLL_INTERVAL_SEC = 2.0
UPLOAD_MIN_INTERVAL_SEC = 2.0

# Quectel CME error code for "GNSS is on but doesn't have a fix yet" -- not
# a real error, just means keep polling.
CME_NOT_FIXED = "516"


def at_cmd(cmd: str, timeout: float = 5.0) -> str:
    with os.fdopen(os.open(AT_LOCK, os.O_CREAT | os.O_RDWR, 0o666), "r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        with Serial(AT_PORT, baudrate=115200, timeout=timeout) as ser:
            ser.reset_input_buffer()
            ser.write(f"{cmd}\r".encode())
            lines = []
            while True:
                line = ser.readline()
                if not line:
                    raise RuntimeError(f"AT command timeout: {cmd}")
                line = line.decode("utf-8", errors="replace").strip()
                if line in ("OK", "ERROR") or line.startswith("+CME ERROR"):
                    if line.startswith("+CME ERROR") and CME_NOT_FIXED not in line:
                        cloudlog.debug(f"gpsd: {cmd} -> {line}")
                    break
                if line and line != cmd:
                    lines.append(line)
        return "\n".join(lines)


def gps_enabled() -> bool:
    return "QGPS: 1" in at_cmd("AT+QGPS?")


def parse_qgpsloc(resp: str) -> dict | None:
    """Parses a "+QGPSLOC: <utc>,<lat>,<lon>,<hdop>,<alt>,<fix>,<cog>,<spkm>,
    <spkn>,<date>,<nsat>" line (mode=2 -> lat/lon are already signed decimal
    degrees, no N/S/E/W letters to strip). Returns None if there's no fix
    yet (e.g. the command errored with +CME ERROR: 516) or the response
    doesn't parse as expected."""
    for line in resp.splitlines():
        line = line.strip()
        if not line.startswith("+QGPSLOC:"):
            continue
        fields = [f.strip() for f in line.split(":", 1)[1].split(",")]
        if len(fields) < 8:
            return None
        try:
            return {
                "lat": float(fields[1]),
                "lng": float(fields[2]),
                "altitude": float(fields[4]),
                "bearing_deg": float(fields[6]),
                "speed_kph": float(fields[7]),
            }
        except ValueError:
            return None
    return None


class GpsDaemon:
    def __init__(self):
        self.device_id = get_device_id()
        self.params = Params()
        self.last_upload = 0.0

    def setup(self):
        if not gps_enabled():
            at_cmd("AT+QGPS=1")
            cloudlog.info("gpsd: enabled modem GNSS engine")
        else:
            cloudlog.info("gpsd: modem GNSS engine already enabled")

    def push_fix(self, fix: dict):
        self.params.put("GpsLat", fix["lat"])
        self.params.put("GpsLng", fix["lng"])
        self.params.put("GpsSpeedKph", fix["speed_kph"])
        self.params.put("GpsBearingDeg", fix["bearing_deg"])
        self.params.put_bool("GpsHasFix", True)

        now = time.monotonic()
        if now - self.last_upload < UPLOAD_MIN_INTERVAL_SEC:
            return
        self.last_upload = now

        endpoint = f"{get_api_url()}/devices/{self.device_id}/location"
        payload = {
            "lat": fix["lat"],
            "lng": fix["lng"],
            "speed_kph": fix["speed_kph"],
            "bearing_deg": fix["bearing_deg"],
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
        }
        try:
            post_json(endpoint, payload, timeout=2)
        except Exception as e:
            cloudlog.debug(f"gpsd: failed to push location: {e}")

    def poll_once(self):
        try:
            resp = at_cmd("AT+QGPSLOC=2")
        except Exception as e:
            cloudlog.debug(f"gpsd: AT+QGPSLOC failed: {e}")
            self.params.put_bool("GpsHasFix", False)
            return

        fix = parse_qgpsloc(resp)
        if fix is None:
            self.params.put_bool("GpsHasFix", False)
            return

        self.push_fix(fix)

    def run(self):
        cloudlog.info("gpsd started")

        if Serial is None:
            cloudlog.warning("gpsd: pyserial not installed, idling")
            while True:
                time.sleep(10)

        while True:
            try:
                self.setup()
            except Exception as e:
                cloudlog.error(f"gpsd: setup failed, retrying in 3s: {e}")
                time.sleep(3.0)
                continue

            while True:
                try:
                    self.poll_once()
                except Exception as e:
                    cloudlog.error(f"gpsd: poll error: {e}")
                time.sleep(POLL_INTERVAL_SEC)


def main():
    GpsDaemon().run()


if __name__ == "__main__":
    main()
