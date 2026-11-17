import copy
import io
from flask import Blueprint, request, jsonify, Response, send_file, render_template
from image_lab.validation import Invalid
from .catalog import Catalog
from .raster import encode, checker, contact, repeat_preview, seam_report, transform
from .packing import render, options, reconstruct, metadata_of, LayoutBlocked
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

    def query():
        packing = request.get_json(silent=True)
        packing = packing.get('packing') if isinstance(packing, dict) else None
        return {'mode': request.args.get('mode', 'preserve'), 'packing': packing}

    def shown(key):
        """The pending preview when one is waiting, else the live resolved layout."""
        p = catalog.read(key)
        pending = catalog.pending(p)
        if pending is not None and request.args.get('pending', '1') == '1':
            layout = copy.deepcopy(pending['layout'])
            layout['pending'] = True
            return p, layout
        layout = catalog.resolve(p, query())
        layout['pending'] = False
        return p, layout

    def packed(key):
        p, layout = shown(key)
        return p, layout, render(layout, catalog.prepared(p))

    def with_metadata(layout):
        return {**layout, 'metadata': metadata_of(layout)}

    @app.errorhandler(LayoutBlocked)
    def blocked(error):
        return jsonify({'error':str(error),'type':'layout_blocked','blockers':error.blockers}),409

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
        p=catalog.read(key)
        return jsonify({**p,'layout':catalog.read_layout(key),'pending':bool(catalog.pending(p)),'layout_fresh':catalog.layout_fresh(p)})

    @api.patch('/projects/<key>')
    def update_project(key):
        d=body()
        def apply(p):
            if 'name' in d:
                p['name']=str(d['name'])[:100]
            if 'packing' in d:
                p['packing']=options(d['packing'])
        return jsonify(catalog.change(key,d['revision'],apply))

    @api.post('/projects/<key>/lock')
    def set_locks(key):
        d=body()
        if 'ids' not in d:
            raise Invalid('请给出锁定精灵清单')
        ids = d['ids'] if isinstance(d['ids'], list) else [d.get('id')]
        return jsonify(catalog.set_locked(key,d['revision'],[str(sid) for sid in ids],d.get('locked',True)))

    @api.post('/projects/<key>/sprites')
    def add(key):
        file=request.files.get('file')
        if not file:
            raise Invalid('请选择图像')
        return jsonify(catalog.add(key,int(request.form['revision']),file.read(),request.form.get('name',file.filename))),201

    @api.post('/projects/<key>/sprites/<sid>/replace')
    def replace(key,sid):
        file=request.files.get('file')
        if not file:
            raise Invalid('请选择替换图像')
        return jsonify(catalog.replace(key,sid,int(request.form['revision']),file.read(),request.form.get('name')))

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
        p, result = shown(key)
        return jsonify({**result,'revision':p['revision'],'metadata':metadata_of(result)})

    @api.post('/projects/<key>/layout/preview')
    def preview(key):
        return jsonify(with_metadata(catalog.stage(key,body())))

    @api.post('/projects/<key>/layout/confirm')
    def confirm(key):
        result = catalog.confirm(key,body())
        result['layout'] = with_metadata(result['layout'])
        return jsonify(result)

    @api.delete('/projects/<key>/layout/pending')
    def discard(key):
        return jsonify(catalog.discard(key))

    @api.post('/projects/<key>/layout/repack')
    def repack(key):
        """Full repack: one explicit decision, but still previewed and confirmed."""
        d=body()
        return jsonify(with_metadata(catalog.stage(key,{**d,'mode':'repack'})))

    @api.post('/projects/<key>/layout/repack/confirm')
    def repack_confirm(key):
        result = catalog.repack_confirm(key,body())
        result['layout'] = with_metadata(result['layout'])
        return jsonify(result)

    @api.delete('/projects/<key>/layout')
    def forget(key):
        return jsonify(catalog.reset_layout(key,int(request.args.get('revision',-1))))

    @api.get('/projects/<key>/atlas.png')
    def atlas(key):
        p, layout = shown(key)
        image = render(layout, catalog.prepared(p), request.args.get('outlines')=='1')
        return png(checker(image) if request.args.get('checker')=='1' else image)

    @api.get('/projects/<key>/reconstructed/<sid>.png')
    def reconstructed(key,sid):
        _,result,image=packed(key)
        placement=next((s for s in result['sprites'] if s['id']==sid),None)
        if placement is None:
            raise Invalid('精灵未打包')
        return png(reconstruct(image,placement))

    @api.get('/projects/<key>/export.zip')
    def export(key):
        p,result,image=packed(key)
        if request.args.get('pending','1')=='1' and catalog.pending(p):
            raise Invalid('还有未确认的布局预览；请先确认，或明确选择完全重排后再导出')
        if catalog.read_layout(key) is None:
            raise Invalid('尚无已确认布局，请先生成预览并确认')
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
