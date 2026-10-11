"""Portable local or Lightroom Look/profile fitting with the sequential recipe."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import struct
import sys
from typing import Any

from .bootstrap import PROJECT_ROOT
from .calibration import inspect_xmp
from .calibration_assets import load_gamma_base
from .color_fit import FIT_RECIPE, FIT_RECIPE_VERSION

RESOURCE_FILES = {'matrix': 'candidate_matrix.s16le', 'gamma': 'candidate_gamma.u16le',
                  'multi': 'candidate_multi_main.s32le'}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def _dependency(path: Path, purpose: str) -> dict:
    data = path.read_bytes()
    return {'path': str(path.resolve().relative_to(PROJECT_ROOT)).replace('\\', '/') if path.resolve().is_relative_to(PROJECT_ROOT) else str(path.resolve()),
            'sha256': _sha(data), 'bytes': len(data), 'purpose': purpose,
            'requires_local_file': True}


def _numeric_backend():
    from . import numeric
    return numeric


def generic_audit(metadata: dict, heldout: str, manifest: dict) -> dict:
    """Audit supplied pairs/versions/WB without naming any historical sample."""
    if len(metadata) < 3 or heldout not in metadata:
        raise ValueError('需要至少三对 DNG/Lightroom JPEG，并在拟合前明确指定独立检查样片')
    if not manifest['attributes_without_table_payloads'].get('UUID'):
        raise ValueError('Look/profile XMP 缺少可核对的 UUID')
    for stem, info in metadata.items():
        camera, target = info['dng'], info['target']
        if not target.get('look_uuid_tables_curves_match'):
            raise ValueError('目标 JPEG 的 Look UUID/表指纹/曲线与源 XMP 不匹配：' + stem)
        if target['look_attributes'].get('UUID') != manifest['attributes_without_table_payloads']['UUID']:
            raise ValueError('目标 Look UUID 与源 XMP 不一致：' + stem)
        for kind in ('LookTable', 'RGBTable'):
            expected = manifest.get('tables', {}).get(kind, {}).get('fingerprint')
            if target['look_attributes'].get(kind) != expected:
                raise ValueError('目标 Look 表指纹/缺省状态与源 XMP 不一致：' + stem)
        if target.get('look_curves', {}) != manifest.get('tone_curves', {}):
            raise ValueError('目标 Look 曲线与源 XMP 不一致：' + stem)
        capture = str(camera.get('capture_datetime', ''))
        target_capture = str(target.get('capture_datetime', ''))
        if capture in ('', 'None') or target_capture in ('', 'None'):
            raise ValueError('无法验证 DNG/JPEG 拍摄时间：' + stem)
        if not info.get('same_raw_name_and_capture_time') or capture != target_capture:
            raise ValueError('DNG/JPEG 文件名或拍摄时间不匹配：' + stem)
        if target.get('possible_local_adjustment_nodes'):
            raise ValueError('目标 JPEG 含当前机内模型不能表达的局部调整：' + stem)
        if not camera.get('sha256') or not target.get('sha256'):
            raise ValueError('样片缺少内容哈希：' + stem)
    settings = {stem: info['target']['outer_develop_settings'] for stem, info in metadata.items()}
    keys = sorted({key for row in settings.values() for key in row})
    differences = {key: {stem: row.get(key) for stem, row in settings.items()}
                   for key in keys if len({row.get(key) for row in settings.values()}) > 1}
    # File identity and camera As Shot values naturally differ per capture.
    rendering_keys = set(keys) - {'RawFileName', 'DateCreated', 'MetadataDate', 'ModifyDate'}
    rendering_differences = {key: differences[key] for key in sorted(rendering_keys) if key in differences}
    development = [stem for stem in metadata if stem != heldout]
    heldout_wb = settings[heldout].get('WhiteBalance')
    return {
        'paired_scenes': len(metadata), 'development_scenes': development,
        'heldout_scene': heldout, 'holdout_excluded_from_selection_and_fit': True,
        'cross_photo_attribute_differences': differences,
        'cross_photo_rendering_attribute_differences': rendering_differences,
        'white_balance_by_scene': {stem: row.get('WhiteBalance') for stem, row in settings.items()},
        'development_all_as_shot_wb': all(settings[stem].get('WhiteBalance') == 'As Shot' for stem in development),
        'heldout_white_balance': heldout_wb,
        'heldout_captured_as_shot_neutral': metadata[heldout]['dng'].get('as_shot_neutral'),
        'white_balance_boundary': (
            'LibRaw source uses each DNG camera AsShotNeutral. Target WhiteBalance is recorded per scene. '
            'A Custom or missing target WB does not establish equal-WB accuracy; As Shot mode alone '
            'does not prove identical Adobe/LibRaw white balance.'),
        'source_versions': {key: manifest['attributes_without_table_payloads'].get(key)
                            for key in ('Version', 'ProcessVersion')},
        'target_versions': {stem: {
            'outer': {key: settings[stem].get(key) for key in ('Version', 'ProcessVersion')},
            'look': {key: info['target']['look_attributes'].get(key) for key in ('Version', 'ProcessVersion')},
            'export_software': info['target'].get('software'),
        } for stem, info in metadata.items()},
        'look_amount_by_scene': {stem: info['target']['look_attributes'].get('Amount') for stem, info in metadata.items()},
        'outer_curves_by_scene': {stem: info['target'].get('outer_develop_curves', {}) for stem, info in metadata.items()},
        'pair_hashes': {stem: {'dng_sha256': info['dng']['sha256'], 'target_jpeg_sha256': info['target']['sha256']}
                        for stem, info in metadata.items()},
        'rendering_boundary': 'These JPEGs are Lightroom exports. Capture metadata is not a live device query or camera ISP validation.',
    }


def _offline_materials(backend, photos: Path, dng: Path, camera: dict, receipt: dict, width: int):
    """Verify local arrays directly, without fabricating Lightroom JPEG metadata."""
    rows = [row for row in receipt.get('samples', []) if row.get('stem') == dng.stem]
    if len(rows) != 1 or receipt.get('width') != width:
        raise ValueError('本地渲染样片记录或尺寸不匹配：' + dng.stem)
    row = rows[0]
    if row.get('dng_sha256') != camera['sha256']:
        raise ValueError('本地渲染的 DNG 哈希不匹配：' + dng.stem)
    arrays = []
    for kind, suffix in (('linear', '.linear.npy'), ('target', '.target.npy')):
        if row.get(kind + '_file') != dng.stem + suffix:
            raise ValueError('本地渲染材料文件名不匹配：' + dng.stem)
        path = photos / row[kind + '_file']
        if not path.is_file() or _sha(path.read_bytes()) != row.get(kind + '_sha256'):
            raise ValueError('本地渲染材料哈希不匹配：' + path.name)
        array = backend.np.load(path, allow_pickle=False)
        if (array.ndim != 3 or array.shape[2] != 3 or array.shape[1] != width
                or list(array.shape) != row.get('shape') or not backend.np.isfinite(array).all()
                or backend.np.min(array) < 0 or backend.np.max(array) > 1):
            raise ValueError('本地渲染像素格式不匹配：' + path.name)
        arrays.append(array)
    target = {'path': str(photos / row['target_file']), 'sha256': row['target_sha256'],
              'classification': 'Local approximate Look render; not Lightroom export or camera JPEG',
              'render_engine': 'offline', 'dng_sha256': row['dng_sha256']}
    return arrays[0], arrays[1], row['geometry'], target


def _native_context(backend, job) -> tuple[dict, Any, list[dict]]:
    """Read MultiAxial controls from the user's input, with a fixed numeric Gamma."""
    if not job.get('firmware_path'):
        raise ValueError('请先导入兼容输入，再创建校色任务')
    curve, gamma_dependency = load_gamma_base()
    from . import firmware
    _Memory, load_image = firmware._Memory, firmware.load_image
    _color_resources = getattr(firmware, '_color_resources', None)
    if _color_resources is None:
        from .filter_reader import _color_resources
    image = load_image(job['firmware_path'])
    if job.get('firmware_sha256') and image.sha256 != job['firmware_sha256']:
        raise ValueError('校色任务的输入文件哈希已变化')
    memory = _Memory(image.rtos)
    bank = _color_resources(memory, 11)['banks'][0]
    descriptor = bytes.fromhex(bank['descriptor_hex']['multi_axial'])
    native, sources = {}, {}
    for offset, size, fmt in ((0, 32, '<16H'), (4, 20, '<20B'), (16, 42, '<21H'), (20, 18, '<9H')):
        address = struct.unpack_from('<I', descriptor, offset)[0]
        raw = memory.read(address, size)
        native[hex(offset)] = list(struct.unpack(fmt, raw))
        sources[hex(offset)] = {'address': address, 'sha256': _sha(raw), 'bytes': size}
    dependency = {'path': str(Path(job['firmware_path']).resolve()), 'sha256': image.sha256,
                  'rtos_sha256': _sha(image.rtos), 'requires_local_file': True,
                  'purpose': 'Imported input Standard profile11/hardware0 MultiAxial controls',
                  'control_sources': sources, 'layout_proof': image.layout_proof}
    return native, backend.np.array(curve, dtype=int), [dependency, gamma_dependency]


