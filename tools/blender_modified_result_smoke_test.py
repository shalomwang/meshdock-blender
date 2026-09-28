"""Real Blender snapshots + fake Tripo transport. Never sends network requests."""
import os,sys,json,uuid,time,traceback,struct,hashlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'build/modified-result-smoke';OUT.mkdir(parents=True,exist_ok=True)
os.environ['MESHDOCK_STAGING']=str(OUT/'staging'/uuid.uuid4().hex)
os.environ['MESHDOCK_BRIDGE_DESCRIPTOR']=str(OUT/'bridge.json');os.environ['MESHDOCK_DEV_MOCK']='1'
sys.path.insert(0,str(ROOT))
import bpy
import meshdock
from types import SimpleNamespace
from mathutils import Vector
meshdock.register()
from meshdock.blender.runtime import get_runtime
from meshdock.blender.process_target import prepare_processing_source,generated_target,local_target,capture_baseline
from meshdock.blender.workbench_preview import own_job,candidate_collection
from meshdock.blender.operators import AI3D_OT_process_candidate
from meshdock.providers.http import DownloadResult
runtime=get_runtime();w=bpy.context.window_manager.windows[0];source=w.scene
(OUT/'temp').mkdir(exist_ok=True);bpy.context.preferences.filepaths.temporary_directory=str(OUT/'temp')
checks=[]
(OUT/'result.json').write_text('{"status":"running"}')

class FakeHttp:
    def __init__(self): self.calls=[];self.uploads=[];self.counter=0
    def request_json(self,method,url,*,token,payload=None):
        self.calls.append((method,url,payload))
        if url.endswith('/files/presign'):
            return {'code':0,'data':{'presigned_url':'https://fake.invalid/upload','file_token':'snapshot-token'}}
        if method=='POST':
            self.counter+=1
            return {'code':0,'data':{'task_id':f'task_{self.counter:04d}'}}
        return {'code':0,'data':{'task_id':url.rsplit('/',1)[-1],'status':'success','progress':100,
                        'output':{'model_url':'https://fake.invalid/result.glb'}}}
    def upload_file(self,url,path,*,content_type): self.uploads.append(Path(path).read_bytes())
    def download(self,url,path,*,max_bytes):
        path.parent.mkdir(parents=True,exist_ok=True);raw=self.uploads[-1];path.write_bytes(raw)
        return DownloadResult(path,len(raw),hashlib.sha256(raw).hexdigest(),'model/gltf-binary')
http=FakeHttp()
runtime.service.providers['tripo_global']._http=http
runtime.service.providers['tripo_global']._poll_interval=0
runtime.credentials.set('tripo_global','fake-local-process-account',note='Test only')

def ctx():
    area=next(a for a in w.screen.areas if a.type=='VIEW_3D')
    return bpy.context.temp_override(window=w,area=area,region=next(r for r in area.regions if r.type=='WINDOW'))

def schedule(fn,delay=.8):
    def run():
        try:
            with ctx(): fn()
        except Exception:
            (OUT/'result.json').write_text(json.dumps({'ok':False,'error':traceback.format_exc(),'scene':bpy.context.scene.name,'window_scene':w.scene.name,'objects':[o.name for o in bpy.context.scene.objects],'layer_objects':[o.name for o in bpy.context.view_layer.objects]}),encoding='utf-8')
            try: meshdock.unregister()
            finally: bpy.ops.wm.quit_blender()
    bpy.app.timers.register(run,first_interval=delay)

def select(obj):
    bpy.ops.object.select_all(action='DESELECT');obj.select_set(True);bpy.context.view_layer.objects.active=obj

def glb(raw):
    length=struct.unpack_from('<I',raw,12)[0];return json.loads(raw[20:20+length])

def object_state(obj):
    return (tuple(tuple(r) for r in obj.matrix_world),tuple(tuple(v.co) for v in obj.data.vertices),
            tuple(c.name for c in obj.users_collection),len(obj.modifiers),obj.hide_get())

