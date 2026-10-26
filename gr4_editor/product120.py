"""Portable product compiler for user-supplied official GR IV 1.20 firmware.

The product has its own recoverable footer. Research revision images remain
inspection-only; no private .97 package, old ROM, or build ledger is required.
"""
from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace
import base64
import json
import struct
import zlib

from .firmware import _Memory
from .slots import BASE, _Patch, _branch, _mov, _target, PARAMETER_IDS
from .layout120_userdata import SOURCE_RTOS_SHA256
from .product120_resources import (reader_helpers, color120, text120, icon120,
                                  install_resources120, SCALAR_BINDINGS)

OFFICIAL_RTOS_SHA256 = SOURCE_RTOS_SHA256
SOURCE_SHA256 = '7563d7e462f0c53b6ab2fbab7cdfbbb57151a41192ee82f4f3a2108f9d77650d'
NATIVE_VERSION_WORDS = {0x5401BCE0:0x01140A0B,0x5401BDFC:0x01140A0B,0x5401BF4C:0x01140A0B,
                        0x53AA06AC:0xE3000A0B,0x53AA06D0:0xE3A0000B}
MAGIC = b'GR4P1201'
COMPILER = 'gr4-product-1.20-v1'
MAX_ACTIVE_CUSTOM_SLOTS = 16


def capabilities():
    return {'maximum_custom_slots':16,'maximum_retained_custom_slots':57,
        'newslots_supported':True,'removal_supported':True,'replacement_supported':True,
        'icon_edit_supported':True,'parameter_defaults_known':True,'menu_reorder_supported':True,
        'maximum_custom_crops':250,'maximum_shutdown_images':16,'hardware':'not_tested'}


def _sha(data):return sha256(data).hexdigest()


def body_digest120(body):
    data=bytearray(body)
    for site in NATIVE_VERSION_WORDS:data[site-BASE:site-BASE+4]=bytes(4)
    return _sha(data)


def _finish(body,record,minimum=0):
    body=bytearray(body);body.extend(bytes((-len(body))%4))
    record={**record,'compiler':COMPILER,'schema_version':1,'body_sha256':body_digest120(body)}
    encoded=json.dumps(record,ensure_ascii=True,separators=(',',':')).encode('ascii')
    encoded+=b' '*(-len(encoded)%4)
    padding=max(0,minimum-(len(body)+len(encoded)+16))
    if padding:
        body.extend(bytes(padding))
        record['body_sha256']=body_digest120(body)
        encoded=json.dumps(record,ensure_ascii=True,separators=(',',':')).encode('ascii')
        encoded+=b' '*(-len(encoded)%4)
    body+=encoded+MAGIC+struct.pack('<I',len(encoded))+bytes(4)
    struct.pack_into('<I',body,len(body)-4,(-sum(r[0] for r in struct.iter_unpack('<I',body)))&0xFFFFFFFF)
    return bytes(body)


def metadata(rtos):
    if len(rtos)<16 or rtos[-16:-8]!=MAGIC:return None
    if len(rtos)%4 or sum(x[0] for x in struct.iter_unpack('<I',rtos))&0xFFFFFFFF:
        raise ValueError('1.20 产品 RTOS 校验失败')
    size=struct.unpack_from('<I',rtos,len(rtos)-8)[0];start=len(rtos)-size-16
    if start<0 or start%4:raise ValueError('1.20 产品记录长度无效')
    value=json.loads(rtos[start:start+size])
    if (value.get('compiler')!=COMPILER or value.get('schema_version')!=1 or
        value.get('compiler_base_sha256')!=SOURCE_RTOS_SHA256 or value.get('body_sha256')!=body_digest120(rtos[:start])):
        raise ValueError('1.20 产品记录或正文身份不一致')
    return value


inspect_metadata=metadata


def canonical_base(rtos):
    if _sha(rtos)==SOURCE_RTOS_SHA256:return bytes(rtos)
    value=metadata(rtos)
    if value is None:raise ValueError('该 1.20 固件不是支持的原厂输入或产品产物')
    size=value['compiler_base_size'];out=bytearray(rtos[:size])
    for address,word in NATIVE_VERSION_WORDS.items():struct.pack_into('<I',out,address-BASE,word)
    seen=set()
    for item in value['compiler_base_restore']:
        at=item['offset'];data=bytes.fromhex(item['hex'])
        if type(at)is not int or at%4 or not 0<=at<=size-4 or len(data)!=4 or at in seen:
            raise ValueError('1.20 原厂恢复记录无效')
        seen.add(at);out[at:at+4]=data
    if _sha(out)!=SOURCE_RTOS_SHA256:raise ValueError('无法精确恢复原厂 1.20 RTOS')
    return bytes(out)


