"""Silhouette triangle meshes for transparent sprites.

The mesh is derived from the alpha channel of the processed sprite (after
turn/flip/scale/trim).  Boundary loops are traced at pixel-corner precision, so
an unsimplified mesh covers exactly the pixels whose alpha passes the mesh
threshold.  Holes and isolated islands stay separate loops: triangles are only
kept inside the even-odd polygon interior, so no face bridges a transparent
hole or joins two islands.
"""

import math

import cv2
import numpy as np
from PIL import Image
from scipy.spatial import Delaunay, QhullError, cKDTree

from image_lab.validation import Invalid, boolean, integer, number
from .raster import checker

MAX_BOUNDARY_EDGES = 150000
MAX_LOOPS = 4096
MAX_POINTS = 40000
MAX_TRIANGLES = 80000
MAX_ROUNDS = 12
MAX_SIMPLIFY = 8.0
MAX_PREVIEW = 1024
CROSS_BUDGET = 4000000
DIRECTIONS = {(1, 0): 0, (0, 1): 1, (-1, 0): 2, (0, -1): 3}


def defaults():
    return {'enabled': False, 'threshold': 0, 'simplify': 0.0}


def normalize(data):
    if data is None:
        return defaults()
    if not isinstance(data, dict):
        raise Invalid('网格参数需要对象')
    result = defaults()
    if 'enabled' in data:
        result['enabled'] = boolean(data['enabled'], '网格开关')
    if 'threshold' in data:
        result['threshold'] = integer(data['threshold'], '网格阈值', 0, 254)
    if 'simplify' in data:
        result['simplify'] = float(number(data['simplify'], '简化程度', 0, MAX_SIMPLIFY))
    return result


def _edges(mask):
    padded = np.pad(mask.astype(np.uint8), 1)
    ys, xs = np.nonzero(mask)
    if not len(xs):
        return np.zeros((0, 2, 2), dtype=np.int64)
    north = padded[ys, xs + 1] == 0
    south = padded[ys + 2, xs + 1] == 0
    west = padded[ys + 1, xs] == 0
    east = padded[ys + 1, xs + 2] == 0
    if int(north.sum() + south.sum() + west.sum() + east.sum()) > MAX_BOUNDARY_EDGES:
        raise Invalid('透明轮廓过于复杂，请提高简化程度或调整阈值')
    parts = []
    for chosen, start, end in (
        (north, (xs, ys), (xs + 1, ys)),
        (south, (xs + 1, ys + 1), (xs, ys + 1)),
        (west, (xs, ys + 1), (xs, ys)),
        (east, (xs + 1, ys), (xs + 1, ys + 1)),
    ):
        if chosen.any():
            parts.append(np.stack([np.stack([start[0][chosen], start[1][chosen]], axis=1), np.stack([end[0][chosen], end[1][chosen]], axis=1)], axis=1))
    return np.concatenate(parts).astype(np.int64) if parts else np.zeros((0, 2, 2), dtype=np.int64)


def _trace(edges):
    if not len(edges):
        return []
    pairs = sorted(zip(map(tuple, edges[:, 0].tolist()), map(tuple, edges[:, 1].tolist())))
    outgoing = {}
    for a, b in pairs:
        outgoing.setdefault(a, []).append(b)
    unused = set(pairs)
    cycles = []
    for first, second in pairs:
        if (first, second) not in unused:
            continue
        start, current, following, cycle = first, first, second, [first]
        unused.discard((first, second))
        while following != start:
            cycle.append(following)
            candidates = [c for c in outgoing.get(following, ()) if (following, c) in unused]
            if not candidates:
                raise Invalid('透明轮廓无法闭合，请调整阈值或提高简化程度')
            dx, dy = following[0] - current[0], following[1] - current[1]
            chosen = max(candidates, key=lambda c: (DIRECTIONS[(c[0] - following[0], c[1] - following[1])] - DIRECTIONS[(dx, dy)]) % 4)
            unused.discard((following, chosen))
            current, following = following, chosen
        if len(cycle) >= 3:
            cycles.append(cycle)
    return cycles


def _split(cycle):
    """Split a boundary walk that revisits a corner (pinch point) into simple loops."""
    result, stack, index = [], [], {}
    for point in cycle:
        if point in index:
            cut = index[point]
            part = stack[cut:]
            if len(part) >= 3:
                result.append(part)
            for old in stack[cut:]:
                index.pop(old, None)
            stack = stack[:cut]
        index[point] = len(stack)
        stack.append(point)
    if len(stack) >= 3:
        result.append(stack)
    return result