def start():
    global B,protected,baseline,job,initial_count
    bpy.context.preferences.view.language='zh_HANS';bpy.context.preferences.view.use_translate_interface=True
    A=source.objects['Cube'];A.name='A';A.location.x=-5
    bpy.ops.mesh.primitive_cube_add(location=(2,0,0));B=bpy.context.object;B.name='自建 B'
    B.modifiers.new('Subdivision','SUBSURF').levels=1
    material=bpy.data.materials.new('B material');material.use_nodes=True
    node=next((n for n in material.node_tree.nodes if n.type=='BSDF_PRINCIPLED'),None) or material.node_tree.nodes.new('ShaderNodeBsdfPrincipled')
    output=next((n for n in material.node_tree.nodes if n.type=='OUTPUT_MATERIAL'),None) or material.node_tree.nodes.new('ShaderNodeOutputMaterial')
    material.node_tree.links.new(node.outputs['BSDF'],output.inputs['Surface'])
    node.inputs['Base Color'].default_value=(.2,.4,.6,1)
    B.data.materials.append(material)
    bpy.ops.mesh.primitive_cube_add(location=(5,0,0));C=bpy.context.object;C.name='C'
    bpy.ops.mesh.primitive_cube_add(location=(8,0,0));D=bpy.context.object;D.name='D'
    bpy.context.view_layer.update();protected=[A,B,C,D];baseline={o.name:object_state(o) for o in protected}
    initial_count=len(source.objects)
    select(B);A.select_set(True);C.select_set(True)
    assert generated_target(bpy.context) is None and local_target(bpy.context)==B
    first=prepare_processing_source(bpy.context,B)
    raw=runtime.service.candidate_local_path(*first).read_bytes();data=glb(raw)
    assert len(data['meshes'])==1
    assert all(n.get('name') not in {'A','C','D'} for n in data['nodes'])
    assert 'materials' in data
    positions=data['meshes'][0]['primitives'][0]['attributes']['POSITION']
    assert data['accessors'][positions]['count']>8, 'Subdivision must be baked into the upload'
    assert all(object_state(o)==baseline[o.name] for o in protected)
    checks.extend(['local snapshot contains only active B despite multiple selection','evaluated modifiers and materials exported','source objects unchanged by snapshot'])
    p=source.meshdock;p.process_operation='retopology';p.process_provider='tripo_global';p.process_face_limit=1000
    # Freeze a confirmation target B, then switch to C before execution.
    op=SimpleNamespace(report=lambda *args:None,fail=lambda exc:(_ for _ in ()).throw(exc))
    AI3D_OT_process_candidate.capture(op,bpy.context)
    select(C)
    assert AI3D_OT_process_candidate.execute(op,bpy.context)=={'FINISHED'}
    globals()['process_job']=source.meshdock.last_job_id
    globals()['task_id']=source.meshdock.last_process_task_id
    globals()['deadline']=time.monotonic()+20
    other=bpy.data.scenes.new('Other scene while waiting');w.scene=other
    schedule(wait_result)

def wait_result():
    task=runtime.service.get_process_status(process_job,task_id)
    if task['state'] in {'queued','processing'}:
        assert time.monotonic()<deadline,task
        schedule(wait_result,.3);return
    assert task['state']=='completed',task
    runtime.drain_auto_imports()
    assert not w.scene.objects
    w.scene=source
    collection=candidate_collection(process_job,task['result_candidate_id'])
    assert collection and all(o.name in source.objects for o in collection.all_objects)
    assert len(source.objects)==initial_count+1, 'The source snapshot must not be imported again'
    assert all(object_state(o)==baseline[o.name] for o in protected)
    uploaded=glb(http.uploads[0]);assert len(uploaded['meshes'])==1
    assert '自建 B' in json.dumps(uploaded,ensure_ascii=False)
    checks.extend(['confirmation stays locked to B after selection changes','processing result returns to original scene','ACD and original B remain unchanged','only derived result imported'])
    # Delete derived result and request again: completion tracking must not resurrect it.
    for o in list(collection.all_objects): bpy.data.objects.remove(o,do_unlink=True)
    bpy.data.collections.remove(collection)
    count=len(source.objects);runtime.request_process_result(process_job,task_id);runtime.drain_auto_imports()
    assert len(source.objects)==count
    checks.append('deleted processed result is not resurrected')
    global job
    job=runtime.service.create_job({'asset_name':'three_results','prompt':'Three test models','provider':'mock','candidate_count':3})
    own_job(source,job['id']);runtime.service.generate_candidates(job['id'])
    globals()['generation_deadline']=time.monotonic()+20
    schedule(generated,1)