def _fit_development(backend, samples, heldout, native, base, max_pixels):
    """Select the two sequential hypotheses; retain the initial Matrix/Gamma."""
    np = backend.np
    development = [stem for stem in samples if stem != heldout]

    def training(selected):
        if heldout in selected:
            raise ValueError('独立检查样片不能用于拟合/假设选择')
        x_rows, y_rows = [], []
        for stem in selected:
            x, y = samples[stem]
            indices = np.linspace(0, len(x) - 1, min(len(x), max_pixels)).astype(int)
            x_rows.append(x[indices])
            y_rows.append(y[indices])
        return np.concatenate(x_rows), np.concatenate(y_rows)

    folds = {}
    kinds, methods = ('luminance', 'chroma_l1'), ('sequential',)
    for test_scene in development:
        train = [stem for stem in development if stem != test_scene]
        x, y = training(train)
        initial = backend.make_base_model(x, y, base)
        tx, ty = samples[test_scene]
        fold = {'training_scenes': train, 'test_scene': test_scene,
                'fit_methods': {method: {} for method in methods}, 'refinement_history': {}}
        for kind in kinds:
            model = deepcopy(initial)
            coeff, _ = backend.fit_continuous_multi(np.clip(x @ model['matrix'].T, 0, 1), y,
                                                    kind, native, model['gamma'])
            fold['fit_methods']['sequential'][kind] = backend.score(backend.evaluate(tx, model, coeff, kind, native), ty)
        folds[test_scene] = fold
    means = {method: {kind: float(np.mean([fold['fit_methods'][method][kind]['rgb_mae_8bit']
                                         for fold in folds.values()])) for kind in kinds} for method in methods}
    if not np.isfinite([value for row in means.values() for value in row.values()]).all():
        raise ValueError('拟合选择误差包含非有限值')
    selected_method, selected = min(((method, kind) for method in methods for kind in kinds),
                                     key=lambda pair: means[pair[0]][pair[1]])
    for fold in folds.values():
        fold['hypotheses'] = fold['fit_methods'][selected_method]
    x, y = training(development)
    model = backend.make_base_model(x, y, base)
    coeff, support = backend.fit_continuous_multi(np.clip(x @ model['matrix'].T, 0, 1), y,
                                                 selected, native, model['gamma'])
    history = []
    selection = {'development_scenes': development, 'folds': folds, 'mean_mae': means,
                 'selected_fit_method': selected_method, 'selected_hypothesis': selected,
                 'refinement_history': history}
    return model, coeff, support, selection


