"""Create the AI workbench without modifying the source layout."""
import bpy
from bpy.props import IntProperty
from .runtime import get_runtime


def show_reference(context, index):
    props = context.scene.meshdock
    if not 0 <= index < len(props.reference_images):
        raise ValueError("Select a reference image first")
    reference = get_runtime().service.references.get(props.reference_images[index].reference_id)
    image = bpy.data.images.load(reference.local_path, check_existing=True)
    props.reference_images[index].preview_image = image
    if context.workspace and context.workspace.get("meshdock_workbench"):
        context.scene["meshdock_large_reference"] = index
    for area in context.screen.areas:
        if area.type == "IMAGE_EDITOR":
            area.spaces.active.image = image
    return image


def migrate_legacy_scene(window):
    """Move a pre-0.13 preview back into its source without deleting user data."""
    old=window.scene
    if not old.get('meshdock_preview'):
        return old
    target=bpy.data.scenes.get(old.get('meshdock_source',''))
    if target is None:
        return old
    for key,value in old.meshdock.items():
        if key=='reference_images': continue
        try: target.meshdock[key]=value.to_dict() if hasattr(value,'to_dict') else value
        except (TypeError,ValueError): pass
    target.meshdock.reference_images.clear()
    for ref in old.meshdock.reference_images:
        item=target.meshdock.reference_images.add()
        for field in ('reference_id','view','filename','format','width','height','bytes','dimensions','preview_image'):
            setattr(item,field,getattr(ref,field))
    existing=set(target.collection.children_recursive)
    for collection in old.collection.children:
        if collection not in existing: target.collection.children.link(collection)
    for obj in old.collection.objects:
        if obj.name not in target.objects: target.collection.objects.link(obj)
    target['meshdock_jobs']=list(dict.fromkeys([*target.get('meshdock_jobs',[]),*old.get('meshdock_jobs',[])]))
    old['meshdock_jobs']=[]
    window.scene=target
    return target


class MESHDOCK_OT_open_ai_workspace(bpy.types.Operator):
    bl_idname = "meshdock.open_ai_workspace"
    bl_label = "Open AI Workspace"
    bl_description = "Open generation tools in your current Blender scene"

    @classmethod
    def poll(cls, context):
        return context.window is not None and context.area is not None

    def execute(self, context):
        from .workbench_preview import preview_scene, is_preview
        window = context.window
        source_workspace = context.workspace
        source_scene = migrate_legacy_scene(window)
        existing = bpy.data.workspaces.get("AI")
        if existing is None:
            before = set(bpy.data.workspaces)
            if bpy.ops.workspace.duplicate() != {"FINISHED"}:
                return {"CANCELLED"}
            existing = next((w for w in bpy.data.workspaces if w not in before), None)
            if existing is None:
                return {"CANCELLED"}
            existing.name = "AI"
        if source_workspace == existing:
            source_workspace = next((w for w in bpy.data.workspaces if w != existing), source_workspace)
        existing["meshdock_source_workspace"] = source_workspace.name
        existing["meshdock_workbench"] = True
        existing["meshdock_ready"] = False
        window.workspace = existing
        window.scene = source_scene

        def configure():
            if window not in list(bpy.context.window_manager.windows) or window.workspace != existing:
                return None
            area = max(window.screen.areas, key=lambda a: a.width*a.height)
            area.type = "VIEW_3D"
            def finish():
                if window not in list(bpy.context.window_manager.windows) or window.workspace != existing:
                    return None
                for area in window.screen.areas:
                    if area.type != "VIEW_3D":
                        continue
                    space=area.spaces.active
                    space.show_region_ui=False
                    space.show_region_toolbar=False
                    space.show_region_tool_header=False
                    space.overlay.show_overlays=True
                    space.show_gizmo=True
                    space.shading.background_type="VIEWPORT"
                    space.shading.background_color=(0.012,0.015,0.022)
                existing["meshdock_ready"] = True
                return None
            bpy.app.timers.register(finish, first_interval=0.15)
            return None
        bpy.app.timers.register(configure, first_interval=0.15)
        return {"FINISHED"}


class MESHDOCK_OT_preview_reference(bpy.types.Operator):
    bl_idname = "meshdock.preview_reference"
    bl_label = "Preview Reference"
    index: IntProperty(default=0, min=0)

    def execute(self, context):
        try:
            show_reference(context, self.index)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


CLASSES=(MESHDOCK_OT_open_ai_workspace,MESHDOCK_OT_preview_reference)
