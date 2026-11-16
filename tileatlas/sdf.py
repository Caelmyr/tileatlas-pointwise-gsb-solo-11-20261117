import io
import json
import zipfile
import numpy as np
from PIL import Image, ImageDraw
from scipy.ndimage import distance_transform_edt
from image_lab.validation import integer, number
from .raster import encode, checker
from .packing import pack


def parameters(args):
    """SDF导出参数：轮廓阈值（alpha大于它算内部）与距离范围spread（像素）。"""
    return {
        'threshold': integer(args.get('threshold', 127), 'SDF阈值', 0, 254),
        'spread': integer(args.get('spread', 8), '距离范围', 1, 64),
    }


def frame(image, threshold, spread):
    """构建SDF帧的alpha平面与裁剪框。

    裁剪框取轮廓包围盒外扩spread，允许超出图边界（图外按透明），
    保证轮廓外留足距离空间，不被透明裁边吃掉。全透明图给出以原点
    为中心的最小明确帧。返回 (alpha数组, 框[左,上,右,下], 是否全透明)。
    """
    alpha = np.asarray(image.getchannel('A'))
    inside = alpha > threshold
    if inside.any():
        ys, xs = np.nonzero(inside)
        box = [int(xs.min()) - spread, int(ys.min()) - spread, int(xs.max()) + 1 + spread, int(ys.max()) + 1 + spread]
        empty = False
    else:
        box = [-spread, -spread, spread + 1, spread + 1]
        empty = True
    data = np.zeros((box[3] - box[1], box[2] - box[0]), dtype=np.uint8)
    x0, y0 = max(0, box[0]), max(0, box[1])
    x1, y1 = min(image.width, box[2]), min(image.height, box[3])
    if x1 > x0 and y1 > y0:
        data[y0 - box[1]:y1 - box[1], x0 - box[0]:x1 - box[0]] = alpha[y0:y1, x0:x1]
    return data, box, empty


def field(alpha, threshold):
    """有符号距离（像素）：轮廓外为正、内为负，零等值线即轮廓。

    欧氏距离变换天然保留内部孔洞与分离小块（每个像素都取到最近
    轮廓的距离）。全透明/全不透明返回明确的恒定场（±inf，编码后
    饱和到最远端）。
    """
    inside = alpha > threshold
    if not inside.any():
        return np.full(alpha.shape, np.inf, dtype=np.float32)
    if inside.all():
        return np.full(alpha.shape, -np.inf, dtype=np.float32)
    outer = distance_transform_edt(~inside).astype(np.float32)
    inner = distance_transform_edt(inside).astype(np.float32)
    return outer - inner


def encode_field(sdf, spread):
    """距离编码到0–255：128附近为轮廓，越大越靠内。解码见decode_field。"""
    normalized = np.clip(0.5 - sdf / (2.0 * spread), 0.0, 1.0)
    return np.rint(normalized * 255).astype(np.uint8)


def decode_field(data, spread):
    """编码逆运算：distance_px = (0.5 - value / 255) * 2 * spread。"""
    return (0.5 - data.astype(np.float32) / 255.0) * 2.0 * spread


def layout(items, settings, threshold, spread):
    """对每个处理后的精灵构建SDF帧并打包。settings的extrude须为0（spread已是边距）。"""
    frames = []
    for item in items:
        alpha, box, empty = frame(item['image'], threshold, spread)
        data = encode_field(field(alpha, threshold), spread)
        frames.append({'id': item['id'], 'name': item['name'], 'image': Image.fromarray(data, 'L'), 'box': box, 'empty': empty, 'bounds': item['bounds'], 'source_size': item['source_size'], 'pivot': item['pivot']})
    result = pack(frames, settings)
    lookup = {f['id']: f for f in frames}
    for placement in result['sprites']:
        placement['box'] = lookup[placement['id']]['box']
    return result, frames


def render(layout_result, frames):
    """渲染单通道（L）距离图集。"""
    lookup = {f['id']: f for f in frames}
    out = Image.new('L', (layout_result['size'], layout_result['size']))
    for placement in layout_result['sprites']:
        image = lookup[placement['id']]['image']
        if placement['rotated']:
            image = image.transpose(Image.Transpose.ROTATE_270)
        out.paste(image, (placement['x'], placement['y']))
    return out


