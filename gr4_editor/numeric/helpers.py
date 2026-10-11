"""Runtime-only source inventory, DNG metadata and candidate review helpers."""
import hashlib
import io
import json
import struct
from pathlib import Path
from xml.etree import ElementTree as ET
import cv2
import numpy as np
import rawpy
from PIL import Image, ImageCms, ImageDraw
from .xmp import decode_table, export_look, export_rgb, CRS, RDF
from .tiff import ifd, value
from .polynomial import score as old_score
from .model import evaluate
from ..codec.gr4_gamma_codec import compose_gamma_sources, encode_gamma
CURVE_NAMES=("ToneCurvePV2012", "ToneCurvePV2012Red", "ToneCurvePV2012Green", "ToneCurvePV2012Blue")
cv2.setNumThreads(4)


def sha(blob):
    return hashlib.sha256(blob).hexdigest()


def dump(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def score(actual, reference):
    actual = np.asarray(actual, dtype=np.float32)
    reference = np.asarray(reference, dtype=np.float32)
    result = old_score(actual, reference)
    errors = np.abs(actual - reference).mean(1) * 255
    result.update(pixel_mean_p99_8bit=float(np.percentile(errors, 99)))
    return result


def settings(node):
    attrs = {k[len(CRS):]: v for element in node.iter()
             for k, v in element.attrib.items()
             if k.startswith(CRS) and not k[len(CRS):].startswith('Table_')}
    curves = {}
    for name in CURVE_NAMES:
        curve = node.find('.//' + CRS + name)
        if curve is not None:
            curves[name] = [[int(v.strip()) for v in point.text.split(',')]
                            for point in curve.findall('.//' + RDF + 'li')]
    return attrs, curves


def source_manifest(path, dest):
    data = Path(path).read_bytes()
    tree = ET.fromstring(data)
    attrs, curves = settings(tree)
    manifest = {'source': str(path.resolve()), 'source_sha256': sha(data),
                'attributes_without_table_payloads': attrs, 'tone_curves': curves,
                'tables': {}}
    name = tree.find('.//' + CRS + 'Name')
    manifest['preset_names'] = [item.text for item in name.findall('.//' + RDF + 'li')] if name is not None else []
    for reference, exporter, filename in (
        ('LookTable', export_look, 'look_table_hsv.csv'),
        ('RGBTable', export_rgb, 'rgb_table_only.cube')):
        if reference not in attrs:
            continue
        fingerprint = attrs[reference]
        encoded = next(e.attrib[CRS + 'Table_' + fingerprint]
                       for e in tree.iter() if CRS + 'Table_' + fingerprint in e.attrib)
        decoded = decode_table(encoded, fingerprint)
        Path(dest / (reference + '.decoded.bin')).write_bytes(decoded)
        manifest['tables'][reference] = {'fingerprint': fingerprint,
                                        'decoded_md5_matches': True,
                                        'decoded_sha256': sha(decoded),
                                        **exporter(decoded, Path(dest / filename))}
    manifest['scope'] = ('Complete supplied XMP inventory. An absent LookTable or curve is '
                         'recorded as absent; the RGB .cube remains only the RGBTable. '
                         'Targets use the local approximate renderer.')
    dump(Path(dest / 'xmp_manifest.json'), manifest)
    return manifest


def camera_metadata(path):
    with Image.open(path) as image:
        tags = image.tag_v2
        exif = image.getexif().get_ifd(34665)
        maker = tags[50740]
        if not (maker.startswith(b'RICOH\0II')):
            raise ValueError('Unsupported DNG MakerNote signature')
        entries = ifd(maker, 8, '<')
        versions = {}
        for tag in (0x27, 0x28):
            if tag in entries:
                raw = value(maker, entries[tag], '<')
                versions[hex(tag)] = {'raw_hex': raw.hex(),
                                      'parts_xor_ff': [b ^ 255 for b in raw] if len(raw) == 4 else None}
        return {'path': str(path.resolve()), 'sha256': sha(Path(path).read_bytes()),
                'camera_model': tags.get(272), 'software': tags.get(305),
                'capture_datetime': str(exif.get(36867, tags.get(306))),
                'baseline_exposure_ev': float(tags[50730]),
                'as_shot_neutral': [float(v) for v in tags[50728]],
                'maker_note_versions': versions,
                'capture_image_control_code': '0x%04x' % struct.unpack('<H', value(maker, entries[0x4f], '<'))[0],
                'version_boundary': 'Capture-time metadata, not a live device version getter.'}


def resized(rgb, width):
    height = round(rgb.shape[0] * width / rgb.shape[1])
    return cv2.GaussianBlur(cv2.resize(rgb, (width, height), interpolation=cv2.INTER_AREA), (3, 3), .75)


def target_metadata(path, manifest):
    """Read actual Lightroom JPEG metadata for Look and capture pairing audits."""
    with Image.open(path) as image:
        encoded = image.info.get('xmp')
        if not encoded:
            raise ValueError('Lightroom 目标 JPEG 缺少完整 XMP 元数据：' + path.name)
        try:
            tree = ET.fromstring(encoded)
        except ET.ParseError as exc:
            raise ValueError('Lightroom 目标 JPEG 的 XMP 无效：' + path.name) from exc
        look = tree.find('.//' + CRS + 'Look')
        if look is None:
            raise ValueError('Lightroom 目标 JPEG 缺少 Look：' + path.name)
        attrs, curves = settings(look)
        outer = next((element for element in tree.iter() if CRS + 'RawFileName' in element.attrib), None)
        if outer is None:
            raise ValueError('Lightroom 目标 JPEG 缺少原始文件名：' + path.name)
        outer_attrs = {key[len(CRS):]: item for key, item in outer.attrib.items()
                       if key.startswith(CRS) and not key[len(CRS):].startswith('Table_')}
        outer_curves = {}
        for name in CURVE_NAMES:
            node = outer.find(CRS + name)
            if node is not None:
                outer_curves[name] = [[int(value.strip()) for value in item.text.split(',')]
                                      for item in node.findall('.//' + RDF + 'li')]
        exif = image.getexif()
        detail = exif.get_ifd(34665)
        icc = image.info.get('icc_profile')
        profile = ImageCms.getProfileName(ImageCms.ImageCmsProfile(io.BytesIO(icc))).strip() if icc else None
        matches = (attrs.get('UUID') == manifest['attributes_without_table_payloads'].get('UUID')
                   and all(attrs.get(table) == info['fingerprint'] for table, info in manifest['tables'].items())
                   and curves == manifest['tone_curves'])
        retouch = [element.tag for element in tree.iter()
                   if any(word in element.tag for word in ('Mask', 'Retouch', 'Correction'))]
        return {'path': str(path.resolve()), 'sha256': sha(path.read_bytes()), 'size': list(image.size),
                'software': exif.get(305), 'capture_datetime': str(detail.get(36867)),
                'raw_file_name': outer_attrs.get('RawFileName'),
                'look_attributes': attrs, 'look_curves': curves,
                'look_uuid_tables_curves_match': matches,
                'camera_profile': outer_attrs.get('CameraProfile'), 'outer_develop_settings': outer_attrs,
                'outer_develop_curves': outer_curves, 'possible_local_adjustment_nodes': retouch,
                'icc_profile_name': profile, 'icc_profile_sha256': sha(icc) if icc else None,
                'classification': 'Lightroom target export; not a supplied camera JPEG.'}


def read_target(path, width):
    """Decode a Lightroom target in sRGB before using the common resize recipe."""
    with Image.open(path) as image:
        icc = image.info.get('icc_profile')
        if icc:
            profile = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            image = ImageCms.profileToProfile(image, profile, ImageCms.createProfile('sRGB'), outputMode='RGB')
        else:
            image = image.convert('RGB')
        return resized(np.asarray(image).astype(np.float32) / 255, width)


def alignment(source, target):
    """Check crop/rotation consistency; retain original shared pixel coordinates."""
    display = np.log1p(6 * source) / np.log(7)
    gray_a = cv2.cvtColor(np.rint(display * 255).astype(np.uint8), cv2.COLOR_RGB2GRAY)
    gray_b = cv2.cvtColor(np.rint(target * 255).astype(np.uint8), cv2.COLOR_RGB2GRAY)
    detector = cv2.SIFT_create(nfeatures=2500)
    key_a, des_a = detector.detectAndCompute(gray_a, None)
    key_b, des_b = detector.detectAndCompute(gray_b, None)
    if des_a is None or des_b is None:
        raise ValueError('DNG/Lightroom JPEG 没有足够的几何特征')
    matches = cv2.BFMatcher().knnMatch(des_a, des_b, k=2)
    good = [pair[0] for pair in matches if len(pair) == 2 and pair[0].distance < .7 * pair[1].distance]
    if len(good) < 8:
        raise ValueError('DNG/Lightroom JPEG 的几何匹配不足')
    a = np.float32([key_a[item.queryIdx].pt for item in good])
    b = np.float32([key_b[item.trainIdx].pt for item in good])
    transform, inliers = cv2.estimateAffinePartial2D(a, b, method=cv2.RANSAC, ransacReprojThreshold=1)
    if transform is None or inliers is None or not inliers.any():
        raise ValueError('DNG/Lightroom JPEG 无法核对几何关系')
    residual = np.linalg.norm(a @ transform[:, :2].T + transform[:, 2] - b, axis=1)
    scale = float(np.sqrt(abs(np.linalg.det(transform[:, :2]))))
    return {'sift_matches': len(good), 'inliers': int(inliers.sum()), 'affine': transform.tolist(),
            'scale': scale, 'translation_pixels': transform[:, 2].tolist(),
            'median_inlier_error_pixels': float(np.median(residual[inliers.ravel().astype(bool)])),
            'sampling': 'same image coordinates; no estimated transform applied'}, transform


def matched(source, target):
    x = source[::4, ::4].reshape(-1, 3)
    y = target[::4, ::4].reshape(-1, 3)
    logged = np.log1p(6 * x) / np.log(7)
    mask = ((logged.min(1) > .025) & (logged.max(1) < .975)
            & (y.min(1) > .025) & (y.max(1) < .975))
    return x[mask], y[mask]


def preview(source, model, coefficients, kind, native):
    flat = source.reshape(-1, 3)
    result = np.empty_like(flat)
    for start in range(0, len(flat), 65536):
        result[start:start + 65536] = evaluate(flat[start:start + 65536], model, coefficients, kind, native)
    return result.reshape(source.shape)


def write_preview(path, rgb):
    cv2.imencode('.jpg', cv2.cvtColor(np.rint(np.clip(rgb, 0, 1) * 255).astype(np.uint8),
                                   cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 95])[1].tofile(str(path))


def full_metrics(actual, target):
    errors = np.abs(actual - target).mean(2) * 255
    lab_actual = cv2.cvtColor(actual.astype(np.float32), cv2.COLOR_RGB2LAB)
    lab_target = cv2.cvtColor(target.astype(np.float32), cv2.COLOR_RGB2LAB)
    chroma_actual = np.linalg.norm(lab_actual[:, :, 1:], axis=2)
    chroma_target = np.linalg.norm(lab_target[:, :, 1:], axis=2)
    luma = target @ np.array([.2126, .7152, .0722])
    bands = {}
    for name, mask in [('shadow', luma < .1), ('midtone', (luma >= .1) & (luma <= .9)), ('highlight', luma > .9)]:
        bands[name] = {'pixels': int(mask.sum()), 'rgb_mae_8bit': float(errors[mask].mean()) if mask.any() else None}
    return {'rgb_mae_8bit': float(errors.mean()), 'pixel_mean_p90_8bit': float(np.percentile(errors, 90)),
            'pixel_mean_p99_8bit': float(np.percentile(errors, 99)),
            'mean_lstar_actual': float(lab_actual[:, :, 0].mean()), 'mean_lstar_target': float(lab_target[:, :, 0].mean()),
            'mean_chroma_actual': float(chroma_actual.mean()), 'mean_chroma_target': float(chroma_target.mean()),
            'chroma_ratio_actual_to_target': float(chroma_actual.mean() / max(chroma_target.mean(), 1e-12)),
            'target_luma_bands': bands}


def visual_reviews(dest, stems, heldout):
    contact = Image.new('RGB', (600, len(stems) * 230), (20, 20, 20))
    draw = ImageDraw.Draw(contact)
    for row, stem in enumerate(stems):
        with Image.open(Path(dest / (stem + '_target_srgb_preview.jpg'))) as image:
            contact.paste(image.resize((320, 213)), (0, row * 230 + 17))
        draw.text((330, row * 230 + 50), stem, fill='white')
    contact.save(Path(dest / 'materials_contact_sheet.jpg'))
    comparison = Image.new('RGB', (1350, 450), (22, 22, 22))
    draw = ImageDraw.Draw(comparison)
    draw.text((0, 5), heldout + ' untouched holdout | target / native hypothesis / abs error x4', fill='white')
    for col, suffix in enumerate(('target_srgb_preview', 'hypothesis_preview', 'error_x4')):
        with Image.open(Path(dest / (heldout + '_' + suffix + '.jpg'))) as image:
            comparison.paste(image.resize((450, 300)), (col * 450, 30))
    draw.text((0, 350), 'Software domain hypothesis. Not a camera JPEG or proven ISP model.', fill='white')
    draw.text((0, 380), 'Selection and fit exclude this target scene. Numerical errors are in fit_report.json.', fill='white')
    comparison.save(Path(dest / 'holdout_comparison.jpg'))


def resource_check(model, coeff, base):
    assert model['gamma_sources'].shape == (3, 256)
    assert model['gamma_sources'].min() >= 0 and model['gamma_sources'].max() <= 16384
    assert all(-32768 <= v <= 32767 for v in model['q13'])
    assert coeff.shape == (12, 5, 3, 3) and coeff.min() >= -2048 and coeff.max() <= 2047
    assert np.all(coeff.sum(-1) == 1024)
    _, gamma = compose_gamma_sources(base.tolist(), model['gamma_sources'].tolist())
    for curve in gamma:
        nodes, increments = encode_gamma(curve)
        assert len(nodes) == 512 and len(increments) == 2048
    return {'matrix_q13_s16': model['q13'], 'hypothesis_effective_matrix_q8': model['packed'],
            'multi_s12_min': int(coeff.min()), 'multi_s12_max': int(coeff.max()),
            'all_180_multi_rows_sum_to_1024': True, 'gamma_source_range': [int(model['gamma_sources'].min()), int(model['gamma_sources'].max())],
            'four_composed_gamma_curves_encode': True,
            'camera_base_matrix': 'Keep all three Standard camera-base matrix pointers. These sources are the style matrix only.'}


def render_dng(path, width, exposure):
    with rawpy.imread(str(path)) as raw:
        rgb = raw.postprocess(output_color=rawpy.ColorSpace.sRGB, gamma=(1, 1),
                              output_bps=16, use_camera_wb=True, no_auto_bright=True)
        sizes = raw.sizes
        left = sizes.crop_left_margin - sizes.left_margin
        top = sizes.crop_top_margin - sizes.top_margin
        rgb = rgb[top:top + sizes.crop_height, left:left + sizes.crop_width]
        geometry = {'raw_size': [sizes.raw_width, sizes.raw_height],
                    'crop': [left, top, sizes.crop_width, sizes.crop_height],
                    'libraw_rgb_size': [rgb.shape[1], rgb.shape[0]]}
    return np.clip(resized(rgb.astype(np.float32) / 65535, width) * 2**exposure, 0, 1), geometry
