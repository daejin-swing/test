import select
import time
from struct import pack, unpack_from, calcsize

from serial import Serial
from crcmod import mkCrcFun

# ported from openpilot's system/qcomgpsd/modemdiag.py -- talks HDLC-framed
# Qualcomm DIAG protocol over the modem's diag port to request/receive GNSS
# position reports. See openpilot/system/qcomgpsd/ for the full version
# (raw GPS/GLONASS measurement reports, OEMDRE) this project doesn't need.

DIAG_PORT = "/dev/ttyUSB0"


class ModemDiag:
  def __init__(self, port: str = DIAG_PORT):
    self.serial = self.open_serial(port)
    self.pend = b''

  def open_serial(self, port: str):
    serial = Serial(port, baudrate=115200, rtscts=True, dsrdtr=True, timeout=0, exclusive=True)
    serial.flush()
    serial.reset_input_buffer()
    serial.reset_output_buffer()
    return serial

  ccitt_crc16 = mkCrcFun(0x11021, initCrc=0, xorOut=0xffff)
  ESCAPE_CHAR = b'\x7d'
  TRAILER_CHAR = b'\x7e'

  def hdlc_encapsulate(self, payload):
    payload += pack('<H', ModemDiag.ccitt_crc16(payload))
    payload = payload.replace(self.ESCAPE_CHAR, bytes([self.ESCAPE_CHAR[0], self.ESCAPE_CHAR[0] ^ 0x20]))
    payload = payload.replace(self.TRAILER_CHAR, bytes([self.ESCAPE_CHAR[0], self.TRAILER_CHAR[0] ^ 0x20]))
    payload += self.TRAILER_CHAR
    return payload

  def hdlc_decapsulate(self, payload):
    assert len(payload) >= 3, f"frame too short: {len(payload)} bytes"
    assert payload[-1:] == self.TRAILER_CHAR, "frame missing trailer byte"
    payload = payload[:-1]
    payload = payload.replace(bytes([self.ESCAPE_CHAR[0], self.TRAILER_CHAR[0] ^ 0x20]), self.TRAILER_CHAR)
    payload = payload.replace(bytes([self.ESCAPE_CHAR[0], self.ESCAPE_CHAR[0] ^ 0x20]), self.ESCAPE_CHAR)
    assert payload[-2:] == pack('<H', ModemDiag.ccitt_crc16(payload[:-2])), "CRC16 mismatch"
    return payload[:-2]

  def recv(self):
    raw_payload = [self.pend]
    while self.TRAILER_CHAR not in raw_payload[-1]:
      select.select([self.serial.fd], [], [])
      raw = self.serial.read(0x10000)
      raw_payload.append(raw)
    raw_payload = b''.join(raw_payload)
    raw_payload, self.pend = raw_payload.split(self.TRAILER_CHAR, 1)
    raw_payload += self.TRAILER_CHAR
    unframed_message = self.hdlc_decapsulate(raw_payload)
    return unframed_message[0], unframed_message[1:]

  def send(self, packet_type, packet_payload):
    self.serial.write(self.hdlc_encapsulate(bytes([packet_type]) + packet_payload))

  def resync(self, timeout: float = 1.0):
    """Discard whatever partial frame is already in flight when we attach to
    the port, so recv() starts parsing from a genuine frame boundary.
    Otherwise the first bytes we see are likely the tail of a frame that
    started before we opened the port, which reliably fails CRC."""
    end = time.time() + timeout
    buf = self.pend
    self.pend = b''
    while self.TRAILER_CHAR not in buf:
      if time.time() > end:
        return
      r, _, _ = select.select([self.serial.fd], [], [], 0.2)
      if r:
        buf += self.serial.read(0x10000)
    _, self.pend = buf.split(self.TRAILER_CHAR, 1)

  def close(self):
    self.serial.close()


DIAG_LOG_F = 16
DIAG_LOG_CONFIG_F = 115
LOG_CONFIG_RETRIEVE_ID_RANGES_OP = 1
LOG_CONFIG_SET_MASK_OP = 3
LOG_CONFIG_SUCCESS_S = 0


def send_recv(diag, packet_type, packet_payload):
  diag.send(packet_type, packet_payload)
  while True:
    opcode, payload = diag.recv()
    if opcode != DIAG_LOG_F:
      break
  return opcode, payload


def setup_logs(diag, types_to_log):
  opcode, payload = send_recv(diag, DIAG_LOG_CONFIG_F, pack('<3xI', LOG_CONFIG_RETRIEVE_ID_RANGES_OP))

  header_spec = '<3xII'
  operation, status = unpack_from(header_spec, payload)
  assert operation == LOG_CONFIG_RETRIEVE_ID_RANGES_OP, f"unexpected operation in ID-ranges response: {operation}"
  assert status == LOG_CONFIG_SUCCESS_S, f"ID-ranges request failed: status {status}"

  log_masks = unpack_from('<16I', payload, calcsize(header_spec))

  for log_type, log_mask_bitsize in enumerate(log_masks):
    if log_mask_bitsize:
      log_mask = [0] * ((log_mask_bitsize + 7) // 8)
      for i in range(log_mask_bitsize):
        if ((log_type << 12) | i) in types_to_log:
          log_mask[i // 8] |= 1 << (i % 8)
      opcode, payload = send_recv(diag, DIAG_LOG_CONFIG_F, pack('<3xIII',
          LOG_CONFIG_SET_MASK_OP,
          log_type,
          log_mask_bitsize
      ) + bytes(log_mask))
      assert opcode == DIAG_LOG_CONFIG_F, f"unexpected opcode setting mask for log_type {log_type}: {opcode}"
      operation, status = unpack_from(header_spec, payload)
      assert operation == LOG_CONFIG_SET_MASK_OP, f"unexpected operation setting mask for log_type {log_type}: {operation}"
      assert status == LOG_CONFIG_SUCCESS_S, f"set-mask failed for log_type {log_type}: status {status}"