def _straighten(cycle):
    points = []
    for point in cycle:
        if not points or points[-1] != point:
            points.append(point)
    if len(points) > 1 and points[0] == points[-1]:
        points.pop()
    while len(points) > 3:
        kept, changed, count = [], False, len(points)
        for i in range(count):
            ax, ay = points[i - 1]
            bx, by = points[i]
            cx, cy = points[(i + 1) % count]
            if (bx - ax) * (cy - by) == (by - ay) * (cx - bx):
                changed = True
            else:
                kept.append(points[i])
        if not changed:
            break
        points = kept
    return points


def _area(points):
    array = np.asarray(points, dtype=float)
    return float(0.5 * np.sum(array[:, 0] * np.roll(array[:, 1], -1) - np.roll(array[:, 0], -1) * array[:, 1]))


def _simple(points):
    array = np.asarray(points, dtype=float)
    count = len(array)
    if count < 3:
        return False
    following = np.roll(array, -1, axis=0)
    for i in range(count):
        j = np.arange(i + 2, count - (1 if i == 0 else 0))
        if not len(j):
            continue
        a, b = array[i], following[i]
        c, d = array[j], following[j]
        o1 = (b[0] - a[0]) * (c[:, 1] - a[1]) - (b[1] - a[1]) * (c[:, 0] - a[0])
        o2 = (b[0] - a[0]) * (d[:, 1] - a[1]) - (b[1] - a[1]) * (d[:, 0] - a[0])
        o3 = (d[:, 0] - c[:, 0]) * (a[1] - c[:, 1]) - (d[:, 1] - c[:, 1]) * (a[0] - c[:, 0])
        o4 = (d[:, 0] - c[:, 0]) * (b[1] - c[:, 1]) - (d[:, 1] - c[:, 1]) * (b[0] - c[:, 0])
        if (((o1 > 0) != (o2 > 0)) & ((o3 > 0) != (o4 > 0))).any():
            return False
    return True


def _crossing(cycles, tolerance=0.0):
    """Detect loops that cut through each other.

    Simplification can only create a crossing between two loops when their
    points come within twice the tolerance, so a KD-tree prunes the pair list.
    """
    if len(cycles) < 2:
        return False
    labels, coordinates = [], []
    for index, cycle in enumerate(cycles):
        coordinates.extend(cycle)
        labels.extend([index] * len(cycle))
    labels = np.asarray(labels, dtype=np.int64)
    points = np.asarray(coordinates, dtype=float)
    pairs = set()
    if tolerance > 0:
        for first, second in cKDTree(points).query_pairs(2 * tolerance + 1e-6, output_type='ndarray'):
            if labels[first] != labels[second]:
                pairs.add((int(min(labels[first], labels[second])), int(max(labels[first], labels[second]))))
    else:
        pairs = {(i, j) for i in range(len(cycles)) for j in range(i + 1, len(cycles))}
    if not pairs:
        return False
    array = [np.asarray(cycle, dtype=float) for cycle in cycles]
    following = [np.roll(item, -1, axis=0) for item in array]
    boxes = [np.array([np.min(item, axis=0), np.max(item, axis=0)]) for item in array]
    budget = CROSS_BUDGET
    for i, j in pairs:
        if boxes[i][0, 0] >= boxes[j][1, 0] or boxes[j][0, 0] >= boxes[i][1, 0] or boxes[i][0, 1] >= boxes[j][1, 1] or boxes[j][0, 1] >= boxes[i][1, 1]:
            continue
        budget -= len(array[i]) * len(array[j])
        if budget < 0:
            return True
        a, an = array[i], following[i]
        b, bn = array[j], following[j]
        o1 = (an[:, None, 0] - a[:, None, 0]) * (b[None, :, 1] - a[:, None, 1]) - (an[:, None, 1] - a[:, None, 1]) * (b[None, :, 0] - a[:, None, 0])
        o2 = (an[:, None, 0] - a[:, None, 0]) * (bn[None, :, 1] - a[:, None, 1]) - (an[:, None, 1] - a[:, None, 1]) * (bn[None, :, 0] - a[:, None, 0])
        o3 = (bn[None, :, 0] - b[None, :, 0]) * (a[:, None, 1] - b[None, :, 1]) - (bn[None, :, 1] - b[None, :, 1]) * (a[:, None, 0] - b[None, :, 0])
        o4 = (bn[None, :, 0] - b[None, :, 0]) * (an[:, None, 1] - b[None, :, 1]) - (bn[None, :, 1] - b[None, :, 1]) * (an[:, None, 0] - b[None, :, 0])
        # strict signs: loops may share a corner or run along the same edge without crossing
        if ((o1 * o2 < 0) & (o3 * o4 < 0)).any():
            return True
    return False


