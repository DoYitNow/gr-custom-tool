"""Local target RAW rendering fixes whitepoint without changing fitter input."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gr4_editor.offline_raw import RAW_PARAMETERS, TARGET_RENDER_SETTINGS, render_dng, render_target


def fixture(rgb=None):
    rgb = (np.arange(5 * 6 * 3).reshape(5, 6, 3) * 500).astype(np.uint16) if rgb is None else rgb
    raw = SimpleNamespace(white_level=16317,
                          sizes=SimpleNamespace(raw_width=12, raw_height=10,
                                                crop_left_margin=3, left_margin=2,
                                                crop_top_margin=5, top_margin=3,
                                                crop_width=3, crop_height=2),
                          postprocess=Mock(return_value=rgb))
    context = Mock()
    context.__enter__ = Mock(return_value=raw)
    context.__exit__ = Mock(return_value=False)
    color = object()
    backend = SimpleNamespace(np=np, rawpy=SimpleNamespace(ColorSpace=SimpleNamespace(sRGB=color),
                                                          imread=Mock(return_value=context)),
                              resized=Mock(side_effect=lambda pixels, width: pixels.copy()))
    return backend, raw, context, rgb, color


class OfflineRawTests(unittest.TestCase):
    def test_fixed_whitepoint_camera_wb_and_existing_crop_are_recorded(self):
        backend, raw, context, rgb, color = fixture()
        pixels, geometry = render_dng(Path('synthetic.DNG'), 775, 0., backend=backend)
        backend.rawpy.imread.assert_called_once_with('synthetic.DNG')
        raw.postprocess.assert_called_once_with(output_color=color, gamma=(1, 1), output_bps=16,
                                               use_camera_wb=True, no_auto_bright=True,
                                               adjust_maximum_thr=0.0)
        expected = rgb[2:4, 1:4].astype(np.float32) / 65535
        resized_pixels, width = backend.resized.call_args.args
        np.testing.assert_array_equal(resized_pixels, expected)
        self.assertEqual(width, 775)
        np.testing.assert_array_equal(pixels, expected)
        self.assertEqual(geometry['raw_size'], [12, 10])
        self.assertEqual(geometry['crop'], [1, 2, 3, 2])
        self.assertEqual(geometry['libraw_rgb_size'], [3, 2])
        self.assertEqual(geometry['white_level'], 16317)
        self.assertEqual(geometry['raw_parameters'], RAW_PARAMETERS)
        json.dumps(geometry)
        context.__exit__.assert_called_once_with(None, None, None)

    def test_baseline_exposure_follows_resize_and_clips_only_after_it(self):
        backend, raw, context, rgb, color = fixture()
        resized = np.array([[[.1, .4, .8], [1., 0., .25]]], dtype=np.float32)
        backend.resized.side_effect = None
        backend.resized.return_value = resized
        pixels, geometry = render_dng('synthetic.DNG', 1550, 1., backend=backend)
        np.testing.assert_array_equal(pixels, np.array([[[.2, .8, 1.], [1., 0., .5]]], dtype=np.float32))
        input_pixels, width = backend.resized.call_args.args
        np.testing.assert_array_equal(input_pixels, rgb[2:4, 1:4].astype(np.float32) / 65535)
        self.assertEqual(width, 1550)
        self.assertEqual(geometry['baseline_exposure_ev'], 1.)

    def test_negative_baseline_exposure_preserves_linear_values(self):
        backend, raw, context, rgb, color = fixture(np.full((5, 6, 3), 65535, dtype=np.uint16))
        pixels, geometry = render_dng('synthetic.DNG', 775, -1., backend=backend)
        np.testing.assert_array_equal(pixels, np.full((2, 3, 3), .5, dtype=np.float32))
        self.assertEqual(geometry['baseline_exposure_ev'], -1.)

    def test_unfiltered_raw_uses_area_without_upscaling_native_crop(self):
        backend, raw, context, rgb, color = fixture()
        backend.cv2 = cv2
        pixels, geometry = render_dng('synthetic.DNG', 775, 0., backend=backend, prefilter=False)
        np.testing.assert_array_equal(pixels, rgb[2:4, 1:4].astype(np.float32) / 65535)
        backend.resized.assert_not_called()
        self.assertEqual(geometry['rendered_size'], [3, 2])
        self.assertFalse(geometry['prefilter'])
        pixels, geometry = render_dng('synthetic.DNG', 2, 0., backend=backend, prefilter=False)
        expected = cv2.resize(rgb[2:4, 1:4].astype(np.float32) / 65535, (2, 1), interpolation=cv2.INTER_AREA)
        np.testing.assert_array_equal(pixels, expected)
        self.assertEqual(geometry['rendered_size'], [2, 1])

    def test_gray_edge_look_precedes_display_resize_and_gaussian(self):
        # A PIL-generated grayscale step demonstrates the noncommuting order
        # without any Lightroom output or fitted color parameters.
        image = Image.new('L', (12, 8), 0)
        image.paste(255, (6, 0, 12, 8))
        rgb = np.asarray(image.convert('RGB')).astype(np.uint16) * 257
        backend, raw, context, _, color = fixture(rgb)
        raw.sizes = SimpleNamespace(raw_width=12, raw_height=8, crop_left_margin=0, left_margin=0,
                                    crop_top_margin=0, top_margin=0, crop_width=12, crop_height=8)
        backend.cv2 = cv2
        backend.resized.side_effect = lambda pixels, width: cv2.GaussianBlur(
            cv2.resize(pixels, (width, round(pixels.shape[0] * width / pixels.shape[1])),
                       interpolation=cv2.INTER_AREA), (3, 3), .75)
        base = object()
        with patch('gr4_editor.offline_look.render_pixels', side_effect=lambda data, linear, **kwargs: np.sqrt(linear)) as look:
            target, geometry = render_target(b'synthetic-look', 'synthetic.DNG', 3, 0., backend=backend, base_profile=base)
        supersampled = cv2.resize(rgb.astype(np.float32) / 65535, (6, 4), interpolation=cv2.INTER_AREA)
        expected = cv2.GaussianBlur(cv2.resize(np.sqrt(supersampled), (3, 2), interpolation=cv2.INTER_AREA), (3, 3), .75)
        np.testing.assert_array_equal(target, expected)
        previous, _ = render_dng('synthetic.DNG', 3, 0., backend=backend)
        self.assertGreater(float(np.abs(np.sqrt(previous) - target).max()), .05)
        np.testing.assert_array_equal(look.call_args.args[1], supersampled)
        self.assertIs(look.call_args.kwargs['base_profile'], base)
        self.assertEqual(geometry['look_rendered_size'], [6, 4])
        self.assertEqual(geometry['final_size'], [3, 2])
        self.assertEqual(geometry['target_render_settings'], TARGET_RENDER_SETTINGS)
        json.dumps(geometry)

    def test_final_aspect_uses_original_crop_instead_of_odd_intermediate_height(self):
        backend = SimpleNamespace(cv2=cv2)
        # Native 6192x4128 -> width1550 rounds to1033 rows. Recomputing
        # aspect from1033 would round775 output to516 instead of517 rows.
        linear = np.broadcast_to(np.linspace(0., 1., 1550, dtype=np.float32)[None, :, None], (1033, 1550, 3))
        geometry = {'crop': [28, 24, 6192, 4128], 'rendered_size': [1550, 1033], 'prefilter': False}
        with patch('gr4_editor.offline_raw.render_dng', return_value=(linear, geometry)) as read:
            with patch('gr4_editor.offline_look.render_pixels', side_effect=lambda data, pixels, **kwargs: pixels):
                target, result = render_target(b'identity', 'synthetic.DNG', 775, .494, backend=backend, base_profile=None)
        read.assert_called_once_with('synthetic.DNG', 1550, .494, backend=backend, prefilter=False)
        self.assertEqual(target.shape, (517, 775, 3))
        self.assertEqual(result['final_size'], [775, 517])
        expected = cv2.GaussianBlur(cv2.resize(linear, (775, 517), interpolation=cv2.INTER_AREA), (3, 3), .75)
        np.testing.assert_array_equal(target, expected)


if __name__ == '__main__':
    unittest.main()