def supports_image(rtos):
    try:return _sha(canonical_base(rtos))==SOURCE_RTOS_SHA256
    except (ValueError,KeyError,TypeError,struct.error):return False


def prepare_update_policy(rtos,allow_older=True):
    from .update_policy import apply_update_policy,remove_update_policy
    value=metadata(rtos)
    if value is None:return (apply_update_policy if allow_older else remove_update_policy)(rtos)
    original=canonical_base(rtos)
    size=struct.unpack_from('<I',rtos,len(rtos)-8)[0];body=rtos[:len(rtos)-size-16]
    body,proof=(apply_update_policy if allow_older else remove_update_policy)(body)
    value['allow_older']=bool(allow_older)
    value['compiler_base_restore']=[{'offset':i*4,'hex':struct.pack('<I',a).hex()}
        for i,(a,b) in enumerate(zip(memoryview(original).cast('I'),memoryview(body)[:len(original)].cast('I'))) if a!=b]
    return _finish(body,value),proof


def _section(image,name):
    row=next(r for r in image.sections if r['name']==name)
    return image.decoded[row['start']:row['end']]


def _res_recovery(image):
    from .shutdown import _res_records
    res=_section(image,'RES');section=next(r for r in image.sections if r['name']=='RES')
    records=_res_records(image)
    end=max([16]+[r['offset']+r['size']-section['start'] for r in records.values()])
    return {'size':len(res),'sha256':_sha(res),'count':len(records),'records_end':end,
            'tail':base64.b64encode(zlib.compress(res[end:])).decode('ascii')}


def _base_image(image):
    value=metadata(image.rtos)
    if value is None:
        if image.sha256!=SOURCE_SHA256 or _sha(image.candidate)!=SOURCE_SHA256:
            raise ValueError('首次 1.20 构建需要原厂完整固件')
        return image,{'icons_size':len(image.icon_bytes),'icons_sha256':_sha(image.icon_bytes),'res':_res_recovery(image)}
    record=value['base_resources'];icons=image.icon_bytes[:record['icons_size']]
    if _sha(icons)!=record['icons_sha256']:raise ValueError('原厂图标基线已改变')
    res_record=record['res'];res=bytearray(_section(image,'RES')[:res_record['records_end']])
    struct.pack_into('<I',res,12,res_record['count'])
    res.extend(zlib.decompress(base64.b64decode(res_record['tail'])))
    if len(res)!=res_record['size'] or _sha(res)!=res_record['sha256']:raise ValueError('原厂 RES 基线已改变')
    # Resource-only image view is sufficient for RES/JPEG catalog operations.
    base=SimpleNamespace(rtos=canonical_base(image.rtos),icon_bytes=icons,
        decoded=bytes(res),sections=[{'name':'RES','start':0,'end':len(res),'resource_count':res_record['count']}],
        sha256=SOURCE_SHA256,version=(1,20,10,11),candidate=image.candidate)
    return base,deepcopy(record)


def wrap_official_input(image):
    """Add only a recovery footer to the exact official executable body."""
    from .product120_crops import factory_crops120
    base,resources=_base_image(image)
    if metadata(image.rtos) is not None:
        raise ValueError('原厂包装只接受完整原厂 1.20 输入')
    records=factory_crops120(base.rtos)
    record={'compiler_base_sha256':SOURCE_RTOS_SHA256,'compiler_base_size':len(base.rtos),
        'compiler_base_restore':[],'base_resources':resources,'source_package_sha256':image.sha256,
        'filter_records':[],'crop_records':records,'crop_order':[0,1,3,2],'crop_next_public':7,
        'shutdown_plan':None,'pictorialist':False,'allow_older':False,
        'feature_flags':{'rating':False,'af':False,'soft_glow':False},
        'installation':{},'hardware':'not_tested'}
    return _finish(base.rtos,record)


