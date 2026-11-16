import math
from PIL import Image, ImageDraw
from image_lab.validation import Invalid
from .raster import extrude


def intersects(a, b):
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def contained(a, b):
    return a[0] >= b[0] and a[1] >= b[1] and a[0] + a[2] <= b[0] + b[2] and a[1] + a[3] <= b[1] + b[3]


def split_free(free, used):
    result = []
    for box in free:
        if not intersects(box, used):
            result.append(box)
            continue
        x, y, w, h = box
        ux, uy, uw, uh = used
        if ux > x:
            result.append((x, y, ux - x, h))
        if ux + uw < x + w:
            result.append((ux + uw, y, x + w - ux - uw, h))
        if uy > y:
            result.append((x, y, w, uy - y))
        if uy + uh < y + h:
            result.append((x, uy + uh, w, y + h - uy - uh))
    unique = list(dict.fromkeys(result))
    return [a for i, a in enumerate(unique) if a[2] > 0 and a[3] > 0 and not any(i != j and contained(a, b) for j, b in enumerate(unique))]


def options(data):
    size = int(data.get('size', 512))
    if size not in (128, 256, 512, 1024, 2048):
        raise Invalid('图集边长支持128、256、512、1024、2048')
    pad = int(data.get('padding', 2))
    bleed = int(data.get('extrude', 1))
    if not 0 <= pad <= 16 or not 0 <= bleed <= 8:
        raise Invalid('间距须0–16，外扩须0–8')
    return {'size': size, 'padding': pad, 'extrude': bleed, 'rotate': bool(data.get('rotate', True))}


def pack(items, settings):
    size, gap, bleed = settings['size'], settings['padding'], settings['extrude']
    free = [(0, 0, size, size)]
    placements = []
    ordered = sorted(items, key=lambda item: (-max(item['image'].size), -item['image'].width * item['image'].height, item['id']))
    for item in ordered:
        w, h = item['image'].size
        choices = []
        for box in free:
            for rotated in (False, True) if settings['rotate'] and w != h else (False,):
                rw, rh = (h, w) if rotated else (w, h)
                aw, ah = rw + 2 * bleed + gap, rh + 2 * bleed + gap
                if aw <= box[2] and ah <= box[3]:
                    score = (min(box[2] - aw, box[3] - ah), max(box[2] - aw, box[3] - ah), box[1], box[0], rotated)
                    choices.append((score, box[0], box[1], aw, ah, rw, rh, rotated))
        if not choices:
            raise Invalid('图集放不下所有精灵，请增大边长或减少选择')
        _, x, y, aw, ah, rw, rh, rotated = min(choices)
        used = (x, y, aw, ah)
        free = split_free(free, used)
        placements.append({'id': item['id'], 'name': item['name'], 'x': x + bleed, 'y': y + bleed, 'w': rw, 'h': rh, 'rotated': rotated, 'allocation': list(used), 'bounds': item['bounds'], 'source_size': item['source_size'], 'pivot': item['pivot'], 'empty': item['empty'], 'nine_info': item.get('nine_info')})
    validate(placements, size)
    occupied = sum(p['w'] * p['h'] for p in placements)
    return {'size': size, 'settings': settings, 'sprites': placements, 'occupancy': occupied / (size * size), 'free_rectangles': len(free)}


def validate(placements, size):
    for index, item in enumerate(placements):
        x, y, w, h = item['allocation']
        if min(x, y) < 0 or x + w > size or y + h > size:
            raise Invalid('图集矩形越界')
        for other in placements[:index]:
            if intersects(item['allocation'], other['allocation']):
                raise Invalid('图集矩形相交')


def render(layout, items, outlines=False):
    lookup = {item['id']: item for item in items}
    out = Image.new('RGBA', (layout['size'], layout['size']))
    bleed = layout['settings']['extrude']
    for placement in layout['sprites']:
        image = lookup[placement['id']]['image']
        if placement['rotated']:
            image = image.transpose(Image.Transpose.ROTATE_270)
        extended = extrude(image, bleed)
        out.paste(extended, (placement['x'] - bleed, placement['y'] - bleed))
    if outlines:
        draw = ImageDraw.Draw(out)
        for placement in layout['sprites']:
            x, y, w, h = placement['allocation']
            draw.rectangle((x, y, x + w - 1, y + h - 1), outline='#ffcc55')
    return out


def sprite_metadata(placement, size):
    x, y, w, h = (placement[k] for k in ('x', 'y', 'w', 'h'))
    source_w, source_h = placement['source_size']
    left, top, right, bottom = placement['bounds']
    px = placement['pivot'][0] * source_w - left
    py = placement['pivot'][1] * source_h - top
    corners = [[x / size, y / size], [(x + w) / size, y / size], [(x + w) / size, (y + h) / size], [x / size, (y + h) / size]]
    if placement['rotated']:
        corners = corners[1:] + corners[:1]
    meta = {'id': placement['id'], 'name': placement['name'], 'frame': {'x': x, 'y': y, 'w': w, 'h': h}, 'rotated': placement['rotated'], 'source_size': [source_w, source_h], 'trim': [left, top, right - left, bottom - top], 'pivot': placement['pivot'], 'trimmed_pivot_pixels': [px, py], 'uv_corners': corners, 'empty': placement['empty']}
    if placement.get('nine_info'):
        meta['nine'] = placement['nine_info']
    return meta


def reconstruct(atlas, placement):
    x, y, w, h = (placement[k] for k in ('x', 'y', 'w', 'h'))
    crop = atlas.crop((x, y, x + w, y + h))
    if placement['rotated']:
        crop = crop.transpose(Image.Transpose.ROTATE_90)
    out = Image.new('RGBA', tuple(placement['source_size']))
    out.paste(crop, tuple(placement['bounds'][:2]))
    return out
