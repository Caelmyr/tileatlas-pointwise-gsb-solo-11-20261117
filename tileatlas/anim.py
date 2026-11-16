from PIL import ImageDraw
from image_lab.validation import Invalid
from .raster import trim_bounds, checker

MAX_CLIPS = 32
MAX_CELLS = 256
MAX_FRAMES = 512
MAX_ITEMS = 640


def ints(data, key, fields, default, low, high, label):
    value = data.get(key)
    if value is None:
        value = default
    if not isinstance(value, dict):
        raise Invalid(label + '需要对象')
    result = {}
    for field in fields:
        number = value.get(field, default[field])
        if isinstance(number, bool) or not isinstance(number, (int, float)) or int(number) != number:
            raise Invalid(label + '需要整数')
        number = int(number)
        if not low <= number <= high:
            raise Invalid(f'{label}须在{low}–{high}之间')
        result[field] = number
    return result


def geometry(size, frame, margin, spacing):
    width, height = size
    fw, fh = frame['w'], frame['h']
    mx, my = margin['x'], margin['y']
    sx, sy = spacing['x'], spacing['y']
    if mx * 2 + fw > width or my * 2 + fh > height:
        raise Invalid(f'帧{fw}×{fh}加外边距超出源图{width}×{height}范围')
    columns, rem_w = divmod(width - 2 * mx + sx, fw + sx)
    rows, rem_h = divmod(height - 2 * my + sy, fh + sy)
    if rem_w or rem_h:
        parts = []
        if rem_w:
            parts.append(f'横向每列{fw}px排{columns}列后余{rem_w}px')
        if rem_h:
            parts.append(f'纵向每行{fh}px排{rows}行后余{rem_h}px')
        raise Invalid('帧尺寸与源图对不上（' + '；'.join(parts) + '），请调整帧尺寸、外边距或格间距')
    cells = columns * rows
    if cells > MAX_CELLS:
        raise Invalid(f'将切出{cells}帧，单个片段最多{MAX_CELLS}帧')
    return {'columns': columns, 'rows': rows, 'cells': cells}


def cell_position(index, geo, order):
    if order == 'column':
        return index // geo['rows'], index % geo['rows']
    return index % geo['columns'], index // geo['columns']


def normalize_frames(frames, cells, excluded):
    if not isinstance(frames, list) or not 1 <= len(frames) <= MAX_FRAMES:
        raise Invalid(f'帧序列须为1–{MAX_FRAMES}项')
    result = []
    for entry in frames:
        if not isinstance(entry, dict):
            raise Invalid('帧序列项需要对象')
        cell, ms = entry.get('cell'), entry.get('ms')
        if isinstance(cell, bool) or not isinstance(cell, (int, float)) or int(cell) != cell or not 0 <= int(cell) < cells:
            raise Invalid(f'帧编号须为0–{cells - 1}的整数')
        if isinstance(ms, bool) or not isinstance(ms, (int, float)) or not 1 <= int(ms) <= 60000:
            raise Invalid('帧时长须为1–60000毫秒')
        cell = int(cell)
        if cell in excluded:
            raise Invalid(f'帧{cell}已被排除，不能加入序列')
        result.append({'cell': cell, 'ms': int(ms)})
    return result


