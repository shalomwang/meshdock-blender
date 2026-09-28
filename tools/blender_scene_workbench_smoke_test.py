"""Isolated GUI regression for the current-scene workbench. No provider billing."""
import os,sys,json,traceback,uuid,struct,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'build'/('scene-workbench-smoke-small' if '--compact' in sys.argv else 'scene-workbench-smoke')
OUT.mkdir(parents=True,exist_ok=True)
(OUT/'result.json').write_text('{"status":"running"}')
os.environ['MESHDOCK_STAGING']=str(OUT/'staging'/uuid.uuid4().hex)
os.environ['MESHDOCK_BRIDGE_DESCRIPTOR']=str(OUT/'bridge.json')
os.environ['MESHDOCK_DEV_MOCK']='1'
sys.path.insert(0,str(ROOT))
import bpy
import meshdock
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
    bpy.context.preferences.view.language='zh_HANS'
    bpy.context.preferences.view.use_translate_interface=True
    assert bpy.ops.meshdock.open_ai_workspace()=={'FINISHED'}
    schedule(setup,2)

def setup():
    global session,job
    assert window.scene==source and not preview.is_preview(window.scene)
    assert [a.type for a in source_workspace.screens[0].areas]==original_layout
    assert 'OUTLINER' in [a.type for a in window.screen.areas]
    session=workbench._sessions[window.as_pointer()]
    assert not workbench._draw_errors,workbench._draw_errors
    checks.extend(['workspace keeps current scene','original layout and Outliner preserved'])
    p=source.meshdock
    account=get_runtime().credentials.set('tripo_global','ui-test-only',note='Test fixture')
    p.input_mode='image';p.generation_account='tripo_global|'+account
    im=bpy.data.images.new('Reference fixture',width=256,height=256)
    im.generated_color=(.18,.38,.65,1);im.filepath_raw=str(OUT/'reference.png');im.file_format='PNG';im.save()
    bpy.ops.meshdock.add_reference_image(filepath=str(OUT/'reference.png'),view='front')
    job=get_runtime().service.create_job({'asset_name':'scene_fixture','prompt':'Test','provider':'mock','candidate_count':2})
    preview.own_job(source,job['id']);p.last_job_id=job['id']
    get_runtime().service.generate_candidates(job['id'])
    # Completing while the user is editing must wait, then import to its bound scene.
    bpy.context.view_layer.objects.active=source.objects['Cube'];source.objects['Cube'].select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')
    get_runtime().request_auto_import(job['id'])
    schedule(edit_wait,1)

def edit_wait():
    assert get_runtime().auto_import_status(job['id'])=='pending'
    assert len(source.objects)==len(original)
    bpy.ops.object.mode_set(mode='OBJECT')
    other=bpy.data.scenes.new('Other scene')
    window.scene=other
    schedule(imported,2)

def imported():
    assert len(window.scene.objects)==0
    assert len(source.objects)>len(original)
    assert get_runtime().auto_import_status(job['id'])!='failed',get_runtime()._auto_import_errors
    window.scene=source
    for name,(_,hidden) in original.items(): assert source.objects[name].hide_get()==hidden
    checks.extend(['auto import targets originating scene after scene switch','imports wait for edit mode','existing objects stay visible'])
    p=source.meshdock
    current=get_runtime().service.get_job(job['id'])
    preview.select_model(bpy.context,job['id'],current['candidates'][0]['id'])
    session.refresh(bpy.context)
    session.category='HISTORY'
    schedule(cards,2)

def cards():
    assert not workbench._draw_errors,workbench._draw_errors
    bpy.ops.screen.screenshot(filepath=str(OUT/'01-scene-workbench.png'))
    assert not any(h[4][0] in {'add','back','native','export','shading','spin','frame'} for h in session.hits)
    second=get_runtime().service.get_job(job['id'])['candidates'][1]['id']
    click(('select',job['id'],second))
    schedule(selected)

def selected():
    second=get_runtime().service.get_job(job['id'])['candidates'][1]['id']
    assert source.meshdock.candidate_id==second
    collection=preview.candidate_collection(job['id'],second)
    assert bpy.context.active_object.name in collection.all_objects
    assert all(source.objects[n].hide_get()==hidden for n,(_,hidden) in original.items())
    checks.extend(['history click selects actual scene object','removed redundant header and footer controls'])
    click(('select',job['id'],second),'RIGHTMOUSE')
    schedule(menu)

def menu():
    bpy.ops.screen.screenshot(filepath=str(OUT/'02-history-menu.png'))
    event('ESC');event('ESC','RELEASE')
    schedule(native)

def native():
    event('TAB');event('TAB','RELEASE')
    schedule(native_checked)

def native_checked():
    assert bpy.context.active_object.mode=='EDIT'
    import bmesh
    mesh=bmesh.from_edit_mesh(bpy.context.active_object.data)
    mesh.verts.ensure_lookup_table();mesh.verts[0].co.z+=.25
    bmesh.update_edit_mesh(bpy.context.active_object.data)
    event('TAB');event('TAB','RELEASE')
    checks.append('native Tab and mesh editing work without expanded mode')
    schedule(exported)

