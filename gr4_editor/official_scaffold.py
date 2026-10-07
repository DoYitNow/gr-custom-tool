# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Initialize only crop/End Screen catalogs and native persistence entries."""


def initialize_scaffold(patch):
    from .demo_build import _branch
    from .official_catalogs import initialize_catalogs

    catalogs = initialize_catalogs(patch)
    continuations = []
    for site in (0x533E1C04, 0x533E17CC):
        instruction = patch.word(site)
        if instruction != 0xE1A0C00D:
            raise ValueError("官方设置保存/加载入口与适配布局不一致")
        address = patch.append(lambda at, site=site, instruction=instruction:
                               [instruction, _branch(at + 4, site + 4)], 16)
        patch.set_word(site, _branch(site, address), "preserve native persistence continuation")
        continuations.append({"site": hex(site), "continuation": hex(address)})
    return {"base": "official-1.11.10.7", "catalogs": catalogs,
            "continuations": continuations,
            "resources": "read from the user's input"}
