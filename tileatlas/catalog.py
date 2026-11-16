import copy
import json
import uuid
import threading
import time
from pathlib import Path
from image_lab.validation import Invalid, Conflict
from .raster import encode, decode, transform, fingerprint
from . import nine as ninegrid


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

    def create(self, name):
        with self.lock:
            p = {'id': uuid.uuid4().hex[:24], 'name': str(name).strip()[:100] or '新图集', 'revision': 0, 'created': time.time(), 'sprites': [], 'packing': {'size': 512, 'padding': 2, 'extrude': 1, 'rotate': True}}
            self.write(p)
            return p

    def list(self):
        with self.lock:
            items = []
            for path in sorted(self.root.glob('*/project.json')):
                p = json.loads(path.read_text())
                items.append({'id': p['id'], 'name': p['name'], 'revision': p['revision'], 'sprites': len(p['sprites'])})
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
            if 'nine' in data:
                item['nine'] = ninegrid.validate(data['nine'], self.raw(p, item).size)
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
            raw = self.raw(p, item)
            data = transform(raw, item['settings'])
            entry = {**item, **data}
            if item.get('nine') and item['nine'].get('enabled'):
                entry['nine_info'] = ninegrid.frame_info(item['nine'], item['settings'], raw.size, data)
            items.append(entry)
        if not items:
            raise Invalid('请至少启用一个精灵')
        return items

    def duplicates(self, p):
        groups = {}
        for item in self.prepared(p):
            groups.setdefault(fingerprint(item['image']), []).append(item['id'])
        return [ids for ids in groups.values() if len(ids) > 1]
