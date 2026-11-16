import sys
import random
from PIL import Image, ImageDraw
from image_lab.validation import Invalid

SIDES = ['top', 'right', 'bottom', 'left']
SIDE_NAMES = ['上', '右', '下', '左']
# (dx, dy, 自身边, 邻居对应边)
DIRS = [(0, -1, 0, 2), (1, 0, 1, 3), (0, 1, 2, 0), (-1, 0, 3, 1)]
TURN = {1: Image.Transpose.ROTATE_270, 2: Image.Transpose.ROTATE_180, 3: Image.Transpose.ROTATE_90}
MAX_MAP = 32


def default_tile():
    return {'enabled': False, 'edges': {side: '' for side in SIDES}, 'rotations': [0]}


def normalize_tile(data):
    if data is None:
        return default_tile()
    if not isinstance(data, dict):
        raise Invalid('瓦片规则需要对象')
    edges = data.get('edges') or {}
    if not isinstance(edges, dict):
        raise Invalid('瓦片边标签需要对象')
    clean = {side: str(edges.get(side, '')).strip()[:24] for side in SIDES}
    rotations = data.get('rotations', [0])
    if not isinstance(rotations, list):
        raise Invalid('允许旋转需要数组')
    try:
        allowed = sorted({int(r) % 4 for r in rotations})
    except (TypeError, ValueError):
        raise Invalid('旋转角度须为0–3')
    if not allowed:
        raise Invalid('瓦片至少允许一种旋转')
    return {'enabled': bool(data.get('enabled')), 'edges': clean, 'rotations': allowed}


def tile_of(sprite):
    tile = sprite.get('tile')
    if not isinstance(tile, dict) or not isinstance(tile.get('edges'), dict):
        return default_tile()
    return tile


def rotate_edges(edges, turns):
    # 瓦片顺时针旋转turns·90°，四边标签随图像一起转动
    e = list(edges)
    for _ in range(turns % 4):
        e = [e[3], e[0], e[1], e[2]]
    return e


def tile_variants(sprites):
    variants = []
    for s in sprites:
        tile = tile_of(s)
        if not tile.get('enabled'):
            continue
        base = [tile['edges'][side] for side in SIDES]
        for rot in tile.get('rotations', [0]):
            rot = int(rot) % 4
            variants.append({'sprite': s['id'], 'name': s['name'], 'rotation': rot, 'edges': rotate_edges(base, rot)})
    return variants


def map_options(data, sprites):
    try:
        w, h = int(data.get('width', 8)), int(data.get('height', 8))
    except (TypeError, ValueError):
        raise Invalid('地图宽高需要整数')
    if not 1 <= w <= MAX_MAP or not 1 <= h <= MAX_MAP:
        raise Invalid(f'地图宽高须为1–{MAX_MAP}格')
    try:
        seed = int(data.get('seed', 0))
    except (TypeError, ValueError):
        raise Invalid('随机种子需要整数')
    if not 0 <= seed <= 2147483647:
        raise Invalid('随机种子须为0–2147483647')
    by_id = {s['id']: s for s in sprites}
    raw_pins = data.get('pinned') or {}
    if not isinstance(raw_pins, dict):
        raise Invalid('固定格子需要对象')
    pinned = {}
    for key, val in raw_pins.items():
        try:
            xs, ys = str(key).split(',')
            x, y = int(xs), int(ys)
        except (TypeError, ValueError):
            raise Invalid('固定坐标格式应为"x,y"')
        if not (0 <= x < w and 0 <= y < h):
            raise Invalid(f'固定格子({x},{y})超出地图范围')
        if not isinstance(val, dict):
            raise Invalid('固定内容需要对象')
        sid = str(val.get('sprite', ''))
        sprite = by_id.get(sid)
        if sprite is None:
            raise Invalid(f'格子({x},{y})固定的精灵不存在')
        tile = tile_of(sprite)
        if not tile.get('enabled'):
            raise Invalid(f'格子({x},{y})固定的「{sprite["name"]}」未启用瓦片规则')
        try:
            rot = int(val.get('rotation', 0)) % 4
        except (TypeError, ValueError):
            raise Invalid('固定朝向须为0–3')
        if rot not in tile.get('rotations', [0]):
            raise Invalid(f'格子({x},{y})固定的旋转{rot * 90}°不在「{sprite["name"]}」允许范围内')
        pinned[f'{x},{y}'] = {'sprite': sid, 'rotation': rot}
    return {'width': w, 'height': h, 'seed': seed, 'wrap': bool(data.get('wrap')), 'pinned': pinned}


