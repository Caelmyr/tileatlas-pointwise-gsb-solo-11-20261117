import hashlib
import json
import math
from PIL import Image, ImageDraw
from image_lab.validation import Invalid
from .raster import extrude


class LayoutBlocked(Invalid):
    """A layout could not be produced without violating locks or capacity."""

    def __init__(self, message, blockers):
        super().__init__(message)
        self.blockers = blockers


def largest_free(free):
    return max(free, key=lambda box: box[2] * box[3], default=(0, 0, 0, 0))


def required_size(item, settings):
    w, h = item['image'].size
    bleed, gap = settings['extrude'], settings['padding']
    sizes = [(w + 2 * bleed + gap, h + 2 * bleed + gap)]
    if settings['rotate'] and w != h:
        sizes.append((h + 2 * bleed + gap, w + 2 * bleed + gap))
    return min(sizes)


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


def allocation(item, x, y, rotated, settings):
    w, h = item['image'].size
    rw, rh = (h, w) if rotated else (w, h)
    bleed, gap = settings['extrude'], settings['padding']
    ax, ay = x - bleed, y - bleed
    aw, ah = rw + 2 * bleed + gap, rh + 2 * bleed + gap
    size = settings['size']
    if ax < 0 or ay < 0 or ax + aw > size or ay + ah > size:
        return None
    return (ax, ay, aw, ah, rw, rh, rotated)


def find_slot(item, free, settings):
    w, h = item['image'].size
    gap, bleed = settings['padding'], settings['extrude']
    choices = []
    for box in free:
        for rotated in (False, True) if settings['rotate'] and w != h else (False,):
            rw, rh = (h, w) if rotated else (w, h)
            aw, ah = rw + 2 * bleed + gap, rh + 2 * bleed + gap
            if aw <= box[2] and ah <= box[3]:
                score = (min(box[2] - aw, box[3] - ah), max(box[2] - aw, box[3] - ah), box[1], box[0], rotated)
                choices.append((score, box[0], box[1], aw, ah, rw, rh, rotated))
    return min(choices) if choices else None


def place(item, x, y, rw, rh, rotated, settings):
    bleed, gap = settings['extrude'], settings['padding']
    used = (x - bleed, y - bleed, rw + 2 * bleed + gap, rh + 2 * bleed + gap)
    return {'id': item['id'], 'name': item['name'], 'x': x, 'y': y, 'w': rw, 'h': rh, 'rotated': rotated, 'allocation': list(used), 'bounds': item['bounds'], 'source_size': item['source_size'], 'pivot': item['pivot'], 'empty': item['empty'], 'asset': item.get('asset'), 'locked': bool(item.get('locked'))}


def pack(items, settings, pinned=()):
    free = [(0, 0, settings['size'], settings['size'])]
    placements = []
    for item, slot in pinned:
        free = split_free(free, tuple(slot[:4]))
        placements.append(place(item, slot[0] + settings['extrude'], slot[1] + settings['extrude'], slot[4], slot[5], slot[6], settings))
    ordered = sorted(items, key=lambda item: (-max(item['image'].size), -item['image'].width * item['image'].height, item['id']))
    for item in ordered:
        slot = find_slot(item, free, settings)
        if slot is None:
            entry = blocker(item, 'capacity', settings)
            rect = largest_free(free)
            entry['largest_free'] = [rect[2], rect[3]]
            raise LayoutBlocked(blocker_message([entry], settings), [entry])
        _, x, y, aw, ah, rw, rh, rotated = slot
        free = split_free(free, (x, y, aw, ah))
        placements.append(place(item, x + settings['extrude'], y + settings['extrude'], rw, rh, rotated, settings))
    validate(placements, settings['size'])
    occupied = sum(p['w'] * p['h'] for p in placements)
    return {'size': settings['size'], 'settings': settings, 'sprites': placements, 'occupancy': occupied / (settings['size'] ** 2), 'free_rectangles': len(free), 'mode': 'repack', 'changes': [], 'removed': [], 'confirmed': False}