def run_job(job: dict, receipt: dict) -> dict:
    """Return resource hex and a generic report, or raise without success data."""
    if job.get('fit_recipe_version', FIT_RECIPE_VERSION) != FIT_RECIPE_VERSION:
        raise ValueError('拟合配方版本已变化，请创建新校色任务')
    source = Path(job['xmp_path']).resolve()
    data = source.read_bytes()
    digest = _sha(data)
    inventory = inspect_xmp(data)
    if inventory['kind'] != 'profile':
        raise ValueError('普通 Develop 预设尚未有已验证的渲染/拟合适配，不能当作 Look/profile 转换')
    if not inventory['renderable']:
        raise ValueError('XMP 含当前颜色模型不能表达的局部调整')
    if digest != job.get('xmp_sha256') or receipt.get('xmp_sha256') != digest or receipt.get('status') != 'rendered':
        raise ValueError('渲染回执/任务与当前 XMP SHA256 不一致')
    engine = job.get('render_engine', 'offline')
    if engine not in ('offline', 'lightroom') or receipt.get('render_engine') != engine:
        raise ValueError('渲染方式与校色任务不匹配')
    local = engine == 'offline'
    if local:
        from .offline_look import inspect_support, load_base_profile
        support = inspect_support(data, base_profile=load_base_profile())
        if not support['supported']:
            raise ValueError(support['reason'])
        if (receipt.get('job_id') != job.get('id') or receipt.get('render_recipe_version') != support['recipe_version']
                or receipt.get('support') != support):
            raise ValueError('本地渲染配方与校色任务不匹配')
    else:
        from .lightroom_bridge import NATIVE_RENDER_RECIPE_VERSION
        if (receipt.get('job_id') != job.get('id')
                or receipt.get('render_recipe_version') != NATIVE_RENDER_RECIPE_VERSION
                or receipt.get('nonce') != job.get('render_nonce')):
            raise ValueError('Lightroom 回执与当前渲染请求不匹配')
    photos, output = Path(job['photos']).resolve(), Path(job['output']).resolve()
    pairs = sorted(path for path in photos.iterdir() if path.is_file() and path.suffix.lower() == '.dng')
    stems = [path.stem for path in pairs]
    heldout = job.get('heldout')
    if len(pairs) < 3 or len(set(stems)) != len(stems) or heldout not in stems:
        raise ValueError('需要至少三张 DNG，并预先指定其中一张独立检查样片')
    width, max_pixels = int(job.get('fit_width', 1550)), int(job.get('max_fit_pixels_per_scene', 12000))
    if width < 64 or max_pixels < 64:
        raise ValueError('拟合尺寸或训练像素数量太小')
    output.mkdir(parents=True, exist_ok=True)
    backend = _numeric_backend()
    manifest = backend.source_manifest(source, output)
    metadata = {}
    local_pixels = {}
    declared = {Path(row['name']).stem: row for row in job.get('dataset', {}).get('samples', [])}
    for dng in pairs:
        camera = backend.camera_metadata(dng)
        if local:
            linear, pixels, geometry, target = _offline_materials(backend, photos, dng, camera, receipt, width)
            local_pixels[dng.stem] = (linear, pixels, geometry)
        else:
            jpeg = dng.with_suffix('.jpg')
            declared_targets = [row for row in receipt.get('outputs', []) if row.get('name') == dng.stem]
            if len(declared_targets) != 1 or not jpeg.is_file():
                raise ValueError('缺少本次 Lightroom 导出材料：' + jpeg.name)
            target_record = declared_targets[0]
            if (target_record.get('dng_sha256') != camera['sha256']
                    or target_record.get('jpeg_sha256') != _sha(jpeg.read_bytes())):
                raise ValueError('Lightroom 导出材料哈希不匹配：' + dng.stem)
            target = backend.target_metadata(jpeg, manifest)
        if 'GR IV' not in str(camera.get('camera_model', '')).upper():
            raise ValueError('不是支持的相机 DNG：' + dng.name)
        if declared and (dng.stem not in declared or declared[dng.stem].get('sha256') != camera['sha256']):
            raise ValueError('DNG 与登记的标准样本哈希不一致：' + dng.name)
        paired = target['dng_sha256'] == camera['sha256'] if local else (
            str(target.get('raw_file_name', '')).lower() == dng.name.lower()
            and target.get('capture_datetime') == camera.get('capture_datetime'))
        metadata[dng.stem] = {'dng': camera, 'target': target, 'same_raw_name_and_capture_time': paired}
    if local:
        audit = {'paired_scenes': len(pairs), 'development_scenes': [stem for stem in stems if stem != heldout],
                 'heldout_scene': heldout, 'holdout_excluded_from_selection_and_fit': True,
                 'render_engine': 'offline', 'approximate': True, 'look_support': support,
                 'pair_hashes': {stem: {'dng_sha256': row['dng']['sha256'], 'target_array_sha256': row['target']['sha256']}
                                for stem, row in metadata.items()},
                 'rendering_boundary': receipt['boundary'],
                 'white_balance_boundary': ('LibRaw camera AsShotNeutral; installed DCP Look is used when present. '
                                            'DCP camera matrices/HueSat and Adobe Develop/PV processing are not reproduced.')}
    else:
        audit = generic_audit(metadata, heldout, manifest)
        audit.update(render_engine='lightroom', approximate=False)
    materials = {'source_xmp_sha256': digest, 'pairs': metadata, 'develop_settings_audit': audit,
                 'receipt': receipt, 'source_manifest': manifest,
                 'missing_materials': ['Same-shot camera JPEGs for direct ISP/pixel accuracy validation']}
    _write_json(output / 'materials_review.json', materials)
    native, base, dependencies = _native_context(backend, job)
    np = backend.np
    samples, inputs, targets, geometries = {}, {}, {}, {}
    # Only the registered DNG files and this job's verified rendered materials are used.
    for dng in pairs:
        stem = dng.stem
        if local:
            source_pixels, target, geometry = local_pixels[stem]
        else:
            target = backend.read_target(dng.with_suffix('.jpg'), width)
            source_pixels, geometry = backend.render_dng(dng, width, metadata[stem]['dng']['baseline_exposure_ev'])
        if source_pixels.shape != target.shape:
            raise ValueError('DNG/JPEG 裁切或旋转不一致：' + stem)
        alignment = ({'scale': 1.0, 'translation_pixels': [0, 0],
                      'method': 'Shared original RAW crop; target Look rendered at 2x sampling before display-sRGB resize'}
                     if local else backend.alignment(source_pixels, target)[0])
        if abs(alignment['scale'] - 1) > .002 or max(abs(v) for v in alignment['translation_pixels']) > 2:
            raise ValueError('DNG/JPEG 几何偏差太大：' + stem)
        geometry['alignment'] = alignment
        pair = backend.matched(source_pixels, target)
        if len(pair[0]) < 64:
            raise ValueError('可用的非裁切/非饱和配对像素太少：' + stem)
        inputs[stem], targets[stem], samples[stem], geometries[stem] = source_pixels, target, pair, geometry
    model, coeff, support, selection = _fit_development(backend, samples, heldout, native, base, max_pixels)
    development, folds = selection['development_scenes'], selection['folds']
    selected, selected_method = selection['selected_hypothesis'], selection['selected_fit_method']
    means = selection['mean_mae'][selected_method]
    layout = backend.resource_check(model, coeff, base)
    blobs = {'matrix': struct.pack('<9h', *model['q13']),
             'gamma': struct.pack('<768H', *model['gamma_sources'].reshape(-1).tolist()),
             'multi': struct.pack('<540i', *coeff.reshape(-1).tolist())}
    results = {}
    for stem, (tx, ty) in samples.items():
        rendered = backend.preview(inputs[stem], model, coeff, selected, native)
        results[stem] = {'role': 'independent_holdout' if stem == heldout else 'development_training',
                         'sampled': backend.score(backend.evaluate(tx, model, coeff, selected, native), ty),
                         'full_image': backend.full_metrics(rendered, targets[stem])}
        backend.write_preview(output / (stem + '_hypothesis_preview.jpg'), rendered)
        backend.write_preview(output / (stem + '_target_srgb_preview.jpg'), targets[stem])
        backend.write_preview(output / (stem + '_error_x4.jpg'), np.abs(rendered - targets[stem]) * 4)
    from . import numeric
    code_paths = [Path(numeric.__file__), *Path(numeric.__file__).parent.glob('*.py'),
                  Path(__file__), Path(__file__).with_name('color_fit.py'),
                  Path(__file__).with_name('calibration_assets.py')]
    if local:
        code_paths += [Path(__file__).with_name('offline_look.py'), Path(__file__).with_name('offline_raw.py')]
    else:
        from .lightroom_bridge import PRODUCT_ASSETS
        code_paths += [Path(__file__).with_name('lightroom_bridge.py'), *PRODUCT_ASSETS.rglob('*.lua'),
                       PRODUCT_ASSETS / 'import-presets.ps1']
    dependencies += [_dependency(path, 'Portable runtime conversion implementation') for path in sorted(set(code_paths))]
    report = {
        'status': 'ENGINEERING_COLOR_CANDIDATE_OFFLINE_ONLY', 'adapter': 'generic_look_profile_paired_fit',
        'implementation_sha256': _sha(Path(__file__).read_bytes()), 'source_xmp_sha256': digest,
        'fit_recipe_version': FIT_RECIPE_VERSION, 'fit_recipe': deepcopy(FIT_RECIPE),
        'render_engine': engine, 'rendering_boundary': audit['rendering_boundary'], 'approximate_rendering': local,
        'source_materials_report_sha256': _sha((output / 'materials_review.json').read_bytes()),
        'development_scenes': development, 'final_holdout_scene': heldout,
        'holdout_target_excluded_from_selection_and_fit': True, 'develop_settings_audit': audit,
        'development_leave_one_scene_out': folds, 'development_mean_mae_8bit': means,
        'development_fit_method_mean_mae_8bit': selection['mean_mae'],
        'selected_hypothesis': selected, 'selected_fit_method': selected_method,
        'refinement_history': selection['refinement_history'], 'candidate_evaluation': results,
        'resources': {RESOURCE_FILES[key]: {'bytes': len(blob), 'sha256': _sha(blob)} for key, blob in blobs.items()},
        'resource_layout_check': layout, 'region_bank_support': support, 'geometry': geometries,
        'native_standard_controls': native, 'dependencies': dependencies,
        'portable_without_local_research_fixtures': True,
        'runtime_versions': {'python': sys.version, 'numpy': np.__version__, 'opencv': backend.cv2.__version__, 'rawpy': backend.rawpy.__version__},
        'source_domain': ('DNG -> LibRaw camera-WB linear sRGB -> DNG BaselineExposure; target local Look approximation in sRGB.'
                          if local else 'DNG -> LibRaw camera-WB linear sRGB -> DNG BaselineExposure; target ICC converted to sRGB.'),
        'target_domain': ('DNG -> LibRaw camera WB, fixed DNG whitepoint (adjust_maximum_thr=0) -> '
                          'linear sRGB + BaselineExposure -> local SDK/Look approximation -> display sRGB.'
                          if local else 'Lightroom target JPEG, ICC converted to sRGB.'),
        'assumptions': [
            'LibRaw linear sRGB is a proxy, not an identified camera ISP buffer.',
            'Matrix -> MultiAxial -> Gamma order and Q10 RGB multiplication are pixel hypotheses.',
            'Native region controls and five-bank interpolation semantics remain unverified for camera pixels.',
            'Fixed style34 constructor-default/hardware0 numeric Gamma baseline is reused; it is a fitting-domain hypothesis.',
            'Keep the initial monotone polynomial Gamma envelope while fitting Multi; joint Gamma/Multi refinement is disabled. This is not a decoded Lightroom engine.',
            'Re-fitting generates replacement resources for the selected preset; other presets are retained.',
            'Retain original camera basis and auxiliary Multi sources in each target hardware descriptor.',
        ],
        'limits': ('Local targets are an approximation, not Lightroom exports. ' if local else 'Targets are actual Lightroom exports. ') + 'Independent holdout excluded from selection and fitting. No camera ISP accuracy, installation, boot, recovery or personal UserData result.',
        'hardware_verified': False,
    }
    for key, blob in blobs.items():
        (output / RESOURCE_FILES[key]).write_bytes(blob)
    np.savez(output / 'candidate_model.npz', q13=model['q13'], matrix=model['matrix'], gamma=model['gamma'],
             gamma_sources=model['gamma_sources'], multi=coeff)
    _write_json(output / 'fit_report.json', report)
    backend.visual_reviews(output, list(samples), heldout)
    return {'resources': {key: blob.hex() for key, blob in blobs.items()}, 'fit_report': report}
