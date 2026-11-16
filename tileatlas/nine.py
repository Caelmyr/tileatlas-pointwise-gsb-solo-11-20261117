from PIL import Image
from image_lab.validation import Invalid


def defaults(size):
    w, h = size
    return {'enabled': False, 'left': round(w / 3), 'right': round(w * 2 / 3), 'top': round(h / 3), 'bottom': round(h * 2 / 3), 'center': 'stretch', 'target': [w, h]}


def validate(nine, size):
    if not isinstance(nine, dict):
        raise Invalid('九宫格设置必须是对象')
    w, h = size

    def cut(name):
        v = nine.get(name, 0)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) != v:
            raise Invalid('九宫格切线须为整数像素')
        return int(v)

    left, right, top, bottom = cut('left'), cut('right'), cut('top'), cut('bottom')
    if not 0 <= left <= right <= w or not 0 <= top <= bottom <= h:
        raise Invalid(f'九宫格切线须在宽0–{w}、高0–{h}内，且左≤右、上≤下')
    center = nine.get('center', 'stretch')
    if center not in ('stretch', 'tile'):
        raise Invalid('九宫格中心模式须为stretch(拉伸)或tile(平铺)')
    target = nine.get('target', [w, h])
    if not isinstance(target, list) or len(target) != 2 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) != v for v in target):
        raise Invalid('九宫格目标尺寸须为两个整数')
    tw, th = int(target[0]), int(target[1])
    if not 1 <= tw <= 4096 or not 1 <= th <= 4096 or tw * th > 8000000:
        raise Invalid('九宫格目标尺寸须在4096边长及800万像素内')
    return {'enabled': bool(nine.get('enabled', False)), 'left': left, 'right': right, 'top': top, 'bottom': bottom, 'center': center, 'target': [tw, th]}


def merged(stored, args, size):
    base = defaults(size)
    if isinstance(stored, dict):
        base.update({k: v for k, v in stored.items() if k in base})
    base['enabled'] = True

    def num(name):
        v = args.get(name)
        if v is None or v == '':
            return base.get(name)
        try:
            return int(v)
        except (TypeError, ValueError):
            raise Invalid('九宫格参数须为整数') from None

    candidate = {'enabled': True, 'left': num('left'), 'right': num('right'), 'top': num('top'), 'bottom': num('bottom'), 'center': args.get('center', base.get('center', 'stretch')), 'target': [num('w') if args.get('w') not in (None, '') else base['target'][0], num('h') if args.get('h') not in (None, '') else base['target'][1]]}
    return validate(candidate, size)


def source_cuts(nine, settings, raw_size):
    """把原画布切线经旋转、镜像、缩放映射到变换后源画布，返回(x1, x2, y1, y2)。"""
    w, h = raw_size
    x1, x2, y1, y2 = (float(nine[k]) for k in ('left', 'right', 'top', 'bottom'))
    turn = int(settings.get('turn', 0)) % 4
    if turn == 1:
        x1, x2, y1, y2 = h - y2, h - y1, x1, x2
        w, h = h, w
    elif turn == 2:
        x1, x2, y1, y2 = w - x2, w - x1, h - y2, h - y1
    elif turn == 3:
        x1, x2, y1, y2 = y1, y2, w - x2, w - x1
        w, h = h, w
    if settings.get('flip_x'):
        x1, x2 = w - x2, w - x1
    if settings.get('flip_y'):
        y1, y2 = h - y2, h - y1
    scale = float(settings.get('scale', 1))
    sw, sh = max(1, round(w * scale)), max(1, round(h * scale))

    def clip(v, limit):
        return min(max(int(round(v * scale)), 0), limit)

    return clip(x1, sw), clip(x2, sw), clip(y1, sh), clip(y2, sh)


def frame_info(nine, settings, raw_size, prepared):
    """换算到裁边后打包帧：帧内切线、源画布切线、四边宽度与最小目标尺寸。"""
    x1, x2, y1, y2 = source_cuts(nine, settings, raw_size)
    left, top = prepared['bounds'][:2]
    fw, fh = prepared['image'].size
    fx1, fx2 = min(max(x1 - left, 0), fw), min(max(x2 - left, 0), fw)
    fy1, fy2 = min(max(y1 - top, 0), fh), min(max(y2 - top, 0), fh)
    border = [fx1, fy1, fw - fx2, fh - fy2]
    return {'cuts_frame': [fx1, fy1, fx2, fy2], 'cuts_source': [x1, y1, x2, y2], 'border': border, 'center': nine['center'], 'target': list(nine['target']), 'min_size': [border[0] + border[2], border[1] + border[3]]}


def render(frame, info, target, pixel_art=True):
    """按目标尺寸渲染九宫格：四角固定、边条单向拉伸、中心拉伸或平铺。不修改frame。"""
    tw, th = target
    fw, fh = frame.size
    x1, y1, x2, y2 = info['cuts_frame']
    mw, mh = info['min_size']
    if tw < mw or th < mh:
        raise Invalid(f'目标{tw}×{th}容纳不下九宫格固定边，最小需要{mw}×{mh}')
    resample = Image.Resampling.NEAREST if pixel_art else Image.Resampling.LANCZOS
    cw, ch = tw - mw, th - mh
    out = Image.new('RGBA', (tw, th))

    def fill(box, pos, size=None, tile=False):
        region = frame.crop(box)
        w0, h0 = region.size
        dw, dh = size or region.size
        if dw <= 0 or dh <= 0:
            return
        if w0 <= 0 or h0 <= 0:
            raise Invalid('九宫格源区域为空，无法填充目标区域，请调整切线')
        if tile:
            for yy in range(pos[1], pos[1] + dh, h0):
                for xx in range(pos[0], pos[0] + dw, w0):
                    out.paste(region, (xx, yy))
        elif (w0, h0) != (dw, dh):
            out.paste(region.resize((dw, dh), resample), pos)
        else:
            out.paste(region, pos)

    rw, bh = fw - x2, fh - y2
    fill((0, 0, x1, y1), (0, 0))
    fill((x2, 0, fw, y1), (tw - rw, 0))
    fill((0, y2, x1, fh), (0, th - bh))
    fill((x2, y2, fw, fh), (tw - rw, th - bh))
    fill((x1, 0, x2, y1), (x1, 0), (cw, y1))
    fill((x1, y2, x2, fh), (x1, th - bh), (cw, bh))
    fill((0, y1, x1, y2), (0, y1), (x1, ch))
    fill((x2, y1, fw, y2), (tw - rw, y1), (rw, ch))
    fill((x1, y1, x2, y2), (x1, y1), (cw, ch), tile=info['center'] == 'tile')
    return out
