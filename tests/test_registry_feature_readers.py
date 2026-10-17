# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Filter registries retain crop and shutdown reader capabilities."""

from contextlib import ExitStack
from hashlib import sha256
from io import BytesIO
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

from gr4_editor import crops, demo_build, shutdown, slots


class CropReaderTests(unittest.TestCase):
    def read(self, supported):
        def dimensions(model, public, mag, selector):
            return {"model": model, "magnification": mag, "selector": selector,
                    "width": 720, "height": 480, "diagnostics": []}

        native = SimpleNamespace(
            diagnostics=[], magnifications=lambda model: [1.0],
            call=lambda *args: 100, preview_id=lambda public: 0,
            preview=lambda internal: {"width": 720, "height": 480},
            small=lambda *args: {"width": 720, "height": 480, "diagnostics": []},
            dimensions=dimensions, main_descriptor=dimensions,
            dng_window=lambda model, public, mag: {
                "model": model, "width": 720, "height": 480, "left": 0,
                "top": 0, "sensor_origin": [0, 0], "diagnostics": []},
            raw_options=lambda *args: {"diagnostics": []},
        )
        with ExitStack() as stack:
            stack.enter_context(patch.object(crops, "_Memory", return_value=object()))
            stack.enter_context(patch.object(crops, "menu_layout", return_value={"order": [0]}))
            stack.enter_context(patch.object(crops, "CropNative", return_value=native))
            stack.enter_context(patch.object(crops, "_name_resource", return_value=("3:2", {"id": 100})))
            stack.enter_context(patch.object(demo_build, "_metadata", return_value={}))
            stack.enter_context(patch.object(demo_build, "supports_image", return_value=False))
            stack.enter_context(patch.object(slots, "supports_image", return_value=supported))
            return crops.read_crops(b"synthetic native crop layout")

    def test_filter_registry_keeps_observed_crops_writable(self):
        rows, inspection = self.read(supported=True)
        self.assertEqual(rows[0]["identity"], {"public_id": 0, "text_id": 100, "preview_id": 0})
        self.assertEqual(rows[0]["actual_ratio"], "3:2")
        self.assertEqual(len(rows[0]["geometry"]["photo_sizes"]), 8)
        self.assertEqual(inspection["native_readback"], "passed")
        self.assertTrue(inspection["compiler_supported"])
        self.assertTrue(crops.capabilities(inspection)["can_write"])

    def test_unregistered_layout_stays_a_draft_after_successful_geometry_read(self):
        rows, inspection = self.read(supported=False)
        self.assertEqual(rows[0]["actual_ratio"], "3:2")
        self.assertEqual(inspection["native_readback"], "passed")
        self.assertFalse(inspection["compiler_supported"])
        self.assertFalse(crops.capabilities(inspection)["can_write"])
        self.assertTrue(crops.capabilities(inspection)["draft_only"])


