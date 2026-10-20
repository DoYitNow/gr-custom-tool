# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Execute generated video-summary ARM without distributing firmware fixtures.

Native getter and mapper calls use independent semantic fixtures. The callback
is synthetic and only models the two installed call sites and their argument
conventions; these tests make no native-ROM or camera execution claim.
"""
import struct
import unittest

from unicorn import Uc, UC_ARCH_ARM, UC_MODE_ARM, UC_HOOK_CODE, UC_HOOK_MEM_READ
from unicorn import arm_const as a

from gr4_editor import slots


GETTER_SITE, ICON_SITE = 0x531B15C4, 0x531B15D0
NATIVE_GETTER, NATIVE_ICON = 0x5329D154, 0x533971CC
IDENTITIES = [{'style_id': style, 'icon_id': 663 + style - 34}
              for style in range(34, 43)]
NATIVE_ICONS = {11: 366, 12: 563, 17: 217, 24: 19,
                31: 357, 32: 358, 33: 359}


class SummaryArm:
    USERDATA, MANAGER = 0x62001000, 0x62004000
    STACK, STOP, CALLBACK = 0x620FE000, 0x620FF000, 0x531B15B8
    CALLEE = tuple(getattr(a, 'UC_ARM_REG_R%d' % n) for n in range(4, 12))

    def __init__(self, image, origin, entries, code_bytes):
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_ARM)
        self.uc.mem_map(slots.BASE, (len(image) + 4095) & ~4095)
        self.uc.mem_write(slots.BASE, image)
        self.uc.mem_map(0x62000000, 0x100000)
        self.origin, self.end, self.entries = origin, origin + code_bytes, entries
        self.uc.hook_add(UC_HOOK_CODE, self.hook)
        self.uc.hook_add(UC_HOOK_MEM_READ, self.read_hook)
        self.reset()

    def reset(self):
        self.getter_calls, self.icon_calls, self.reads, self.wrapper_states = [], [], [], {}

    def hook(self, uc, address, size, context):
        if self.origin <= address < self.end:
            name = ('video_settings_getter' if address < self.entries['video_settings_icon']
                    else 'video_settings_icon')
            state = (uc.reg_read(a.UC_ARM_REG_SP), uc.reg_read(a.UC_ARM_REG_LR))
            if address == self.entries[name]:
                self.wrapper_states[name] = state
            assert self.wrapper_states[name] == state, 'summary wrapper changed SP or LR'
        if address == NATIVE_GETTER:
            pointer = uc.reg_read(a.UC_ARM_REG_R0)
            self.getter_calls.append(pointer)
            assert pointer == self.USERDATA
            selector, style = uc.mem_read(pointer + 0x514, 2)
            self.ret(30 + selector if selector else style)
        elif address == NATIVE_ICON:
            manager, style = uc.reg_read(a.UC_ARM_REG_R0), uc.reg_read(a.UC_ARM_REG_R1)
            self.icon_calls.append((manager, style))
            assert manager == self.MANAGER
            self.ret(NATIVE_ICONS[style])

    def read_hook(self, uc, access, address, size, value, context):
        if self.USERDATA <= address < self.USERDATA + 0x2000:
            self.reads.append((address - self.USERDATA, size))

    def ret(self, value):
        self.uc.reg_write(a.UC_ARM_REG_R0, value)
        self.uc.reg_write(a.UC_ARM_REG_PC, self.uc.reg_read(a.UC_ARM_REG_LR))

    def fields(self, video, selector=0, photo=11, photo_selector=0):
        self.uc.mem_write(self.USERDATA, bytes(0x2000))
        self.uc.mem_write(self.USERDATA + 0x152, bytes((photo_selector, photo)))
        self.uc.mem_write(self.USERDATA + 0x514, bytes((selector, video)))

    def call(self, entry, *args):
        self.reset()
        self.uc.reg_write(a.UC_ARM_REG_CPSR, 0x13)
        for n in range(13):
            self.uc.reg_write(getattr(a, 'UC_ARM_REG_R%d' % n), 0xA5000000 + n)
        for n, value in enumerate(args):
            self.uc.reg_write(getattr(a, 'UC_ARM_REG_R%d' % n), value)
        preserved = tuple(self.uc.reg_read(reg) for reg in self.CALLEE)
        self.uc.reg_write(a.UC_ARM_REG_SP, self.STACK)
        self.uc.reg_write(a.UC_ARM_REG_LR, self.STOP)
        self.uc.emu_start(entry, self.STOP, count=1000)
        assert self.uc.reg_read(a.UC_ARM_REG_PC) == self.STOP, 'ARM instruction budget exhausted'
        assert tuple(self.uc.reg_read(reg) for reg in self.CALLEE) == preserved
        assert self.uc.reg_read(a.UC_ARM_REG_SP) == self.STACK
        if entry in self.entries.values():
            assert self.uc.reg_read(a.UC_ARM_REG_LR) == self.STOP
        return self.uc.reg_read(a.UC_ARM_REG_R0)

    def summary(self, video, selector=0, photo=11, photo_selector=0):
        self.fields(video, selector, photo, photo_selector)
        return self.call(self.CALLBACK, self.USERDATA, self.MANAGER)


def synthetic_image():
    image = bytearray(0x800000)
    # Save the incoming UserData/manager, use the two real installation
    # addresses, then return the mapped icon. All other image bytes are zero.
    words = [0xE92D4030, 0xE1A04000, 0xE1A05001,
             slots._branch(GETTER_SITE, NATIVE_GETTER, link=True),
             0xE1A01000, 0xE1A00005,
             slots._branch(ICON_SITE, NATIVE_ICON, link=True), 0xE8BD8030]
    struct.pack_into('<8I', image, SummaryArm.CALLBACK - slots.BASE, *words)
    return image


def generated_arm(identities=IDENTITIES):
    patch = slots._Patch(synthetic_image())
    origin = slots.BASE + len(patch.image)
    words, entries = slots._video_settings_summary_code(origin, identities)
    patch.append(words, 16)
    return SummaryArm(bytes(patch.image), origin, entries, len(words) * 4)


class VideoSettingsSummaryTests(unittest.TestCase):
    def test_registered_video_styles_use_their_own_selection_and_icon(self):
        p = generated_arm()
        for identity in IDENTITIES:
            for photo, photo_selector in ((11, 0), (34, 0), (42, 3)):
                with self.subTest(video=identity['style_id'], photo=photo):
                    p.fields(identity['style_id'], photo=photo, photo_selector=photo_selector)
                    self.assertEqual(p.call(p.entries['video_settings_getter'], p.USERDATA),
                                     identity['style_id'])
                    self.assertEqual(p.getter_calls, [])
                    self.assertEqual(p.reads, [(0x514, 1), (0x515, 1)])
                    self.assertEqual(p.call(p.entries['video_settings_icon'], p.MANAGER,
                                            identity['style_id']), identity['icon_id'])
                    self.assertEqual(p.icon_calls, [])
                    self.assertEqual(p.reads, [])

    def test_native_styles_tail_call_the_original_entries_with_original_arguments(self):
        p = generated_arm()
        for style in (11, 12, 17, 24):
            with self.subTest(style=style):
                p.fields(style, photo=42)
                self.assertEqual(p.call(p.entries['video_settings_getter'], p.USERDATA), style)
                self.assertEqual(p.getter_calls, [p.USERDATA])
                self.assertEqual(p.reads, [(0x514, 1), (0x515, 1)])
                self.assertEqual(p.call(p.entries['video_settings_icon'], p.MANAGER, style),
                                 NATIVE_ICONS[style])
                self.assertEqual(p.icon_calls, [(p.MANAGER, style)])
                self.assertEqual(p.reads, [])

    def test_video_custom1_to_3_preserve_the_native_selector(self):
        p = generated_arm()
        for selector in (1, 2, 3):
            for stored in (11, 34, 42):
                with self.subTest(selector=selector, stored=stored):
                    p.fields(stored, selector, photo=42, photo_selector=3)
                    style = 30 + selector
                    self.assertEqual(p.call(p.entries['video_settings_getter'], p.USERDATA), style)
                    self.assertEqual(p.getter_calls, [p.USERDATA])
                    self.assertEqual(p.reads, [(0x514, 1)])
                    self.assertEqual(p.call(p.entries['video_settings_icon'], p.MANAGER, style),
                                     NATIVE_ICONS[style])
                    self.assertEqual(p.icon_calls, [(p.MANAGER, style)])

    def test_new_registry_styles_use_the_supplied_icon_identity(self):
        identities = [{'style_id': 47, 'icon_id': 0xBEEF},
                      {'style_id': 90, 'icon_id': 0xA123}]
        p = generated_arm(identities)
        for identity in identities:
            with self.subTest(identity=identity):
                p.fields(identity['style_id'], photo=34)
                self.assertEqual(p.call(p.entries['video_settings_getter'], p.USERDATA),
                                 identity['style_id'])
                self.assertEqual(p.getter_calls, [])
                self.assertEqual(p.call(p.entries['video_settings_icon'], p.MANAGER,
                                        identity['style_id']), identity['icon_id'])
                self.assertEqual(p.icon_calls, [])

    def test_empty_registry_keeps_native_video_and_custom_presets(self):
        p = generated_arm([])
        for selector in range(4):
            with self.subTest(selector=selector):
                p.fields(17, selector, photo=42)
                style = 30 + selector if selector else 17
                self.assertEqual(p.call(p.entries['video_settings_getter'], p.USERDATA), style)
                self.assertEqual(p.getter_calls, [p.USERDATA])
                self.assertEqual(p.call(p.entries['video_settings_icon'], p.MANAGER, style),
                                 NATIVE_ICONS[style])
                self.assertEqual(p.icon_calls, [(p.MANAGER, style)])

    def test_identity_registry_installs_both_calls_and_shared_arm_for_new_builds(self):
        rows = [{'identity': {**identity, 'maker_note_code': 0x8106 + index,
                              'playback_property': 21 + index, 'text_id': 700 + index}}
                for index, identity in enumerate(IDENTITIES)]
        patch = slots._Patch(synthetic_image())
        report = slots._identity_registry(patch, rows)
        entries = {name: int(report['entries'][name], 0)
                   for name in ('video_settings_getter', 'video_settings_icon')}
        for site, name in ((GETTER_SITE, 'video_settings_getter'), (ICON_SITE, 'video_settings_icon')):
            self.assertEqual(patch.word(site) & 0xFF000000, 0xEB000000)
            self.assertEqual(slots._target(site, patch.word(site)), entries[name])
        origin = entries['video_settings_getter']
        words, generated_entries = slots._video_settings_summary_code(origin, IDENTITIES)
        self.assertEqual(entries, generated_entries)
        self.assertEqual(bytes(patch.image[origin - slots.BASE:]),
                         struct.pack('<%dI' % len(words), *words))
        p = SummaryArm(bytes(patch.image), origin, entries, len(words) * 4)
        icons = {row['style_id']: row['icon_id'] for row in IDENTITIES}
        for video in (11, 12, 17, 24, *icons):
            for selector in range(4):
                for photo, photo_selector in ((11, 0), (34, 0), (42, 3)):
                    with self.subTest(video=video, selector=selector, photo=photo):
                        style = 30 + selector if selector else video
                        expected = icons[style] if style in icons else NATIVE_ICONS[style]
                        self.assertEqual(p.summary(video, selector, photo, photo_selector), expected)
                        self.assertTrue(all(offset in (0x514, 0x515) for offset, _ in p.reads))
                        self.assertEqual(set(p.wrapper_states), set(entries))
                        self.assertEqual(p.getter_calls,
                                         [] if selector == 0 and video in icons else [p.USERDATA])
                        self.assertEqual(p.icon_calls,
                                         [] if style in icons else [(p.MANAGER, style)])


if __name__ == '__main__':
    unittest.main()
