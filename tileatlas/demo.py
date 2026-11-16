import random
from PIL import Image, ImageDraw
from .raster import encode

COLORS = {'g': '#6cda83', 'w': '#5aa7e8'}


def create(catalog):
    project = catalog.create('合成素材 · Woodland UI')
    for index, (name, size, color) in enumerate([('leaf',(80,64),'#6cda83'),('gem',(50,90),'#67a9f1'),('key',(110,45),'#f0bd62'),('potion',(64,96),'#cc8aec'),('coin',(56,56),'#f1cf55'),('banner',(150,50),'#e57670')]):
        image = Image.new('RGBA', size)
        draw = ImageDraw.Draw(image)
        if index % 3 == 0:
            draw.ellipse((9,7,size[0]-13,size[1]-8), fill=color)
            draw.line((12,size[1]-10,size[0]-15,11),fill='white',width=3)
        elif index % 3 == 1:
            draw.polygon([(size[0]//2,5),(size[0]-6,size[1]//2),(size[0]//2,size[1]-7),(7,size[1]//2)], fill=color)
            draw.rectangle((size[0]//2-3,14,size[0]//2+2,size[1]//2),fill='white')
        else:
            draw.rounded_rectangle((6,8,size[0]-8,size[1]-9),radius=8,fill=color)
            draw.rectangle((15,15,26,size[1]-17),fill='white')
        project = catalog.add(project['id'],project['revision'],encode(image),name)
    return project


def terrain_tile(combo, size=32):
    # combo: 上右下左四边标签，g=草地 w=水域；边条颜色即标签颜色，标签一致的边拼起来像素连续
    image = Image.new('RGBA', (size, size))
    draw = ImageDraw.Draw(image)
    base = 'g' if combo.count('g') >= combo.count('w') else 'w'
    draw.rectangle((0, 0, size - 1, size - 1), fill=COLORS[base])
    deep, mid = 12, size // 2
    wedges = [[(0, 0), (size, 0), (mid + 4, deep), (mid - 4, deep)],
              [(size, 0), (size, size), (size - deep, mid + 4), (size - deep, mid - 4)],
              [(0, size), (size, size), (mid + 4, size - deep), (mid - 4, size - deep)],
              [(0, 0), (0, size), (deep, mid + 4), (deep, mid - 4)]]
    for side, edge in enumerate(combo):
        if edge != base:
            draw.polygon(wedges[side], fill=COLORS[edge])
    strips = [(0, 0, size, 2), (size - 2, 0, size, size), (0, size - 2, size, size), (0, 0, 2, size)]
    for side, edge in enumerate(combo):
        draw.rectangle(strips[side], fill=COLORS[edge])
    rng = random.Random(combo)
    for _ in range(30):
        x, y = rng.randrange(3, size - 3), rng.randrange(3, size - 3)
        r, g, b, a = image.getpixel((x, y))
        draw.point((x, y), fill=(int(r * .82), int(g * .82), int(b * .82), a))
    return image


def create_terrain(catalog):
    project = catalog.create('合成地形 · 草地与水域')
    specs = [('gggg', '草地'), ('wwww', '水域'), ('gggw', '水岸'), ('ggww', '水湾'), ('gwgw', '水道')]
    for combo, label in specs:
        image = terrain_tile(combo)
        project = catalog.add(project['id'], project['revision'], encode(image), f'{label}-{combo}')
        sid = project['sprites'][-1]['id']
        edges = dict(zip(['top', 'right', 'bottom', 'left'], combo))
        project = catalog.update(project['id'], sid, project['revision'], {'tile': {'enabled': True, 'edges': edges, 'rotations': [0, 1, 2, 3]}})
    return project