def _simplify(cycle, tolerance):
    if tolerance <= 0 or len(cycle) < 5:
        return cycle
    array = np.asarray(cycle, dtype=np.float32).reshape(-1, 1, 2)
    width = float(np.ptp(array[:, 0, 0]))
    height = float(np.ptp(array[:, 0, 1]))
    original_area = abs(_area(cycle))
    if min(width, height) <= 2 * tolerance + 1 or original_area <= 4 * tolerance * tolerance + 1:
        return cycle
    reduced = cv2.approxPolyDP(array, float(tolerance), True).reshape(-1, 2)
    if len(reduced) < 3:
        return cycle
    area = abs(_area(reduced))
    if area < 0.25 * original_area or area > 4 * original_area:
        return cycle
    points = [tuple(float(v) for v in point) for point in reduced]
    if len(points) > 3000 or not _simple(points):
        return cycle
    return points


def _covered(a, b, links, points, tree):
    if (a, b) in links:
        return True
    ax, ay = a
    vx, vy = b[0] - ax, b[1] - ay
    length = vx * vx + vy * vy
    if length == 0:
        return True
    tolerance = 1e-7 * math.sqrt(length) + 1e-9
    candidates = tree.query_ball_point(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2), math.sqrt(length) / 2 + tolerance)
    if not candidates:
        return False
    relative = points[candidates] - np.asarray((ax, ay))
    t = (relative[:, 0] * vx + relative[:, 1] * vy) / length
    cross = relative[:, 0] * vy - relative[:, 1] * vx
    on = (np.abs(cross) <= tolerance) & (t > 1e-9) & (t < 1 - 1e-9)
    indices = np.asarray(candidates)[on]
    order = indices[np.argsort(t[on])]
    chain = [a] + [tuple(points[i]) for i in order] + [b]
    return all((chain[i], chain[i + 1]) in links for i in range(len(chain) - 1))


def _links(triangulation, points):
    result = set()
    for simplex in triangulation.simplices:
        for index in range(3):
            a = tuple(points[simplex[index]])
            b = tuple(points[simplex[(index + 1) % 3]])
            result.add((a, b))
            result.add((b, a))
    return result


def _triangulate(cycles):
    points, segments = [], set()
    for cycle in cycles:
        points.extend(cycle)
        count = len(cycle)
        for index in range(count):
            segments.add((tuple(cycle[index]), tuple(cycle[(index + 1) % count])))
    points = np.unique(np.asarray(points, dtype=float), axis=0)
    if len(points) < 3:
        return points, np.zeros((0, 3), dtype=np.int64)
    for _ in range(MAX_ROUNDS + 1):
        if len(points) > MAX_POINTS:
            raise Invalid('轮廓顶点过多，请提高简化程度')
        try:
            triangulation = Delaunay(points)
        except (QhullError, ValueError) as error:
            raise Invalid('轮廓退化，无法生成三角网格，请调整简化或阈值') from error
        links = _links(triangulation, points)
        tree = cKDTree(points)
        missing = [(a, b) for a, b in segments if not _covered(a, b, links, points, tree)]
        if not missing:
            return points, triangulation.simplices
        additions, split = set(), set()
        for a, b in segments:
            if (a, b) in links:
                split.add((a, b))
                continue
            middle = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            additions.add(middle)
            split.add((a, middle))
            split.add((middle, b))
        if not additions:
            break
        points = np.unique(np.vstack([points, np.asarray(sorted(additions), dtype=float)]), axis=0)
        segments = split
    raise Invalid('轮廓约束无法稳定三角化，请提高简化程度')


