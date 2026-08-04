import unittest

import numpy as np

from ui.camera_feed import yuv_to_rgb


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


if __name__ == "__main__":
  unittest.main()