class ShutdownReaderTests(unittest.TestCase):
    def setUp(self):
        self.rtos, self.canonical = b"synthetic filter registry", b"synthetic canonical input"
        stream = BytesIO()
        Image.new("RGB", (720, 480), (24, 48, 96)).save(stream, format="JPEG", quality=90)
        self.jpeg = stream.getvalue()
        section = bytearray(b"RES\0" + bytes(8) + struct.pack("<I", len(shutdown._TARGETS)))
        self.memory_bytes, self.words = {}, {}
        for index, (_identifier, native_id, filename, _name) in enumerate(shutdown._TARGETS):
            path = ("A:\\Resource\\Jpeg\\" + filename).encode("ascii")
            section.extend(path.ljust(32, b"\0") + struct.pack("<I", len(self.jpeg)) + self.jpeg)
            descriptor, path_address = 0x53100000 + index * 16, 0x53101000 + index * 64
            self.words[shutdown.JPEG_TABLE + native_id * 4] = descriptor
            for address, blob in ((descriptor, struct.pack("<HHI", 720, 480, path_address)),
                                  (path_address, path + b"\0")):
                self.memory_bytes.update({address + offset: byte for offset, byte in enumerate(blob)})
        self.code = []
        for name, start, _end, _expected in shutdown._CODE:
            blob = ("synthetic code: " + name).encode("ascii")
            self.code.append((name, start, start + len(blob), sha256(blob).hexdigest()))
            self.memory_bytes.update({start + offset: byte for offset, byte in enumerate(blob)})
        self.image = SimpleNamespace(rtos=self.rtos, sha256="1" * 64, internal_version="1.11.10.90",
                                     decoded=bytes(section), sections=[{
                                         "name": "RES", "start": 0, "end": len(section),
                                         "resource_count": len(shutdown._TARGETS)}])
        self.installed = {"schema_version": 2, "jpeg_table": {"address": hex(shutdown.JPEG_TABLE)},
                          "presets": [{"slot": 1, "selection_id": 17, "native_id": 12,
                                       "name": "Kept menu", "item_id": "kept-item", "fit": "contain",
                                       "payload_base64": shutdown._b64(self.jpeg)}]}

    def memory(self, data):
        lookup = dict(self.memory_bytes)
        # The installed menu changed its selection code. The reader must
        # obtain the original control-body proof from the verified baseline.
        if data != self.canonical:
            lookup[self.code[0][1]] ^= 1

        def read(address, size):
            return bytes(lookup[address + offset] for offset in range(size))

        return SimpleNamespace(read=read, word=lambda address: self.words[address],
                               pair=lambda *args: shutdown.JPEG_TABLE,
                               destination=0x56000000, source=0x54000000)

    def read(self, supported):
        with ExitStack() as stack:
            stack.enter_context(patch.object(shutdown, "_Memory", side_effect=self.memory))
            stack.enter_context(patch.object(shutdown, "_CODE", self.code))
            stack.enter_context(patch.object(demo_build, "_metadata", return_value={"shutdown_menu": self.installed}))
            stack.enter_context(patch.object(demo_build, "supports_image", return_value=False))
            stack.enter_context(patch.object(demo_build, "canonical_base", side_effect=ValueError("foreign registry")))
            stack.enter_context(patch.object(slots, "supports_image", return_value=supported))
            stack.enter_context(patch.object(slots, "canonical_base", return_value=self.canonical))
            stack.enter_context(patch("gr4_editor.shutdown_compiler.read_menu_resources", return_value=self.installed))
            return shutdown.read_shutdown(self.image)

    def test_filter_registry_recovers_factory_resources_and_editable_installed_menu(self):
        inventory = self.read(supported=True)
        self.assertEqual(inventory["inspection"]["resource_readback"], "passed")
        self.assertEqual([row["id"] for row in inventory["resources"]], ["goodbye", "hdf", "mono", "20th"])
        self.assertTrue(inventory["inspection"]["selection_code_verified"])
        self.assertTrue(inventory["capabilities"]["can_prepare_assets"])
        self.assertTrue(inventory["capabilities"]["can_build_menu"])
        self.assertEqual(inventory["native_selection"]["image_choices"], [0, 17])
        draft = shutdown.validate_draft(None, inventory)
        self.assertEqual(draft["target_id"], "goodbye")
        self.assertEqual([(row["id"], row["name"], row["selection_id"]) for row in draft["items"]],
                         [("kept-item", "Kept menu", 17)])
        self.assertEqual(shutdown._decode_b64(draft["assets"][0]["menu_payload_base64"], "menu"), self.jpeg)

    def test_unknown_registry_cannot_borrow_filter_canonical_proof(self):
        inventory = self.read(supported=False)
        self.assertEqual(inventory["resources"], [])
        self.assertFalse(inventory["capabilities"]["can_prepare_assets"])
        self.assertFalse(inventory["capabilities"]["can_build_menu"])
        self.assertIn("foreign registry", inventory["inspection"]["notes"])


if __name__ == "__main__":
    unittest.main()