def exported():
    source.meshdock.export_directory=str(OUT/'exports')
    assert bpy.ops.meshdock.workbench_export('EXEC_DEFAULT')=={'FINISHED'}
    glb=max((OUT/'exports').rglob('*.glb'),key=lambda p:p.stat().st_mtime)
    raw=glb.read_bytes();length=struct.unpack_from('<I',raw,12)[0];data=json.loads(raw[20:20+length])
    collection=preview.candidate_collection(job['id'],source.meshdock.candidate_id)
    assert len(data['meshes'])==len([o for o in collection.all_objects if o.type=='MESH'])
    checks.append('context export contains selected generated result only')
    session.category='GENERATE'
    event('T');event('T','RELEASE')
    schedule(native_tools)

def native_tools():
    assert bpy.context.space_data.show_region_toolbar
    assert not session.hits, 'Workbench must yield to native tools'
    event('T');event('T','RELEASE')
    checks.append('native toolbar takes precedence without overlap')
    schedule(category)

def category():
    click(('category','RETOPOLOGY'))
    schedule(category_checked)

def category_checked():
    assert session.category=='RETOPOLOGY'
    checks.append('category navigation changes inline content')
    bpy.ops.screen.screenshot(filepath=str(OUT/'03-retopology.png'))
    # Draw supported retopology controls with mocked capabilities, never submit a task.
    service=get_runtime().service
    saved=service.available_process_operations
    service.available_process_operations=lambda *args,**kwargs:{'operations':{'retopology':{'providers':['tripo_global'],'defaults':{'face_limit':5000}}}}
    source.meshdock.process_operation='retopology'
    window.scene['test_saved_capabilities']=False
    globals()['saved_capabilities']=saved
    schedule(retopo_controls)

def retopo_controls():
    assert any(h[4]==('field','process_face_limit') for h in session.hits)
    bpy.ops.screen.screenshot(filepath=str(OUT/'04-retopology-controls.png'))
    get_runtime().service.available_process_operations=saved_capabilities
    # An unrelated scene mesh cannot process the last generated candidate.
    bpy.ops.object.select_all(action='DESELECT');source.objects['Cube'].select_set(True)
    bpy.context.view_layer.objects.active=source.objects['Cube']
    session.refresh(bpy.context)
    schedule(unrelated)

def unrelated():
    from meshdock.blender.process_target import generated_target,local_target
    assert generated_target(bpy.context) is None and local_target(bpy.context)==source.objects['Cube']
    checks.append('ordinary scene object resolves to its own local processing target')
    click(('category','GENERATE'))
    schedule(generation)

def generation():
    click(('mode','multiview'))
    schedule(views)

def views():
    assert {h[4][1] for h in session.hits if h[4][0]=='upload'}=={'front','back','left','right'}
    bpy.ops.screen.screenshot(filepath=str(OUT/'05-multiview.png'))
    click(('mode','text'));schedule(text_input)

def text_input():
    assert any(h[4]==('options','INPUT') for h in session.hits)
    click(('mode','image'));schedule(drafts)

def drafts():
    assert source.meshdock.reference_images
    # Inline settings can be exposed by scrolling to their section.
    session.left_scroll=220
    schedule(settings)

def settings():
    session.dispatch(('section','geometry'),bpy.context)
    schedule(settings_checked)

def settings_checked():
    assert 'geometry' in session.sections
    checks.extend(['mode buttons and image drafts work','generation settings expand inline'])
    bpy.ops.screen.screenshot(filepath=str(OUT/'06-generation-settings.png'))
    # Explicit user deletion must not be undone by refresh.
    candidate=get_runtime().service.get_job(job['id'])['candidates'][1]['id']
    col=preview.candidate_collection(job['id'],candidate)
    for o in list(col.all_objects): bpy.data.objects.remove(o,do_unlink=True)
    bpy.data.collections.remove(col)
    count=len(source.objects)
    session.refresh(bpy.context);session.refresh(bpy.context)
    assert len(source.objects)==count
    checks.append('deleted model is not reimported by refresh')
    for name,(matrix,hidden) in original.items():
        assert tuple(tuple(row) for row in source.objects[name].matrix_world)==matrix
        assert source.objects[name].hide_get()==hidden
    checks.append('existing transforms and visibility preserved')
    session.left_scroll=0;session.sections.clear()
    bpy.ops.wm.save_as_mainfile(filepath=str(OUT/'workbench.blend'))
    bpy.app.handlers.load_post.append(loaded)
    bpy.ops.wm.open_mainfile(filepath=str(OUT/'workbench.blend'))

@bpy.app.handlers.persistent
def loaded(_):
    global window
    window=bpy.context.window_manager.windows[0]
    bpy.app.handlers.load_post.remove(loaded)
    schedule(reopened,2)

def reopened():
    assert not preview.is_preview(window.scene)
    assert not workbench._draw_errors,workbench._draw_errors
    assert window.scene.meshdock.reference_images
    assert workbench._sessions
    checks.append('file reopens in current scene with working controls')
    meshdock.unregister()
    assert not workbench._sessions and workbench._handler is None
    checks.append('clean addon unload')
    (OUT/'result.json').write_text(json.dumps({'ok':True,'checks':checks}),encoding='utf-8')
    bpy.ops.wm.quit_blender()

schedule(start)
