#!/usr/bin/env python3
import math
import time

from sensord.sensors.i2c_sensor import Sensor
from sensord.sensors.lsm6ds3_accel import LSM6DS3_Accel
from sensord.sensors.lsm6ds3_gyro import LSM6DS3_Gyro

from common.log import cloudlog
from common.params import Params

# adapted from openpilot's system/sensord/sensord.py -- the real daemon reads
# the IMU off a GPIO data-ready interrupt (shared IRQ line, ~104Hz) and
# publishes over msgq. This project has neither GPIO event plumbing nor a
# pub/sub bus ported, so this is a plain polling loop instead, writing the
# latest reading straight to Params (same pattern as location/gpsd.py).
#
# GSensorVal is in g's (magnitude of the raw accel vector / 9.81 -- ~1.0 at
# rest since gravity itself contributes 1g), matching the units
# dashcam/recorder.py's event metadata and server/config.py's
# event_trigger.g_sensor_threshold already assume.

I2C_BUS_IMU = 1
POLL_INTERVAL_SEC = 0.05  # ~20Hz -- plenty for g-force/event-trigger purposes


class SensorDaemon:
    def __init__(self):
        self.params = Params()
        self.accel = LSM6DS3_Accel(I2C_BUS_IMU)
        self.gyro = LSM6DS3_Gyro(I2C_BUS_IMU)

    def setup(self):
        for sensor in (self.accel, self.gyro):
            try:
                sensor.reset()
            except Exception as e:
                cloudlog.debug(f"sensord: reset failed (non-fatal): {e}")
        self.accel.init()
        self.gyro.init()
        cloudlog.info(f"sensord: IMU initialized ({self.accel.source})")

    def poll_once(self):
        try:
            ax, ay, az, _ = self.accel.get_event()
            if self.accel.is_data_valid():
                self.params.put("AccelX", ax)
                self.params.put("AccelY", ay)
                self.params.put("AccelZ", az)
                g_force = math.sqrt(ax * ax + ay * ay + az * az) / 9.81
                self.params.put("GSensorVal", g_force)
        except Sensor.DataNotReady:
            pass

        try:
            gx, gy, gz, _ = self.gyro.get_event()
            if self.gyro.is_data_valid():
                self.params.put("GyroX", gx)
                self.params.put("GyroY", gy)
                self.params.put("GyroZ", gz)
        except Sensor.DataNotReady:
            pass

    def run(self):
        cloudlog.info("sensord started")
        while True:
            try:
                self.setup()
                while True:
                    self.poll_once()
                    time.sleep(POLL_INTERVAL_SEC)
            except Exception as e:
                cloudlog.error(f"sensord: error, reinitializing in 3s: {e}")
                for sensor in (self.accel, self.gyro):
                    try:
                        sensor.shutdown()
                    except Exception:
                        pass
                time.sleep(3.0)


def main():
    SensorDaemon().run()


if __name__ == "__main__":
    main()
