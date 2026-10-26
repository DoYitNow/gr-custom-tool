"""Version-specific resource reader/writer; every native 1.20 bank is retained."""
from copy import deepcopy
from hashlib import sha256
import struct

from .firmware import _Memory
from .resources120 import TABLE_SITES, ICON_COUNT, TEXT_COUNT, NATIVE_COLUMNS
from .slots import _mov

# Audited native field/API roles, formerly supplied by a local research JSON.
SCALAR_BINDINGS = {
    'source_rtos_sha256': 'cf51224e6eb6bed79eeafa2e7923acf5434df39629510e75907c1dfbffab61a9',
    'scalar_layout': {
        'getters': [1396471744,1396473512,1396475280,1396478120,1396480800,1396483480,1396486160,1396496392,1396499072],
        'setters': [1396429408,1396431456,1396433504,1396436896,1396440096,1396443296,1396446496,1396457916,1396461116],
        'property_setters': [1400368320,1400368372,1400368424,1400368476,1400368528,1400368580,1400368632,1400369100,1400369152],
        'app_get':1394869924,'userdata_get':1394871352,'node_get':1400176648,
        'still_movie':2934,'photo_selector':3222,'photo_styles':3223,
        'mutator_start':1395377132,'wrapper_start':1395377344,'wrapper_end':1395377528},
    'default_entries':[1396507964,1396510696,1396513428,1396517776,1396521900,1396526024,1396530148,1396549468,1396553592]}


def reader_helpers():
    try:
        from . import filter_reader as helpers
    except ImportError:
        from . import firmware as helpers
    return helpers


def text120(memory, identity):
    root = memory.word(0x55002118 + 4)
    descriptor = memory.word(root + identity * 4)
    header = memory.read(descriptor, 8)
    if header[1] != 1:
        raise ValueError('1.20 UTF-16 文字描述符无效')
    ptr, = struct.unpack_from('<I', header, 4)
    raw = memory.read(ptr, header[0] * 2)
    if not raw.endswith(b'\0\0'):
        raise ValueError('1.20 文字缺少结束符')
    return raw[:-2].decode('utf-16-le'), {'hex': raw.hex(), 'sha256':sha256(raw).hexdigest(), 'record_address':descriptor}


def icon120(memory, image, identity):
    catalog = memory.pair(0x534013F8, 0x534013FC)
    descriptor = memory.word(catalog + identity * 4)
    header = memory.read(descriptor, 12)
    kind, w, h, flags, at = struct.unpack('<HHHHI', header)
    size, offset = w * h * 4, at * 4
    if kind != 1 or not 0 < size or not 0 <= offset <= offset + size <= len(image.icon_bytes):
        raise ValueError('1.20 图标描述符/像素范围无效')
    raw = image.icon_bytes[offset:offset + size]
    return dict(hex=raw.hex(),sha256=sha256(raw).hexdigest(),offset=offset,bytes=size,width=w,height=h,
                flags=flags,pixel_format='RGBA8',descriptor_address=descriptor,descriptor_hex=header.hex())


def color120(memory, profile):
    h = reader_helpers()
    tables = []
    for family, stride, low, high in TABLE_SITES:
        word = memory.word(stride)
        if word & 0xFFFFFF00 != 0xE3A02000 or not 0 <= profile < word & 255:
            raise ValueError('1.20 颜色表列数/profile 无效')
        tables.append((family, word & 255, memory.pair(low, high)))
    banks = []
    for bank in range(3):
        descriptors, addresses = {}, {}
        for family, columns, table in tables:
            ptr = memory.word(table + (bank * columns + profile) * 4)
            addresses[family], descriptors[family] = ptr, memory.read(ptr, 128).hex()
        matrix, multi, gamma = (bytes.fromhex(descriptors[k]) for k in ('color_matrix','multi_axial','member_188'))
        main, aux = struct.unpack_from('<II', multi, 8)
        channels = [h._blob(memory, struct.unpack_from('<I', gamma, 8 + n * 4)[0], 512) for n in range(3)]
        gb = b''.join(bytes.fromhex(r['hex']) for r in channels)
        banks.append(dict(hardware_index=bank,descriptor_hex=descriptors,descriptor_addresses=addresses,
            camera_basis=h._blob(memory,struct.unpack_from('<I',matrix)[0],18),
            matrix=h._blob(memory,struct.unpack_from('<I',matrix,4)[0],18),
            multi_main_wrapper=h._blob(memory,main,20),multi_aux_wrapper=h._blob(memory,aux,20),
            multi_main=h._blob(memory,memory.word(main+16),2160),multi_aux=h._blob(memory,memory.word(aux+16),1440),
            gamma=dict(hex=gb.hex(),sha256=sha256(gb).hexdigest(),bytes=len(gb),channels=channels)))
    return {'banks':banks,'native_table_columns':[t[1] for t in tables]}