def repack(items, settings, record, locked=None):
    """Deliberately ignore saved positions and pack from scratch.

    Locked sprites still keep their confirmed rectangle when they have one: the
    lock means the sprite must not move, including under a full repack.
    """
    saved = {entry['id']: entry for entry in (record or {}).get('placements', [])}
    locked = set(locked or ())
    pinned, others, blockers = [], [], []
    for item in items:
        entry = saved.get(item['id'])
        if item['id'] in locked and entry is not None:
            slot = allocation(item, entry['x'], entry['y'], entry['rotated'], settings)
            if slot is None:
                blockers.append(blocker(item, 'locked', settings, note='锁定项的新尺寸无法留在原位'))
                continue
            pinned.append((item, slot))
        else:
            others.append(item)
    if blockers:
        raise LayoutBlocked(blocker_message(blockers, settings), blockers)
    pinned_ids = {item['id'] for item, _ in pinned}
    layout = pack([item for item in others if item['id'] not in pinned_ids], settings, pinned)
    lookup = {item['id']: item for item in items}
    removed = {sid: entry['name'] for sid, entry in saved.items() if sid not in lookup}
    for placement in layout['sprites']:
        placement['locked'] = placement['id'] in locked
    layout['changes'] = describe_changes(layout['sprites'], saved, lookup, locked, removed, (record or {}).get('size'), settings['size'])
    layout['removed'] = [{'id': sid, 'name': name, 'reason': 'removed'} for sid, name in removed.items()]
    return layout