def solve(cfg, variants):
    if not variants:
        raise Invalid('没有可用瓦片：请先在精灵上启用「地形瓦片」并标注四边标签')
    w, h, wrap = cfg['width'], cfg['height'], cfg['wrap']
    n = w * h
    rng = random.Random(cfg['seed'])
    count = len(variants)
    full = (1 << count) - 1
    # reach[side][v]: 当本格放变体v时，side方向邻居可放的变体位集
    by_label = [{}, {}, {}, {}]
    for i, v in enumerate(variants):
        for side in range(4):
            by_label[side][v['edges'][side]] = by_label[side].get(v['edges'][side], 0) | (1 << i)
    reach = [[by_label[opp].get(v['edges'][side], 0) for v in variants] for side, opp in ((0, 2), (1, 3), (2, 0), (3, 1))]

    def neighbors(index):
        x, y = index % w, index // w
        result = []
        for dx, dy, side, opp in DIRS:
            nx, ny = x + dx, y + dy
            if wrap:
                nx, ny = nx % w, ny % h
            elif not (0 <= nx < w and 0 <= ny < h):
                continue
            j = ny * w + nx
            if j != index:
                result.append((side, opp, j))
        return result

    domains = [full] * n
    if wrap:  # 环绕时宽度或高度为1意味着格子与自身相邻
        for index in range(n):
            x, y = index % w, index // w
            if w == 1:
                domains[index] = sum(1 << i for i, v in enumerate(variants) if v['edges'][1] == v['edges'][3])
                if not domains[index]:
                    raise Invalid(f'无可行组合：格子({x},{y})在首尾环绕下左右边互为邻居，没有左右标签相同的瓦片')
            if h == 1:
                domains[index] &= sum(1 << i for i, v in enumerate(variants) if v['edges'][0] == v['edges'][2])
                if not domains[index]:
                    raise Invalid(f'无可行组合：格子({x},{y})在首尾环绕下上下边互为邻居，没有上下标签相同的瓦片')
    for key, pin in cfg['pinned'].items():
        x, y = (int(v) for v in key.split(','))
        chosen = sum(1 << i for i, v in enumerate(variants) if v['sprite'] == pin['sprite'] and v['rotation'] == pin['rotation'])
        domains[y * w + x] &= chosen

    def propagate(domains, start):
        queue = list(start)
        queued = set(queue)
        while queue:
            j = queue.pop()
            queued.discard(j)
            dj = domains[j]
            if not dj:
                return j
            for side, opp, k in neighbors(j):
                allowed = 0
                m = dj
                while m:
                    lsb = m & -m
                    allowed |= reach[side][lsb.bit_length() - 1]
                    m ^= lsb
                reduced = domains[k] & allowed
                if reduced != domains[k]:
                    domains[k] = reduced
                    if not reduced:
                        return k
                    if k not in queued:
                        queue.append(k)
                        queued.add(k)
        return None

    def explain(domains, index):
        x, y = index % w, index // w
        remaining = full
        steps = []
        for side, opp, j in neighbors(index):
            labels = sorted({variants[i]['edges'][opp] for i in range(count) if domains[j] >> i & 1})
            remaining &= sum(1 << i for i, v in enumerate(variants) if v['edges'][side] in labels)
            text = '、'.join(f'「{label}」' if label else '「空」' for label in labels) or '（无候选）'
            steps.append(f'{SIDE_NAMES[side]}边需匹配{text}，剩{remaining.bit_count()}个候选')
            if not remaining:
                break
        detail = '：' + '，'.join(steps) if steps else ''
        return f'格子({x},{y})没有可行瓦片{detail}'

    bad = propagate(domains, range(n))
    if bad is not None:
        raise Invalid('无可行组合：' + explain(domains, bad))

    conflict = {'cell': None, 'domains': None}
    budget = {'nodes': 20000}
    sys.setrecursionlimit(max(sys.getrecursionlimit(), n + 100))

    def search(domains):
        budget['nodes'] -= 1
        if budget['nodes'] <= 0:
            raise Invalid('求解超出预算：地图过大或约束过紧，请缩小地图、增加固定格子或补充瓦片')
        smallest, ties = count + 1, []
        for i in range(n):
            bits = domains[i].bit_count()
            if bits == 0:
                return None
            if 1 < bits < smallest:
                smallest, ties = bits, [i]
            elif bits == smallest:
                ties.append(i)
        if not ties:
            return domains
        cell = ties[rng.randrange(len(ties))]
        values = [i for i in range(count) if domains[cell] >> i & 1]
        rng.shuffle(values)
        for v in values:
            trial = list(domains)
            trial[cell] = 1 << v
            bad = propagate(trial, [cell])
            if bad is not None:
                if conflict['cell'] is None:
                    conflict['cell'] = bad
                    conflict['domains'] = trial
                continue
            result = search(trial)
            if result is not None:
                return result
        return None

    result = search(domains)
    if result is None:
        detail = explain(conflict['domains'], conflict['cell']) if conflict['cell'] is not None else '瓦片集合本身无法铺满该地图'
        raise Invalid('无可行组合：' + detail + '。可调整固定格子、更换种子、补充瓦片或放宽旋转后重试')
    solution = [(d & -d).bit_length() - 1 for d in result]
    verify(solution, variants, neighbors, w, h, wrap)
    return [[{'sprite': variants[solution[y * w + x]]['sprite'], 'rotation': variants[solution[y * w + x]]['rotation']} for x in range(w)] for y in range(h)]


