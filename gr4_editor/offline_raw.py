"""Fixed-whitepoint RAW input for local Look target rendering.

The firmware fitter keeps its established source rendering. The compatible
RAW helper fixes LibRaw's whitepoint while preserving camera WB and exposure;
Look targets use finer samples and filter only after display sRGB rendering.
"""
from __future__ import annotations


RAW_PARAMETERS = {'output_color': 'sRGB', 'gamma': [1, 1], 'output_bps': 16,
                  'use_camera_wb': True, 'no_auto_bright': True,
                  'adjust_maximum_thr': 0.0}

TARGET_RENDER_SETTINGS = {'supersample': 2, 'linear_prefilter': False,
                          'final_srgb_resample': 'area',
                          'final_srgb_gaussian_kernel': [3, 3],
                          'final_srgb_gaussian_sigma': .75,
                          'final_aspect': 'original_crop'}


def render_dng(path, width, exposure, *, backend, prefilter=True):
    """Return fixed-whitepoint linear sRGB and JSON-safe geometry metadata."""
    parameters = dict(RAW_PARAMETERS, output_color=backend.rawpy.ColorSpace.sRGB,
                      gamma=(1, 1))
    with backend.rawpy.imread(str(path)) as raw:
        white_level = int(raw.white_level)
        rgb = raw.postprocess(**parameters)
        sizes = raw.sizes
        left = sizes.crop_left_margin - sizes.left_margin
        top = sizes.crop_top_margin - sizes.top_margin
        rgb = rgb[top:top + sizes.crop_height, left:left + sizes.crop_width]
        geometry = {'raw_size': [sizes.raw_width, sizes.raw_height],
                    'crop': [left, top, sizes.crop_width, sizes.crop_height],
                    'libraw_rgb_size': [rgb.shape[1], rgb.shape[0]],
                    'white_level': white_level,
                    'raw_parameters': dict(RAW_PARAMETERS),
                    'baseline_exposure_ev': float(exposure)}
    normalized = rgb.astype(backend.np.float32) / 65535
    if prefilter:
        linear = backend.resized(normalized, width)
    else:
        render_width = min(int(width), rgb.shape[1])
        render_height = round(rgb.shape[0] * render_width / rgb.shape[1])
        linear = backend.cv2.resize(normalized, (render_width, render_height),
                                    interpolation=backend.cv2.INTER_AREA)
    geometry['rendered_size'] = [linear.shape[1], linear.shape[0]]
    geometry['prefilter'] = bool(prefilter)
    return backend.np.clip(linear * 2**exposure, 0, 1), geometry


def render_target(data, path, width, exposure, *, backend, base_profile):
    """Render the Look at 2x sampling, then resize/filter in display sRGB.

    Final height comes from the original RAW crop, avoiding a second aspect
    rounding when the intermediate height is odd (e.g. 1033 -> 517 rows).
    """
    from .offline_look import render_pixels

    linear, geometry = render_dng(path, int(width) * TARGET_RENDER_SETTINGS['supersample'],
                                  exposure, backend=backend, prefilter=False)
    rendered = render_pixels(data, linear, base_profile=base_profile)
    crop_width, crop_height = geometry['crop'][2:]
    final_size = [int(width), round(crop_height * width / crop_width)]
    target = backend.cv2.resize(rendered, tuple(final_size), interpolation=backend.cv2.INTER_AREA)
    target = backend.cv2.GaussianBlur(target, tuple(TARGET_RENDER_SETTINGS['final_srgb_gaussian_kernel']),
                                      TARGET_RENDER_SETTINGS['final_srgb_gaussian_sigma'])
    geometry['look_rendered_size'] = [rendered.shape[1], rendered.shape[0]]
    geometry['final_size'] = final_size
    geometry['target_render_settings'] = dict(TARGET_RENDER_SETTINGS)
    return target, geometry