def read_filters120(image):
    from .filter120 import FACTORY_ORDER
    h=reader_helpers();memory=_Memory(image.rtos);value=metadata(image.rtos)
    vector=memory.pair(0x533DE1F0,0x533DE1F8);count=memory.word(0x533DE208)&255
    order=list(memory.read(vector,count))
    declared={r['identity']['style_id']:r for r in (value or {}).get('filter_records',[])}
    styles=list(order)+[s for s in FACTORY_ORDER if s not in order]+[s for s in declared if s not in order]
    result=[]
    for style in styles:
        custom=style>=34;item=deepcopy(declared.get(style,{}));identity=item.get('identity',{})
        if custom and not item:raise ValueError('自定义滤镜缺少产品身份记录')
        if custom:
            profile=identity['profile_id'];textid=identity['text_id'];iconid=identity['icon_id']
            actual_profile=h._native_lookup(memory,0x5393AA94,style,stop=0x5393AAA8)
            actual_text=h._native_lookup(memory,0x5338EDF8,style)
            actual_icon=h._native_lookup(memory,0x533905D8,style)
            if (actual_profile,actual_text,actual_icon)!=(profile,textid,iconid):
                raise ValueError('1.20 自定义滤镜原生身份路由与记录不一致')
        else:
            profile=h._native_lookup(memory,0x5393AA94,style,stop=0x5393AAA8)
            textid=h._native_lookup(memory,0x5338EDF8,style)
            iconid=h._native_lookup(memory,0x533905D8,style)
        name,text=text120(memory,textid);resources=color120(memory,profile)
        resources.update(name=text,icon=icon120(memory,image,iconid))
        enabled=style in order and (custom or style not in (25,26,27,28,29,30,31,32))
        if style==31:enabled=bool((value or {}).get('pictorialist'))
        params=[]
        for i,(key,label) in enumerate(h.PARAMETERS+h.EXTENDED_PARAMETERS):
            p=dict(id=key,name=label,min=0,max=8,default=None,current_value=None,editable=custom and i<9,
                   default_source='unknown',value_source='no camera UserData in firmware container')
            p.update(h._parameter_field_info(style,key))
            if custom and i<9:
                actual=h._native_lookup(memory,SCALAR_BINDINGS['default_entries'][i],style,default_object=True)
                if not 0<=actual<=8:raise ValueError('1.20 自定义滤镜默认值无效')
                p.update(default=actual,default_source='native verified getter')
                old=next((x for x in item.get('parameters',[]) if x['id']==key),{})
                p['enabled']=old.get('enabled',True)
            elif not custom:p['editable']=False
            params.append(p)
        identity.update(style_id=style,profile_id=profile,text_id=textid,icon_id=iconid,
            menu_ordinal=order.index(style)+1 if style in order else None)
        if not custom:
            table=struct.unpack('<44H',memory.read(0x53FD7FC8,88))
            identity['maker_note_code']=next((table[n+1] for n in range(0,44,2) if table[n]==style),None)
        result.append({**item,'id':item.get('id',style),'kind':'custom' if custom else 'native',
            'identity':identity,'name':name,'enabled':enabled,'editable':custom,
            'visibility_editable':style not in (25,26,27,28,29,30,31,32),
            'parameters':params,'resources':resources,'source_family':'1.20','inspection_notes':[]})
    return result


