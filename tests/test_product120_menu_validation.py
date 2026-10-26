"""A product filter menu must contain an entry the color body can select."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gr4_editor.product120 import validate_filter_menu120


def row(style, enabled=True, kind='native'):
    return {'id': style, 'kind': kind, 'enabled': enabled, 'identity': {'style_id': style}}


class Product120MenuValidationTests(unittest.TestCase):
    def test_disabling_every_filter_rejects(self):
        with self.assertRaisesRegex(ValueError, '至少保留一项活动滤镜'):
            validate_filter_menu120([row(style, False) for style in range(11, 33)])

    def test_empty_menu_rejects(self):
        with self.assertRaisesRegex(ValueError, '至少保留一项活动滤镜'):
            validate_filter_menu120([])

    def test_active_custom_filter_is_sufficient(self):
        validate_filter_menu120([row(11, False), row(34, kind='custom')])

    def test_pictorialist_switch_alone_is_sufficient(self):
        validate_filter_menu120([row(11, False), row(31, False)], pictorialist=True)

    def test_every_supported_color_body_native_style_can_be_the_only_entry(self):
        for style in range(11, 25):
            with self.subTest(style=style):
                validate_filter_menu120([row(style)])

    def test_closed_mono_entries_and_row31_do_not_bypass_the_switch(self):
        for style in range(25, 33):
            with self.subTest(style=style), self.assertRaisesRegex(ValueError, '至少保留一项活动滤镜'):
                validate_filter_menu120([row(style)])

    def test_inactive_custom_does_not_count(self):
        with self.assertRaisesRegex(ValueError, '至少保留一项活动滤镜'):
            validate_filter_menu120([row(34, False, 'custom')])


if __name__ == '__main__':
    unittest.main()