def _inside(points, cycles, chunk=4096):
    """Even-odd point-in-polygon test, binned by scanline so dense silhouettes stay cheap."""
    prepared = []
    for cycle in cycles:
        array = np.asarray(cycle, dtype=float)
        ax, ay = array[:, 0], array[:, 1]
        bx, by = np.roll(ax, -1), np.roll(ay, -1)
        vertical = ay != by
        if vertical.any():
            prepared.append((ax[vertical], ay[vertical], bx[vertical], by[vertical]))
    if not prepared:
        return np.zeros(len(points), dtype=bool)
    ax = np.concatenate([item[0] for item in prepared])
    ay = np.concatenate([item[1] for item in prepared])
    bx = np.concatenate([item[2] for item in prepared])
    by = np.concatenate([item[3] for item in prepared])
    low = np.minimum(ay, by)
    high = np.maximum(ay, by)
    inside = np.zeros(len(points), dtype=bool)
    rows = np.floor(points[:, 1]).astype(np.int64)
    for row in np.unique(rows):
        selected = np.nonzero(rows == row)[0]
        active = (low < row + 1) & (high > row)
        if not active.any():
            continue
        ex, ey, fx, fy = ax[active], ay[active], bx[active], by[active]
        for start in range(0, len(selected), chunk):
            part = selected[start:start + chunk]
            x = points[part, 0]
            y = points[part, 1]
            counts = np.zeros(len(part), dtype=np.int64)
            for begin in range(0, len(ex), chunk):
                sx = ex[begin:begin + chunk]
                sy = ey[begin:begin + chunk]
                tx = fx[begin:begin + chunk]
                ty = fy[begin:begin + chunk]
                crossing = (sy[None, :] > y[:, None]) != (ty[None, :] > y[:, None])
                if not crossing.any():
                    continue
                x_intersection = sx[None, :] + (y[:, None] - sy[None, :]) * (tx - sx)[None, :] / (ty - sy)[None, :]
                counts += (crossing & (x[:, None] < x_intersection)).sum(axis=1)
            inside[part] = counts % 2 == 1
    return inside


def _orient(points, triangles):
    if not len(triangles):
        return triangles
    array = np.asarray(points, dtype=float)
    a = array[triangles[:, 0]]
    b = array[triangles[:, 1]]
    c = array[triangles[:, 2]]
    area = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
    result = triangles[~(np.abs(area) < 1e-9)]
    result = result.copy()
    flip = area[~(np.abs(area) < 1e-9)] < 0
    result[flip] = result[flip][:, [0, 2, 1]]
    return result


def rasterize(points, triangles, shape):
    """Pixel-center coverage of a triangle mesh."""
    output = np.zeros(shape, dtype=bool)
    array = np.asarray(points, dtype=float)
    height, width = shape
    for triangle in triangles:
        (ax, ay), (bx, by), (cx, cy) = array[triangle]
        x0 = max(int(math.ceil(min(ax, bx, cx) - 0.5)), 0)
        x1 = min(int(math.floor(max(ax, bx, cx) - 0.5)) + 1, width)
        y0 = max(int(math.ceil(min(ay, by, cy) - 0.5)), 0)
        y1 = min(int(math.floor(max(ay, by, cy) - 0.5)) + 1, height)
        if x1 <= x0 or y1 <= y0:
            continue
        px = (np.arange(x0, x1, dtype=float) + 0.5)[None, :]
        py = (np.arange(y0, y1, dtype=float) + 0.5)[:, None]
        e0 = (px - ax) * (by - ay) - (py - ay) * (bx - ax)
        e1 = (px - bx) * (cy - by) - (py - by) * (cx - bx)
        e2 = (px - cx) * (ay - cy) - (py - cy) * (ax - cx)
        output[y0:y1, x0:x1] |= ((e0 >= 0) & (e1 >= 0) & (e2 >= 0)) | ((e0 <= 0) & (e1 <= 0) & (e2 <= 0))
    return output


def _coverage(mesh_mask, mask):
    intersection = int(np.count_nonzero(mesh_mask & mask))
    extra = int(np.count_nonzero(mesh_mask & ~mask))
    missing = int(np.count_nonzero(mask & ~mesh_mask))
    union = intersection + extra + missing
    return {'pixels': int(np.count_nonzero(mesh_mask)), 'mask_pixels': int(np.count_nonzero(mask)), 'extra': extra, 'missing': missing, 'iou': float(intersection / union) if union else 1.0}


