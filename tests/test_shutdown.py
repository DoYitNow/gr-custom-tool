# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Synthetic image preparation and shutdown menu draft regressions."""
import base64

from copy import deepcopy

from dataclasses import replace

from hashlib import sha256

from io import BytesIO

import json

from pathlib import Path

import random

import struct

import sys

import unittest

import zipfile

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gr4_editor.firmware import BASE, load_image

from gr4_editor.shutdown import (
    _jpeg_info, _pad_jpeg, prepare_asset, read_shutdown, replacement_bundle, validate_draft,
    MAX_MENU_ITEMS,
)

def image_bytes(size=(120, 80), mode='RGB', color=(180, 50, 20), **save_args):
    result = BytesIO()
    picture = Image.new(mode, size, color)
    picture.save(result, format=save_args.pop('format', 'PNG'), **save_args)
    return result.getvalue()

def target(size=7264, width=720, height=480):
    return {'id': 'goodbye', 'path': 'A:\\Resource\\Jpeg\\GoodBye.jpg',
            'native_id': 1, 'size': size, 'width': width, 'height': height,
            'sha256': '0' * 64, 'offset': 100, 'record_offset': 64,
            'display_layer_verified': True,
            'selection_condition': {'summary': 'fixture', 'verified': True}}

def inventory(row):
    return {'schema_version': 1, 'resources': [row], 'source_sha256': '1' * 64,
            'source_version': 'fixture', 'inspection': {'resource_readback': 'fixture'}}

def draft_for(asset):
    return {'schema_version': 1, 'target_id': asset['target_id'],
            'selected_asset_id': asset['id'], 'assets': [asset]}

