import time
import ctypes
from collections.abc import Iterable

from common.i2c import SMBus

# adapted from openpilot's system/sensord/sensors/i2c_sensor.py -- the I2C
# register plumbing is ported as-is, but get_event() returns a plain
# (x, y, z, timestamp) tuple instead of a cereal capnp SensorEventData
# message, since this project has no msgq pub/sub -- callers write straight
# to Params instead (see sensord/sensord.py).


class Sensor:
  class SensorException(Exception):
    pass

  class DataNotReady(SensorException):
    pass

  def __init__(self, bus: int) -> None:
    self.bus = SMBus(bus)
    self.source = "unknown"
    self.start_ts = 0.

  def __del__(self):
    self.bus.close()

  def read(self, addr: int, length: int) -> bytes:
    return bytes(self.bus.read_i2c_block_data(self.device_address, addr, length))

  def write(self, addr: int, data: int) -> None:
    self.bus.write_byte_data(self.device_address, addr, data)

  def writes(self, writes: Iterable[tuple[int, int]]) -> None:
    for addr, data in writes:
      self.write(addr, data)

  def verify_chip_id(self, address: int, expected_ids: list[int]) -> int:
    chip_id = self.read(address, 1)[0]
    assert chip_id in expected_ids
    return chip_id

  # Abstract methods that must be implemented by subclasses
  @property
  def device_address(self) -> int:
    raise NotImplementedError

  def reset(self) -> None:
    # optional. not part of init due to shared registers
    pass

  def init(self) -> None:
    raise NotImplementedError

  def get_event(self, ts: float | None = None) -> tuple[float, float, float, float]:
    raise NotImplementedError

  def shutdown(self) -> None:
    raise NotImplementedError

  def is_data_valid(self) -> bool:
    if self.start_ts == 0:
      self.start_ts = time.monotonic()
    # give the sensor half a second to settle after init before trusting it
    return (time.monotonic() - self.start_ts) > 0.5

  # *** helpers ***
  @staticmethod
  def wait():
    time.sleep(0.005)

  @staticmethod
  def parse_16bit(lsb: int, msb: int) -> int:
    return ctypes.c_int16((msb << 8) | lsb).value