def generated():
    global target,ids,other_states
    status=runtime.service.get_job(job['id'])
    if status['state'] in {'queued','submitted','processing'}:
        assert time.monotonic()<generation_deadline,status
        schedule(generated,.3);return
    assert status['state']=='candidates_ready',status
    runtime.import_all_models(job['id'])
    candidates=runtime.service.get_job(job['id'])['candidates'];ids=(job['id'],candidates[1]['id'])
    col=candidate_collection(*ids);target=next(o for o in col.all_objects if o.type=='MESH');select(target)
    other_states={o.name:object_state(o) for i in [0,2] for o in candidate_collection(job['id'],candidates[i]['id']).all_objects if o.type=='MESH'}
    assert col.get('meshdock_baseline_glb'), 'Initial import must record a baseline'
    assert prepare_processing_source(bpy.context,target,ids)==ids, 'Unchanged result should reuse original source'
    checks.append('one of three untouched results reuses its own source')
    before=target.data.vertices[0].co.copy();target.data.vertices[0].co.z+=.25
    modified=prepare_processing_source(bpy.context,target,ids)
    assert modified!=ids
    assert runtime.service.get_job(modified[0])['candidates'][0]['provider']=='local'
    assert len(glb(runtime.service.candidate_local_path(*modified).read_bytes())['meshes'])==1
    checks.append('edited generated mesh becomes a fresh upload')
    target.data.vertices[0].co=before
    material=bpy.data.materials.new('Edited appearance');material.diffuse_color=(.7,.1,.2,1);target.data.materials.append(material)
    assert prepare_processing_source(bpy.context,target,ids)!=ids
    target.data.materials.clear()
    target.location.y+=1
    assert prepare_processing_source(bpy.context,target,ids)!=ids
    target.location.y-=1
    checks.extend(['material edits trigger fresh upload','transform edits trigger fresh upload'])
    modifier=target.modifiers.new('Disabled subdivision','SUBSURF');modifier.show_viewport=False
    capture_baseline(bpy.context,col)
    assert prepare_processing_source(bpy.context,target,ids)==ids
    modifier.levels=3
    assert prepare_processing_source(bpy.context,target,ids)!=ids
    checks.append('modifier settings trigger upload even if exported shape is unchanged')
    part=target.copy();part.data=target.data.copy();part.name='Second part in result two';col.objects.link(part)
    bpy.context.view_layer.update()
    multipart=prepare_processing_source(bpy.context,target,ids)
    assert len(glb(runtime.service.candidate_local_path(*multipart).read_bytes())['meshes'])==2
    checks.append('modified multipart result uploads all its parts only')
    del col['meshdock_baseline_glb']
    assert prepare_processing_source(bpy.context,target,ids)!=ids
    checks.append('legacy model without baseline uploads conservatively')
    assert all(object_state(bpy.data.objects[name])==value for name,value in other_states.items())
    checks.append('other two generated results are unchanged')
    select(B)
    bpy.ops.meshdock.open_ai_workspace()
    schedule(interface,2)

def interface():
    from meshdock.blender import workbench
    if w.as_pointer() not in workbench._sessions:
        schedule(interface,1);return
    session=workbench._sessions[w.as_pointer()]
    session.dispatch(('category','RETOPOLOGY'),bpy.context)
    schedule(final,1)

def final():
    from meshdock.blender import workbench
    session=workbench._sessions[w.as_pointer()]
    assert not workbench._draw_errors,workbench._draw_errors
    assert any(h[4]==('job','process_candidate') for h in session.hits)
    bpy.ops.screen.screenshot(filepath=str(OUT/'local-retopology.png'))
    checks.append('local selected mesh has working processing UI')
    meshdock.unregister()
    (OUT/'result.json').write_text(json.dumps({'ok':True,'checks':checks}),encoding='utf-8')
    bpy.ops.wm.quit_blender()

schedule(start)
