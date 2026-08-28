import select
import threading
import time
from struct import pack, unpack_from, calcsize

from serial import Serial
from crcmod import mkCrcFun

# ported from openpilot's system/qcomgpsd/modemdiag.py -- talks HDLC-framed
# Qualcomm DIAG protocol over the modem's diag port to request/receive GNSS
# position reports. See openpilot/system/qcomgpsd/ for the full version
# (raw GPS/GLONASS measurement reports, OEMDRE) this project doesn't need.
#
# This device's DIAG port is far chattier than comma's stock modem -- a
# continuous ~20KB/s flood of interleaved diag traffic, not just occasional
# log messages. The original recv() interleaves the raw serial read with
# frame decoding (unescaping + CRC) in one thread; under this much sustained
# throughput, the time spent decoding one frame is enough for the kernel's
# small tty receive buffer to overflow before we get back to reading, which
# silently drops bytes and corrupts the next frame's boundary. To avoid that,
# a dedicated thread does nothing but drain the OS buffer into memory as fast
# as possible; recv()/resync() only ever pull already-buffered bytes, so
# frame decoding time never blocks the read side.

DIAG_PORT = "/dev/ttyUSB0"


class ModemDiag:
  def __init__(self, port: str = DIAG_PORT):
    self.serial = self.open_serial(port)
    self.pend = b''

    self._buf = bytearray()
    self._buf_lock = threading.Lock()
    self._stop = threading.Event()
    self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
    self._reader_thread.start()

  def open_serial(self, port: str):
    # Upstream (real comma hardware) opens this with rtscts=True, dsrdtr=True.
    # This device's ttyUSB* ports don't seem to implement real RTS/CTS/DSR/DTR
    # wiring (we hit the same class of problem on the AT port earlier -- writes
    # blocked with EAGAIN until hardware flow control was disabled there too).
    serial = Serial(port, baudrate=115200, rtscts=False, dsrdtr=False, timeout=0, exclusive=True)
    serial.flush()
    serial.reset_input_buffer()
    serial.reset_output_buffer()
    return serial

  def _reader_loop(self):
    while not self._stop.is_set():
      try:
        r, _, _ = select.select([self.serial.fd], [], [], 0.5)
        if not r:
          continue
        data = self.serial.read(0x10000)
        if data:
          with self._buf_lock:
            self._buf.extend(data)
      except OSError:
        break

  def _take_buffered(self) -> bytes:
    with self._buf_lock:
      if not self._buf:
        return b''
      data = bytes(self._buf)
      self._buf.clear()
      return data

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
    expected_crc = pack('<H', ModemDiag.ccitt_crc16(payload[:-2]))
    assert payload[-2:] == expected_crc, (
        f"CRC16 mismatch: got {payload[-2:].hex()} expected {expected_crc.hex()}, "
        f"frame ({len(payload)}B) = {payload.hex()}"
    )
    return payload[:-2]

  def recv(self):
    raw_payload = [self.pend]
    self.pend = b''
    while self.TRAILER_CHAR not in raw_payload[-1]:
      chunk = self._take_buffered()
      if chunk:
        raw_payload.append(chunk)
      else:
        time.sleep(0.002)
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
      chunk = self._take_buffered()
      if chunk:
        buf += chunk
      else:
        time.sleep(0.01)
    _, self.pend = buf.split(self.TRAILER_CHAR, 1)

  def close(self):
    self._stop.set()
    self._reader_thread.join(timeout=1.0)
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