def generate(image, threshold=0, simplify=0.0, measure=True):
    alpha = np.asarray(image.getchannel('A'))
    mask = alpha > int(threshold)
    if not mask.any():
        return {'empty': True, 'threshold': int(threshold), 'simplify': float(simplify), 'points': np.zeros((0, 2), dtype=float), 'triangles': np.zeros((0, 3), dtype=np.int64), 'loops': 0, 'islands': 0, 'holes': 0, 'coverage': {'pixels': 0, 'mask_pixels': 0, 'extra': 0, 'missing': 0, 'iou': 1.0}, 'mask': mask}
    cycles = []
    for raw in _trace(_edges(mask)):
        for part in _split(raw):
            cycles.append(_straighten(part))
    if len(cycles) > MAX_LOOPS:
        raise Invalid('透明轮廓过于零碎（超过4096个轮廓环），请提高简化程度或调整阈值')
    simplified = [_simplify(cycle, float(simplify)) for cycle in cycles]
    if float(simplify) > 0 and _crossing(simplified):
        tolerance = float(simplify)
        while tolerance > 0.05:
            tolerance /= 2
            simplified = [_simplify(cycle, tolerance) for cycle in cycles]
            if not _crossing(simplified):
                break
        else:
            simplified = cycles
    points, triangles = _triangulate(simplified)
    if len(triangles):
        triangles = _orient(points, triangles[_inside(points[triangles].mean(axis=1), simplified)])
    if len(triangles):
        used = np.unique(triangles)
        remap = np.full(len(points), -1, dtype=np.int64)
        remap[used] = np.arange(len(used))
        points = points[used]
        triangles = remap[triangles]
    else:
        points = np.zeros((0, 2), dtype=float)
        triangles = np.zeros((0, 3), dtype=np.int64)
    if len(triangles) > MAX_TRIANGLES:
        raise Invalid('三角网格过于复杂，请提高简化程度')
    mesh_mask = rasterize(points, triangles, mask.shape) if measure and len(triangles) else np.zeros(mask.shape, dtype=bool)
    count, _ = cv2.connectedComponents(mask.astype(np.uint8), connectivity=4)
    holes = sum(1 for cycle in cycles if _area(cycle) < 0)
    coverage = _coverage(mesh_mask, mask) if measure else None
    return {'empty': False, 'threshold': int(threshold), 'simplify': float(simplify), 'points': points, 'triangles': triangles, 'loops': len(simplified), 'islands': int(count - 1), 'holes': int(holes), 'coverage': coverage, 'mask': mask}


def describe(result):
    return {'empty': bool(result['empty']), 'threshold': int(result['threshold']), 'simplify': float(result['simplify']), 'vertices': int(len(result['points'])), 'triangles': int(len(result['triangles'])), 'loops': int(result['loops']), 'islands': int(result['islands']), 'holes': int(result['holes']), 'coverage': result['coverage']}


def _canvas(point, item):
    left, top = item['bounds'][0], item['bounds'][1]
    processed_width, processed_height = item['source_size']
    original_width, original_height = item['original_size']
    settings = item['settings']
    turn = int(settings.get('turn', 0)) % 4
    turned_width, turned_height = (original_height, original_width) if turn % 2 else (original_width, original_height)
    x = (point[0] + left) * turned_width / processed_width
    y = (point[1] + top) * turned_height / processed_height
    if settings.get('flip_x'):
        x = turned_width - x
    if settings.get('flip_y'):
        y = turned_height - y
    if turn == 1:
        return (y, original_height - x)
    if turn == 2:
        return (original_width - x, original_height - y)
    if turn == 3:
        return (original_width - y, x)
    return (x, y)


def _uv(point, placement, size):
    x, y = placement['x'], placement['y']
    if placement['rotated']:
        return ((x + placement['w'] - point[1]) / size, (y + point[0]) / size)
    return ((x + point[0]) / size, (y + point[1]) / size)


def _rounded(values):
    return [[round(float(v), 6) for v in point] for point in values]


