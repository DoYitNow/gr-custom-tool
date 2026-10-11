"""Production sequential fit recipe."""
from __future__ import annotations


FIT_RECIPE_VERSION = 3
FIT_RECIPE = {'fit_methods': ['sequential'], 'joint_gamma_multi': False,
              'gamma_model': 'initial monotone polynomial envelope',
              'gamma_source_scale': 16384,
              'selection': 'development leave-one-scene-out matched RGB MAE'}