def stable_pack(items, settings, record, locked=None):
    """Reuse confirmed placements and only move what can no longer stay.

    Every enabled sprite is accounted for. Locked sprites are pinned; a locked
    sprite that cannot stay where it was (or cannot be given space) aborts the
    update with a structured explanation. Nothing is ever dropped.
    """
    saved = {entry['id']: entry for entry in (record or {}).get('placements', [])}
    locked = set(locked or ())
    removed = {sid: entry['name'] for sid, entry in saved.items() if sid not in {item['id'] for item in items}}
    candidates, wanted, blockers = [], [], []
    lookup = {item['id']: item for item in items}

    for item in sorted(items, key=lambda i: (-max(i['image'].size), -i['image'].width * i['image'].height, i['id'])):
        entry, pinned = saved.get(item['id']), item['id'] in locked
        slot = allocation(item, entry['x'], entry['y'], entry['rotated'], settings) if entry else None
        if entry is not None and pinned and slot is None:
            blockers.append(blocker(item, 'locked', settings, note='锁定项的新尺寸无法留在原位'))
            continue
        if slot is not None and entry['rotated'] and not settings['rotate']:
            if pinned:
                blockers.append(blocker(item, 'locked', settings, note='锁定项原本旋转放置，但本次打包禁止旋转'))
                continue
            slot = None
        unchanged = entry is not None and entry.get('asset') == item.get('asset') and entry.get('source_size') == item['source_size'] and entry.get('bounds') == item['bounds']
        if entry is None or slot is None:
            wanted.append({'item': item, 'entry': entry, 'pinned': pinned, 'unchanged': unchanged})
            continue
        candidates.append({'item': item, 'entry': entry, 'slot': slot, 'pinned': pinned, 'unchanged': unchanged})

    # Pinned and unchanged sprites claim their old rectangles first; a locked
    # rectangle wins over any confirmed sprite it collides with.
    candidates.sort(key=lambda c: (not c['pinned'], not c['unchanged'], c['item']['id']))
    accepted = []
    for candidate in candidates:
        rect = candidate['slot'][:4]
        clash = next((a for a in accepted if intersects(rect, a['slot'][:4])), None)
        if clash is None:
            accepted.append(candidate)
            continue
        if candidate['pinned']:
            blockers.append(blocker(candidate['item'], 'locked', settings, conflicts=[clash['item']['id']], note=f"与锁定精灵“{clash['item']['name']}”的矩形重叠"))
        else:
            wanted.append({'item': candidate['item'], 'entry': candidate['entry'], 'pinned': False, 'unchanged': candidate['unchanged']})

    free = [(0, 0, settings['size'], settings['size'])]
    placements = []
    for candidate in accepted:
        slot, item = candidate['slot'], candidate['item']
        free = split_free(free, tuple(slot[:4]))
        placements.append(place(item, slot[0] + settings['extrude'], slot[1] + settings['extrude'], slot[4], slot[5], slot[6], settings))

    # Everything else competes for the remaining rectangles. Stability order:
    # pinned, then unchanged confirmed sprites, then changed ones, then new ones.
    wanted.sort(key=lambda w: (not w['pinned'], not w['unchanged'], w['item']['id']))
    for candidate in wanted:
        item, pinned = candidate['item'], candidate['pinned']
        slot = find_slot(item, free, settings)
        if slot is None:
            entry = blocker(item, 'locked' if pinned else 'capacity', settings)
            if not pinned:
                rect = largest_free(free)
                entry['largest_free'] = [rect[2], rect[3]]
            blockers.append(entry)
            continue
        _, x, y, aw, ah, rw, rh, rotated = slot
        free = split_free(free, (x, y, aw, ah))
        placements.append(place(item, x + settings['extrude'], y + settings['extrude'], rw, rh, rotated, settings))

    if blockers:
        raise LayoutBlocked(blocker_message(blockers, settings), blockers)
    packed = {placement['id'] for placement in placements}
    missing = [item for item in items if item['id'] not in packed]
    if missing:
        entries = [blocker(item, 'capacity', settings) for item in missing]
        raise LayoutBlocked(blocker_message(entries, settings), entries)
    validate(placements, settings['size'])
    for placement in placements:
        placement['locked'] = placement['id'] in locked
    changes = describe_changes(placements, saved, lookup, locked, removed, (record or {}).get('size'), settings['size'])
    free.sort()
    occupied = sum(p['w'] * p['h'] for p in placements)
    return {
        'size': settings['size'], 'settings': settings, 'sprites': placements,
        'occupancy': occupied / (settings['size'] ** 2), 'free_rectangles': len(free),
        'mode': 'preserve', 'changes': changes,
        'removed': [{'id': sid, 'name': name, 'reason': 'removed'} for sid, name in removed.items()],
        'confirmed': False,
    }


def blocker(item, reason, settings, note=None, conflicts=None):
    needed = required_size(item, settings)
    entry = {'id': item['id'], 'name': item['name'], 'reason': reason,
             'needed': list(needed), 'conflicts': list(conflicts or [])}
    if note:
        entry['note'] = note
    return entry


def blocker_message(blockers, settings):
    parts = []
    for item in blockers:
        width, height = item['needed']
        if item['reason'] == 'capacity':
            largest = item.get('largest_free')
            room = f"，最大剩余矩形{largest[0]}×{largest[1]}" if largest else ''
            parts.append(f"“{item['name']}”需要{width}×{height}{room}，剩余空间放不下")
        elif item['reason'] == 'locked':
            detail = ('，' + item['note']) if item.get('note') else ''
            parts.append(f"锁定项“{item['name']}”需要{width}×{height}，无法保留原位{detail}")
        else:
            parts.append(f"“{item['name']}”的确认位置与其他图块冲突")
    return f"无法在边长{settings['size']}下生成布局：" + '；'.join(parts) + '。未移动锁定项，也未丢弃任何图块；可解除锁定、增大图集、调整间距/外扩/旋转，或选择完全重排。'


