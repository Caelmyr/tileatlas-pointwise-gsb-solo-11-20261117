import io
import hashlib
import threading
from flask import Blueprint, request, jsonify, Response, send_file, render_template
from image_lab.validation import Invalid
from .catalog import Catalog
from .raster import encode, checker, contact, repeat_preview, seam_report, transform
from .packing import pack, render, options, sprite_metadata, reconstruct
from .anim import normalize_clip, clip_metadata, geometry, preview as clip_grid
from .delivery import atlas_bundle, project_bundle, import_project
from .demo import create


def install(app, root):
    catalog = Catalog(root)
    app.extensions['tileatlas'] = catalog
    api = Blueprint('tileatlas',__name__,url_prefix='/atlas')

    def body():
        data = request.get_json(silent=True)
        if not isinstance(data,dict):
            raise Invalid('请提交JSON对象')
        return data

    def png(image):
        return Response(encode(image),mimetype='image/png',headers={'Cache-Control':'no-store'})

    def download(raw,name):
        return send_file(io.BytesIO(raw),download_name=name,as_attachment=True,mimetype='application/zip')

    def packed(p, data=None):
        settings = options(data or p['packing'])
        items = catalog.pack_items(p)
        layout = pack(items,settings)
        return layout, render(layout,items)

    @app.get('/')
    def home():
        return render_template('atlas.html')

    @api.get('/health')
    def health():
        return jsonify({'product':'TileAtlas','status':'ok','projects':len(catalog.list()),'limits':{'sprites':128,'atlas':2048}})

    @api.get('/projects')
    def listing():
        return jsonify(catalog.list())

    @api.post('/projects')
    def create_project():
        return jsonify(catalog.create(body().get('name','新图集'))),201

    @api.post('/demo')
    def demo():
        return jsonify(create(catalog)),201

    @api.get('/projects/<key>')
    def project(key):
        return jsonify(catalog.read(key))

    @api.patch('/projects/<key>')
    def update_project(key):
        d=body()
        def apply(p):
            if 'name' in d:
                p['name']=str(d['name'])[:100]
            if 'packing' in d:
                p['packing']=options(d['packing'])
        return jsonify(catalog.change(key,d['revision'],apply))

    @api.post('/projects/<key>/sprites')
    def add(key):
        file=request.files.get('file')
        if not file:
            raise Invalid('请选择图像')
        return jsonify(catalog.add(key,int(request.form['revision']),file.read(),request.form.get('name',file.filename))),201

    @api.patch('/projects/<key>/sprites/<sid>')
    def update(key,sid):
        d=body()
        return jsonify(catalog.update(key,sid,d['revision'],d))

    @api.delete('/projects/<key>/sprites/<sid>')
    def remove(key,sid):
        return jsonify(catalog.remove(key,sid,body()['revision']))

    @api.get('/projects/<key>/sprites/<sid>.png')
    def sprite_image(key,sid):
        p=catalog.read(key)
        s=catalog.sprite(p,sid)
        image=catalog.raw(p,s)
        if request.args.get('original')!='1':
            image=transform(image,s['settings'])['image']
        if request.args.get('repeat')=='1':
            image=repeat_preview(image)
        return png(checker(image) if request.args.get('checker')=='1' else image)

    @api.get('/projects/<key>/sprites/<sid>/inspect')
    def inspect(key,sid):
        p=catalog.read(key)
        s=catalog.sprite(p,sid)
        t=transform(catalog.raw(p,s),s['settings'])
        return jsonify({'id':sid,'bounds':t['bounds'],'source_size':t['source_size'],'cropped_size':list(t['image'].size),'empty':t['empty'],'seams':seam_report(t['image'])})

    @api.get('/projects/<key>/duplicates')
    def duplicates(key):
        return jsonify(catalog.duplicates(catalog.read(key)))

    @api.get('/projects/<key>/contact.png')
    def contact_sheet(key):
        items=catalog.prepared(catalog.read(key))
        return png(contact([i['image'] for i in items],[i['name'] for i in items]))

    @api.get('/projects/<key>/layout')
    def layout(key):
        p=catalog.read(key)
        result,image=packed(p)
        return jsonify({**result,'revision':p['revision'],'metadata':[sprite_metadata(s,result['size']) for s in result['sprites']],'clips':[clip_metadata(c) for c in p.get('clips',[])]})

    @api.post('/projects/<key>/clips')
    def add_clip(key):
        d=body()
        return jsonify(catalog.add_clip(key,d['revision'],d.get('clip',{}))),201

    @api.patch('/projects/<key>/clips/<cid>')
    def update_clip(key,cid):
        d=body()
        return jsonify(catalog.update_clip(key,cid,d['revision'],d.get('clip',{})))

    @api.delete('/projects/<key>/clips/<cid>')
    def remove_clip(key,cid):
        return jsonify(catalog.remove_clip(key,cid,body()['revision']))

    @api.post('/projects/<key>/clip-preview')
    def clip_preview(key):
        p=catalog.read(key)
        sheet=catalog.raw(p,catalog.sprite(p,str(body().get('source',''))))
        clip=normalize_clip(body(),sheet.size,check_frames=False)
        geo=geometry(sheet.size,clip['frame'],clip['margin'],clip['spacing'])
        if request.args.get('image')=='1':
            response=png(clip_grid(sheet,clip))
            response.headers['X-Columns']=str(geo['columns'])
            response.headers['X-Rows']=str(geo['rows'])
            response.headers['X-Cells']=str(geo['cells'])
            return response
        return jsonify(geo)

    @api.get('/projects/<key>/atlas.png')
    def atlas(key):
        p=catalog.read(key)
        items=catalog.pack_items(p)
        result=pack(items,options(p['packing']))
        image=render(result,items,request.args.get('outlines')=='1')
        return png(checker(image) if request.args.get('checker')=='1' else image)

    @api.get('/projects/<key>/reconstructed/<sid>.png')
    def reconstructed(key,sid):
        result,image=packed(catalog.read(key))
        placement=next((s for s in result['sprites'] if s['id']==sid),None)
        if placement is None:
            raise Invalid('精灵未打包')
        return png(reconstruct(image,placement))

    @api.get('/projects/<key>/export.zip')
    def export(key):
        p=catalog.read(key)
        result,image=packed(p)
        return download(atlas_bundle(p,result,image),'tileatlas.zip')

    @api.get('/projects/<key>/project.zip')
    def archive(key):
        return download(project_bundle(catalog,catalog.read(key)),'tileatlas-project.zip')

    @api.post('/import')
    def restore():
        file=request.files.get('file')
        if not file:
            raise Invalid('请选择项目ZIP')
        return jsonify(import_project(catalog,file.read())),201

    app.register_blueprint(api)
