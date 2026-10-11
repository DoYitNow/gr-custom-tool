"""Production selection keeps initial resources and excludes holdout data."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gr4_editor.fitting import _fit_development, _native_context, run_job
from gr4_editor import numeric


class NativeInputTests(unittest.TestCase):
    def test_input_is_required_without_a_control_fixture_fallback(self):
        with self.assertRaisesRegex(ValueError, '导入兼容输入'):
            _native_context(numeric, {})

    def test_stale_fit_recipe_is_rejected_before_reading_materials(self):
        for recipe in (1, 2):
            with self.subTest(recipe=recipe), self.assertRaisesRegex(ValueError, '拟合配方版本'):
                run_job({'fit_recipe_version': recipe}, {})


class FitSelectionTests(unittest.TestCase):
    def select(self, desired):
        class ForbiddenHoldout:
            def __iter__(self):
                raise AssertionError('holdout entered selection')
        pairs = {name: (np.full((100, 3), value), np.full((100, 3), desired))
                 for name, value in (('forest', .1), ('city', .2), ('river', .3))}
        pairs['reserved'] = ForbiddenHoldout()
        calls = []
        def make_model(x, y, base):
            calls.append(x.copy())
            return {'matrix': np.eye(3), 'gamma': np.tile(np.linspace(0, 1, 256), (3, 1)),
                    'gamma_sources': np.tile(np.arange(256), (3, 1)), 'method': 'sequential'}
        def evaluate(x, model, coeff, kind, native):
            prediction = {('sequential', 'luminance'): .2, ('sequential', 'chroma_l1'): .4,
                          ('refined', 'luminance'): .6, ('refined', 'chroma_l1'): .8}[(model['method'], kind)]
            return np.full_like(x, prediction)
        coefficient = np.tile(np.eye(3, dtype=int) * 1024, (12, 5, 1, 1))
        backend = SimpleNamespace(np=np, make_base_model=make_model,
                                  fit_continuous_multi=lambda *args: (coefficient.copy(), ['support']),
                                  evaluate=evaluate,
                                  score=lambda actual, expected: {'rgb_mae_8bit': float(np.abs(actual - expected).mean() * 255)})
        model, coeff, support, selection = _fit_development(backend, pairs, 'reserved', {}, [], 64)
        np.testing.assert_array_equal(model['gamma_sources'], np.tile(np.arange(256), (3, 1)))
        self.assertEqual(len(calls), 4)  # Three folds and the final all-dev fit.
        self.assertEqual(len(calls[-1]), 3 * 64)
        for row in selection['folds'].values():
            self.assertNotIn('reserved', row['training_scenes'])
            self.assertNotIn(row['test_scene'], row['training_scenes'])
            self.assertEqual(set(row['fit_methods']), {'sequential'})
            for scores in row['fit_methods'].values():
                self.assertEqual(set(scores), {'luminance', 'chroma_l1'})
        return selection

    def test_refined_would_win_but_is_excluded_from_production_selection(self):
        selected = self.select(.8)
        self.assertEqual((selected['selected_fit_method'], selected['selected_hypothesis']), ('sequential', 'chroma_l1'))
        self.assertEqual(selected['refinement_history'], [])

    def test_original_luminance_selection_and_holdout_exclusion_are_preserved(self):
        selected = self.select(.2)
        self.assertEqual((selected['selected_fit_method'], selected['selected_hypothesis']), ('sequential', 'luminance'))
        self.assertEqual(selected['refinement_history'], [])


if __name__ == '__main__':
    unittest.main()
