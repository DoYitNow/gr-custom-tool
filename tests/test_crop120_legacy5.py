"""Keep unrelated native panoramic words intact on fresh 1.20 projects."""
from copy import deepcopy
from pathlib import Path
import struct
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from gr4_editor.crop120 import LIVE_NATIVE5_COEFFICIENTS,install_geometry120
from gr4_editor.product120_crops import factory_crops120,preview_geometry120,CropNative120
from gr4_editor.crop_geometry import asm
from gr4_editor.slots import BASE,_Patch

ROOT=Path(__file__).resolve().parents[2]
RTOS=ROOT/'data/firmware/research/v120-migration-20261022/download.rtos'


@unittest.skipUnless(RTOS.is_file(),'requires local official 1.20 native input')
class LegacyPanoramic120Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.native=RTOS.read_bytes()
        cls.factory=factory_crops120(cls.native)

    def test_no_legacy5_keeps_all_eight_original_words(self):
        patch=_Patch(self.native)
        report=install_geometry120(patch,deepcopy(self.factory),baseline_rtos=self.native)
        self.assertEqual(report['native5_live_coefficients'],[])
        for at,register,old,new in LIVE_NATIVE5_COEFFICIENTS:
            self.assertEqual(patch.image[at-BASE:at-BASE+4],self.native[at-BASE:at-BASE+4])
            self.assertEqual(patch.image[at-BASE:at-BASE+4],asm(f'mov {register},#{old}',at))

    def test_legacy5_retains_the_eight_reviewed_ratio_replacements(self):
        proposal=preview_geometry120(self.native,'2.35:1')
        legacy={'id':'legacy5','kind':'custom','name':'2.35:1','requested_ratio':'2.35:1',
            'identity':{'public_id':5},'geometry':proposal['geometry']}
        patch=_Patch(self.native)
        report=install_geometry120(patch,[*deepcopy(self.factory),legacy],baseline_rtos=self.native)
        self.assertEqual(len(report['native5_live_coefficients']),8)
        for at,register,old,new in LIVE_NATIVE5_COEFFICIENTS:
            self.assertEqual(patch.image[at-BASE:at-BASE+4],asm(f'mov {register},#{new}',at))
        self.assertIn('native180:77',report['native5_live_roi'])
        # The condition change must retain the reviewed AF aperture and all
        # normal/small focus-box dimensions when legacy5 is actually present.
        from unicorn import UC_HOOK_CODE
        from unicorn import arm_const as arm
        native=CropNative120(bytes(patch.image))
        def service(cpu,at,size,unused):
            if at in (0x532406A4,0x53240C38,0x538F881C):
                cpu.reg_write(arm.UC_ARM_REG_R0,0x62010000 if at!=0x538F881C else 1)
                cpu.reg_write(arm.UC_ARM_REG_PC,cpu.reg_read(arm.UC_ARM_REG_LR))
        native.uc.hook_add(UC_HOOK_CODE,service)
        native.uc.mem_write(0x62010000,bytes(0x2000))
        native.uc.mem_write(0x62010000+0xFE3,b'\x05')
        native.call(0x533ADEB8,0,0x6202D000)
        self.assertEqual(struct.unpack('<4I',native.uc.mem_read(0x6202D000,16)),(80,120,560,240))
        for mode,size in ((0,(560,240)),(14,(336,144)),(15,(336,144))):
            native.call(0x533AE04C,0x6202D000,0,mode)
            self.assertEqual(struct.unpack('<2I',native.uc.mem_read(0x6202D000,8)),size)


if __name__=='__main__':unittest.main()
