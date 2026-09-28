"""Isolated GUI regression for the current-scene workbench. No provider billing."""
import os,sys,json,traceback,uuid,struct,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'build'/('workflow-smoke-dpi' if '--high-dpi' in sys.argv else 'workflow-smoke')
OUT.mkdir(parents=True,exist_ok=True)
(OUT/'result.json').write_text('{"status":"running"}')
os.environ['MESHDOCK_STAGING']=str(OUT/'staging'/uuid.uuid4().hex)
os.environ['MESHDOCK_BRIDGE_DESCRIPTOR']=str(OUT/'bridge.json')
os.environ['MESHDOCK_DEV_MOCK']='1'
sys.path.insert(0,str(ROOT))
import bpy
import meshdock
if "--high-dpi" in sys.argv: bpy.context.preferences.view.ui_scale=1.5
(OUT/'temp').mkdir(exist_ok=True)
bpy.context.preferences.filepaths.temporary_directory=str(OUT/'temp')
meshdock.register()
from meshdock.blender.runtime import get_runtime
from meshdock.blender import workbench,workbench_preview as preview
window=bpy.context.window_manager.windows[0]
source=window.scene
source_workspace=window.workspace
source.objects['Cube'].location.x=-4
bpy.context.view_layer.update()
original={o.name:(tuple(tuple(r) for r in o.matrix_world),o.hide_get()) for o in source.objects}
original_layout=[a.type for a in window.screen.areas]
checks=[]
job=None
session=None

def override():
    area=next(a for a in window.screen.areas if a.type=='VIEW_3D')
    return bpy.context.temp_override(window=window,area=area,region=next(r for r in area.regions if r.type=='WINDOW'))

def schedule(fn,delay=0.7):
    def guarded():
        try:
            with override(): fn()
        except Exception:
            result={'ok':False,'error':traceback.format_exc(),'draw_errors':workbench._draw_errors}
            (OUT/'result.json').write_text(json.dumps(result),encoding='utf-8')
            try: meshdock.unregister()
            finally: bpy.ops.wm.quit_blender()
    bpy.app.timers.register(guarded,first_interval=delay)

def event(kind,value='PRESS',x=None,y=None,ctrl=False):
    r=bpy.context.region
    window.event_simulate(type=kind,value=value,ctrl=ctrl,x=int(x if x is not None else r.x+r.width*.55),y=int(y if y is not None else r.y+r.height*.5))

def click(action,button='LEFTMOUSE'):
    h=next(h for h in session.hits if h[4]==action)
    r=bpy.context.region
    x=r.x+(h[0]+h[2]/2)*session.scale
    y=r.y+r.height-(h[1]+h[3]/2+session.inset)*session.scale
    event('MOUSEMOVE','NOTHING',x,y);event(button,'PRESS',x,y);event(button,'RELEASE',x,y)

def start():
    global session
    bpy.context.preferences.view.language='zh_HANS'
    assert source.meshdock.prompt==''
    bpy.ops.meshdock.open_ai_workspace()
    schedule(input_checks,1)

def input_checks():
    global session,old_ids
    if window.as_pointer() not in workbench._sessions:
        schedule(input_checks,.5);return
    session=workbench._sessions[window.as_pointer()]
    from meshdock.blender.properties import active_references
    p=source.meshdock;rt=get_runtime()
    account=rt.credentials.set('hunyuan_direct','fixture-only')
    p.generation_account='hunyuan_direct|'+account;p.generation_model='3.1';p.input_mode='multiview'
    im=bpy.data.images.new('test input',width=256,height=256);im.filepath_raw=str(OUT/'view.png');im.file_format='PNG';im.save()
    for view in ['front','top']: bpy.ops.meshdock.add_reference_image(filepath=str(OUT/'view.png'),view=view)
    p.generation_model='3.0'
    assert {r.view for r in p.reference_images}=={'front','top'}
    assert {r.view for r in active_references(p)}=={'front'}
    p.generation_model='3.1'
    assert {r.view for r in active_references(p)}=={'front','top'}
    checks.append('model changes preserve drafts and submit only compatible views')
    # A generated result older than 100 jobs, with its part selected.
    from meshdock.core.models import Candidate,JobState
    job=rt.service.create_job({'asset_name':'old_result','provider':'mock','prompt':'old result'})
    col=bpy.data.collections.new('Old independent result');source.collection.children.link(col)
    obj=source.objects['Cube']
    for owner in list(obj.users_collection): owner.objects.unlink(obj)
    col.objects.link(obj)
    old=rt.service._get_job(job['id']);old.state=JobState.CANDIDATES_READY
    old.candidates.append(Candidate(id='old-candidate',provider='tripo_global',label='old result',local_model_path='',format='glb',imported_collection=col.name))
    old_ids=(old.id,'old-candidate')
    for i in range(110): rt.service.create_job({'asset_name':'history_'+str(i),'provider':'mock','prompt':'filler'})
    from meshdock.blender.process_target import generated_target
    assert generated_target(bpy.context)==old_ids
    col.name='Renamed independent result'
    assert generated_target(bpy.context)==old_ids
    assert preview.candidate_collection(*old_ids)==col
    session.refresh(bpy.context)
    assert p.last_job_id==old.id
    checks.append('old and renamed generated results retain identity beyond 100 jobs')
    account=rt.credentials.set('tripo_global','fixture-only')
    p.generation_account='tripo_global|'+account
    session.dispatch(('category','PROCESS'),bpy.context);p.process_operation='texture'
    schedule(process_checks)