def normalize_clip(data, sheet_size, check_frames=True):
    if not isinstance(data, dict):
        raise Invalid('片段参数需要对象')
    frame = ints(data, 'frame', ('w', 'h'), {'w': 0, 'h': 0}, 1, 4096, '帧尺寸')
    margin = ints(data, 'margin', ('x', 'y'), {'x': 0, 'y': 0}, 0, 2048, '外边距')
    spacing = ints(data, 'spacing', ('x', 'y'), {'x': 0, 'y': 0}, 0, 1024, '格间距')
    order = data.get('order', 'row')
    if order not in ('row', 'column'):
        raise Invalid('读取方向须为row或column')
    geo = geometry(sheet_size, frame, margin, spacing)
    pivot = data.get('pivot', [.5, .5])
    if not isinstance(pivot, list) or len(pivot) != 2 or any(isinstance(n, bool) or not isinstance(n, (int, float)) or not -2 <= n <= 3 for n in pivot):
        raise Invalid('轴心为两个-2至3的坐标')
    threshold = data.get('threshold', 0)
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 <= int(threshold) <= 254:
        raise Invalid('透明阈值须为0–254')
    raw_excluded = data.get('excluded', [])
    if not isinstance(raw_excluded, list):
        raise Invalid('排除帧需要数组')
    excluded = set()
    for item in raw_excluded:
        if isinstance(item, bool) or not isinstance(item, (int, float)) or int(item) != item:
            raise Invalid('排除帧编号须为整数')
        excluded.add(int(item))
    if excluded and (min(excluded) < 0 or max(excluded) >= geo['cells']):
        raise Invalid(f'排除帧编号须在0–{geo["cells"] - 1}之间')
    frames = data.get('frames')
    if frames is None or not check_frames:
        frames = [{'cell': i, 'ms': 100} for i in range(geo['cells']) if i not in excluded]
    else:
        frames = normalize_frames(frames, geo['cells'], excluded)
    if check_frames and not frames:
        raise Invalid('片段至少保留一帧')
    return {'name': str(data.get('name', '')).strip()[:100] or '未命名片段', 'source': str(data.get('source', '')), 'frame': frame, 'margin': margin, 'spacing': spacing, 'order': order, 'pivot': [float(pivot[0]), float(pivot[1])], 'trim': bool(data.get('trim', True)), 'threshold': int(threshold), 'excluded': sorted(excluded), 'frames': frames}


def slice_items(clip, sheet):
    geo = geometry(sheet.size, clip['frame'], clip['margin'], clip['spacing'])
    fw, fh = clip['frame']['w'], clip['frame']['h']
    mx, my = clip['margin']['x'], clip['margin']['y']
    sx, sy = clip['spacing']['x'], clip['spacing']['y']
    items = []
    for cell in sorted({entry['cell'] for entry in clip['frames']}):
        col, row = cell_position(cell, geo, clip['order'])
        x, y = mx + col * (fw + sx), my + row * (fh + sy)
        region = sheet.crop((x, y, x + fw, y + fh))
        bounds, empty = trim_bounds(region, clip['threshold'])
        if not clip['trim']:
            bounds = [0, 0, fw, fh]
        items.append({'id': f"{clip['id']}.{cell}", 'name': f"{clip['name']} · {cell}", 'image': region.crop(bounds), 'bounds': bounds, 'source_size': [fw, fh], 'pivot': clip['pivot'], 'empty': empty})
    return items


def clip_metadata(clip):
    frames = [{'sprite': f"{clip['id']}.{entry['cell']}", 'cell': entry['cell'], 'duration_ms': entry['ms']} for entry in clip['frames']]
    return {'id': clip['id'], 'name': clip['name'], 'source': clip['source'], 'frame_size': [clip['frame']['w'], clip['frame']['h']], 'pivot': clip['pivot'], 'frames': frames, 'total_duration_ms': sum(entry['ms'] for entry in clip['frames'])}


def preview(sheet, clip):
    geo = geometry(sheet.size, clip['frame'], clip['margin'], clip['spacing'])
    fw, fh = clip['frame']['w'], clip['frame']['h']
    mx, my = clip['margin']['x'], clip['margin']['y']
    sx, sy = clip['spacing']['x'], clip['spacing']['y']
    out = checker(sheet)
    draw = ImageDraw.Draw(out, 'RGBA')
    excluded = set(clip['excluded'])
    for index in range(geo['cells']):
        col, row = cell_position(index, geo, clip['order'])
        x, y = mx + col * (fw + sx), my + row * (fh + sy)
        color = (255, 107, 107, 255) if index in excluded else (127, 224, 160, 255)
        if index in excluded:
            draw.rectangle((x, y, x + fw - 1, y + fh - 1), fill=(255, 80, 80, 70))
        draw.rectangle((x, y, x + fw - 1, y + fh - 1), outline=color, width=2 if min(fw, fh) >= 8 else 1)
        if fw >= 26 and fh >= 14:
            draw.text((x + 4, y + 3), str(index), fill=color)
    return out