def _shutdown_inventory(image):
    from .shutdown import _res_records,_jpeg_info,validate_draft
    from .shutdown120 import JPEG_TABLE,_read_static
    value=metadata(image.rtos);base,_=_base_image(image);records=_res_records(base)
    resources=[]
    for identifier,native,name in (('goodbye',1,'GR standard'),('hdf',2,'HDF'),('mono',3,'Mono'),('20th',11,'GR 20th anniversary')):
        ptr=struct.unpack('<I',_read_static(base.rtos,JPEG_TABLE+native*4,4))[0]
        w,h,pathat=struct.unpack('<HHI',_read_static(base.rtos,ptr,8))
        path=_read_static(base.rtos,pathat,32).split(b'\0',1)[0].decode('ascii')
        if path not in records:continue
        record=records[path];payload=base.decoded[record['offset']:record['offset']+record['size']]
        info=_jpeg_info(payload)
        resources.append({**record,'id':identifier,'native_id':native,'name':name,'path':path,
            'width':w,'height':h,'sha256':_sha(payload),'payload_base64':base64.b64encode(payload).decode(),
            'preview_data_url':'data:image/jpeg;base64,'+base64.b64encode(payload).decode(),
            'jpeg_info':info,'encoding':'JPEG baseline','replacement_supported':True,'display_layer_verified':True})
    inventory={'schema_version':2,'source_sha256':image.sha256,'source_version':'.'.join(map(str,image.version)),
        'resources':resources,'inspection':{'resource_readback':'passed','selection_code_verified':True,'notes':[]},
        'native_selection':{'verified':True,'camera_current_selection':None},
        'capabilities':{'can_prepare_assets':True,'can_patch_firmware':True,'can_build_menu':True,
            'can_change_camera_selection':False,'hardware_verified':False,'menu_build_reason':'1.20 原生菜单适配'},
        'reason':'可添加关机图案；原厂 GR WORLD 下载路径保留。'}
    if value and value.get('shutdown_plan'):
        current=_res_records(image);presets=deepcopy(value['shutdown_plan']['presets'])
        memory=_Memory(image.rtos);table=memory.pair(0x53264278,0x53264288)
        for row in presets:
            record=current[row['path']];payload=image.decoded[record['offset']:record['offset']+record['size']]
            if _sha(payload)!=row['sha256']:raise ValueError('关机图案资源与记录不一致')
            ptr=memory.word(table+row['native_id']*4);w,h,pathat=struct.unpack('<HHI',memory.read(ptr,8))
            if (w,h,memory.read(pathat,32).split(b'\0',1)[0].decode('ascii'))!=(720,480,row['path']):
                raise ValueError('关机图案原生 JPEG 描述符与记录不一致')
            row['payload_base64']=base64.b64encode(payload).decode('ascii')
        inventory['installed_menu']={'schema_version':2,'presets':presets}
    return inventory


def snapshot120(image):
    from .product120_crops import factory_crops120,verify_crops120
    from .shutdown import validate_draft
    value=metadata(image.rtos);base,_=_base_image(image)
    records=deepcopy((value or {}).get('crop_records') or factory_crops120(base.rtos))
    if value:verify_crops120(image.rtos,records)
    order=(value or {}).get('crop_order',[0,1,3,2]);byid={r['identity']['public_id']:r for r in records}
    installed=(value or {}).get('installation',{}).get('crop_menu')
    if installed:
        from .crop120_menu import ORDER_PAIRS,COUNT_SITES
        memory=_Memory(image.rtos);table=memory.pair(*ORDER_PAIRS[0])
        if list(memory.read(table,len(order)))!=order or any(memory.word(site)!=_mov(0,len(order)) for site in COUNT_SITES):
            raise ValueError('1.20 裁切原生顺序/数量与记录不一致')
        texts=int(installed['text_table'],16)
        for public,row in byid.items():
            if public>=4 and text120(memory,memory.word(texts+public*4))[0]!=row['name']:
                raise ValueError('1.20 裁切原生名称与记录不一致')
    crops=[byid[n] for n in order]
    inventory=_shutdown_inventory(image)
    return {'filters':read_filters120(image),'crops':crops,'crop_records':records,
        'crop_inspection':{'native_readback':'passed','compiler_supported':True,'raw_recut_complete':True,
            'next_public':(value or {}).get('crop_next_public',7),'hardware':'not_tested'},
        'shutdown_inventory':inventory,'shutdown':validate_draft(None,inventory),
        'pictorialist':bool((value or {}).get('pictorialist'))}


def preview_crop120(image,ratio):
    from .product120_crops import preview_geometry120
    base,_=_base_image(image);snap=snapshot120(image)
    return {**preview_geometry120(base.rtos,ratio,geometry_sources=snap['crop_records']),
        'constraints':['对称偶数原点；尺寸按4像素对齐','保留照片历史裁切身份'],'hardware':'not_tested'}


