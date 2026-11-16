import io
import math
import warnings
import hashlib
import numpy as np
from PIL import Image, ImageOps, ImageDraw
from image_lab.validation import Invalid


def decode(raw):
    if len(raw) > 24 * 1024 * 1024:
        raise Invalid('单图最多24MB')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            image = Image.open(io.BytesIO(raw))
            if min(image.size) < 1 or max(image.size) > 4096 or image.width * image.height > 8000000:
                raise Invalid('图像须在4096像素边长及800万像素内')
            return ImageOps.exif_transpose(image).convert('RGBA')
    except (OSError, Image.DecompressionBombWarning, Image.DecompressionBombError) as error:
        raise Invalid('无法读取PNG、JPEG或WebP图像') from error


def encode(image):
    stream = io.BytesIO()
    image.save(stream, 'PNG')
    return stream.getvalue()


def fingerprint(image):
    h = hashlib.sha256()
    h.update(str(image.size).encode())
    h.update(image.tobytes())
    return h.hexdigest()


def trim_bounds(image, threshold=0):
    alpha = np.asarray(image.getchannel('A'))
    ys, xs = np.nonzero(alpha > threshold)
    if not len(xs):
        return [0, 0, 1, 1], True
    return [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1], False


def transform(image, settings):
    turn = int(settings.get('turn', 0)) % 4
    if turn:
        image = image.transpose({1: Image.Transpose.ROTATE_270, 2: Image.Transpose.ROTATE_180, 3: Image.Transpose.ROTATE_90}[turn])
    if settings.get('flip_x'):
        image = ImageOps.mirror(image)
    if settings.get('flip_y'):
        image = ImageOps.flip(image)
    scale = float(settings.get('scale', 1))
    if not math.isfinite(scale) or not .05 <= scale <= 8:
        raise Invalid('缩放须在0.05–8之间')
    size = tuple(max(1, round(side * scale)) for side in image.size)
    if max(size) > 4096 or size[0] * size[1] > 8000000:
        raise Invalid('变换后图像超出预算')
    if image.size != size:
        image = image.resize(size, Image.Resampling.NEAREST if settings.get('pixel_art', True) else Image.Resampling.LANCZOS)
    bounds, empty = trim_bounds(image, int(settings.get('threshold', 0)))
    if not settings.get('trim', True):
        bounds = [0, 0, *image.size]
    crop = image.crop(bounds)
    return {'image': crop, 'source_size': list(image.size), 'bounds': bounds, 'empty': empty}


def checker(image, cell=12):
    yy, xx = np.indices((image.height, image.width))
    base = np.where((xx // cell + yy // cell) % 2, 59, 39).astype(np.uint8)
    bg = Image.fromarray(np.stack([base, base, base, np.full_like(base, 255)], axis=2), 'RGBA')
    bg.alpha_composite(image)
    return bg


def extrude(image, border):
    if not border:
        return image.copy()
    data = np.asarray(image)
    return Image.fromarray(np.pad(data, ((border, border), (border, border), (0, 0)), mode='edge'), 'RGBA')


def contact(images, labels, columns=4, cell=160):
    rows = max(1, math.ceil(len(images) / columns))
    out = Image.new('RGBA', (columns * cell, rows * (cell + 24)), '#1c2635')
    draw = ImageDraw.Draw(out)
    for index, image in enumerate(images):
        thumb = checker(image)
        thumb.thumbnail((cell - 12, cell - 12))
        x, y = index % columns * cell, index // columns * (cell + 24)
        out.alpha_composite(thumb, (x + (cell - thumb.width) // 2, y + (cell - thumb.height) // 2))
        draw.text((x + 6, y + cell + 3), labels[index][:20], fill='white')
    return out


def repeat_preview(image, count=3):
    if max(image.size) * count > 4096:
        raise Invalid('平铺预览超出预算')
    out = Image.new('RGBA', (image.width * count, image.height * count))
    for y in range(count):
        for x in range(count):
            out.paste(image, (x * image.width, y * image.height))
    return out


def seam_report(image):
    arr = np.asarray(image).astype(np.float32) / 255
    rgb = arr[:, :, :3] * arr[:, :, 3:4]
    x = np.abs(rgb[:, 0] - rgb[:, -1])
    y = np.abs(rgb[0] - rgb[-1])
    return {'horizontal_mean': float(x.mean()), 'vertical_mean': float(y.mean()), 'horizontal_peak': float(x.max()), 'vertical_peak': float(y.max()), 'alpha_left_right': float(np.abs(arr[:, 0, 3] - arr[:, -1, 3]).mean()), 'alpha_top_bottom': float(np.abs(arr[0, :, 3] - arr[-1, :, 3]).mean())}


def sample_uv(image, u, v):
    data = np.asarray(image).astype(np.float32)
    x = np.clip(np.asarray(u) * image.width - .5, 0, image.width - 1)
    y = np.clip(np.asarray(v) * image.height - .5, 0, image.height - 1)
    x0, y0 = np.floor(x).astype(int), np.floor(y).astype(int)
    x1, y1 = np.minimum(x0 + 1, image.width - 1), np.minimum(y0 + 1, image.height - 1)
    wx, wy = (x - x0)[..., None], (y - y0)[..., None]
    top = data[y0, x0] * (1 - wx) + data[y0, x1] * wx
    bottom = data[y1, x0] * (1 - wx) + data[y1, x1] * wx
    return np.rint(top * (1 - wy) + bottom * wy).astype(np.uint8)
