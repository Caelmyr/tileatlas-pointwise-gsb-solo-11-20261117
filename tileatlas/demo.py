from PIL import Image, ImageDraw
from .raster import encode


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
