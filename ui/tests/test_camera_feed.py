import unittest

import numpy as np

from ui.camera_feed import center_crop, yuv_to_rgb


class TestYuvToRgb(unittest.TestCase):
  def test_pure_gray(self):
    y = np.full((4, 4), 128, dtype=np.uint8)
    u = np.full((2, 2), 128, dtype=np.uint8)
    v = np.full((2, 2), 128, dtype=np.uint8)
    rgb = yuv_to_rgb(y, u, v)
    self.assertEqual(rgb.shape, (4, 4, 3))
    self.assertTrue((rgb == 128).all())

  def test_output_dtype_and_clipping(self):
    y = np.full((2, 2), 255, dtype=np.uint8)
    u = np.full((1, 1), 0, dtype=np.uint8)
    v = np.full((1, 1), 255, dtype=np.uint8)
    rgb = yuv_to_rgb(y, u, v)
    self.assertEqual(rgb.dtype, np.uint8)
    self.assertTrue((rgb <= 255).all() and (rgb >= 0).all())


class TestCenterCrop(unittest.TestCase):
  def test_crop_shape_and_offset(self):
    frame = np.arange(100 * 200 * 3, dtype=np.uint8).reshape(100, 200, 3)
    cropped, (x0, y0) = center_crop(frame, ratio=0.5)
    self.assertEqual(cropped.shape, (50, 100, 3))
    self.assertEqual((x0, y0), (50, 25))

  def test_crop_is_centered_content(self):
    frame = np.arange(100 * 200 * 3, dtype=np.uint8).reshape(100, 200, 3)
    cropped, (x0, y0) = center_crop(frame, ratio=0.5)
    self.assertTrue((cropped == frame[y0:y0 + 50, x0:x0 + 100]).all())


if __name__ == "__main__":
  unittest.main()