def sprite_metadata(placement, size):
    """单个精灵的解码元数据：帧位置、UV、原画布位置（外扩spread后的框，可为负）。"""
    x, y, w, h = (placement[k] for k in ('x', 'y', 'w', 'h'))
    source_w, source_h = placement['source_size']
    left, top = placement['box'][:2]
    corners = [[x / size, y / size], [(x + w) / size, y / size], [(x + w) / size, (y + h) / size], [x / size, (y + h) / size]]
    if placement['rotated']:
        corners = corners[1:] + corners[:1]
    return {
        'id': placement['id'], 'name': placement['name'],
        'frame': {'x': x, 'y': y, 'w': w, 'h': h},
        'rotated': placement['rotated'],
        'uv_corners': corners,
        'canvas_position': placement['box'],
        'source_size': [source_w, source_h],
        'pivot': placement['pivot'],
        'pivot_pixels': [placement['pivot'][0] * source_w - left, placement['pivot'][1] * source_h - top],
        'empty': placement['empty'],
    }


def bundle(p, layout_result, image, params):
    """打包下载：单通道距离图集PNG + 解码所需全部元数据。"""
    metadata = {
        'format': 'tileatlas-sdf-1',
        'project': p['name'],
        'revision': p['revision'],
        'image': 'sdf.png',
        'mode': 'L',
        'size': [layout_result['size'], layout_result['size']],
        'threshold': params['threshold'],
        'spread': params['spread'],
        'signed_distance': 'distance_px = (0.5 - value / 255) * 2 * spread；正值在轮廓外，负值在轮廓内，绝对值达到spread表示已远离轮廓',
        'sprites': [sprite_metadata(item, layout_result['size']) for item in layout_result['sprites']],
    }
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('sdf.png', encode(image))
        archive.writestr('sdf.json', json.dumps(metadata, ensure_ascii=False, indent=2))
    return stream.getvalue()


def shape(sdf, scale):
    """按scale缩放距离场后取零等值线重建形状，演示缩大缩小边缘仍平滑。"""
    h, w = sdf.shape
    size = (max(1, round(w * scale)), max(1, round(h * scale)))
    resized = np.asarray(Image.fromarray(sdf, 'F').resize(size, Image.Resampling.BILINEAR))
    mask = np.where(resized <= 0, 255, 0).astype(np.uint8)
    rgba = np.stack([np.full_like(mask, 255), np.full_like(mask, 255), np.full_like(mask, 255), mask], axis=2)
    return checker(Image.fromarray(rgba, 'RGBA'))


def stroke(sdf, width):
    """描边演示：|距离|<=width的环带为描边色，主体为白色。"""
    body = sdf <= 0
    band = sdf <= width
    rgba = np.zeros((*sdf.shape, 4), dtype=np.uint8)
    rgba[band] = (240, 157, 99, 255)
    rgba[body] = (255, 255, 255, 255)
    return checker(Image.fromarray(rgba, 'RGBA'))


def preview(image, threshold, spread, outline):
    """2×2预览：距离场本身、放大3×、缩小0.4×、描边后的形状。"""
    alpha, box, empty = frame(image, threshold, spread)
    sdf = np.clip(field(alpha, threshold), -spread, spread)
    limit = 640
    h, w = sdf.shape
    shrink = min(1.0, limit / max(h, w, 1))
    if shrink < 1.0:
        sdf = np.asarray(Image.fromarray(sdf, 'F').resize((max(1, round(w * shrink)), max(1, round(h * shrink))), Image.Resampling.BILINEAR))
        outline = outline * shrink
    data = encode_field(sdf, spread)
    cells = [
        ('FIELD', Image.fromarray(data, 'L').convert('RGBA')),
        ('UP 3x', shape(sdf, 3.0)),
        ('DOWN 0.4x', shape(sdf, 0.4)),
        ('OUTLINE', stroke(sdf, outline)),
    ]
    cw, ch = data.shape[1], data.shape[0]
    label_h, pad = 20, 10
    out = Image.new('RGBA', (cw * 2 + pad * 3, (ch + label_h) * 2 + pad * 3), (21, 30, 39, 255))
    draw = ImageDraw.Draw(out)
    for index, (title, cell) in enumerate(cells):
        x = pad + (index % 2) * (cw + pad)
        y = pad + (index // 2) * (ch + label_h + pad)
        slot = Image.new('RGBA', (cw, ch), (32, 42, 53, 255))
        slot.paste(cell, ((cw - cell.width) // 2, (ch - cell.height) // 2))
        out.paste(slot, (x, y))
        draw.text((x + 2, y + ch + 5), title, fill=(180, 220, 195, 255))
    return out
