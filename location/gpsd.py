#!/usr/bin/env python3
import fcntl
import math
import os
import time
import datetime
from struct import unpack_from, calcsize

try:
    from serial import Serial
    from location.modemdiag import ModemDiag, DIAG_PORT, DIAG_LOG_F, setup_logs
    from location.structs import dict_unpacker, position_report, LOG_GNSS_POSITION_REPORT
except ImportError:
    Serial = None
    ModemDiag = DIAG_PORT = DIAG_LOG_F = setup_logs = None
    dict_unpacker = position_report = LOG_GNSS_POSITION_REPORT = None

from common.config import get_device_id, get_api_url
from common.log import cloudlog
from common.params import Params
from common.http_client import post_json

# The GPS fix comes from the LTE modem's built-in GNSS engine (a Quectel
# EG916Q-GL here), not a discrete u-blox chip -- ModemManager only exposes
# coarse cell-tower location for this modem (`mmcli -m 0 --location-status`
# -> capabilities: 3gpp-lac-ci only), so we talk to the modem directly:
# AT commands to turn GNSS on, and the Qualcomm DIAG protocol (over a
# separate serial port) to pull structured position reports. See
# openpilot/system/qcomgpsd/ for the upstream version this is trimmed from
# (that one also decodes raw pseudoranges for laikad; this project only
# wants a lat/lng fix for a live map marker).

AT_PORT = "/dev/modem_at0" if os.path.exists("/dev/modem_at0") else "/dev/ttyUSB2"
AT_LOCK = "/dev/shm/modem.lock"

POSITION_REPORT_UPDATE_MIN_INTERVAL_SEC = 2.0

unpack_position = dict_unpacker(position_report)[0] if dict_unpacker is not None else None


def at_cmd(cmd: str) -> str:
    with os.fdopen(os.open(AT_LOCK, os.O_CREAT | os.O_RDWR, 0o666), "r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        with Serial(AT_PORT, baudrate=115200, timeout=5) as ser:
            ser.reset_input_buffer()
            ser.write(f"{cmd}\r".encode())
            lines = []
            while True:
                line = ser.readline()
                if not line:
                    raise RuntimeError(f"AT command timeout: {cmd}")
                line = line.decode("utf-8", errors="replace").strip()
                if line in ("OK", "ERROR") or line.startswith("+CME ERROR"):
                    break
                if line and line != cmd:
                    lines.append(line)
        return "\n".join(lines)


def gps_enabled() -> bool:
    return "QGPS: 1" in at_cmd("AT+QGPS?")


def setup_quectel_gps():
    """Route the modem's NMEA output and turn its GNSS engine on if it isn't
    already (both are idempotent -- QGPS=1 while already on just replies
    +CME ERROR: 504, which at_cmd treats as a normal terminator)."""
    at_cmd('AT+QGPSCFG="outport","usbnmea"')
    if not gps_enabled():
        at_cmd("AT+QGPS=1")
        cloudlog.info("gpsd: enabled modem GNSS engine")
    else:
        cloudlog.info("gpsd: modem GNSS engine already enabled")


class GpsDaemon:
    def __init__(self):
        self.device_id = get_device_id()
        self.params = Params()
        self.last_upload = 0.0

    def push_fix(self, lat: float, lng: float, speed_mps: float, bearing_deg: float,
                 accuracy_m: float, has_fix: bool):
        self.params.put("GpsLat", lat)
        self.params.put("GpsLng", lng)
        self.params.put("GpsSpeedKph", speed_mps * 3.6)
        self.params.put("GpsBearingDeg", bearing_deg)
        self.params.put("GpsAccuracyM", accuracy_m)
        self.params.put_bool("GpsHasFix", has_fix)

        if not has_fix:
            return

        now = time.monotonic()
        if now - self.last_upload < POSITION_REPORT_UPDATE_MIN_INTERVAL_SEC:
            return
        self.last_upload = now

        endpoint = f"{get_api_url()}/devices/{self.device_id}/location"
        payload = {
            "lat": lat,
            "lng": lng,
            "speed_kph": speed_mps * 3.6,
            "bearing_deg": bearing_deg,
            "accuracy_m": accuracy_m,
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
        }
        try:
            post_json(endpoint, payload, timeout=2)
        except Exception as e:
            cloudlog.debug(f"gpsd: failed to push location: {e}")

    def handle_position_report(self, payload: bytes):
        report = unpack_position(payload)

        # 0: none, 1: WLS, 2: Kalman filter, 3: externally injected, 4: internal db
        if report["u_PosSource"] != 2:
            return
        # uint16 max is an invalid sentinel value from the modem
        if report["w_GpsWeekNumber"] >= 0xFFFF:
            return

        lat = report["t_DblFinalPosLatLon[0]"] * 180 / math.pi
        lng = report["t_DblFinalPosLatLon[1]"] * 180 / math.pi
        bearing = report["q_FltHeadingRad"] * 180 / math.pi

        v_enu = [report["q_FltVelEnuMps[0]"], report["q_FltVelEnuMps[1]"], report["q_FltVelEnuMps[2]"]]
        speed = math.sqrt(sum(x ** 2 for x in v_enu))

        vertical_accuracy = report["q_FltVdop"]
        # quectel clips verticalAccuracy to 500 when there's no fix
        has_fix = vertical_accuracy != 500

        self.push_fix(lat, lng, speed, bearing, vertical_accuracy, has_fix)

    def run(self):
        cloudlog.info("gpsd started")

        if Serial is None:
            cloudlog.warning("gpsd: pyserial not installed, idling")
            while True:
                time.sleep(10)

        while True:
            diag = None
            try:
                setup_quectel_gps()

                diag = ModemDiag(DIAG_PORT)
                diag.resync()
                setup_logs(diag, [LOG_GNSS_POSITION_REPORT])
                cloudlog.info("gpsd: DIAG logging configured, listening for position reports")

                while True:
                    try:
                        opcode, payload = diag.recv()
                        if opcode != DIAG_LOG_F:
                            continue

                        (pending_msgs, log_outer_length), inner = unpack_from("<BH", payload), payload[calcsize("<BH"):]
                        if log_outer_length != len(inner):
                            continue

                        (log_inner_length, log_type, log_time), log_payload = (
                            unpack_from("<HHQ", inner), inner[calcsize("<HHQ"):]
                        )
                        if log_inner_length != len(inner) or log_type != LOG_GNSS_POSITION_REPORT:
                            continue

                        self.handle_position_report(log_payload)
                    except AssertionError as e:
                        # A single corrupt/desynced HDLC frame on the wire --
                        # drop it and keep listening on the same DIAG session
                        # instead of tearing the whole thing down and paying
                        # for a full reconnect + re-setup over one bad frame.
                        cloudlog.debug(f"gpsd: dropping malformed DIAG frame: {e}")
            except Exception as e:
                cloudlog.error(f"gpsd: error ({type(e).__name__}), reconnecting in 3s: {e}")
            finally:
                # exclusive=True means a leaked fd here blocks our own next
                # ModemDiag(DIAG_PORT) open with EAGAIN -- always release it
                # before retrying, not just on the happy path.
                if diag is not None:
                    try:
                        diag.close()
                    except Exception:
                        pass
            time.sleep(3.0)


def main():
    GpsDaemon().run()


if __name__ == "__main__":
    main()
