"""Portable 1.20 writer, cold readback, retirement, and policy restoration."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import base64
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from gr4_editor import product120
from gr4_editor.firmware import load_image,_rebuild_decoded
from gr4_editor.codec.frames import section_rows

ROOT=Path(__file__).resolve().parents[2]
OFFICIAL=Path(os.environ.get('GR4_OFFICIAL120',ROOT/'data/official/gr4-v120/fwdc248b.bin'))


@unittest.skipUnless(OFFICIAL.is_file(),'requires user-supplied official 1.20 BIN (GR4_OFFICIAL120)')
class Product120Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from gr4_editor.shutdown import prepare_asset
        cls.image=load_image(OFFICIAL)
        cls.snapshot=product120.snapshot120(cls.image)
        filters=deepcopy(cls.snapshot['filters'])
        row=deepcopy(filters[0]);row.update(id='qa-custom',kind='custom',identity={},
            template_id=11,name='Portable 120',enabled=True)
        row['parameters'][0].update(default=6,value=6)
        filters.insert(1,row)
        crops=deepcopy(cls.snapshot['crops'])
        crops.append({'id':'qa-crop','kind':'custom','identity':{},'name':'2.35:1','requested_ratio':'2.35:1'})
        target=cls.snapshot['shutdown_inventory']['resources'][0]
        asset=prepare_asset(base64.b64decode(target['payload_base64']),'test.jpg',target)
        draft=deepcopy(cls.snapshot['shutdown']);draft['assets']=[asset]
        draft['items']=[{'id':'qa-picture','selection_id':0x4151,'name':'Test picture','asset_id':asset['id'],'fit':'contain'}]
        cls.output=product120.compile_product120(cls.image,filters,crops,draft,pictorialist=True,version='1.20.10.12')
        rtos,icons,res,_=cls.output
        decoded=_rebuild_decoded(cls.image,{'RTOS':rtos,'ICONBIN':icons,'RES':res})
        cls.cold=replace(cls.image,rtos=rtos,icon_bytes=icons,decoded=decoded,
            sections=section_rows(decoded),sha256='c'*64,version=(1,20,10,12))

    def test_independent_snapshot_and_color_payload_readback(self):
        snap=product120.snapshot120(self.cold)
        custom=next(r for r in snap['filters'] if r['kind']=='custom')
        self.assertEqual(custom['name'],'Portable 120')
        self.assertEqual(custom['parameters'][0]['default'],6)
        self.assertEqual([r['name'] for r in snap['shutdown']['items']],['Test picture'])
        self.assertTrue(snap['pictorialist'])
        self.assertEqual([r['id'] for r in snap['filters'][:3]],[11,'qa-custom',12])
        self.assertEqual(snap['crops'][-1]['requested_ratio'],'2.35:1')
        self.assertTrue(all(isinstance(r['id'],str) for r in snap['crops']))

    def test_complete_native_base_and_policy_roundtrips(self):
        rtos=self.output[0]
        for enabled in (False,True,False):
            rtos,proof=product120.prepare_update_policy(rtos,enabled)
            self.assertEqual(product120.canonical_base(rtos),self.image.rtos)
            self.assertEqual(proof['policy'],'allow_older' if enabled else 'factory')
            self.assertEqual(product120.metadata(rtos)['allow_older'],enabled)

    def test_official_rollback_wrap_does_not_compile_or_modify_program(self):
        rtos=product120.wrap_official_input(self.image)
        self.assertEqual(rtos[:len(self.image.rtos)],self.image.rtos)
        self.assertEqual(product120.metadata(rtos)['installation'],{})
        for enabled in (True,False):
            rtos,_=product120.prepare_update_policy(rtos,enabled)
            self.assertEqual(product120.canonical_base(rtos),self.image.rtos)

    def test_corrupted_footer_or_body_cannot_become_editable(self):
        body=bytearray(self.output[0]);body[0x1000]^=1
        self.assertFalse(product120.supports_image(bytes(body)))
        body=bytearray(self.output[0]);body[-10]^=1
        self.assertFalse(product120.supports_image(bytes(body)))

    def test_removal_retires_filter_and_crop_identity(self):
        snap=product120.snapshot120(self.cold)
        filters=[r for r in snap['filters'] if r['kind']!='custom']
        crops=[r for r in snap['crops'] if r['kind']!='custom']
        draft=deepcopy(snap['shutdown']);draft['items']=[]
        rtos,icons,res,proof=product120.compile_product120(self.cold,filters,crops,draft,pictorialist=False)
        record=product120.metadata(rtos)
        self.assertEqual(len(record['filter_records']),1)
        self.assertFalse(record['filter_records'][0]['enabled'])
        self.assertEqual(len(record['crop_records']),5)
        self.assertFalse(record['crop_records'][-1]['active'])
        self.assertFalse(record['pictorialist'])
        self.assertEqual(product120.canonical_base(rtos),self.image.rtos)
        self.assertEqual(icons[:len(self.image.icon_bytes)],self.image.icon_bytes)


if __name__=='__main__':unittest.main()