def _filter_plan(image,desired):
    original=read_filters120(image);byid={str(r['id']):r for r in original};drafts={str(r['id']):r for r in desired}
    if len(drafts)!=len(desired):raise ValueError('滤镜 ID 重复')
    prior=metadata(image.rtos) or {};archive=deepcopy([r for r in original if r.get('kind')=='custom'])
    bystyle={r['identity']['style_id']:r for r in archive}
    reserved=[r.get('identity',{}).get('style_id') for r in desired if r.get('kind')=='custom']
    high=max([33,*bystyle,*[s for s in reserved if isinstance(s,int)]])+1
    result=[];seen=set()
    for row in desired:
        if row.get('kind')!='custom':continue
        item=deepcopy(row);old=byid.get(str(item['id']));identity=item.get('identity') or {}
        if old and old.get('kind')=='custom':identity=deepcopy(old['identity'])
        if not identity:
            source=item.get('source_identity') or {}
            legacy=source.get('style_id')
            if isinstance(legacy,int) and 34<=legacy<=90 and legacy not in bystyle and legacy not in seen:style=legacy
            else:style=high
            while style in bystyle or style in seen:style+=1
            identity={'style_id':style};high=max(high,style+1)
        style=identity['style_id']
        if not 34<=style<=90 or style in seen:raise ValueError('自定义滤镜身份空间已满或重复')
        seen.add(style);item['identity']=identity
        if not item.get('resources',{}).get('banks'):
            key=str(item.get('template_id'));visited=set()
            while key not in byid:
                if key in visited or key not in drafts:raise ValueError('新增滤镜缺少有效原厂模板')
                visited.add(key);key=str(drafts[key].get('template_id'))
            item['resources']=deepcopy(byid[key]['resources'])
        result.append(item);bystyle[style]=item
    active_ids={str(r['id']) for r in result if r.get('enabled',True)}
    for style,item in bystyle.items():
        if style not in seen:item['enabled']=False;result.append(item)
    if len(active_ids)>16:raise ValueError('1.20 当前最多支持16项活动自定义滤镜')
    if len(result)>57:raise ValueError('1.20 保留滤镜身份超过57项')
    result.sort(key=lambda r:r['identity']['style_id'])
    for i,row in enumerate(result):
        identity=row['identity'];style=identity['style_id']
        old=row.get('source_identity') or identity
        identity['legacy_maker_note_code']=identity.get('legacy_maker_note_code',old.get('maker_note_code',0x8106+style-34))
        identity.update(maker_note_code=0x8200+style,raw_ordinal=23+i,playback_property=23+i)
    return result


def _pictorialist(patch,enabled):
    if not enabled:return None
    from .mono_effects120 import _wrapper,PRODUCER
    site=0x533DE144;prior=_target(site,patch.word(site))
    helper=patch.append(lambda at:[0xE3520000,_branch(at+4,at+36,condition=1),0xE351001F,
        _branch(at+12,at+36,condition=1),0xE5D0001F,0xE2500001,0x13A00001,0xE12FFF1E,
        0xE1A00000,_branch(at+36,prior)],16)
    patch.set_word(site,_branch(site,helper),'选择画意31；保留其他原厂准入')
    if patch.word(PRODUCER)!=0xE1A0C00D:raise ValueError('1.20 画意灰度配置入口不匹配')
    def generate(at):
        words=_wrapper(at);words[16]=0xE1A00000
        return words
    gray=patch.append(generate,16);patch.set_word(PRODUCER,_branch(PRODUCER,gray),'仅画意31启用原生灰度矩阵')
    for site in (0x533B61B4,0x533B9048):
        if patch.word(site)!=_branch(site,0x533F1DC8,link=True):raise ValueError('画意回放字段入口不匹配')
        patch.set_word(site,0xE3A00001,'画意31回放 INFO 名称及参数')
    return {'admission':hex(helper),'gray':hex(gray),'enabled_styles':[31],
        'effect_status':'原厂未开放，独立画意效果未完成；当前为Mono Standard灰度路径','hardware':'not_tested'}


def validate_filter_menu120(desired, pictorialist=False):
    """Require a selectable color-body row, excluding closed Mono entries."""
    from .filter120 import FACTORY_ORDER
    available = set(FACTORY_ORDER) - set(range(25, 33))
    if pictorialist:
        return
    for row in desired:
        if not row.get('enabled', True):
            continue
        if row.get('kind') == 'custom' or row.get('identity', {}).get('style_id') in available:
            return
    raise ValueError('至少保留一项活动滤镜')


