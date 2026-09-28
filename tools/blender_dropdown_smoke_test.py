"""Isolated GUI regression for the current-scene workbench. No provider billing."""
import os,sys,json,traceback,uuid,struct,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'build'/'dropdown-check'
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
    global session
    account=get_runtime().credentials.set('tripo_global','test-only')
    p=source.meshdock;p.generation_account='tripo_global|'+account;p.generation_model='v3.1-20260211'
    bpy.ops.meshdock.open_ai_workspace()
    schedule(prepare,1)

def prepare():
    global session
    session=workbench._sessions[window.as_pointer()]
    session.sections.add('geometry')
    schedule(menu)

def menu():
    click(('field','tripo_geometry_quality'))
    schedule(capture,1)

def capture():
    bpy.ops.screen.screenshot(filepath=str(OUT/'dropdown.png'))
    h=next(h for h in session.hits if h[4]==('field','tripo_geometry_quality'))
    r=bpy.context.region
    x=r.x+(h[0]+h[2]/2)*session.scale
    y=r.y+r.height-(h[1]+h[3]/2+session.inset)*session.scale-22
    print('MENU_CLICK',x,y,window.height,session.scale,flush=True)
    source['click_xy']=[x,y]
    event('MOUSEMOVE','NOTHING',x,y)
    schedule(press_option,.4)

def press_option():
    x,y=source['click_xy']
    event('LEFTMOUSE','PRESS',x,y)
    schedule(release_option,.2)

def release_option():
    x,y=source['click_xy']
    event('LEFTMOUSE','RELEASE',x,y)
    schedule(selected)

def selected():
    bpy.ops.screen.screenshot(filepath=str(OUT/'after-click.png'))
    assert source.meshdock.tripo_geometry_quality=='detailed',source.meshdock.tripo_geometry_quality
    assert not session.notice,session.notice
    click(('field','generation_model'))
    schedule(dynamic)

def dynamic():
    bpy.ops.screen.screenshot(filepath=str(OUT/'model-dropdown.png'))
    event('ESC');schedule(finish)

def finish():
    assert not workbench._draw_errors
    assert not session.notice,session.notice
    (OUT/'result.json').write_text('{"ok": true, "selected": "detailed", "dynamic_menu": true}')
    meshdock.unregister();bpy.ops.wm.quit_blender()

schedule(start)