def document(item, placement, size, threshold=None, simplify=None):
    settings = normalize(item.get('mesh'))
    threshold = settings['threshold'] if threshold is None else integer(threshold, '网格阈值', 0, 254)
    simplify = settings['simplify'] if simplify is None else float(number(simplify, '简化程度', 0, MAX_SIMPLIFY))
    result = generate(item['image'], threshold, simplify, measure=False)
    points = result['points']
    flipped = bool(item['settings'].get('flip_x')) != bool(item['settings'].get('flip_y'))
    return {
        'format': 'tileatlas-mesh-1',
        'id': item['id'],
        'name': item['name'],
        'empty': bool(result['empty']),
        'atlas': {'size': int(size), 'frame': {'x': int(placement['x']), 'y': int(placement['y']), 'w': int(placement['w']), 'h': int(placement['h'])}, 'rotated': bool(placement['rotated'])},
        'sprite': {'processed_size': [int(v) for v in item['image'].size], 'source_size': [int(v) for v in item['source_size']], 'original_size': [int(v) for v in item['original_size']], 'trim': [int(v) for v in item['bounds']], 'pivot': [float(v) for v in item['pivot']]},
        'mesh': {
            'threshold': int(result['threshold']),
            'simplify': float(result['simplify']),
            'winding': 'clockwise',
            'canvas_winding': 'counter-clockwise' if flipped else 'clockwise',
            'vertices': _rounded(points),
            'triangles': [[int(v) for v in triangle] for triangle in result['triangles']],
            'uvs': _rounded(_uv(point, placement, size) for point in points),
            'canvas_vertices': _rounded(_canvas(point, item) for point in points),
            'loops': int(result['loops']),
            'islands': int(result['islands']),
            'holes': int(result['holes']),
        },
    }


def bundle(items, layout):
    lookup = {item['id']: item for item in items}
    documents = []
    for placement in layout['sprites']:
        item = lookup.get(placement['id'])
        if item is None or not normalize(item.get('mesh'))['enabled']:
            continue
        documents.append(document(item, placement, layout['size']))
    return documents


def _lines(size, documents):
    width = max(1, int(round(max(size) / 512)))
    canvas = np.zeros((size[1], size[0], 4), dtype=np.uint8)
    for item in documents:
        scale = item['atlas']['size']
        vertices = item['mesh']['uvs']
        for triangle in item['mesh']['triangles']:
            for a, b in ((0, 1), (1, 2), (2, 0)):
                p = (int(round(vertices[triangle[a]][0] * scale)), int(round(vertices[triangle[a]][1] * scale)))
                q = (int(round(vertices[triangle[b]][0] * scale)), int(round(vertices[triangle[b]][1] * scale)))
                cv2.line(canvas, p, q, (255, 206, 86, 210), width, cv2.LINE_AA)
    return canvas


def overlay(image, documents):
    canvas = image.convert('RGBA').copy()
    canvas.alpha_composite(Image.fromarray(_lines(canvas.size, documents), 'RGBA'))
    return canvas


def preview(image, threshold=0, simplify=0.0):
    result = generate(image, threshold, simplify)
    canvas = checker(image)
    if result['empty']:
        array = np.asarray(canvas).copy()
        cv2.putText(array, 'fully transparent', (8, max(16, min(array.shape[0] - 6, 24))), cv2.FONT_HERSHEY_SIMPLEX, .5, (255, 200, 90, 255), 1, cv2.LINE_AA)
        return Image.fromarray(array, 'RGBA')
    array = np.asarray(canvas).copy()
    mesh_mask = rasterize(result['points'], result['triangles'], result['mask'].shape)
    extra = mesh_mask & ~result['mask']
    missing = result['mask'] & ~mesh_mask
    array[extra] = (array[extra].astype(np.float32) * .35 + np.asarray([232, 82, 72, 255], np.float32) * .65).astype(np.uint8)
    array[missing] = (array[missing].astype(np.float32) * .35 + np.asarray([70, 160, 235, 255], np.float32) * .65).astype(np.uint8)
    output = Image.fromarray(array, 'RGBA')
    width = max(1, int(round(max(output.size) / 512)))
    lines = np.zeros((output.height, output.width, 4), dtype=np.uint8)
    for triangle in result['triangles']:
        tri = result['points'][triangle]
        for a, b in ((0, 1), (1, 2), (2, 0)):
            cv2.line(lines, (int(round(tri[a][0])), int(round(tri[a][1]))), (int(round(tri[b][0])), int(round(tri[b][1]))), (255, 206, 86, 220), width, cv2.LINE_AA)
    output.alpha_composite(Image.fromarray(lines, 'RGBA'))
    if max(output.size) > MAX_PREVIEW:
        scale = MAX_PREVIEW / max(output.size)
        output = output.resize((max(1, round(output.width * scale)), max(1, round(output.height * scale))), Image.Resampling.LANCZOS)
    return output
