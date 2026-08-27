import os
import math
import time

from sensord.sensors.i2c_sensor import Sensor

# ported from openpilot's system/sensord/sensors/lsm6ds3_gyro.py, adapted
# the same way as lsm6ds3_accel.py (get_event returns a plain tuple).


class LSM6DS3_Gyro(Sensor):
  LSM6DS3_GYRO_I2C_REG_DRDY_CFG  = 0x0B
  LSM6DS3_GYRO_I2C_REG_INT1_CTRL = 0x0D
  LSM6DS3_GYRO_I2C_REG_CTRL2_G   = 0x11
  LSM6DS3_GYRO_I2C_REG_CTRL5_C   = 0x14
  LSM6DS3_GYRO_I2C_REG_STAT_REG  = 0x1E
  LSM6DS3_GYRO_I2C_REG_OUTX_L_G  = 0x22

  LSM6DS3_GYRO_ODR_104HZ       = (0b0100 << 4)
  LSM6DS3_GYRO_INT1_DRDY_G     = 0b10
  LSM6DS3_GYRO_DRDY_GDA        = 0b10
  LSM6DS3_GYRO_DRDY_PULSE_MODE = (1 << 7)

  LSM6DS3_GYRO_ODR_208HZ       = (0b0101 << 4)
  LSM6DS3_GYRO_FS_2000dps      = (0b11 << 2)
  LSM6DS3_GYRO_POSITIVE_TEST   = (0b01 << 2)
  LSM6DS3_GYRO_NEGATIVE_TEST   = (0b11 << 2)
  LSM6DS3_GYRO_MIN_ST_LIMIT_mdps = 150000.0
  LSM6DS3_GYRO_MAX_ST_LIMIT_mdps = 700000.0

  @property
  def device_address(self) -> int:
    return 0x6A

  def reset(self):
    self.write(0x12, 0x1)
    time.sleep(0.1)

  def init(self):
    chip_id = self.verify_chip_id(0x0F, [0x69, 0x6A])
    self.source = "lsm6ds3trc" if chip_id == 0x6A else "lsm6ds3"

    if "LSM_SELF_TEST" in os.environ:
      self.self_test(self.LSM6DS3_GYRO_POSITIVE_TEST)
      self.self_test(self.LSM6DS3_GYRO_NEGATIVE_TEST)

    self.writes((
      # Default is +- 250 deg/s
      (self.LSM6DS3_GYRO_I2C_REG_CTRL2_G, self.LSM6DS3_GYRO_ODR_104HZ),
      (self.LSM6DS3_GYRO_I2C_REG_DRDY_CFG, self.LSM6DS3_GYRO_DRDY_PULSE_MODE),
    ))
    value = self.read(self.LSM6DS3_GYRO_I2C_REG_INT1_CTRL, 1)[0]
    value |= self.LSM6DS3_GYRO_INT1_DRDY_G
    self.write(self.LSM6DS3_GYRO_I2C_REG_INT1_CTRL, value)

  def get_event(self, ts: float | None = None) -> tuple[float, float, float, float]:
    if ts is None:
      ts = time.time()

    status_reg = self.read(self.LSM6DS3_GYRO_I2C_REG_STAT_REG, 1)[0]
    if not (status_reg & self.LSM6DS3_GYRO_DRDY_GDA):
      raise self.DataNotReady

    b = self.read(self.LSM6DS3_GYRO_I2C_REG_OUTX_L_G, 6)
    x = self.parse_16bit(b[0], b[1])
    y = self.parse_16bit(b[2], b[3])
    z = self.parse_16bit(b[4], b[5])
    scale = (8.75 / 1000.0) * (math.pi / 180.0)

    # matches upstream's axis remap
    return y * scale, -x * scale, z * scale, ts

  def shutdown(self) -> None:
    value = self.read(self.LSM6DS3_GYRO_I2C_REG_INT1_CTRL, 1)[0]
    value &= ~self.LSM6DS3_GYRO_INT1_DRDY_G
    self.write(self.LSM6DS3_GYRO_I2C_REG_INT1_CTRL, value)

    value = self.read(self.LSM6DS3_GYRO_I2C_REG_CTRL2_G, 1)[0]
    value &= 0x0F
    self.write(self.LSM6DS3_GYRO_I2C_REG_CTRL2_G, value)

  # *** self-test stuff ***
  def _wait_for_data_ready(self):
    while True:
      drdy = self.read(self.LSM6DS3_GYRO_I2C_REG_STAT_REG, 1)[0]
      if drdy & self.LSM6DS3_GYRO_DRDY_GDA:
        break

  def _read_and_avg_data(self) -> list[float]:
    out_buf = [0.0, 0.0, 0.0]
    for _ in range(5):
      self._wait_for_data_ready()
      b = self.read(self.LSM6DS3_GYRO_I2C_REG_OUTX_L_G, 6)
      for j in range(3):
        val = self.parse_16bit(b[j * 2], b[j * 2 + 1]) * 70.0  # mdps/LSB for 2000 dps
        out_buf[j] += val
    return [x / 5.0 for x in out_buf]

  def self_test(self, test_type: int):
    self.write(self.LSM6DS3_GYRO_I2C_REG_CTRL2_G, self.LSM6DS3_GYRO_ODR_208HZ | self.LSM6DS3_GYRO_FS_2000dps)

    time.sleep(0.15)
    self._wait_for_data_ready()
    val_st_off = self._read_and_avg_data()

    self.write(self.LSM6DS3_GYRO_I2C_REG_CTRL5_C, test_type)

    time.sleep(0.05)
    self._wait_for_data_ready()
    val_st_on = self._read_and_avg_data()

    self.write(self.LSM6DS3_GYRO_I2C_REG_CTRL2_G, 0)
    self.write(self.LSM6DS3_GYRO_I2C_REG_CTRL5_C, 0)

    test_val = [abs(on - off) for on, off in zip(val_st_on, val_st_off, strict=False)]
    for val in test_val:
      if val < self.LSM6DS3_GYRO_MIN_ST_LIMIT_mdps or val > self.LSM6DS3_GYRO_MAX_ST_LIMIT_mdps:
        raise Sensor.SensorException(f"Gyroscope self-test failed for test type {test_type}")