def raw(value, size):
    data = bytes.fromhex(value['hex'] if isinstance(value,dict) else value)
    if len(data) != size:
        raise ValueError('滤镜颜色或图标资源长度无效')
    return data


def install_resources120(patch,catalogs,icons,rows):
    """Re-encode payloads with target-native pointer templates, never old addresses.

    The supported custom writer edits the matrix/LUT/gamma payloads. Other
    native descriptor fields follow the complete official 1.20 Standard bank.
    """
    memory, columns = _Memory(bytes(patch.image)), NATIVE_COLUMNS + len(rows)
    tables, templates = {}, {}
    for family,stride,low,high in TABLE_SITES:
        if patch.word(stride) != 0xE3A02026:
            raise ValueError('资源编译基底不是原厂 1.20 的38列颜色表')
        original = struct.unpack('<114I',memory.read(memory.pair(low,high),456))
        tables[family] = [list(original[b*38:(b+1)*38])+[original[b*38+11]]*len(rows) for b in range(3)]
        templates[family] = [memory.read(original[b*38+11],128) for b in range(3)]
    result, payloads = bytearray(icons), []
    for index,row in enumerate(rows):
        item=row['identity']; item.update(profile_id=38+index,text_id=881+index,icon_id=709+index)
        banks=sorted(row['resources']['banks'],key=lambda b:b['hardware_index'])
        if [b['hardware_index'] for b in banks] != [0,1,2]:
            raise ValueError('每项自定义滤镜需要三个完整硬件 bank')
        for bank in banks:
            b=bank['hardware_index']; desc={f:bytearray(templates[f][b]) for f in tables}
            addresses={}; digests={}
            for name,size in (('camera_basis',18),('matrix',18),('multi_main',2160),('multi_aux',1440)):
                data=raw(bank[name],size); addresses[name]=patch.append(data,16); digests[name]=sha256(data).hexdigest()
            for name,target in (('multi_main_wrapper','multi_main'),('multi_aux_wrapper','multi_aux')):
                wrapper=bytearray(raw(bank[name],20)); struct.pack_into('<I',wrapper,16,addresses[target])
                addresses[name]=patch.append(wrapper,16)
            basis=struct.unpack_from('<I',desc['color_matrix'])[0]
            for at in range(0,128,4):
                if struct.unpack_from('<I',desc['color_matrix'],at)[0]==basis:
                    struct.pack_into('<I',desc['color_matrix'],at,addresses['camera_basis'])
            struct.pack_into('<I',desc['color_matrix'],4,addresses['matrix'])
            struct.pack_into('<II',desc['multi_axial'],8,addresses['multi_main_wrapper'],addresses['multi_aux_wrapper'])
            gamma=raw(bank['gamma'],1536); digests['gamma']=sha256(gamma).hexdigest()
            for n in range(3):struct.pack_into('<I',desc['member_188'],8+n*4,patch.append(gamma[n*512:(n+1)*512],16))
            for family,descriptor in desc.items():tables[family][b][item['profile_id']]=patch.append(descriptor,16)
            payloads.append({'style':item['style_id'],'bank':b,'sha256':digests})
        catalogs.add_text(item['text_id'],row['name'])
        catalogs.add_icon(item['icon_id'],raw(row.get('icon_rgba',row['resources']['icon']),6400),result)
    for family,stride,low,high in TABLE_SITES:
        values=[x for bank in tables[family] for x in bank];at=patch.append(struct.pack('<%dI'%len(values),*values),16)
        patch.set_word(stride,0xE3A02000|columns,'1.20 原厂38列与自定义滤镜资源')
        patch.set_word(low,_mov(3,at&65535),'1.20 颜色目录低位')
        patch.set_word(high,_mov(3,at>>16,high=True),'1.20 颜色目录高位')
    return bytes(result),{'payloads':payloads,'native_columns_preserved':38,'target_native_descriptors':True}
