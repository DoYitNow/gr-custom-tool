# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Crop identity preservation and retirement without author archive files."""
from copy import deepcopy
import unittest

from gr4_editor.crop_registry import plan_crops


def originals():
    rows = [{'id': f'crop-{public}', 'kind': 'factory', 'identity': {'public_id': public},
             'name': ratio, 'requested_ratio': ratio}
            for public, ratio in ((0, '3:2'), (1, '4:3'), (3, '1:1'), (2, '16:9'))]
    rows.append({'id': 'custom', 'kind': 'custom', 'identity': {'public_id': 5},
                 'name': 'Wide', 'requested_ratio': '65:24', 'actual_ratio': '65:24',
                 'geometry': {'screen': {'width': 704, 'height': 260}}})
    return rows


class CropIdentityTests(unittest.TestCase):
    def test_rename_preserves_identity_and_geometry(self):
        source = originals()
        desired = deepcopy(source)
        desired[-1]['name'] = 'New name'
        # Exact equivalent spelling avoids a new identity.
        desired[-1]['requested_ratio'] = '130:48'
        planned = plan_crops(source, desired)
        self.assertEqual(planned['order'][-1], 5)
        self.assertEqual(planned['crops'][-1]['geometry'], source[-1]['geometry'])

    def test_new_geometry_retires_identity_and_preserves_archive_on_next_edit(self):
        source = originals()
        desired = deepcopy(source)
        desired[-1]['requested_ratio'] = '3:1'
        first = plan_crops(source, desired)
        self.assertEqual(first['order'][-1], 7)
        retired = next(row for row in first['records'] if row['identity']['public_id'] == 5)
        self.assertFalse(retired['active'])
        self.assertEqual(retired['geometry'], source[-1]['geometry'])
        desired = deepcopy(first['crops'][:-1])
        desired.append({'id': 'new', 'kind': 'custom', 'name': 'Portrait', 'requested_ratio': '4:5'})
        second = plan_crops(first['crops'], desired, records=first['records'], next_public=first['next_public'])
        self.assertEqual(second['order'][-1], 8)
        self.assertFalse(next(row for row in second['records'] if row['identity']['public_id'] == 7)['active'])

    def test_factory_protection_and_byte_boundary(self):
        source = originals()
        with self.assertRaisesRegex(ValueError, '原厂'):
            plan_crops(source, source[1:])
        desired = deepcopy(source)
        desired += [{'id': f'new-{i}', 'kind': 'custom', 'name': 'Ratio', 'requested_ratio': '3:1'} for i in range(2)]
        with self.assertRaisesRegex(ValueError, 'byte'):
            plan_crops(source, desired, next_public=255)


if __name__ == '__main__':
    unittest.main()