def compile_product120(image,filters=None,crops=None,shutdown=None,*,pictorialist=False,version=None,allow_older=True,crop_records=None):
    from .resources120 import Catalog120
    from .filter120 import install_filter_menu120,FACTORY_ORDER
    from .registry120 import install_registry_state120
    from .scalar120 import install_scalar120
    from .namespace120 import install_namespace120,OWNER_BYTES
    from .native_raw120 import raw_layout120,migrate_raw_arrays120
    from .crop_registry import plan_crops
    from .product120_crops import factory_crops120,preview_geometry120,verify_crops120
    from .crop120 import install_geometry120
    from .crop120_menu import install_crop_menu120
    from .crop120_state import install_crop_state120
    from .crop120_raw import install_raw120
    from .crop120_identity import install_image_identity120
    from .shutdown import validate_draft,item_menu_payload
    from .shutdown_compiler import rebuild_res,menu_path
    from .shutdown120 import install_shutdown120_assets,install_shutdown120
    from .update_policy import apply_update_policy,remove_update_policy
    if type(pictorialist)is not bool:raise ValueError('画意摄影开关必须为布尔值')
    from .versions import parse_version,version_word
    target=parse_version(version or image.version)
    if target[:3]!=(1,20,10) or target<tuple(image.version):raise ValueError('1.20 编译版本必须保持版本族并不低于输入')
    base,base_resources=_base_image(image);old=metadata(image.rtos) or {};snap=snapshot120(image)
    desired=deepcopy(snap['filters'] if filters is None else filters)
    validate_filter_menu120(desired,pictorialist)
    rows=_filter_plan(image,desired)
    patch=_Patch(base.rtos);patch.official_base=True
    catalog=Catalog120(patch,icon_capacity=2048,text_capacity=2048)
    icons,resource_report=install_resources120(patch,catalog,base.icon_bytes,rows)
    active=[r for r in rows if r.get('enabled',True)]
    native_order=[s for s in FACTORY_ORDER if next((r.get('enabled',True) for r in desired if r.get('identity',{}).get('style_id')==s),s not in (25,26,27,28,29,30,31,32)) or s in (25,26,27,28,29,30,31,32)]
    by_id={str(r['id']):r for r in active};menu_order=[]
    for row in desired:
        if row.get('kind')=='custom':
            item=by_id.get(str(row['id']))
            if item:menu_order.append(item['identity']['style_id'])
        else:
            style=row.get('identity',{}).get('style_id')
            if style in native_order:menu_order.append(style)
    menu_order += [s for s in native_order if s not in menu_order]
    menu=install_filter_menu120(patch,active,baseline_rtos=base.rtos,native_order=native_order,menu_order=menu_order,routing_rows=rows)
    count=23+len(rows);raw_layout=raw_layout120(count)
    registry=install_registry_state120(patch,rows,state_offset=raw_layout['state_heap_offset'],baseline_rtos=base.rtos)
    scalar=install_scalar120(patch,rows,registry['symbols']['registry_main'],bindings=SCALAR_BINDINGS,baseline_rtos=base.rtos)
    originals=deepcopy(snap['crops']);records=deepcopy(snap['crop_records']);wanted=deepcopy(originals if crops is None else crops)
    if crop_records is not None:
        if not isinstance(crop_records,list):raise ValueError('迁移裁切历史必须为记录列表')
        for record in crop_records:
            public=record.get('identity',{}).get('public_id')
            if not isinstance(public,int) or not 0<=public<=255:raise ValueError('迁移裁切历史身份无效')
            if public<4:continue  # Native target-family factory rows remain authoritative.
            if not record.get('geometry'):raise ValueError('迁移裁切历史缺少几何')
            previous=next((r for r in records if r['identity']['public_id']==public),None)
            if previous and previous['geometry']!=record['geometry']:
                raise ValueError('迁移裁切历史与目标照片身份冲突')
            if previous is None:records.append(deepcopy(record))
    legacy_planning=[]
    for row in wanted:
        legacy=row.get('source_identity',{}).get('public_id')
        source_geometry=row.get('source_geometry',row.get('geometry'))
        source_ratio=row.get('source_requested_ratio',row.get('requested_ratio'))
        if row.get('kind')=='custom' and isinstance(legacy,int) and 4<=legacy<=255 and source_geometry:
            previous=next((r for r in records if r['identity']['public_id']==legacy),None)
            if previous and previous['geometry']!=source_geometry:
                raise ValueError('迁移裁切身份与目标既有照片几何冲突')
            archive=deepcopy(row)
            archive.update(identity=deepcopy(row['source_identity']),geometry=deepcopy(source_geometry),requested_ratio=source_ratio)
            archive['actual_ratio']=archive.get('actual_ratio',source_ratio)
            legacy_planning.append(archive)
            if previous is None:records.append(archive)
    # The source identity is an immutable old-photo meaning; new ratios get
    # the allocator's next unused ID, including after migration draft edits.
    originals+=legacy_planning
    for row in wanted:
        if row.get('kind')!='custom':continue
        previous=next((r for r in originals if r['id']==row['id']),None)
        if previous and row['requested_ratio']==previous['requested_ratio']:row['geometry']=deepcopy(previous['geometry'])
        else:
            proposal=preview_geometry120(base.rtos,row['requested_ratio'],geometry_sources=records)
            row.update(geometry=proposal['geometry'],actual_ratio=proposal['actual_ratio'])
    plan=plan_crops(originals,wanted,records=records,next_public=max(old.get('crop_next_public',7),max([6]+[r['identity']['public_id'] for r in records])+1))
    if len(plan['order'])>254:raise ValueError('活动裁切超过250项自定义比例')
    geometry=install_geometry120(patch,plan['records'],baseline_rtos=base.rtos)
    text_first=881+len(rows);icon_first=709+len(rows)
    icons,crop_menu=install_crop_menu120(patch,plan,catalog,icons,baseline_rtos=base.rtos,text_first=text_first,icon_first=icon_first)
    crop_state=install_crop_state120(patch,int(crop_menu['active_bitmap'],16))
    crop_raw=install_raw120(patch,plan,baseline_rtos=base.rtos)
    crop_identity=install_image_identity120(patch,plan,baseline_rtos=base.rtos)
    ncustom=sum(r['identity']['public_id']>=4 for r in plan['records']);text_first+=ncustom;icon_first+=ncustom
    draft=validate_draft(snap['shutdown'] if shutdown is None else shutdown,snap['shutdown_inventory'])
    assets={r['id']:r for r in draft['assets']};presets=[]
    for index,item in enumerate(draft['items']):
        if item.get('asset_id') not in assets:raise ValueError('请为关机图案选择图片: '+item['name'])
        asset=assets[item['asset_id']];payload=item_menu_payload(asset,item['fit'])
        presets.append({**item,'item_id':item['id'],'index':index,'slot':index+1,'native_id':13+index,
            'path':menu_path(item['selection_id']),'sha256':_sha(payload),'bytes':len(payload),
            'payload_base64':base64.b64encode(payload).decode('ascii')})
    shutdown_plan={'schema_version':2,'presets':presets,'legacy_choices':{},'default_selection':0,'selection_scope':'global'}
    owner_end=registry['state_end'];initializer=registry['initializer'];shutdown_report=None
    if presets:
        icons,sa=install_shutdown120_assets(catalog,icons,shutdown_plan,first_text_id=text_first,first_icon_id=icon_first)
        # Match .16's factory preview correction from the user's own JPEG1.
        factory=snap['shutdown_inventory']['resources'][0]
        from PIL import Image,ImageOps
        from io import BytesIO
        with Image.open(BytesIO(base64.b64decode(factory['payload_base64']))) as source:
            thumb=ImageOps.contain(source.convert('RGB'),(520,200),Image.Resampling.LANCZOS)
        canvas=Image.new('RGBA',(520,200),(0,0,0,255));canvas.paste(thumb,((520-thumb.width)//2,(200-thumb.height)//2))
        standard=icon_first+len(presets);ib=bytearray(icons);catalog.add_icon(standard,canvas.tobytes(),ib,width=520,height=200);icons=bytes(ib)
        shutdown_report=install_shutdown120(patch,shutdown_plan,{'owner_cell':registry['owner_cell'],'owner_getter':registry['owner_getter'],
            'owner_offset':owner_end,'record':73,'prior_save':_target(0x533F3674,patch.word(0x533F3674)),
            'prior_load':_target(0x533F3254,patch.word(0x533F3254)),'prior_initializer':initializer,
            'save_entry':0x533F3674,'load_entry':0x533F3254,'text_ids':sa['text_ids'],
            'preview_icon_ids':sa['preview_icon_ids'],'standard_preview_icon':standard,'apdata_upper_bound':74})
        initializer=shutdown_report['initializer'];owner_end+=8
    namespace=None
    if rows:
        namespace=install_namespace120(patch,rows,owner_offset=owner_end,baseline_rtos=base.rtos,
            prior_initializer=initializer,product=True)
        initializer=namespace['initializer'];owner_end+=OWNER_BYTES
    # RAW field sidecar capacity depends on all retained identities, not just
    # the current menu. Restore the native capacity before installing it.
    if patch.word(0x53379C6C)!=0xE3A0001A:patch.set_word(0x53379C6C,0xE3A0001A,'重建全部保留滤镜 RAW 容量')
    raw_report=migrate_raw_arrays120(patch,count,baseline_rtos=base.rtos,
        custom_state_bytes=owner_end-raw_layout['state_heap_offset'],
        active_raw_ordinals=[r['identity']['raw_ordinal'] for r in active],
        init_callback=initializer,custom_rows=rows,main_helper=registry['symbols']['registry_main'])
    effect=_pictorialist(patch,pictorialist)
    res=rebuild_res(base,presets,owned_paths=set())
    res+=bytes(max(0,len(_section(image,'RES'))-len(res)))
    icons+=bytes(max(0,len(image.icon_bytes)-len(icons)))
    body,policy=(apply_update_policy if allow_older else remove_update_policy)(bytes(patch.image))
    word=version_word(target);body=bytearray(body)
    for address in (0x5401BCE0,0x5401BDFC,0x5401BF4C):struct.pack_into('<I',body,address-BASE,word)
    struct.pack_into('<I',body,0x53AA06AC-BASE,0xE3000A00|target[3])
    struct.pack_into('<I',body,0x53AA06D0-BASE,0xE3A00000|target[3])
    body=bytes(body)
    restore=[{'offset':i*4,'hex':struct.pack('<I',a).hex()} for i,(a,b) in enumerate(zip(memoryview(base.rtos).cast('I'),memoryview(body)[:len(base.rtos)].cast('I'))) if a!=b]
    # Payloads are read from native resource tables; manifest stores identities
    # and UI defaults only, including retired filters needed by old photos.
    filter_records=[{k:deepcopy(r[k]) for k in ('id','identity','name','enabled','parameters') if k in r} for r in rows]
    record={'compiler_base_sha256':SOURCE_RTOS_SHA256,'compiler_base_size':len(base.rtos),
        'compiler_base_restore':restore,'base_resources':base_resources,'source_package_sha256':image.sha256,
        'filter_records':filter_records,'crop_records':plan['records'],'crop_order':plan['order'],'crop_next_public':plan['next_public'],
        'shutdown_plan':{**shutdown_plan,'presets':[{k:v for k,v in r.items() if k!='payload_base64'} for r in presets]},
        'pictorialist':pictorialist,'allow_older':bool(allow_older),'feature_flags':{'rating':False,'af':False,'soft_glow':False},
        'installation':{'resources':resource_report,'filter_menu':menu,'crop_menu':crop_menu,'namespace':namespace},
        'hardware':'not_tested'}
    rtos=_finish(body,record,minimum=len(image.rtos))
    if BASE+len(rtos)>=0x55000000:raise ValueError('产品 RTOS 超出已验证可执行地址范围')
    if canonical_base(rtos)!=base.rtos:raise ValueError('1.20 原厂 RTOS 恢复验证失败')
    crop_proof=verify_crops120(rtos,plan['records'])
    observed=read_filters120(SimpleNamespace(rtos=rtos,icon_bytes=icons))
    for row in rows:
        read=next(r for r in observed if r['id']==row['id'])
        for original,actual in zip(sorted(row['resources']['banks'],key=lambda b:b['hardware_index']),read['resources']['banks']):
            for key in ('camera_basis','matrix','multi_main','multi_aux','gamma'):
                if original[key]['hex']!=actual[key]['hex']:raise ValueError('1.20 滤镜颜色资源读回不一致')
    return rtos,icons,res,{'status':'passed_offline','compiler':COMPILER,'native_base_restored':True,
        'crop_validation':crop_proof,'filter_resources':resource_report,'pictorialist':effect,
        'registry':registry,'scalar':scalar,'raw_owner':raw_report,'shutdown':shutdown_report,
        'geometry':geometry,'crop_state':crop_state,'crop_raw':crop_raw,'crop_identity':crop_identity,
        'update_policy':policy,'feature_flags':record['feature_flags'],'hardware':'not_tested'}