def process_checks():
    region=next(r for r in next(a for a in window.screen.areas if a.type=='VIEW_3D').regions if r.type=='WINDOW')
    nav=[h for h in session.hits if h[4][0]=='category']
    assert len(nav)==6 and all(h[1]>=0 and h[1]+h[3]<=region.height/session.scale for h in nav)
    from meshdock.blender.properties import effective_process_params
    p=source.meshdock
    assert any(h[4]==('field','proc_prompt') for h in session.hits)
    assert not any(h[4]==('field','process_json') for h in session.hits)
    p.proc_prompt='painted wood'
    assert effective_process_params(p)['texture_prompt']=={'text':'painted wood'}
    bpy.ops.screen.screenshot(filepath=str(OUT/'01-process.png'))
    checks.append('friendly process form emits provider parameters without exposing JSON')
    session.dispatch(('category','ANIMATE'),bpy.context)
    schedule(animation_checks)

def animation_checks():
    assert not any(h[4]==('job','preview_character_action') for h in session.hits)
    assert any(h[4]==('process_mode','rig') for h in session.hits)
    bpy.ops.screen.screenshot(filepath=str(OUT/'02-animation.png'))
    checks.append('animation offers rigging next step and disables unavailable playback')
    session.dispatch(('category','RETOPOLOGY'),bpy.context)
    bpy.ops.object.mode_set(mode='EDIT')
    schedule(edit_checks)

def edit_checks():
    from meshdock.blender.workflow import process_blocker
    assert 'Tab' in process_blocker(bpy.context,source.meshdock)
    assert not any(h[4]==('job','process_candidate') for h in session.hits)
    bpy.ops.object.mode_set(mode='OBJECT')
    checks.append('edit mode explains the actual blocker')
    # Read-only verification uses a fake transport; never touches the network.
    from meshdock.blender import account_checks
    rt=get_runtime();adapter=rt.service.providers['tripo_global']
    class Fake:
        def request_json(self,method,url,**kwargs):
            assert method=='GET' and url.endswith('/account/balance')
            return {'code':0,'data':{'balance':42}}
    adapter._http=Fake()
    profile=rt.credentials.list_profiles('tripo_global')[-1]['id']
    account_checks.check(rt,'tripo_global',profile)
    source['check_profile']=profile
    session.dispatch(('category','PLATFORM'),bpy.context)
    schedule(account_checked)

def account_checked():
    from meshdock.blender import account_checks
    assert account_checks.status('tripo_global',source['check_profile'])['state']=='verified'
    bpy.ops.screen.screenshot(filepath=str(OUT/'03-platform.png'))
    checks.append('account check is a read-only GET and reports verified balance')
    source.meshdock.history_search='old result';session.refresh(bpy.context)
    assert session.history_total==1
    session.dispatch(('category','HISTORY'),bpy.context)
    schedule(history_checked)

def history_checked():
    assert any(h[4]==('select',*old_ids) for h in session.hits)
    bpy.ops.screen.screenshot(filepath=str(OUT/'04-history.png'))
    checks.append('search finds an older result and history has one pagination control')
    session.dispatch(('category','GENERATE'),bpy.context)
    source.meshdock.native_controls=True
    bpy.context.space_data.show_region_ui=True
    schedule(native_checked)

def native_checked():
    bpy.ops.screen.screenshot(filepath=str(OUT/'05-native.png'))
    assert not workbench._draw_errors,workbench._draw_errors
    checks.append('native panel renders as an alternative to GPU controls')
    (OUT/'result.json').write_text(json.dumps({'ok':True,'checks':checks}))
    meshdock.unregister();bpy.ops.wm.quit_blender()

schedule(start)
