# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only
"""Product-level 1.20 projects and explicit 1.11 -> 1.20 recipe migration.

Both local applications use this implementation. Existing 1.11 operations stay
with their existing service; firmware addresses never cross the migration API.
"""
from copy import deepcopy
from pathlib import Path

from .store import sha256, timestamp, write_json
from .versions import version_text, version_word


def family(value):
    parts = str(value).split('.')
    return '.'.join(parts[:2])


def native_effects(value=None):
    if value is None:
        value = {'schema_version': 1, 'pictorialist': False}
    if (not isinstance(value, dict) or set(value) - {'schema_version', 'pictorialist'}
            or value.get('schema_version', 1) != 1 or type(value.get('pictorialist')) is not bool):
        raise ValueError('画意开关必须明确为布尔值')
    return {'schema_version': 1, 'pictorialist': value['pictorialist']}


def build_options(value=None):
    if value is None:
        return {'allow_older': True}
    result = value.get('allow_older') if isinstance(value, dict) else value
    if type(result) is not bool:
        raise ValueError('低版本固件支持开关必须是布尔值')
    return {'allow_older': result}


def _is120(project):
    return family(project.get('input', {}).get('version', '')) == '1.20'


def _decorate(rows):
    from io import BytesIO
    import base64
    from PIL import Image
    for row in rows:
        icon = row.get('resources', {}).get('icon', {})
        if not icon.get('hex'):
            continue
        width, height = icon.get('width', 40), icon.get('height', 40)
        pixels = bytes.fromhex(icon['hex'])
        if len(pixels) != width * height * 4:
            continue
        output = BytesIO()
        picture = Image.frombytes('RGBA', (width, height), pixels)
        picture.thumbnail((40, 40), Image.Resampling.LANCZOS)
        picture.save(output, format='PNG')
        row['icon_data_url'] = 'data:image/png;base64,' + base64.b64encode(output.getvalue()).decode('ascii')
    return rows


