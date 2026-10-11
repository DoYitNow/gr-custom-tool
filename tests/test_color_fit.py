"""Sequential production model stays native-encodable and preserves neutral rows."""
from pathlib import Path
import sys
import unittest
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gr4_editor import color_fit, numeric
from gr4_editor.calibration_assets import load_gamma_base


class ColorFitTests(unittest.TestCase):
    def test_recipe_only_selects_sequential_models(self):
        self.assertEqual(color_fit.FIT_RECIPE_VERSION, 3)
        self.assertEqual(color_fit.FIT_RECIPE['fit_methods'], ['sequential'])
        self.assertFalse(color_fit.FIT_RECIPE['joint_gamma_multi'])

    def test_initial_model_encodes_gamma_and_identity_multi_without_changes(self):
        base = np.array(load_gamma_base()[0], dtype=int)
        x = np.random.default_rng(2026).uniform(.025, .975, (500, 3))
        target = np.asarray(np.sqrt(x), dtype=np.float32)
        model = numeric.make_base_model(x, target, base)
        self.assertTrue(np.all(np.diff(model['gamma_sources'], axis=1) >= 0))
        coeff = np.tile(np.eye(3, dtype=int) * 1024, (12, 5, 1, 1))
        gamma = model['gamma_sources'].copy()
        result = numeric.resource_check(model, coeff, base)
        self.assertTrue(result['four_composed_gamma_curves_encode'])
        self.assertTrue(result['all_180_multi_rows_sum_to_1024'])
        np.testing.assert_array_equal(model['gamma_sources'], gamma)


if __name__ == '__main__':
    unittest.main()