def describe_changes(placements, saved, lookup, locked, removed_names, before_size=None, after_size=None):
    """Explain every UV that a confirm step would make visible."""
    result = []
    rescaled = before_size is not None and after_size is not None and before_size != after_size
    for placement in placements:
        item = lookup[placement['id']]
        entry = saved.get(item['id'])
        reasons = []
        if entry is None:
            kind = 'new'
        else:
            same_position = (entry['x'], entry['y']) == (placement['x'], placement['y']) and entry['rotated'] == placement['rotated']
            same_footprint = entry.get('source_size') == item['source_size'] and entry.get('bounds') == item['bounds']
            same_pivot = entry.get('pivot') == item['pivot']
            if not same_position:
                reasons.append('moved')
            if not same_footprint:
                reasons.append('resized')
            if not same_pivot:
                reasons.append('pivot')
            if rescaled:
                reasons.append('atlas_resized')
            if not reasons:
                continue
            kind = 'changed'
        result.append({
            'id': item['id'], 'name': item['name'], 'kind': kind, 'reasons': reasons,
            'locked': item['id'] in locked,
            'before': uv_of(entry), 'after': uv_of(placement),
            'before_uv': corners_of(entry, before_size or after_size), 'after_uv': corners_of(placement, after_size),
        })
    for sid, name in removed_names.items():
        result.append({'id': sid, 'name': name, 'kind': 'removed', 'reasons': ['removed'], 'locked': sid in locked, 'before': uv_of(saved.get(sid)), 'after': None, 'before_uv': corners_of(saved.get(sid), before_size), 'after_uv': None})
    return result


def corners_of(placement, size):
    if placement is None or not size:
        return None
    x, y, w, h = (placement[k] for k in ('x', 'y', 'w', 'h'))
    corners = [[x / size, y / size], [(x + w) / size, y / size], [(x + w) / size, (y + h) / size], [x / size, (y + h) / size]]
    if placement.get('rotated'):
        corners = corners[1:] + corners[:1]
    return [[round(u, 6), round(v, 6)] for u, v in corners]


def uv_of(placement):
    if placement is None:
        return None
    return {'x': placement['x'], 'y': placement['y'], 'w': placement['w'], 'h': placement['h'], 'rotated': placement['rotated'], 'source_size': placement.get('source_size'), 'bounds': placement.get('bounds'), 'pivot': placement.get('pivot')}


def metadata_of(layout):
    return [sprite_metadata(placement, layout['size']) for placement in layout['sprites']]



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
    return {'id': placement['id'], 'name': placement['name'], 'frame': {'x': x, 'y': y, 'w': w, 'h': h}, 'rotated': placement['rotated'], 'source_size': [source_w, source_h], 'trim': [left, top, right - left, bottom - top], 'pivot': placement['pivot'], 'trimmed_pivot_pixels': [px, py], 'uv_corners': corners, 'empty': placement['empty']}


def reconstruct(atlas, placement):
    x, y, w, h = (placement[k] for k in ('x', 'y', 'w', 'h'))
    crop = atlas.crop((x, y, x + w, y + h))
    if placement['rotated']:
        crop = crop.transpose(Image.Transpose.ROTATE_90)
    out = Image.new('RGBA', tuple(placement['source_size']))
    out.paste(crop, tuple(placement['bounds'][:2]))
    return out


def signature(items, settings):
    payload = json.dumps({'settings': settings, 'items': [[item['id'], item.get('asset'), item['source_size'], item['bounds'], item.get('pivot')] for item in sorted(items, key=lambda i: i['id'])]}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def record_of(layout, revision, items):
    """A compact snapshot safe to persist and reuse on later runs."""
    return {
        'format': 'tileatlas-layout-1', 'revision': revision,
        'size': layout['size'], 'settings': layout['settings'],
        'signature': signature(items, layout['settings']),
        'placements': [{
            'id': item['id'], 'name': item['name'], 'asset': item.get('asset'),
            'x': item['x'], 'y': item['y'], 'w': item['w'], 'h': item['h'],
            'rotated': item['rotated'], 'allocation': item['allocation'],
            'bounds': item['bounds'],
            'source_size': item['source_size'], 'pivot': item['pivot'],
        } for item in layout['sprites']],
    }