class MultiVersionEditorMixin:
    def status(self):
        result = super().status()
        result['supported_families'] = ['1.11', '1.20']
        return result

    def _caps120(self, editable, rows=()):
        from .product120 import capabilities
        slots = capabilities()
        active = sum(row.get('kind') == 'custom' and row.get('enabled', True) for row in rows)
        limit = slots['maximum_custom_slots']
        return {'can_import': True, 'can_edit': editable, 'can_build': editable,
                'feature_backend_verified': editable, 'filter_backend_verified': editable,
                'requires_toolchain': editable, 'crop_supported': editable,
                'shutdown_supported': editable, 'pictorialist_switch': editable,
                'can_add_filter': editable and active < limit, 'can_remove_filter': editable,
                'max_custom_slots': limit, 'active_custom_slots': active,
                'newslots_supported': editable, 'slot_backend': slots,
                'firmware_family': '1.20'}

    def import_firmware(self, data, filename='fwdc248b.bin', expected_family='auto'):
        from .firmware import load_image, inspect_firmware
        if expected_family not in ('auto', '1.11', '1.20'):
            raise ValueError('请选择自动识别、1.11 或 1.20')
        path, digest = self.store.object(data)
        image = load_image(path)
        actual = family(image.internal_version)
        if expected_family != 'auto' and actual != expected_family:
            raise ValueError(f'选择的是 {expected_family}，文件实际为 {actual}；请重新选择')
        if actual != '1.20':
            result = super().import_firmware(data, filename)
            result['input']['family'] = actual
            result['capabilities']['pictorialist_switch'] = False
            result.setdefault('native_effects_draft', native_effects())
            return self.store.save_project(result)
        from .product120 import snapshot120
        snapshot = snapshot120(image)
        info = inspect_firmware(image)
        info['editor_supported'] = image.editable
        crops = snapshot['crops']
        shutdown = snapshot['shutdown']
        project = {'schema_version': 1, 'edition': 'demo-crop-shutdown',
                   'id': self.store.new_id('project'), 'title': Path(filename).stem,
                   'created_at': timestamp(), 'input_path': str(path),
                   'input': {'filename': Path(filename).name, 'sha256': digest,
                             'version': image.internal_version, 'family': actual,
                             'known_stage_id': image.known_stage_id},
                   'firmware': info, 'can_generate': image.editable,
                   'suggested_version': self._allocate_version(image.version),
                   'builds': self._build_ancestry(digest),
                   'filters': _decorate(snapshot['filters']),
                   'original_filters': deepcopy(snapshot['filters']),
                   'crops': crops, 'original_crops': deepcopy(crops),
                   'crop_inspection': snapshot['crop_inspection'],
                   'crop_capabilities': {'can_edit': image.editable, 'can_add': image.editable,
                       'can_write': image.editable, 'draft_only': not image.editable,
                       'reason': '1.20 原生裁切适配；离线验证，实机待验证'},
                   'shutdown_inventory': snapshot['shutdown_inventory'],
                   'shutdown': shutdown, 'shutdown_draft': deepcopy(shutdown),
                   'native_effects_draft': native_effects({'pictorialist': snapshot['pictorialist']}),
                   'build_options': build_options(), 'update_policy_draft': build_options(),
                   'filter_backend_verified': image.editable, 'xmp_version': 1,
                   'soft_glow_draft': {'enabled': False},
                   'calibration': self.calibration.status()}
        project['capabilities'] = self._caps120(image.editable, project['filters'])
        return self.store.save_project(project)

    def project(self, project_id):
        saved = self.store.get_project(project_id)
        if not _is120(saved):
            result = super().project(project_id)
            result['capabilities']['pictorialist_switch'] = False
            result.setdefault('native_effects_draft', native_effects())
            return result
        saved['native_effects_draft'] = native_effects(saved.get('native_effects_draft'))
        saved['capabilities'] = self._caps120(bool(saved['firmware'].get('editor_supported')), saved['filters'])
        saved['can_generate'] = saved['capabilities']['can_build']
        saved['suggested_version'] = self._allocate_version(saved['input']['version'])
        saved['calibration'] = self.calibration.status()
        saved['shutdown_draft'] = deepcopy(saved['shutdown'])
        return saved

    def save(self, project_id, *args, native_effects_draft=None, **kwargs):
        stored = self.store.get_project(project_id)
        if not _is120(stored):
            if native_effects_draft is not None and native_effects(native_effects_draft)['pictorialist']:
                raise ValueError('画意摄影开关仅适用于已适配的 1.20 固件')
            return super().save(project_id, *args, **kwargs)
        # The research and public services retain their existing positional APIs.
        keys = (('title', 'crops', 'shutdown', 'update_policy_draft', 'build_options', 'filters')
                if hasattr(self, '_filter_rows') else
                ('filters', 'title', 'crops', 'shutdown_draft', 'soft_glow_draft',
                 'native_mono_draft', 'update_policy_draft', 'build_options'))
        values = dict(zip(keys, args))
        values.update(kwargs)
        from .shutdown import validate_draft
        with self.store.lock:
            project = self.project(project_id)
            if not project['can_generate']:
                raise ValueError('此 1.20 输入布局尚未通过产品适配')
            title = values.get('title')
            if title is not None:
                if not isinstance(title, str) or not title.strip() or len(title) > 120:
                    raise ValueError('项目名称必须为 1–120 个字符')
                project['title'] = title.strip()
            if values.get('filters') is not None:
                rows = deepcopy(values['filters'])
                if not isinstance(rows, list):
                    raise ValueError('滤镜编辑记录必须是列表')
                current = {row['id']: row for row in project['filters']}
                ids = [row.get('id') for row in rows]
                if (any(type(key) not in (str, int) or key == '' for key in ids)
                        or len(ids) != len(set(map(str, ids)))):
                    raise ValueError('滤镜标识必须唯一')
                for row in rows:
                    self._preserve_completed_calibration(row, current.get(row['id'], {}))
                project['filters'] = rows
            if values.get('crops') is not None:
                project['crops'] = self._crop_draft(project, values['crops'])
            shutdown = values.get('shutdown', values.get('shutdown_draft'))
            if shutdown is not None:
                project['shutdown'] = validate_draft(shutdown, project['shutdown_inventory'])
            project['shutdown_draft'] = deepcopy(project['shutdown'])
            if native_effects_draft is not None:
                project['native_effects_draft'] = native_effects(native_effects_draft)
            options = values.get('build_options') or values.get('update_policy_draft')
            project['build_options'] = build_options(options or project.get('build_options'))
            project['update_policy_draft'] = deepcopy(project['build_options'])
            return self.store.save_project(project)

    def _crop_draft(self, project, crops):
        if not _is120(project):
            return super()._crop_draft(project, crops)
        from .crops import validate_draft
        crops = deepcopy(crops)
        known = {row['id']: row for row in project['crops']}
        for row in crops:
            if row.get('id') not in known and row.get('identity') == {}:
                row['identity'] = None
        rows = validate_draft(crops, project)
        for row in rows:
            previous = known.get(row['id'], {})
            if (row.get('source_identity') and previous.get('geometry') and
                    row['requested_ratio'] == previous.get('requested_ratio')):
                row['geometry'] = deepcopy(previous['geometry'])
                row['actual_ratio'] = previous.get('actual_ratio', row['requested_ratio'])
            if row.get('kind') == 'custom' and not row.get('geometry'):
                preview = self.crop_preview(project['id'], row['requested_ratio'])
                row.update(geometry=preview['geometry'], actual_ratio=preview['actual_ratio'],
                           geometry_status='draft_not_native')
        return rows

    def crop_preview(self, project_id, ratio):
        project = self.store.get_project(project_id)
        if not _is120(project):
            return super().crop_preview(project_id, ratio)
        from .firmware import load_image
        from .product120 import preview_crop120
        image = load_image(project['input_path'])
        if image.sha256 != project['input']['sha256']:
            raise ValueError('输入固件已变化')
        return preview_crop120(image, ratio)

    def add_filter(self, project_id, template_id, name=None):
        project = self.project(project_id)
        if not _is120(project):
            return super().add_filter(project_id, template_id, name)
        if not project['capabilities']['can_add_filter']:
            raise ValueError('当前布局不能继续增加自定义滤镜')
        template = next((row for row in project['filters'] if str(row['id']) == str(template_id)), None)
        if not template or not template.get('resources', {}).get('banks'):
            raise ValueError('请选择有完整颜色资源的滤镜模板')
        item = deepcopy(template)
        item.update(id=self.store.new_id('filter'), kind='custom', name=name or '新滤镜',
                    enabled=True, editable=True, visibility_editable=True, identity={},
                    template_id=template_id, menu_order=len(project['filters']),
                    xmp=None, calibration=None, pending_color_conversion=False)
        item.pop('source_identity', None)
        item['source_family'] = '1.20'
        for parameter in item.get('parameters', []):
            writable = parameter['id'] in ('Saturation', 'ColorHue', 'ImageKey', 'Contrast',
                'ContrastHighLight', 'ContrastShadow', 'Sharpness', 'Shading', 'ClarityControl')
            parameter.update(editable=writable, parameter_enable_supported=writable, enabled=writable,
                             parameter_supported=writable, native_field_supported=writable)
            if writable and parameter.get('default') is None:
                parameter['default'] = 4
        project['filters'].append(item)
        return self.store.save_project(project)

    def remove_filter(self, project_id, filter_id):
        project = self.project(project_id)
        if not _is120(project):
            return super().remove_filter(project_id, filter_id)
        row = next((row for row in project['filters'] if str(row['id']) == str(filter_id)), None)
        if not row or row.get('kind') != 'custom':
            raise ValueError('只能删除自定义滤镜')
        project['filters'] = [row for row in project['filters'] if str(row['id']) != str(filter_id)]
        return self.store.save_project(project)

    def add_shutdown_item(self, project_id, name=None, source_id=None, asset_id=None):
        project = self.project(project_id)
        if not _is120(project):
            return super().add_shutdown_item(project_id, name, source_id, asset_id)
        from .shutdown import prepare_asset, validate_draft, _decode_b64
        draft = deepcopy(project['shutdown'])
        if source_id and asset_id:
            raise ValueError('请选择一种图片来源')
        fit = 'contain'
        if source_id:
            resources = project['shutdown_inventory']['resources']
            source = next((row for row in resources if row['id'] == source_id), None)
            if source is None:
                raise ValueError('找不到所选原厂图案')
            target = next((row for row in resources if row['id'] == 'goodbye'), source)
            asset = prepare_asset(_decode_b64(source['payload_base64'], 'factory picture'),
                                  source['name'] + '.jpg', target, fit)
            draft['assets'].append(asset)
            asset_id, name = asset['id'], name or source['name']
        elif asset_id:
            asset = next((row for row in draft['assets'] if row['id'] == asset_id), None)
            if asset is None:
                raise ValueError('找不到所选图片素材')
            fit, name = asset['fit'], name or asset['name']
        self._shutdown_item(draft, name or '新图案', asset_id, fit)
        project['shutdown'] = validate_draft(draft, project['shutdown_inventory'])
        project['shutdown_draft'] = deepcopy(project['shutdown'])
        return self.store.save_project(project)

    def import_shutdown_image(self, project_id, target_id, data, filename='image.png', fit='contain',
                              item_id=None, item_name=None):
        project = self.project(project_id)
        if not _is120(project):
            return super().import_shutdown_image(project_id, target_id, data, filename, fit,
                                                  item_id=item_id, item_name=item_name)
        from .shutdown import prepare_asset, validate_draft, _legacy_item_name
        resources = project['shutdown_inventory']['resources']
        target = next((row for row in resources if row['id'] == (target_id or 'goodbye')), None)
        if target is None:
            raise ValueError('未识别所选关机资源')
        asset = prepare_asset(data, filename, target, fit)
        draft = deepcopy(project['shutdown'])
        draft.update(target_id=target['id'], selected_asset_id=asset['id'])
        draft['assets'].append(asset)
        if item_id:
            item = next((row for row in draft['items'] if row['id'] == item_id), None)
            if item is None:
                raise ValueError('所选关机图案不存在')
            item.update(asset_id=asset['id'], fit=fit)
            if item_name is not None:
                item['name'] = item_name
            draft['selected_item_id'] = item_id
        else:
            self._shutdown_item(draft, item_name or _legacy_item_name(asset['name'], '新图案'), asset['id'], fit)
        project['shutdown'] = validate_draft(draft, project['shutdown_inventory'])
        project['shutdown_draft'] = deepcopy(project['shutdown'])
        return self.store.save_project(project)

    def export_shutdown(self, project_id):
        project = self.project(project_id)
        if not _is120(project):
            return super().export_shutdown(project_id)
        from .shutdown import replacement_bundle
        return replacement_bundle(project['shutdown'], project['shutdown_inventory'], project['input'])

    def upgrade_firmware(self, project_id, data, filename='fwdc248b.bin'):
        """Create a new target project; transfer editable data, never ROM code."""
        source = self.project(project_id)
        if family(source['input']['version']) != '1.11':
            raise ValueError('一键升级需要先导入 1.11 客制固件或打开其编辑记录')
        if not source.get('filters') or not source.get('crop_inspection', {}).get('compiler_supported'):
            raise ValueError('来源 1.11 客制布局尚未识别，不能把未知功能冒充已迁移')
        from .firmware import load_image
        from .product120 import OFFICIAL_RTOS_SHA256, SOURCE_SHA256
        from .migration120 import migrate_configuration
        path, _ = self.store.object(data)
        target_image = load_image(path)
        if (family(target_image.internal_version) != '1.20' or
                sha256(target_image.rtos) != OFFICIAL_RTOS_SHA256 or
                target_image.sha256 != SOURCE_SHA256 or not target_image.editable):
            raise ValueError('请选择已适配的官方原版 1.20 BIN 作为升级目标')
        source_image = load_image(source['input_path'])
        if source_image.sha256 != source['input']['sha256']:
            raise ValueError('来源 1.11 固件已变化')
        from .slots import _metadata
        source['migration_crop_records'] = deepcopy((_metadata(source_image.rtos) or {}).get('crop_registry', []))
        target = self.import_firmware(data, filename, expected_family='1.20')
        try:
            target = migrate_configuration(source, target)
            target['crops'] = self._crop_draft(target, target['crops'])
        except Exception:
            # An unsuccessful migration never becomes an apparently successful
            # blank target project. Imported input objects remain reusable.
            self.store.project_path(target['id']).unlink(missing_ok=True)
            raise
        return self.store.save_project(target)

    def import_project(self, data):
        if not isinstance(data, dict) or family(data.get('input', {}).get('version', '')) != '1.20':
            return super().import_project(data)
        incoming = data.get('input', {})
        digest = incoming.get('sha256', '')
        if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('项目缺少有效输入 SHA256')
        path = self.store.root / 'objects' / (digest + '.bin')
        if not path.is_file():
            raise ValueError('请先导入这个备份对应的输入 BIN')
        project = self.import_firmware(path.read_bytes(), incoming.get('filename', 'fwdc248b.bin'), '1.20')
        # Backups carry data recipes, including archived image meanings. Native
        # addresses are reconstructed by the target compiler, never executed.
        project['compiler_filter_archive'] = deepcopy(data.get('compiler_filter_archive', []))
        project['migration_crop_records'] = deepcopy(data.get('migration_crop_records', []))
        project['migration'] = deepcopy(data.get('migration'))
        project['crops'] += [deepcopy(row) for row in data.get('crops', [])
                             if row.get('kind') == 'custom']
        self.store.save_project(project)
        return self.save(project['id'], title=data.get('title'), filters=data.get('filters'),
                         crops=data.get('crops'), shutdown=data.get('shutdown', data.get('shutdown_draft')),
                         build_options=data.get('build_options'), native_effects_draft=data.get('native_effects_draft'))

    def rollback_history_firmware(self, digest, project_id, version=None, allow_older=True):
        from .history import resolve_firmware
        from .firmware import load_image, repack_image, inspect_firmware
        source = load_image(resolve_firmware(self, digest))
        project = self.project(project_id)
        if family(source.internal_version) != family(project['input']['version']):
            raise ValueError('历史回退请选择相同固件系列；1.11 到 1.20 请使用一键迁移')
        if family(source.internal_version) != '1.20':
            return super().rollback_history_firmware(digest, project_id, version, allow_older)
        from .product120 import canonical_base, snapshot120, metadata, wrap_official_input
        with self.store.lock:
            if not source.editable:
                raise ValueError('该历史 1.20 固件没有可恢复的产品布局')
            next_version = self._allocate_version(source.version, version, current=project['input']['version'])
            rtos = source.rtos if metadata(source.rtos) is not None else wrap_official_input(source)
            candidate, package = repack_image(source, rtos, new_version=version_word(next_version),
                                               allow_older=build_options(allow_older)['allow_older'])
            build_id = self.store.new_id('build')
            folder = self.store.root / 'builds' / build_id
            folder.mkdir(parents=True, exist_ok=True)
            firmware_path = folder / 'fwdc248b.bin'
            firmware_path.write_bytes(candidate)
            result = load_image(firmware_path)
            snapshot120(result)
            if (canonical_base(result.rtos) != canonical_base(source.rtos) or
                    result.icon_bytes != source.icon_bytes):
                raise ValueError('1.20 历史回退的程序或图标恢复失败')
            old_res = next(row for row in source.sections if row['name'] == 'RES')
            new_res = next(row for row in result.sections if row['name'] == 'RES')
            if source.decoded[old_res['start']:old_res['end']] != result.decoded[new_res['start']:new_res['end']]:
                raise ValueError('1.20 历史回退改变了背景资源')
            created = timestamp()
            manifest = {'schema_version': 1, 'edition': project['edition'], 'id': build_id,
                'project_id': project_id, 'created_at': created, 'version': next_version,
                'source': {'sha256': source.sha256, 'version': source.internal_version},
                'sha256': sha256(candidate), 'parent_sha256': source.sha256,
                'restored_from_sha256': source.sha256, 'rollback_from_sha256': project['input']['sha256'],
                'inspection': inspect_firmware(result), 'package_report': package,
                'update_policy': package['update_policy'],
                'validation': {'offline': 'passed', 'hardware': 'not_tested', 'camera_or_card_written': False}}
            manifest_path = folder / 'manifest.json'
            write_json(manifest_path, manifest)
            record = {key: manifest[key] for key in ('id', 'project_id', 'created_at', 'version', 'sha256',
                       'parent_sha256', 'restored_from_sha256', 'rollback_from_sha256')}
            record.update(firmware_path=str(firmware_path), manifest_path=str(manifest_path),
                status='offline_candidate', download_url=f'/api/download?build_id={build_id}&kind=firmware',
                manifest_url=f'/api/download?build_id={build_id}&kind=manifest')
            self.store.register_build(record)
            project['builds'].append(record)
            self.store.save_project(project)
            return {**record, 'report': {'package': package}}

    def build(self, project_id, version=None, allow_older=None):
        stored = self.store.get_project(project_id)
        if not _is120(stored):
            return super().build(project_id, version, allow_older)
        from .firmware import load_image, repack_image, inspect_firmware
        from .product120 import compile_product120, snapshot120, canonical_base
        with self.store.lock:
            project = self.project(project_id)
            image = load_image(project['input_path'])
            if image.sha256 != project['input']['sha256'] or not image.editable:
                raise ValueError('输入固件已变化或布局不受支持')
            for row in project['filters']:
                if not row.get('enabled', True):
                    continue
                if row.get('pending_color_conversion'):
                    raise ValueError('还有滤镜等待 XMP 转换，请转换完成后生成')
                if row.get('xmp') and (row.get('calibration') or {}).get('status') == 'calibration_failed':
                    raise ValueError('滤镜校色失败，请重试或停用该滤镜')
            options = build_options(project['build_options'] if allow_older is None else allow_older)
            next_version = self._allocate_version(image.version, version)
            planned_filters = deepcopy(project['filters'])
            live_ids = {str(row['id']) for row in planned_filters}
            for archived in project.get('compiler_filter_archive', []):
                if str(archived['id']) not in live_ids:
                    row = deepcopy(archived)
                    row['enabled'] = False
                    planned_filters.append(row)
            rtos, icons, res, patch = compile_product120(image, filters=planned_filters,
                crops=project['crops'], shutdown=project['shutdown'],
                crop_records=project.get('migration_crop_records'),
                pictorialist=project['native_effects_draft']['pictorialist'],
                version=next_version, allow_older=options['allow_older'])
            candidate, package = repack_image(image, rtos, new_icon_bytes=icons,
                new_res_section=res, new_version=version_word(next_version),
                allow_older=options['allow_older'])
            build_id = self.store.new_id('build')
            folder = self.store.root / 'builds' / build_id
            folder.mkdir(parents=True, exist_ok=True)
            firmware_path = folder / 'fwdc248b.bin'
            firmware_path.write_bytes(candidate)
            packaged = load_image(firmware_path)
            if not packaged.editable:
                raise ValueError('生成的 1.20 固件不能独立重新导入')
            readback = snapshot120(packaged)
            if canonical_base(packaged.rtos) != canonical_base(image.rtos):
                raise ValueError('生成固件的原厂基线恢复不一致')
            created = timestamp()
            manifest = {'schema_version': 1, 'edition': project['edition'], 'id': build_id,
                'project_id': project_id, 'created_at': created, 'version': next_version,
                'source': project['input'], 'sha256': sha256(candidate), 'parent_sha256': image.sha256,
                'migration': project.get('migration'), 'recipe_filters': project['filters'],
                'recipe_crops': project['crops'], 'recipe_shutdown': project['shutdown'],
                'native_effects_draft': project['native_effects_draft'],
                'patch_report': patch, 'package_report': package,
                'filters': readback['filters'], 'crops': readback['crops'],
                'crop_inspection': readback['crop_inspection'], 'inspection': inspect_firmware(packaged),
                'update_policy': package['update_policy'], 'update_policy_requested': options['allow_older'],
                'source_provenance': self.source_provenance(),
                'validation': {'offline': 'passed', 'hardware': 'not_tested',
                               'camera_or_card_written': False,
                               'scope': 'container checksums, native product resources and canonical base recovery'}}
            manifest_path = folder / 'manifest.json'
            write_json(manifest_path, manifest)
            record = {'id': build_id, 'project_id': project_id, 'created_at': created,
                'version': next_version, 'sha256': manifest['sha256'], 'parent_sha256': image.sha256,
                'firmware_path': str(firmware_path), 'manifest_path': str(manifest_path),
                'status': 'offline_candidate',
                'download_url': f'/api/download?build_id={build_id}&kind=firmware',
                'manifest_url': f'/api/download?build_id={build_id}&kind=manifest'}
            self.store.register_build(record)
            # Keep completed photo identities even when the user continues
            # editing the same project without re-importing its last BIN.
            product_record = __import__('gr4_editor.product120', fromlist=['metadata']).metadata(packaged.rtos)
            project['compiler_filter_archive'] = [deepcopy(row) for row in readback['filters']
                                                   if row.get('kind') == 'custom']
            project['migration_crop_records'] = deepcopy(product_record['crop_records'])
            filters_by_id = {str(row['id']): row for row in readback['filters']}
            for row in project['filters']:
                actual = filters_by_id.get(str(row['id']))
                if actual and row.get('kind') == 'custom':
                    row['identity'] = deepcopy(actual['identity'])
            crops_by_id = {row['id']: row for row in readback['crops']}
            for row in project['crops']:
                actual = crops_by_id.get(row['id'])
                if actual and row.get('kind') == 'custom':
                    row['identity'] = deepcopy(actual['identity'])
                    row['source_identity'] = deepcopy(actual['identity'])
                    row['geometry'] = deepcopy(actual['geometry'])
                    row['source_geometry'] = deepcopy(actual['geometry'])
                    row['actual_ratio'] = actual['actual_ratio']
                    row['source_requested_ratio'] = actual['requested_ratio']
            project['builds'].append(record)
            self.store.save_project(project)
            return {**record, 'report': {'patch': patch, 'package': package}}
