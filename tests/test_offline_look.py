"""Local Look transforms preserve table domains and reject incomplete material."""
import hashlib
import colorsys
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET
import zlib

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gr4_editor import offline_look as look
from gr4_editor import calibration_assets as assets
from gr4_editor.numeric.xmp import ALPHABET, CRS, RDF


def _wire(raw):
    compressed = struct.pack('<I', len(raw)) + zlib.compress(raw)
    encoded = []
    for index in range(0, len(compressed), 4):
        part = compressed[index:index + 4]
        value = int.from_bytes(part, 'little')
        for _ in range(5 if len(part) == 4 else len(part) + 1):
            encoded.append(ALPHABET[value % 85])
            value //= 85
    return hashlib.md5(raw).hexdigest().upper(), ''.join(encoded)


def _rgb_table(function=lambda rgb: rgb, size=3, primaries=0, transfer=1, gamut=0, flags=0):
    axis = (np.arange(size) * 65535 + size // 2) // (size - 1)
    identity = np.stack(np.meshgrid(axis, axis, axis, indexing='ij'), axis=-1)
    values = np.rint(np.clip(function(identity / 65535.), 0., 1.) * 65535).astype(np.int64)
    delta = ((values - identity) & 65535).astype('<u2')
    return (struct.pack('<4I', 1, 1, 3, size) + delta.tobytes()
            + struct.pack('<3I2dI', primaries, transfer, gamut, 0., 2., flags))


def _hsv_table(hue=4, saturation=2, value=2, encoding=0, modify=None):
    table = np.zeros((value, hue, saturation, 3), dtype='<f4')
    table[..., 1:] = 1.
    if modify:
        modify(table)
    return struct.pack('<5I', 0, 1, hue, saturation, value) + table.tobytes() + struct.pack('<I', encoding)


def _xmp(rgb=None, hsv=None, attributes=None, curves=None):
    root = ET.Element(RDF + 'RDF')
    attrs = {'PresetType': 'Look', 'UUID': 'A' * 32, 'ConvertToGrayscale': 'False'}
    attrs.update(attributes or {})
    node = ET.SubElement(root, RDF + 'Description', {CRS + k: str(v) for k, v in attrs.items()})
    names = ET.SubElement(ET.SubElement(node, CRS + 'Name'), RDF + 'Alt')
    ET.SubElement(names, RDF + 'li').text = 'Synthetic Look'
    for kind, raw in (('RGBTable', rgb), ('LookTable', hsv)):
        if raw is not None:
            digest, encoded = _wire(raw)
            node.set(CRS + kind, digest)
            node.set(CRS + 'Table_' + digest, encoded)
    for key, points in (curves or {}).items():
        seq = ET.SubElement(ET.SubElement(node, CRS + key), RDF + 'Seq')
        for x, y in points:
            ET.SubElement(seq, RDF + 'li').text = f'{x}, {y}'
    return ET.tostring(root)


def _tone_asset(path, samples=None):
    samples = np.linspace(0., 1., 1025) if samples is None else samples
    record = {'kind': 'dng-sdk-acr3-default', 'source_url': 'synthetic-test', 'source_sha256': 'test',
              'samples_sha256': hashlib.sha256(struct.pack('<1025f', *samples)).hexdigest(),
              'samples': samples.tolist()}
    path.write_text(json.dumps(record), encoding='utf-8')


def _dcp_look(path):
    # Three first-IFD entries: dimensions, identity HSV samples, encoding.
    dimensions = struct.pack('<3I', 1, 2, 1)
    samples = struct.pack('<6f', 0., 1., 1., 120., 1., 1.)
    payload_start = 8 + 2 + 3 * 12 + 4
    entries = (struct.pack('<HHII', 50981, 4, 3, payload_start)
               + struct.pack('<HHII', 50982, 11, 6, payload_start + len(dimensions))
               + struct.pack('<HHII', 51108, 4, 1, 0))
    path.write_bytes(b'II' + struct.pack('<HIH', 0x4352, 8, 3) + entries + struct.pack('<I', 0) + dimensions + samples)


class OfflineLookTests(unittest.TestCase):
    def test_identity_look_returns_encoded_srgb_and_preserves_shape(self):
        data = _xmp(rgb=_rgb_table(), hsv=_hsv_table())
        values = np.array([[[0., 0., 0.], [.18, .18, .18]], [[1., 1., 1.], [.7, .15, .03]]])
        support = look.inspect_support(data)
        self.assertTrue(support['supported'], support['reason'])
        self.assertTrue(support['approximate'])
        self.assertEqual(support['recipe_version'], look.RECIPE_VERSION)
        self.assertFalse(support['target_render_settings']['linear_prefilter'])
        self.assertEqual(support['target_render_settings']['supersample'], 2)
        np.testing.assert_allclose(look.render_pixels(data, values), look._encode(values), atol=2e-5)

    def test_transfer_functions_have_expected_srgb_reference_values(self):
        linear = np.array([0., .0031308, .18, 1.])
        np.testing.assert_allclose(look._encode(linear), [0., .040449936, .4613561295, 1.], atol=1e-9)
        np.testing.assert_allclose(look._decode(look._encode(linear)), linear, atol=1e-9)

    def test_all_table_transfer_roundtrips_include_dark_toe(self):
        linear = np.r_[0., np.geomspace(1e-8, 1., 30)]
        for transfer in range(5):
            with self.subTest(transfer=transfer):
                np.testing.assert_allclose(look._decode(look._encode(linear, transfer), transfer), linear, atol=2e-10)

    def test_all_table_primaries_roundtrip_through_d50(self):
        values = np.array([[1., 1., 1.], [.12, .4, .7]])
        for primary in range(5):
            with self.subTest(primary=primary):
                working = look._convert(values, primary, 2)
                np.testing.assert_allclose(look._convert(working, 2, primary), values, atol=1e-12)
                np.testing.assert_allclose(working[0], [1., 1., 1.], atol=1e-12)
        # XMP big-table enum: 2 is ProPhoto; a DNG RGBTables tag uses 4 instead.
        support = look.inspect_support(_xmp(rgb=_rgb_table(primaries=2)))
        self.assertEqual(support['tables']['RGBTable']['primaries'], 'ProPhoto RGB')

    def test_identity_rgb_tables_across_encodings_do_not_double_gamma(self):
        values = np.array([[0., 0., 0.], [.08, .18, .48], [1., 1., 1.]])
        for primary in range(5):
            for transfer in range(5):
                with self.subTest(primary=primary, transfer=transfer):
                    data = _xmp(rgb=_rgb_table(primaries=primary, transfer=transfer))
                    np.testing.assert_allclose(look.render_pixels(data, values), look._encode(values), atol=2e-5)

    def test_tetrahedral_interpolation_uses_four_vertices(self):
        axis = np.array([0., 1.])
        grid = np.stack(np.meshgrid(axis, axis, axis, indexing='ij'), axis=-1)
        samples = np.stack([grid[..., 0] * grid[..., 1], grid[..., 1] * grid[..., 2], grid[..., 2] * grid[..., 0]], axis=-1)
        np.testing.assert_allclose(look._tetrahedral(samples, np.array([[.8, .5, .2]])), [[.5, .2, .2]])
        np.testing.assert_allclose(look._tetrahedral(samples, np.array([[1., 1., 1.], [0., 0., 0.]])), [[1., 1., 1.], [0., 0., 0.]])

    def test_rgb_table_channel_order_and_unsigned_delta_wrap(self):
        data = _xmp(rgb=_rgb_table(lambda rgb: rgb[..., [2, 1, 0]]))
        input_rgb = np.array([[.03, .18, .7]])
        result = look.render_pixels(data, input_rgb)
        np.testing.assert_allclose(result, look._encode(input_rgb[..., [2, 1, 0]]), atol=2e-5)

    def test_hsv_hue_wrap_interpolates_last_and_first_hue(self):
        samples = np.zeros((4, 2, 2, 3))
        samples[..., 1:] = 1.
        samples[3, :, :, 0] = 60.
        # h=.875 halfway between final hue (.75) and wrapped first hue (1).
        delta = look._trilinear(samples, np.array([[.875, .5, .5]]), periodic_first=True)
        np.testing.assert_allclose(delta, [[30., 1., 1.]])

    def test_hsv_neutral_and_single_value_axis(self):
        raw = _hsv_table(hue=1, value=1)
        table = look._read_hsv(raw)
        rgb = np.array([[0., 0., 0.], [.18, .18, .18], [.7, .2, .05]])
        np.testing.assert_allclose(look._apply_hsv(rgb, table), rgb, atol=1e-8)

    def test_hsv_srgb_encoding_applies_to_value_only(self):
        def modify(table):
            table[:, :, 1, 2] = .5
        table = look._read_hsv(_hsv_table(encoding=1, modify=modify))
        source = np.array([[.5, .25, 0.]])
        expected_value = look._decode(look._encode(np.array([.5])) * .5)[0]
        np.testing.assert_allclose(look._apply_hsv(source, table), [[expected_value, expected_value * .5, 0.]], atol=1e-8)

    def test_master_and_channel_curves_are_both_applied_before_rgb_table(self):
        curves = {'ToneCurvePV2012': [(0, 0), (255, 127.5)],
                  'ToneCurvePV2012Red': [(0, 0), (255, 127.5)]}
        data = _xmp(rgb=_rgb_table(lambda rgb: rgb * .8), curves=curves)
        source = np.array([[.18, .18, .18]])
        working = look._convert(source, 0, 2)
        encoded = look._encode(working) * .5
        encoded[..., 0] *= .5
        after_curves = look._decode(encoded)
        after_table = look._decode(look._encode(look._convert(after_curves, 2, 0)) * .8)
        np.testing.assert_allclose(look.render_pixels(data, source), look._encode(after_table), atol=2e-5)

    def test_curve_saturation_setting_is_explicitly_reported_as_approximate(self):
        curves = {'ToneCurvePV2012': [(0, 0), (127.5, 180), (255, 255)]}
        data = _xmp(rgb=_rgb_table(), curves=curves, attributes={'CurveRefineSaturation': 50})
        support = look.inspect_support(data)
        self.assertTrue(support['supported'])
        self.assertEqual(support['curve_refine_saturation'], 50)
        self.assertTrue(any('CurveRefineSaturation' in item for item in support['limitations']))
        low = look.render_pixels(_xmp(rgb=_rgb_table(), curves=curves, attributes={'CurveRefineSaturation': 0}), np.array([[.7, .2, .05]]))
        high = look.render_pixels(_xmp(rgb=_rgb_table(), curves=curves, attributes={'CurveRefineSaturation': 100}), np.array([[.7, .2, .05]]))
        self.assertGreater(float(np.max(np.abs(low - high))), .001)

    def test_gamut_extension_keeps_outside_table_residual(self):
        source = np.array([[.8, .04, .01]])  # ProPhoto primary lies outside sRGB.
        extended = look._read_rgb(_rgb_table(primaries=0, transfer=0, gamut=1))
        clipped = look._read_rgb(_rgb_table(primaries=0, transfer=0, gamut=0))
        np.testing.assert_allclose(look._apply_rgb(source, extended), source, atol=2e-5)
        self.assertGreater(float(np.max(np.abs(look._apply_rgb(source, clipped) - source))), .01)

    def test_incomplete_and_external_profiles_are_rejected(self):
        cases = [
            _xmp(), _xmp(rgb=_rgb_table(), attributes={'PresetType': 'Normal'}),
            _xmp(rgb=_rgb_table(), attributes={'CameraProfile': 'Camera Other'}),
            _xmp(rgb=_rgb_table(), attributes={'Stubbed': 'true'}),
            _xmp(hsv=_hsv_table(), attributes={'RequiresRGBTables': 'True'}),
        ]
        for data in cases:
            with self.subTest(data=data[:90]):
                self.assertFalse(look.inspect_support(data)['supported'])
                with self.assertRaises(ValueError):
                    look.render_pixels(data, np.array([[.1, .2, .3]]))

    def test_missing_payload_and_bad_fingerprint_are_rejected(self):
        for mutate in ('missing', 'fingerprint', 'truncated'):
            node = ET.fromstring(_xmp(rgb=_rgb_table())).find(RDF + 'Description')
            fingerprint = node.get(CRS + 'RGBTable')
            payload = node.get(CRS + 'Table_' + fingerprint)
            if mutate == 'missing':
                del node.attrib[CRS + 'Table_' + fingerprint]
            elif mutate == 'fingerprint':
                node.set(CRS + 'RGBTable', '0' * 32)
                node.set(CRS + 'Table_' + '0' * 32, payload)
            else:
                node.set(CRS + 'Table_' + fingerprint, payload[:-5])
            self.assertFalse(look.inspect_support(ET.tostring(node))['supported'])

    def test_unknown_color_parameters_and_nested_masks_are_rejected(self):
        for attributes in ({'Saturation': 30}, {'FutureColorSetting': 2}, {'ConvertToGrayscale': 'True'}, {'Amount': .5}):
            with self.subTest(attributes=attributes):
                self.assertFalse(look.inspect_support(_xmp(rgb=_rgb_table(), attributes=attributes))['supported'])
        tree = ET.fromstring(_xmp(rgb=_rgb_table()))
        ET.SubElement(tree.find(RDF + 'Description'), CRS + 'MaskGroupBasedCorrections')
        self.assertFalse(look.inspect_support(ET.tostring(tree))['supported'])
        tree = ET.fromstring(_xmp(rgb=_rgb_table()))
        ET.SubElement(tree, RDF + 'Description', {CRS + 'Exposure2012': '1'})
        self.assertFalse(look.inspect_support(ET.tostring(tree))['supported'])

    def test_invalid_curve_axis_and_boundaries_are_rejected(self):
        for points in ([(0, 0), (100, 80), (90, 90), (255, 255)], [(0, 0), (255, 256)], [(1, 0), (255, 255)], [(0, 0)]):
            with self.subTest(points=points):
                self.assertFalse(look.inspect_support(_xmp(rgb=_rgb_table(), curves={look.CURVES[0]: points}))['supported'])

    def test_unsupported_table_kind_encoding_and_flags_are_rejected(self):
        for raw in (_rgb_table(primaries=99), _rgb_table(transfer=99), _rgb_table(gamut=99), _rgb_table(flags=1),
                    struct.pack('<I', 99) + _rgb_table()[4:]):
            with self.subTest(header=raw[:16]):
                self.assertFalse(look.inspect_support(_xmp(rgb=raw))['supported'])
        self.assertFalse(look.inspect_support(_xmp(hsv=_hsv_table(encoding=99)))['supported'])

    def test_nonfinite_and_hdr_inputs_are_rejected(self):
        data = _xmp(rgb=_rgb_table())
        for pixels in (np.array([[np.nan, 0., 0.]]), np.array([[1.1, 0., 0.]]), np.array([[-.01, 0., 0.]]), np.zeros((2, 2))):
            with self.subTest(pixels=pixels):
                with self.assertRaises(ValueError):
                    look.render_pixels(data, pixels)

    def test_optional_base_tone_uses_hue_preserving_linear_domain(self):
        base = {'tone_curve': np.array([[0., 0.], [1., .5]]), 'look_table': None,
                'summary': {'kind': 'synthetic', 'limitations': []}}
        data = _xmp(rgb=_rgb_table())
        source = np.array([[.7, .18, .03], [.18, .18, .18]])
        np.testing.assert_allclose(look.render_pixels(data, source, base_profile=base), look._encode(source * .5), atol=2e-5)
        summary = look.inspect_support(data, base_profile=base)
        self.assertEqual(summary['base_profile']['kind'], 'synthetic')
        json.dumps(summary)

    def test_base_and_enhanced_hsv_accumulate_then_tone_precedes_rgb(self):
        base_table = look._read_hsv(_hsv_table(modify=lambda table: table[..., 0].fill(120.)))
        base = {'tone_curve': np.array([[0., 0.], [1., .5]]), 'look_table': base_table,
                'summary': {'kind': 'synthetic', 'limitations': []}}
        working = np.array([[.4, .7, .3]])
        source = look._convert(working, 2, 0)
        axis = (np.arange(3) * 65535 + 3 // 2) // 2 / 65535.
        squared_nodes = np.rint(axis ** 2 * 65535) / 65535.
        for extra_hue in (0., 60.):
            with self.subTest(enhanced_hue=extra_hue):
                data = _xmp(rgb=_rgb_table(lambda rgb: rgb ** 2, primaries=2, transfer=0),
                            hsv=_hsv_table(modify=lambda table: table[..., 0].fill(extra_hue)))
                h, s, v = colorsys.rgb_to_hsv(*working[0])
                rotated = np.array([colorsys.hsv_to_rgb((h + (120. + extra_hue) / 360.) % 1., s, v)])
                after_tone = rotated * .5
                after_rgb = np.array([[np.interp(channel, axis, squared_nodes) for channel in after_tone[0]]])
                expected = look._encode(np.clip(look._convert(after_rgb, 2, 0), 0., 1.))
                np.testing.assert_allclose(look.render_pixels(data, source, base_profile=base), expected, atol=2e-5)
                # Squared LUT before a half-strength tone gives a different result.
                reversed_rgb = np.array([[np.interp(channel, axis, squared_nodes) for channel in rotated[0]]]) * .5
                reversed_expected = look._encode(np.clip(look._convert(reversed_rgb, 2, 0), 0., 1.))
                self.assertGreater(float(np.max(np.abs(expected - reversed_expected))), .01)
        without_enhanced = _xmp(rgb=_rgb_table(lambda rgb: rgb ** 2, primaries=2, transfer=0))
        identity_enhanced = _xmp(rgb=_rgb_table(lambda rgb: rgb ** 2, primaries=2, transfer=0), hsv=_hsv_table())
        np.testing.assert_allclose(look.render_pixels(without_enhanced, source, base_profile=base),
                                   look.render_pixels(identity_enhanced, source, base_profile=base), atol=1e-8)

    def test_local_base_assets_and_dcp_provenance_are_json_safe(self):
        with tempfile.TemporaryDirectory() as folder:
            tone, dcp = Path(folder) / 'tone.json', Path(folder) / 'base.dcp'
            _tone_asset(tone)
            _dcp_look(dcp)
            base = look.load_base_profile(dcp, tone_path=tone)
            self.assertEqual(base['summary']['look_table']['grid'], [1, 2, 1])
            self.assertEqual(base['summary']['dcp']['sha256'], hashlib.sha256(dcp.read_bytes()).hexdigest())
            self.assertEqual(base['summary']['default_tone']['sha256'], hashlib.sha256(tone.read_bytes()).hexdigest())
            json.dumps(base['summary'])
            self.assertEqual(base['tone_curve'].shape, (1025, 2))

    def test_bundled_tone_preserves_rendering_and_records_distinct_cache_location(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict('os.environ', {'PROGRAMDATA': folder}):
            directory = Path(folder)
            tone = directory / 'original-tone.json'
            packaged = directory / 'assets'
            packaged.mkdir()
            _tone_asset(tone, np.linspace(0., 1., 1025) ** .8)
            (packaged / 'acr3-default-tone.json').write_bytes(tone.read_bytes())
            with patch.object(assets, 'ASSET_ROOT', packaged):
                bundled = look.load_base_profile()
            original = look.load_base_profile(tone_path=tone)
            np.testing.assert_array_equal(bundled['tone_curve'], original['tone_curve'])
            source = np.array([[.7, .18, .03], [.18, .18, .18]])
            data = _xmp(rgb=_rgb_table())
            np.testing.assert_array_equal(look.render_pixels(data, source, base_profile=bundled),
                                          look.render_pixels(data, source, base_profile=original))
            self.assertEqual(bundled['summary']['default_tone']['sha256'], original['summary']['default_tone']['sha256'])
            # Existing support hashes include provenance: a moved asset creates a
            # fresh identity for new submissions rather than reusing an old receipt.
            self.assertNotEqual(look.inspect_support(data, base_profile=bundled),
                                look.inspect_support(data, base_profile=original))
            bundled['summary']['default_tone']['path'] = original['summary']['default_tone']['path']
            self.assertEqual(bundled['summary'], original['summary'])

    def test_default_is_independent_of_installed_adobe_profiles(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict('os.environ', {'PROGRAMDATA': folder}):
            tone = Path(folder) / 'tone.json'
            _tone_asset(tone)
            base = look.load_base_profile(tone_path=tone)
            self.assertIsNone(base['summary']['dcp'])
            dcp = Path(folder) / 'Adobe/CameraRaw/CameraProfiles/Adobe Standard/RICOH GR IV Adobe Standard.dcp'
            dcp.parent.mkdir(parents=True)
            _dcp_look(dcp)
            discovered = look.load_base_profile(tone_path=tone)
            self.assertIsNone(discovered['summary']['dcp'])
            np.testing.assert_array_equal(discovered['tone_curve'], base['tone_curve'])

    def test_missing_or_corrupt_baseline_cannot_silently_fall_back(self):
        with tempfile.TemporaryDirectory() as folder:
            tone, dcp = Path(folder) / 'tone.json', Path(folder) / 'base.dcp'
            with self.assertRaisesRegex(ValueError, '缺少'):
                look.load_base_profile(tone_path=tone)
            _tone_asset(tone)
            with self.assertRaisesRegex(ValueError, '不存在'):
                look.load_base_profile(dcp, tone_path=tone)
            record = json.loads(tone.read_text(encoding='utf-8'))
            record['samples'][400] += .0001
            tone.write_text(json.dumps(record), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'SHA256'):
                look.load_base_profile(tone_path=tone)


if __name__ == '__main__':
    unittest.main()
