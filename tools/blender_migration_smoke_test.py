import os,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
os.environ['MESHDOCK_STAGING']=str(ROOT/'build/migration-smoke/staging')
os.environ['MESHDOCK_BRIDGE_DESCRIPTOR']=str(ROOT/'build/migration-smoke/bridge.json')
os.environ['MESHDOCK_DEV_MOCK']='1'
import bpy,meshdock
meshdock.register()
from meshdock.blender.workspace import migrate_legacy_scene
from meshdock.blender.workbench_preview import scene_for_job
w=bpy.context.window_manager.windows[0];source=w.scene
old=bpy.data.scenes.new('Legacy preview');old['meshdock_preview']=True;old['meshdock_source']=source.name
old['meshdock_jobs']=['migration_fixture'];old.meshdock.prompt='Keep this input'
ref=old.meshdock.reference_images.add();ref.reference_id='fixture';ref.view='front';ref.filename='fixture.png'
ref.preview_image=bpy.data.images.new('Fixture image',width=4,height=4)
col=bpy.data.collections.new('Legacy results');old.collection.children.link(col)
obj=bpy.data.objects.new('Legacy model',bpy.data.meshes.new('Legacy mesh'));col.objects.link(obj)
w.scene=old
assert migrate_legacy_scene(w)==source
assert source.meshdock.prompt=='Keep this input'
assert source.meshdock.reference_images[0].preview_image
assert obj.name in source.objects and obj.name in old.objects
assert scene_for_job('migration_fixture')==source
migrate_legacy_scene(w)
assert len(source.meshdock.reference_images)==1
meshdock.unregister()
print('MIGRATION_OK')