def verify(result, variants, neighbors, w, h, wrap):
    for index in range(w * h):
        edges = variants[result[index]]['edges']
        for side, opp, j in neighbors(index):
            if j > index and edges[side] != variants[result[j]]['edges'][opp]:
                raise Invalid('内部错误：拼接结果校验失败')
        if wrap and w == 1 and edges[1] != edges[3] or wrap and h == 1 and edges[0] != edges[2]:
            raise Invalid('内部错误：拼接结果校验失败')


def render_map(tilemap, images, grid=False):
    try:
        w, h = int(tilemap['width']), int(tilemap['height'])
        cells = tilemap['cells']
    except (KeyError, TypeError, ValueError):
        raise Invalid('尚未生成拼接地图')
    if not isinstance(cells, list) or len(cells) != h or any(not isinstance(row, list) or len(row) != w for row in cells):
        raise Invalid('地图数据无效，请重新生成')
    placed = {}
    cw = ch = 1
    for row in cells:
        for cell in row:
            key = (str(cell.get('sprite')), int(cell.get('rotation', 0)) % 4)
            if key not in placed:
                image = images.get(key[0])
                if image is None:
                    raise Invalid('地图引用的精灵已被删除，请重新生成')
                if key[1]:
                    image = image.transpose(TURN[key[1]])
                placed[key] = image
            cw, ch = max(cw, placed[key].width), max(ch, placed[key].height)
    if cw * w > 4096 or ch * h > 4096 or cw * w * ch * h > 8000000:
        raise Invalid('地图预览超出预算，请缩小地图或瓦片尺寸')
    out = Image.new('RGBA', (cw * w, ch * h))
    for y, row in enumerate(cells):
        for x, cell in enumerate(row):
            image = placed[(str(cell.get('sprite')), int(cell.get('rotation', 0)) % 4)]
            out.alpha_composite(image, (x * cw + (cw - image.width) // 2, y * ch + (ch - image.height) // 2))
    if grid:
        draw = ImageDraw.Draw(out, 'RGBA')
        for x in range(1, w):
            draw.line([(x * cw, 0), (x * cw, out.height)], fill=(16, 26, 34, 170))
        for y in range(1, h):
            draw.line([(0, y * ch), (out.width, y * ch)], fill=(16, 26, 34, 170))
    return out


def validate_stored_map(data, sprites):
    if data is None:
        return None
    if not isinstance(data, dict):
        raise Invalid('项目包地图数据无效')
    result = map_options(data, sprites)
    cells = data.get('cells')
    result['cells'] = None
    if cells is not None:
        by_id = {s['id'] for s in sprites}
        if not isinstance(cells, list) or len(cells) != result['height']:
            raise Invalid('项目包地图格子无效')
        clean = []
        for row in cells:
            if not isinstance(row, list) or len(row) != result['width']:
                raise Invalid('项目包地图格子无效')
            out_row = []
            for cell in row:
                if not isinstance(cell, dict) or str(cell.get('sprite')) not in by_id:
                    raise Invalid('项目包地图格子无效')
                try:
                    rot = int(cell.get('rotation', 0)) % 4
                except (TypeError, ValueError):
                    raise Invalid('项目包地图格子无效')
                out_row.append({'sprite': str(cell.get('sprite')), 'rotation': rot})
            clean.append(out_row)
        result['cells'] = clean
    return result
