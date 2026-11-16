from PIL import Image, ImageDraw
from .raster import encode


def walker_sheet():
    fw = fh = 48
    mx = my = 4
    sx = sy = 2
    cols, rows = 4, 2
    width = mx * 2 + cols * fw + (cols - 1) * sx
    height = my * 2 + rows * fh + (rows - 1) * sy
    sheet = Image.new('RGBA', (width, height))
    draw = ImageDraw.Draw(sheet)
    swing = [-6, -3, 0, 3, 6, 3, 0, -3]
    bob = [0, -2, -3, -2, 0, -2, -3, -2]
    for index in range(8):
        col, row = index % cols, index // cols
        x, y = mx + col * (fw + sx), my + row * (fh + sy)
        cx = x + fw // 2
        ground = y + fh - 6 + bob[index]
        hip = ground - 13
        leg = swing[index]
        draw.line((cx - 4, hip, cx - 4 + leg, ground), fill='#3d4b5c', width=4)
        draw.line((cx + 4, hip, cx + 4 - leg, ground), fill='#55697d', width=4)
        draw.ellipse((cx - 10, hip - 17, cx + 10, hip + 3), fill='#6cda83')
        draw.ellipse((cx + 2, hip - 12, cx + 7, hip - 7), fill='#16324a')
        draw.line((cx - 1, hip - 7, cx - 1 - leg, hip - 1), fill='#4fae66', width=3)
        draw.line((cx, hip - 17, cx + leg // 2, hip - 23), fill='#f1cf55', width=2)
    return sheet


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
    sheet = walker_sheet()
    project = catalog.add(project['id'],project['revision'],encode(sheet),'walker-sheet')
    source = project['sprites'][-1]['id']
    project = catalog.add_clip(project['id'],project['revision'],{'name':'walk-cycle','source':source,'frame':{'w':48,'h':48},'margin':{'x':4,'y':4},'spacing':{'x':2,'y':2},'order':'row','pivot':[.5,.85],'frames':[{'cell':i,'ms':90} for i in range(8)]})
    return project
