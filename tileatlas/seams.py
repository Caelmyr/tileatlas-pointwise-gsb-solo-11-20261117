import io
import json
import zipfile
import numpy as np
from PIL import Image, ImageDraw
from image_lab.validation import Invalid, integer
from .raster import encode, seam_report

KINDS = ('heatmap-color', 'heatmap-alpha', 'overlay', 'tiled', 'profile-x', 'profile-y')

# 热图色带锚点（暗 → 亮），值按 ctx['scale'] 归一后查表
_STOPS = [(0.0, (28, 26, 44)), (0.35, (94, 31, 116)), (0.6, (204, 54, 45)), (0.8, (248, 149, 49)), (1.0, (252, 253, 191))]


def _lut():
    pos = [p for p, _ in _STOPS]
    t = np.linspace(0, 1, 256)
    return np.stack([np.interp(t, pos, [c[i] for _, c in _STOPS]) for i in range(3)], axis=1).astype(np.uint8)


LUT = _lut()


def _r(value):
    return round(float(value), 6)


def parse_options(args):
    return {
        'strip': integer(args.get('strip', 8), '条带宽度', 1, 64),
        'candidates': integer(args.get('candidates', 5), '对齐候选数量', 1, 16),
        'peaks': integer(args.get('peaks', 8), '尖峰标记数量', 1, 32),
    }


def _edge_strips(arr, axis, strip):
    """接缝两侧各取 strip 像素，k=0 为边缘像素、随 k 深入图内。
    axis='x' → 左右环绕接缝（剖面沿 y）；axis='y' → 上下环绕接缝（剖面沿 x）。"""
    if axis == 'x':
        return arr[:, ::-1][:, :strip], arr[:, :strip]
    return arr[::-1][:strip].transpose(1, 0, 2), arr[:strip].transpose(1, 0, 2)


def _split(rgba):
    """可见颜色（预乘 alpha 的 RGB）与透明度分开，透明像素的 RGB 差异不计入可见跳变。"""
    return rgba[..., :3] * rgba[..., 3:4], rgba[..., 3]


def _pixel_errors(a, b):
    """逐像素误差，形状 (L, strip)：可见颜色跳变与透明度跳变。"""
    pa, aa = _split(a)
    pb, ab = _split(b)
    return np.abs(pa - pb).mean(axis=2), np.abs(aa - ab)


def _shift_mse(a, b):
    """b 相对 a 循环平移 s（沿接缝方向）后的逐偏移均方误差。
    用预乘颜色+透明度联合特征的 FFT 互相关计算，O(L log L) 覆盖全部偏移。"""
    length = a.shape[0]
    pa, aa = _split(a)
    pb, ab = _split(b)
    fa = np.concatenate([pa, aa[..., None]], axis=2).reshape(length, -1).astype(np.float64)
    fb = np.concatenate([pb, ab[..., None]], axis=2).reshape(length, -1).astype(np.float64)
    energy = (fa ** 2).sum() + (fb ** 2).sum()
    corr = np.fft.irfft(np.fft.rfft(fb, axis=0) * np.conj(np.fft.rfft(fa, axis=0)), n=length, axis=0).sum(axis=1)
    return np.maximum(energy - 2 * corr, 0) / (length * fa.shape[1])


