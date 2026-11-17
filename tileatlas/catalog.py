import copy
import json
import uuid
import threading
import time
from pathlib import Path
from image_lab.validation import Invalid, Conflict
from .raster import encode, decode, transform, fingerprint
from .packing import options, stable_pack, repack, signature, record_of, LayoutBlocked


def record_ids(record):
    return {entry['id'] for entry in (record or {}).get('placements', [])}


class Catalog:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def directory(self, key):
        if not isinstance(key, str) or len(key) != 24 or any(c not in '0123456789abcdef' for c in key):
            raise Invalid('无效图集ID')
        return self.root / key

    def read(self, key):
        path = self.directory(key) / 'project.json'
        if not path.is_file():
            raise Invalid('图集项目不存在')
        return json.loads(path.read_text())

    def write(self, project):
        directory = self.directory(project['id'])
        directory.mkdir(exist_ok=True)
        path = directory / 'project.json'
        temporary = directory / ('write-' + uuid.uuid4().hex)
        temporary.write_text(json.dumps(project, ensure_ascii=False, indent=2))
        temporary.replace(path)

    def read_layout(self, key):
        path = self.directory(key) / 'layout.json'
        if not path.is_file():
            return None
        return json.loads(path.read_text())

    def write_layout(self, key, layout):
        directory = self.directory(key)
        directory.mkdir(exist_ok=True)
        path = directory / 'layout.json'
        temporary = directory / ('layout-' + uuid.uuid4().hex)
        temporary.write_text(json.dumps(layout, ensure_ascii=False, indent=2))
        temporary.replace(path)

    def create(self, name):
        with self.lock:
            p = {'id': uuid.uuid4().hex[:24], 'name': str(name).strip()[:100] or '新图集', 'revision': 0, 'created': time.time(), 'sprites': [], 'packing': {'size': 512, 'padding': 2, 'extrude': 1, 'rotate': True}, 'locked': []}
            self.write(p)
            return p

    def list(self):
        with self.lock:
            items = []
            for path in sorted(self.root.glob('*/project.json')):
                p = json.loads(path.read_text())
                layout = self.read_layout(p['id'])
                items.append({'id': p['id'], 'name': p['name'], 'revision': p['revision'], 'sprites': len(p['sprites']), 'confirmed': bool(layout and layout.get('placements')), 'confirmed_revision': (layout or {}).get('revision'), 'pending': (self.directory(p['id']) / 'pending.json').is_file()})
            return items

    def change(self, key, revision, fn):
        with self.lock:
            p = self.read(key)
            if int(revision) != p['revision']:
                raise Conflict('项目已变化，请刷新后重试')
            fn(p)
            p['revision'] += 1
            self.write(p)
            return copy.deepcopy(p)

    def sprite(self, p, sid):
        for item in p['sprites']:
            if item['id'] == sid:
                return item
        raise Invalid('精灵不存在')

    def raw(self, p, item):
        return decode((self.directory(p['id']) / (item['asset'] + '.png')).read_bytes())

    def add(self, key, revision, raw, name):
        image = decode(raw)
        asset = fingerprint(image)
        def apply(p):
            if len(p['sprites']) >= 128:
                raise Invalid('每图集最多128个精灵')
            (self.directory(key) / (asset + '.png')).write_bytes(encode(image))
            p['sprites'].append({'id': uuid.uuid4().hex[:24], 'name': str(name)[:100], 'asset': asset, 'original_size': list(image.size), 'pivot': [.5, .5], 'enabled': True, 'tags': [], 'settings': {'trim': True, 'threshold': 0, 'scale': 1, 'turn': 0, 'flip_x': False, 'flip_y': False, 'pixel_art': True}})
        return self.change(key, revision, apply)

    def replace(self, key, sid, revision, raw, name=None):
        image = decode(raw)
        asset = fingerprint(image)
        def apply(p):
            item = self.sprite(p, sid)
            (self.directory(key) / (asset + '.png')).write_bytes(encode(image))
            item['asset'] = asset
            item['original_size'] = list(image.size)
            if name is not None:
                item['name'] = str(name)[:100] or item['name']
        return self.change(key, revision, apply)

    def set_locked(self, key, revision, ids, locked=True):
        def apply(p):
            if not isinstance(ids, list):
                raise Invalid('锁定清单需要数组')
            known = {item['id'] for item in p['sprites']}
            unknown = [sid for sid in ids if sid not in known]
            if unknown:
                raise Invalid('锁定精灵不存在：' + ', '.join(unknown[:4]))
            group = set(p.get('locked', []))
            group.update(ids) if locked else group.difference_update(ids)
            p['locked'] = sorted(group)
        return self.change(key, revision, apply)

    def update(self, key, sid, revision, data):
        def apply(p):
            item = self.sprite(p, sid)
            if 'name' in data:
                item['name'] = str(data['name'])[:100]
            if 'enabled' in data:
                item['enabled'] = bool(data['enabled'])
            if 'tags' in data:
                if not isinstance(data['tags'], list):
                    raise Invalid('标签需要数组')
                item['tags'] = list(dict.fromkeys(str(v)[:32] for v in data['tags']))[:16]
            if 'pivot' in data:
                v = data['pivot']
                if len(v) != 2 or not all(isinstance(n, (int, float)) and -2 <= n <= 3 for n in v):
                    raise Invalid('轴心为两个-2至3的坐标')
                item['pivot'] = v
            if 'settings' in data:
                candidate = {**item['settings'], **data['settings']}
                if not 0 <= int(candidate['threshold']) <= 254:
                    raise Invalid('透明阈值须为0–254')
                transform(self.raw(p, item), candidate)
                item['settings'] = candidate
        return self.change(key, revision, apply)

    def remove(self, key, sid, revision):
        def apply(p):
            self.sprite(p, sid)
            p['sprites'] = [s for s in p['sprites'] if s['id'] != sid]
        return self.change(key, revision, apply)

    def prepared(self, p, selected=None):
        items = []
        for item in p['sprites']:
            if not item['enabled'] or selected is not None and item['id'] not in selected:
                continue
            data = transform(self.raw(p, item), item['settings'])
            items.append({**item, **data})
        if not items:
            raise Invalid('请至少启用一个精灵')
        return items

    def duplicates(self, p):
        groups = {}
        for item in self.prepared(p):
            groups.setdefault(fingerprint(item['image']), []).append(item['id'])
        return [ids for ids in groups.values() if len(ids) > 1]

    def locked(self, p):
        known = {item['id'] for item in p['sprites']}
        return [sid for sid in p.get('locked', []) if sid in known]

    def resolve(self, p, data=None):
        """Build the layout a request asks for, without saving anything.

        mode='preserve' reuses the confirmed snapshot; mode='repack' ignores it.
        Without a snapshot the only honest answer is a fresh full pack, so that
        is what preview shows and it is flagged as such.
        """
        data = data or {}
        mode = data.get('mode', 'preserve')
        record = self.read_layout(p['id'])
        packing = data.get('packing')
        if packing is None and mode != 'repack' and record and record.get('settings'):
            packing = record['settings']
        settings = options(packing or p['packing'])
        items = self.prepared(p)
        locked = self.locked(p)
        if mode != 'repack' and record and record.get('placements'):
            if record.get('settings', {}).get('size') != settings['size']:
                raise LayoutBlocked(
                    f"已确认布局基于边长{record.get('settings', {}).get('size')}，当前请求边长{settings['size']}。UV按图集归一化，改变边长会改动全部UV；请先确认完全重排，或把边长改回{record.get('settings', {}).get('size')}。",
                    [{'id': None, 'name': '图集边长', 'reason': 'size', 'needed': [settings['size'], settings['size']]}],
                )
            layout = stable_pack(items, settings, record, locked)
            layout['saved_revision'] = record.get('revision')
            layout['stale'] = record.get('signature') != signature(items, settings)
        else:
            layout = repack(items, settings, record, locked)
            layout['mode'] = 'repack'
            layout['saved_revision'] = (record or {}).get('revision')
            layout['stale'] = False
        layout['removed'] = [entry for entry in layout.get('removed', []) if entry['id'] not in {s['id'] for s in p['sprites'] if s['enabled']}]
        disabled = {item['id']: item for item in p['sprites'] if not item['enabled']}
        packed = {placement['id'] for placement in layout['sprites']}
        seen = set()
        for entry in layout['removed']:
            seen.add(entry['id'])
            if entry['id'] in disabled:
                entry['reason'] = 'disabled'
                entry['name'] = disabled[entry['id']]['name']
        for sid in record_ids(record):
            if sid in disabled and sid not in packed and sid not in seen:
                layout['removed'].append({'id': sid, 'name': disabled[sid]['name'], 'reason': 'disabled'})
                seen.add(sid)
        layout['locked'] = locked
        layout['revision'] = p['revision']
        return layout

    def pending(self, p):
        """The previewed layout, or None when nothing is waiting for confirmation."""
        path = self.directory(p['id']) / 'pending.json'
        if not path.is_file():
            return None
        pending = json.loads(path.read_text())
        try:
            current = signature(self.prepared(p), options(pending['layout']['settings']))
        except Invalid:
            current = None
        if pending.get('revision') != p['revision'] or current is None or pending.get('signature') != current:
            path.unlink(missing_ok=True)
            return None
        return pending

    def layout_fresh(self, p):
        """True when the confirmed snapshot still describes the current sprites exactly."""
        record = self.read_layout(p['id'])
        if not record or not record.get('placements'):
            return False
        try:
            items = self.prepared(p)
        except Invalid:
            return False
        settings = options(record.get('settings') or p['packing'])
        return record.get('signature') == signature(items, settings) and settings == options(p['packing'])

    def stage(self, key, data):
        with self.lock:
            p = self.read(key)
            if 'revision' in data and int(data['revision']) != p['revision']:
                raise Conflict('项目已变化，请刷新后重试')
            layout = self.resolve(p, data)
            path = self.directory(key) / 'pending.json'
            temporary = self.directory(key) / ('pending-' + uuid.uuid4().hex)
            temporary.write_text(json.dumps({'revision': p['revision'], 'signature': signature(self.prepared(p), layout['settings']), 'layout': layout}, ensure_ascii=False, indent=2))
            temporary.replace(path)
            return {**copy.deepcopy(layout), 'pending': True}

    def discard(self, key):
        with self.lock:
            (self.directory(key) / 'pending.json').unlink(missing_ok=True)
            return self.read(key)

    def confirm(self, key, data):
        with self.lock:
            p = self.read(key)
            if 'revision' in data and int(data['revision']) != p['revision']:
                raise Conflict('项目已变化，请刷新后重试')
            pending = self.pending(p)
            if pending is None:
                raise Invalid('没有待确认的布局预览，请先生成预览')
            return self.adopt(key, p, pending['layout'])

    def adopt(self, key, p, layout):
        self.write_layout(key, record_of(layout, p['revision'], self.prepared(p)))
        (self.directory(key) / 'pending.json').unlink(missing_ok=True)
        return {'layout': {**copy.deepcopy(layout), 'pending': False}, 'project': self.sync_packing(p, layout['settings'])}

    def sync_packing(self, p, settings):
        """Keep the project's packing controls aligned with the layout just adopted."""
        if p['packing'] != settings:
            p['packing'] = dict(settings)
            p['revision'] += 1
            self.write(p)
        return copy.deepcopy(p)

    def reset_layout(self, key, revision):
        with self.lock:
            p = self.read(key)
            if int(revision) != p['revision']:
                raise Conflict('项目已变化，请刷新后重试')
            (self.directory(key) / 'layout.json').unlink(missing_ok=True)
            (self.directory(key) / 'pending.json').unlink(missing_ok=True)
            return copy.deepcopy(p)

    def repack_confirm(self, key, data):
        """A deliberately explicit full repack; never used implicitly."""
        with self.lock:
            data = {**data, 'mode': 'repack'}
            p = self.read(key)
            if 'revision' in data and int(data['revision']) != p['revision']:
                raise Conflict('项目已变化，请刷新后重试')
            layout = self.resolve(p, data)
            return self.adopt(key, p, layout)