class AssetTests(unittest.TestCase):
    def test_baseline_rgb_exact_length_and_comment_padding(self):
        asset = prepare_asset(image_bytes(), 'unknown-suffix.xyz', target())
        payload = base64.b64decode(asset['payload_base64'])
        info = _jpeg_info(payload)
        self.assertEqual(len(payload), 7264)
        self.assertEqual((info['width'], info['height']), (720, 480))
        self.assertTrue(info['baseline'])
        self.assertEqual(info['mode'], 'RGB')
        self.assertEqual(info['channels'], 3)
        self.assertEqual([(row['horizontal'], row['vertical']) for row in info['sampling']],
                         [(2, 2), (1, 1), (1, 1)])
        self.assertGreater(asset['adaptation']['padding_bytes'], 0)
        self.assertEqual(asset['adaptation']['padding_bytes'], info['comment_bytes'])
        self.assertTrue(asset['checks']['replacement_ready'])
        self.assertFalse(asset['checks']['hardware_decoder_verified'])
        self.assertEqual(payload[-2:], b'\xff\xd9')
        self.assertEqual(base64.b64decode(asset['source_payload_base64']), image_bytes())
        self.assertNotEqual(asset['id'], prepare_asset(image_bytes(), 'same.png', target())['id'])

    def test_legal_padding_segment_size_edges_and_small_unrepresentable_delta(self):
        raw = image_bytes(format='JPEG')
        for difference in (0, 4, 5, 65537, 65538, 65540, 131075):
            with self.subTest(difference=difference):
                padded = _pad_jpeg(raw, len(raw) + difference)
                self.assertEqual(len(padded), len(raw) + difference)
                self.assertEqual(_jpeg_info(padded)['comment_bytes'], difference)
        for difference in (-1, 1, 2, 3):
            self.assertIsNone(_pad_jpeg(raw, len(raw) + difference))
        with self.assertRaisesRegex(ValueError, 'after its final EOI'):
            _jpeg_info(raw + b'junk')

    def test_alpha_black_background_contain_and_cover(self):
        picture = Image.new('RGBA', (200, 100), (255, 0, 0, 0))
        picture.paste((0, 0, 255, 255), (100, 0, 200, 100))
        source = BytesIO()
        picture.save(source, format='PNG')
        contain = prepare_asset(source.getvalue(), 'alpha.png', target(), 'contain')
        cover = prepare_asset(source.getvalue(), 'alpha.png', target(), 'cover')
        with Image.open(BytesIO(base64.b64decode(contain['payload_base64']))) as preview:
            self.assertLess(max(preview.getpixel((360, 10))), 10)  # letterbox
            self.assertLess(max(preview.getpixel((100, 240))), 10)  # transparent source
            self.assertGreater(preview.getpixel((600, 240))[2], 240)
        with Image.open(BytesIO(base64.b64decode(cover['payload_base64']))) as preview:
            self.assertGreater(preview.getpixel((600, 10))[2], 240)
        self.assertNotEqual(contain['sha256'], cover['sha256'])
        for fit in ('stretch', None):
            with self.assertRaisesRegex(ValueError, 'contain or cover'):
                prepare_asset(source.getvalue(), 'alpha.png', target(), fit)

    def test_orientation_applied_and_progressive_upload_becomes_baseline(self):
        exif = Image.Exif()
        exif[274] = 6
        source = image_bytes(size=(20, 40), format='JPEG', progressive=True, exif=exif)
        asset = prepare_asset(source, 'rotated.jpg', target())
        self.assertEqual([asset['source_width'], asset['source_height']], [20, 40])
        self.assertEqual([asset['oriented_width'], asset['oriented_height']], [40, 20])
        self.assertEqual(asset['source_exif_orientation'], 6)
        self.assertTrue(asset['checks']['baseline_jpeg'])

    def test_noisy_material_cannot_fit_but_keeps_preview_and_blocked_report(self):
        rng = random.Random(41)
        noisy = Image.frombytes('RGB', (720, 480), rng.randbytes(720 * 480 * 3))
        source = BytesIO()
        noisy.save(source, format='PNG')
        row = target(size=500)
        asset = prepare_asset(source.getvalue(), 'noise.png', row)
        self.assertFalse(asset['checks']['exact_length'])
        self.assertFalse(asset['checks']['replacement_ready'])
        self.assertEqual(asset['quality'], 5)
        self.assertTrue(asset['preview_data_url'].startswith('data:image/jpeg;base64,'))
        bundle = replacement_bundle(draft_for(asset), inventory(row), {'sha256': '1' * 64})
        with zipfile.ZipFile(BytesIO(bundle)) as archive:
            self.assertEqual(set(archive.namelist()), {'plan.json', 'report.json'})
            self.assertTrue(json.loads(archive.read('plan.json'))['blocked'])
            self.assertTrue(json.loads(archive.read('report.json'))['blocked'])

    def test_unknown_display_code_blocks_replacement_even_when_size_fits(self):
        row = target()
        row['display_layer_verified'] = False
        asset = prepare_asset(image_bytes(), 'solid.png', row)
        self.assertTrue(asset['checks']['exact_length'])
        self.assertFalse(asset['checks']['replacement_ready'])

    def test_json_roundtrip_rebuilds_client_checks_preview_and_preserves_sources(self):
        row = target()
        first = prepare_asset(image_bytes(), 'first.png', row)
        second = prepare_asset(image_bytes(color=(20, 50, 180)), 'second.png', row)
        value = draft_for(first)
        value['assets'].append(second)
        value = json.loads(json.dumps(value))
        value['assets'][0]['checks'] = {'fake': True}
        value['assets'][0]['preview_data_url'] = 'fake'
        saved = validate_draft(value, inventory(row))
        self.assertEqual(len(saved['assets']), 2)
        self.assertEqual(saved['selected_asset_id'], first['id'])
        self.assertTrue(saved['assets'][0]['checks']['replacement_ready'])
        self.assertNotEqual(saved['assets'][0]['preview_data_url'], 'fake')
        self.assertEqual(saved['assets'][1]['source_payload_base64'], second['source_payload_base64'])
        reloaded = validate_draft(json.loads(json.dumps(saved)), inventory(row))
        self.assertEqual(reloaded, saved)
        value['assets'] = [second]
        value['selected_asset_id'] = None
        self.assertEqual(len(validate_draft(value, inventory(row))['assets']), 1)

    def test_corrupt_backups_and_invalid_images_are_rejected(self):
        row = target()
        original = draft_for(prepare_asset(image_bytes(), 'safe.png', row))
        for field, value in [('sha256', 'x'), ('source_sha256', 'x'),
                             ('target_sha256', 'x'), ('width', 640),
                             ('source_width', 1), ('payload_base64', '!')]:
            with self.subTest(field=field):
                draft = deepcopy(original)
                draft['assets'][0][field] = value
                with self.assertRaises(ValueError):
                    validate_draft(draft, inventory(row))
        duplicate = deepcopy(original)
        duplicate['assets'].append(deepcopy(duplicate['assets'][0]))
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            validate_draft(duplicate, inventory(row))
        with self.assertRaisesRegex(ValueError, '无法读取图片，请选择有效的 JPG、PNG'):
            prepare_asset(b'not a picture', 'bad.png', row)
        # SOF0 also permits CMYK; it must not become a ready RGB replacement.
        cmyk = image_bytes(mode='CMYK', color=(0, 0, 0, 0), size=(720, 480), format='JPEG')
        corrupted = deepcopy(original)
        asset = corrupted['assets'][0]
        asset.update(payload_base64=base64.b64encode(cmyk).decode(),
                     sha256=sha256(cmyk).hexdigest(), bytes=len(cmyk))
        with self.assertRaisesRegex(ValueError, 'encoding/dimensions'):
            validate_draft(corrupted, inventory(row))

    def test_ready_bundle_has_identical_jpeg_plan_and_offline_boundary(self):
        row = target()
        asset = prepare_asset(image_bytes(), 'pattern.png', row)
        bundle = replacement_bundle(draft_for(asset), inventory(row), {'sha256': '1' * 64})
        with zipfile.ZipFile(BytesIO(bundle)) as archive:
            self.assertEqual(set(archive.namelist()), {'replacement-goodbye.jpg', 'plan.json', 'report.json'})
            self.assertEqual(archive.read('replacement-goodbye.jpg'), base64.b64decode(asset['payload_base64']))
            plan = json.loads(archive.read('plan.json'))
            self.assertEqual(plan['actual_build_parent_sha256'], '1' * 64)
            self.assertEqual(plan['target']['offset'], 100)
            self.assertFalse(plan['applied'])
            self.assertFalse(any(plan['boundary'].values()))
            self.assertFalse(json.loads(archive.read('report.json'))['checks']['hardware_decoder_verified'])
        with self.assertRaisesRegex(ValueError, 'input differs'):
            replacement_bundle(draft_for(asset), inventory(row), {'sha256': '2' * 64})
        with self.assertRaisesRegex(ValueError, 'select a custom'):
            replacement_bundle(validate_draft(None, inventory(row)), inventory(row), {})
        self.assertEqual(validate_draft(None, {'resources': []}),
                         {'schema_version': 2, 'target_id': None, 'selected_asset_id': None, 'assets': [],
                          'items': [], 'selected_item_id': None})

class ListDraftTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.target = target()
        cls.inventory = inventory(cls.target)
        cls.asset = prepare_asset(image_bytes(size=(200, 100)), '相片.png', cls.target)

    def make_draft(self, count=1):
        return {'schema_version': 2, 'target_id': 'goodbye', 'selected_asset_id': None,
                'assets': [deepcopy(self.asset)], 'selected_item_id': None,
                'items': [{'id': f'item-{index}', 'selection_id': 0x10000000 + index,
                           'name': f'相片 {index}', 'asset_id': self.asset['id'], 'fit': 'contain'}
                          for index in range(count)]}

    def test_schema1_only_enabled_bound_slots_migrate_and_keep_unbound_sources(self):
        second = prepare_asset(image_bytes(), '未使用.png', self.target)
        legacy = draft_for(self.asset)
        legacy.update(assets=[self.asset, second], menu_enabled=True,
                      preset_asset_ids=[None, self.asset['id']])
        migrated = validate_draft(legacy, self.inventory)
        self.assertEqual(migrated['schema_version'], 2)
        self.assertEqual(len(migrated['assets']), 2)
        self.assertEqual(len(migrated['items']), 1)
        item = migrated['items'][0]
        self.assertEqual((item['selection_id'], item['legacy_slot'], item['name']), (2, 2, '相片'))
        self.assertEqual(validate_draft(deepcopy(migrated), self.inventory), migrated)
        legacy['menu_enabled'] = False
        self.assertEqual(validate_draft(legacy, self.inventory)['items'], [])

    def test_empty_one_three_and_sixteen_rows_are_supported(self):
        for count in (0, 1, 3, MAX_MENU_ITEMS):
            with self.subTest(count=count):
                result = validate_draft(self.make_draft(count), self.inventory)
                self.assertEqual(len(result['items']), count)
                self.assertTrue(all(row['menu_ready'] for row in result['items']))

    def test_rename_reorder_remove_preserve_selection_and_item_ids(self):
        value = self.make_draft(3)
        original_ids = {row['id']: row['selection_id'] for row in value['items']}
        value['items'] = [value['items'][2], value['items'][0]]
        value['items'][0]['name'] = '新的名字'
        value['selected_item_id'] = value['items'][0]['id']
        normalized = validate_draft(value, self.inventory)
        self.assertEqual({row['id']: row['selection_id'] for row in normalized['items']},
                         {key: original_ids[key] for key in ('item-0', 'item-2')})
        self.assertEqual(normalized['items'][0]['name'], '新的名字')
        self.assertEqual(normalized['selected_item_id'], 'item-2')

    def test_fit_edits_update_one_entry_and_rebuild_preview_from_source(self):
        value = self.make_draft()
        contain = validate_draft(value, self.inventory)
        value['items'][0]['fit'] = 'cover'
        value['items'][0]['preview_data_url'] = 'client fabricated'
        cover = validate_draft(value, self.inventory)
        self.assertEqual(cover['items'][0]['id'], contain['items'][0]['id'])
        self.assertEqual(cover['items'][0]['selection_id'], contain['items'][0]['selection_id'])
        self.assertEqual(len(cover['assets']), 1)
        self.assertNotEqual(cover['items'][0]['menu_sha256'], contain['items'][0]['menu_sha256'])
        self.assertNotEqual(cover['items'][0]['preview_data_url'], 'client fabricated')

    def test_incomplete_row_can_save_and_invalid_rows_are_rejected(self):
        value = self.make_draft()
        value['items'][0]['asset_id'] = None
        self.assertFalse(validate_draft(value, self.inventory)['items'][0]['menu_ready'])
        cases = [('selection_id', 0), ('selection_id', -1), ('selection_id', 2**32),
                 ('selection_id', True), ('name', ''), ('name', 'a' * 25),
                 ('name', '照片📸'), ('name', 'bad\x00name'), ('name', '\ud800'),
                 ('asset_id', {}), ('asset_id', 'missing'), ('fit', 'stretch'),
                 ('legacy_slot', True), ('legacy_slot', 3)]
        for key, replacement in cases:
            with self.subTest(key=key, replacement=repr(replacement)):
                invalid = self.make_draft()
                invalid['items'][0][key] = replacement
                with self.assertRaises(ValueError):
                    validate_draft(invalid, self.inventory)
        for modify in ('duplicate-id', 'duplicate-selection', 'too-many', 'invalid-focus'):
            invalid = self.make_draft(3)
            if modify == 'duplicate-id': invalid['items'][1]['id'] = invalid['items'][0]['id']
            if modify == 'duplicate-selection': invalid['items'][1]['selection_id'] = invalid['items'][0]['selection_id']
            if modify == 'too-many': invalid = self.make_draft(MAX_MENU_ITEMS + 1)
            if modify == 'invalid-focus': invalid['selected_item_id'] = 'missing'
            with self.subTest(modify=modify), self.assertRaises(ValueError):
                validate_draft(invalid, self.inventory)
