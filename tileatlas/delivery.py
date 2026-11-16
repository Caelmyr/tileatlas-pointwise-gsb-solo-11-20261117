import io
import json
import zipfile
import hashlib
from image_lab.validation import Invalid
from .raster import encode, decode
from .packing import sprite_metadata, reconstruct


def atlas_bundle(p, layout, image):
    metadata = {'format': 'tileatlas-1', 'project': p['name'], 'revision': p['revision'], 'image': 'atlas.png', 'size': [layout['size'], layout['size']], 'sprites': [sprite_metadata(item, layout['size']) for item in layout['sprites']]}
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('atlas.png', encode(image))
        archive.writestr('atlas.json', json.dumps(metadata, ensure_ascii=False, indent=2))
        for item in layout['sprites']:
            archive.writestr('reconstructed/' + item['id'] + '.png', encode(reconstruct(image, item)))
    return stream.getvalue()


def project_bundle(catalog, p):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('project.json', json.dumps(p, ensure_ascii=False))
        for asset in sorted({s['asset'] for s in p['sprites']}):
            raw = (catalog.directory(p['id']) / (asset + '.png')).read_bytes()
            archive.writestr('assets/' + asset + '.png', raw)
    return stream.getvalue()


def import_project(catalog, raw):
    if len(raw) > 64 * 1024 * 1024:
        raise Invalid('项目包不得超过64MB')
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            if len(infos) > 130 or sum(i.file_size for i in infos) > 128 * 1024 * 1024:
                raise Invalid('项目包展开后超出预算')
            if len({i.filename for i in infos}) != len(infos):
                raise Invalid('项目包含重复成员')
            p = json.loads(archive.read('project.json'))
            sprites = p['sprites']
            if len(sprites) > 128 or len({s['id'] for s in sprites}) != len(sprites):
                raise Invalid('精灵标识重复或数量超限')
            staged = []
            from .raster import fingerprint, transform
            from . import nine as ninegrid
            for s in sprites:
                image = decode(archive.read('assets/' + s['asset'] + '.png'))
                if fingerprint(image) != s['asset']:
                    raise Invalid('项目图片指纹不符')
                transform(image, s['settings'])
                if len(s['pivot']) != 2:
                    raise Invalid('项目轴心无效')
                if s.get('nine') is not None:
                    s['nine'] = ninegrid.validate(s['nine'], image.size)
                staged.append((s['asset'], encode(image)))
            with catalog.lock:
                created = catalog.create(str(p['name']) + ' · 导入')
                created['sprites'] = sprites
                created['packing'] = p['packing']
                from .packing import options
                options(created['packing'])
                for asset, data in staged:
                    (catalog.directory(created['id']) / (asset + '.png')).write_bytes(data)
                catalog.write(created)
                return created
    except (KeyError, ValueError, TypeError, zipfile.BadZipFile) as error:
        raise Invalid('项目包格式无效') from error