def _circular_nms(values, count, exclude=None, radius_div=200):
    """按值升序选取位置，环形距离过近的只保留更优者。"""
    length = len(values)
    radius = max(2, length // radius_div)
    picked = []
    for s in np.argsort(values, kind='stable'):
        s = int(s)
        if s == exclude:
            continue
        if all(min((s - t) % length, (t - s) % length) > radius for t in picked):
            picked.append(s)
        if len(picked) >= count:
            break
    return picked


def _shift_stats(a, b, offset):
    """某一循环偏移下的精确统计：平均、P95 与局部尖峰并存，避免平均掩盖尖峰。"""
    shifted = np.roll(b, -offset, axis=0) if offset else b
    color, alpha = _pixel_errors(a, shifted)
    row_color = color.max(axis=1)
    row_alpha = alpha.max(axis=1)
    combined = (row_color + row_alpha) / 2
    peak_at = int(np.argmax(combined))
    return {
        'offset': int(offset),
        'color_mean': _r(row_color.mean()),
        'alpha_mean': _r(row_alpha.mean()),
        'combined_mean': _r(combined.mean()),
        'combined_p95': _r(np.percentile(combined, 95)),
        'peak': _r(combined[peak_at]),
        'peak_position': peak_at,
    }


def _stats(profile):
    return {'mean': _r(profile.mean()), 'p95': _r(np.percentile(profile, 95)), 'max': _r(profile.max())}


def analyze(image, options):
    """计算平铺接缝诊断上下文。只读分析，不修改传入图像。"""
    width, height = image.size
    strip = min(options['strip'], max(1, min(width, height) // 2))
    arr = np.asarray(image).astype(np.float32) / 255.0
    ctx = {
        'size': [width, height],
        'strip': strip,
        'options': {**options, 'strip': strip},
        'seams': {},
        'profiles': {},
        'maps': {'color': np.zeros((height, width), np.float32), 'alpha': np.zeros((height, width), np.float32)},
    }
    scale = 0.02
    for axis in ('x', 'y'):
        a, b = _edge_strips(arr, axis, strip)
        color, alpha = _pixel_errors(a, b)
        # 逐位置剖面取条带内最大误差：单像素尖峰不被条带平均稀释
        row_color = color.max(axis=1).astype(np.float64)
        row_alpha = alpha.max(axis=1).astype(np.float64)
        combined = (row_color + row_alpha) / 2
        offsets = _circular_nms(_shift_mse(a, b), options['candidates'], exclude=0)
        baseline = _shift_stats(a, b, 0)
        candidates = []
        for s in offsets:
            stats = _shift_stats(a, b, s)
            stats['mean_delta'] = _r(stats['combined_mean'] - baseline['combined_mean'])
            candidates.append(stats)
        label = axis.upper()
        peaks = []
        for rank, p in enumerate(_circular_nms(-combined, options['peaks'], radius_div=100), 1):
            pixels = [[width - 1, p], [0, p]] if axis == 'x' else [[p, height - 1], [p, 0]]
            peaks.append({'label': f'{label}{rank}', 'position': p, 'pixels': pixels,
                          'color': _r(row_color[p]), 'alpha': _r(row_alpha[p]), 'combined': _r(combined[p])})
        ctx['seams'][axis] = {
            'color': _stats(row_color),
            'alpha': _stats(row_alpha),
            'combined': _stats(combined),
            'peaks': peaks,
            'alignment': {'baseline': baseline, 'candidates': candidates},
        }
        ctx['profiles'][axis] = {'color': row_color, 'alpha': row_alpha}
        scale = max(scale, float(color.max()), float(alpha.max()))
        if axis == 'x':
            cols = width - 1 - np.arange(strip)
            for channel, err in (('color', color), ('alpha', alpha)):
                ctx['maps'][channel][:, cols] = np.maximum(ctx['maps'][channel][:, cols], err)
                ctx['maps'][channel][:, :strip] = np.maximum(ctx['maps'][channel][:, :strip], err)
        else:
            rows = height - 1 - np.arange(strip)
            for channel, err in (('color', color), ('alpha', alpha)):
                ctx['maps'][channel][rows, :] = np.maximum(ctx['maps'][channel][rows, :], err.T)
                ctx['maps'][channel][:strip, :] = np.maximum(ctx['maps'][channel][:strip, :], err.T)
    ctx['scale'] = scale
    return ctx


def _alignment_note(seam):
    base = seam['alignment']['baseline']
    candidates = seam['alignment']['candidates']
    notes = []
    if candidates:
        best = min(candidates, key=lambda c: c['combined_mean'])
        drop = base['combined_mean'] - best['combined_mean']
        if drop > 0.001 and best['combined_mean'] < base['combined_mean'] * 0.9:
            notes.append(f"循环平移 {best['offset']} 像素后接缝平均误差可降约 {drop / max(base['combined_mean'], 1e-9) * 100:.0f}%，可据此重新裁剪；原图不会被修改")
        else:
            notes.append('当前对齐已接近候选最优，循环平移收益不明显')
        if best['peak'] > base['peak'] * 1.1:
            notes.append('注意：平均最优候选的局部尖峰高于当前对齐，请结合尖峰列复核，勿只看平均值')
    else:
        notes.append('接缝长度过小，无可用平移候选')
    if seam['combined']['max'] > max(seam['combined']['mean'] * 4, seam['combined']['mean'] + 0.05):
        notes.append('存在显著局部尖峰：整体平均偏低不代表接缝合格，请按标记坐标逐一检查')
    return '；'.join(notes)


def report(ctx, image, meta=None):
    """完整诊断报告（含当前快速统计与逐位置剖面），坐标均为处理后精灵图像像素。"""
    width, height = ctx['size']
    seams = {}
    names = {'x': '左右环绕接缝（垂直接缝线）', 'y': '上下环绕接缝（水平接缝线）'}
    directions = {'x': '沿垂直方向循环平移对侧边缘剖面', 'y': '沿水平方向循环平移对侧边缘剖面'}
    for axis in ('x', 'y'):
        seam = ctx['seams'][axis]
        seams[axis] = {
            'name': names[axis],
            'profile_length': len(ctx['profiles'][axis]['color']),
            'shift_direction': directions[axis],
            **seam,
            'alignment_note': _alignment_note(seam),
        }
    result = {
        'format': 'tileatlas-seams-1',
        'image_size': [width, height],
        'parameters': ctx['options'],
        'heatmap_scale': _r(ctx['scale']),
        'coordinate_note': '坐标均为处理后精灵图像的像素坐标（左上角原点，x向右、y向下）；平铺预览坐标对(宽,高)取模即得原图像素；叠加 sprite.bounds 的(left,top)偏移可换算回原始上传图像。分析为只读，原图未被修改。',
        'quick': seam_report(image),
        'seams': seams,
        'profiles': {axis: {channel: [round(float(v), 5) for v in ctx['profiles'][axis][channel]] for channel in ('color', 'alpha')} for axis in ('x', 'y')},
        'diagnostic_images': list(KINDS),
    }
    if meta:
        result['sprite'] = meta
    return result


def _flatten(image, dim=1.0):
    bg = Image.new('RGBA', image.size, (26, 30, 38, 255))
    bg.alpha_composite(image)
    out = np.asarray(bg.convert('RGB')).astype(np.float32)
    return out * dim


def _heatmap(ctx, image, channel):
    base = _flatten(image, 0.35)
    err = ctx['maps'][channel]
    hot = LUT[np.clip(err / ctx['scale'] * 255, 0, 255).astype(np.uint8)].astype(np.float32)
    mask = err > ctx['scale'] * 0.02
    out = base.copy()
    out[mask] = base[mask] * 0.25 + hot[mask] * 0.75
    return Image.fromarray(out.astype(np.uint8), 'RGB')


def _overlay(ctx, image):
    width, height = ctx['size']
    strip = ctx['strip']
    out = Image.fromarray(_flatten(image, 0.85).astype(np.uint8), 'RGB')
    draw = ImageDraw.Draw(out)
    radius = max(4, min(width, height) // 120)
    for x in (strip - 1, width - strip):
        draw.line((x, 0, x, height - 1), fill=(90, 110, 130), width=1)
    for y in (strip - 1, height - strip):
        draw.line((0, y, width - 1, y), fill=(90, 110, 130), width=1)
    for axis, tone in (('x', (255, 122, 89)), ('y', (117, 199, 255))):
        for peak in ctx['seams'][axis]['peaks']:
            p = peak['position']
            points = [(width - 1, p), (0, p)] if axis == 'x' else [(p, height - 1), (p, 0)]
            for cx, cy in points:
                draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), outline=tone, width=2)
                draw.line((cx - radius, cy, cx + radius, cy), fill=tone, width=1)
                draw.line((cx, cy - radius, cx, cy + radius), fill=tone, width=1)
            lx = min(max(points[0][0] + radius + 2, 2), max(2, width - 34))
            ly = min(max(points[0][1] - radius - 12, 2), max(2, height - 14))
            draw.text((lx, ly), peak['label'], fill=tone)
    return out


def _tiled(ctx, image):
    width, height = ctx['size']
    count = 3 if max(width, height) * 3 <= 4096 else (2 if max(width, height) * 2 <= 4096 else 1)
    tiles = Image.new('RGBA', (width * count, height * count))
    for ty in range(count):
        for tx in range(count):
            tiles.paste(image, (tx * width, ty * height))
    out = Image.fromarray(_flatten(tiles).astype(np.uint8), 'RGB')
    draw = ImageDraw.Draw(out)
    seam_tone = (255, 210, 90)
    for t in range(1, count):
        for x in (t * width - 1, t * width):
            draw.line((x, 0, x, height * count - 1), fill=seam_tone, width=1)
        for y in (t * height - 1, t * height):
            draw.line((0, y, width * count - 1, y), fill=seam_tone, width=1)
    radius = max(4, min(width, height) // 60)
    center = count // 2
    for axis, tone in (('x', (255, 122, 89)), ('y', (117, 199, 255))):
        for peak in ctx['seams'][axis]['peaks']:
            p = peak['position']
            for t in range(1, count):
                for u in range(count):
                    cx, cy = (t * width, u * height + p) if axis == 'x' else (u * width + p, t * height)
                    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), outline=tone, width=2)
                    if t == center and u == center:
                        draw.text((cx + radius + 2, max(2, cy - 6)), peak['label'], fill=tone)
    return out


def _profile_strip(ctx, axis):
    """沿接缝的误差条带：车道1=可见颜色跳变，车道2=透明度跳变，白刻度=尖峰标记。"""
    lane, gap = 64, 4
    scale = ctx['scale']
    bands = []
    for channel in ('color', 'alpha'):
        t = np.clip(ctx['profiles'][axis][channel] / scale * 255, 0, 255).astype(np.uint8)
        bands.append(LUT[t])
    length = len(bands[0])
    if axis == 'x':
        out = np.zeros((length, lane * 2 + gap, 3), np.uint8)
        out[:, :lane] = np.broadcast_to(bands[0][:, None, :], (length, lane, 3))
        out[:, lane + gap:] = np.broadcast_to(bands[1][:, None, :], (length, lane, 3))
        for peak in ctx['seams'][axis]['peaks']:
            out[peak['position'], :4] = 255
    else:
        out = np.zeros((lane * 2 + gap, length, 3), np.uint8)
        out[:lane, :] = np.broadcast_to(bands[0][None, :, :], (lane, length, 3))
        out[lane + gap:, :] = np.broadcast_to(bands[1][None, :, :], (lane, length, 3))
        for peak in ctx['seams'][axis]['peaks']:
            out[:4, peak['position']] = 255
    return Image.fromarray(out, 'RGB')


def render(ctx, image, kind):
    if kind == 'heatmap-color':
        return _heatmap(ctx, image, 'color')
    if kind == 'heatmap-alpha':
        return _heatmap(ctx, image, 'alpha')
    if kind == 'overlay':
        return _overlay(ctx, image)
    if kind == 'tiled':
        return _tiled(ctx, image)
    if kind == 'profile-x':
        return _profile_strip(ctx, 'x')
    if kind == 'profile-y':
        return _profile_strip(ctx, 'y')
    raise Invalid('未知诊断图类型')


def _readme(meta, ctx):
    width, height = ctx['size']
    return f"""TileAtlas 平铺接缝诊断包
========================
项目: {meta.get('project', '')}（修订 {meta.get('revision', '')}）
精灵: {meta.get('sprite', '')}（{meta.get('id', '')}）
图像: {width} × {height}（处理后精灵图像）
参数: 条带宽度 strip={ctx['options']['strip']}，对齐候选 {ctx['options']['candidates']}，尖峰标记 {ctx['options']['peaks']}
热图归一: heatmap_scale={_r(ctx['scale'])}（误差值 / 该值 → 色带位置）

文件说明
--------
seam-report.json   完整报告：快速统计、逐接缝均值/P95/峰值、尖峰坐标、循环平移候选、逐位置剖面
heatmap-color.png  可见颜色跳变热图（预乘透明度后的 RGB 差异，只覆盖比较条带区域）
heatmap-alpha.png  透明度跳变热图
overlay.png        原尺寸标记图：X1../Y1.. 与报告 seams.*.peaks 一一对应，细线为条带边界
tiled.png          循环平铺预览：黄线为接缝，标记落在接缝上；坐标对(宽,高)取模即原图像素
profile-x.png      左右接缝误差条带（高=图像高）：左车道=颜色跳变，右车道=透明度跳变，白刻度=尖峰
profile-y.png      上下接缝误差条带（宽=图像宽）：上车道=颜色跳变，下车道=透明度跳变

坐标说明
--------
所有坐标为处理后精灵图像像素（左上角原点，x向右、y向下）。
平铺预览坐标 (tx,ty) → 原图 (tx % {width}, ty % {height})。
原始上传图像坐标 = 本坐标 + bounds 的 (left, top) 偏移（见 seam-report.json 的 sprite.bounds）。
本诊断不修改原图；对齐候选仅供参考，用于重新裁剪或回源工具修正。
"""


def bundle(ctx, image, meta):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('seam-report.json', json.dumps(report(ctx, image, meta), ensure_ascii=False, indent=2))
        for kind in KINDS:
            archive.writestr(kind + '.png', encode(render(ctx, image, kind)))
        archive.writestr('README.txt', _readme(meta, ctx))
    return stream.getvalue()
