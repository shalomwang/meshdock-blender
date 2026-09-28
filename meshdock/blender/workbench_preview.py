"""Scene isolation, framing, and real model thumbnails for the AI workbench."""
from pathlib import Path
import hashlib
import math
import bpy
from mathutils import Vector, Quaternion
from .runtime import get_runtime

TAG = 'meshdock_preview'
_images = {}
_pending = []
_failed = set()


def is_preview(scene):
    return bool(scene and scene.get(TAG))


def preview_scene(source):
    name = source.get('meshdock_preview_scene', '')
    scene = bpy.data.scenes.get(name)
    if scene and is_preview(scene):
        return scene
    scene = bpy.data.scenes.new('Mesh Dock · Preview')
    scene[TAG] = True
    scene['meshdock_source'] = source.name
    scene['meshdock_jobs'] = []
    source['meshdock_preview_scene'] = scene.name
    # Copy explicit settings, including managed reference collection properties.
    for key, value in source.meshdock.items():
        try:
            scene.meshdock[key] = value.to_dict() if hasattr(value, 'to_dict') else value
        except (TypeError, ValueError):
            pass
    scene.meshdock.reference_images.clear()
    for original in source.meshdock.reference_images:
        try:
            store = get_runtime().service.references
            managed = store.get(original.reference_id)
            record = store.register(Path(managed.local_path), original.view)
            item = scene.meshdock.reference_images.add()
            item.reference_id = record['id']
            for field in ('view', 'filename', 'format', 'width', 'height', 'bytes'):
                setattr(item, field, record[field])
            item.dimensions = f"{record['width']} × {record['height']}"
            item.preview_image = bpy.data.images.load(store.get(record['id']).local_path, check_existing=True)
        except Exception:
            continue  # Missing references from old sessions remain untouched in the source scene.
    scene.world = bpy.data.worlds.new('Mesh Dock · Studio')
    scene.world.color = (0.16, 0.16, 0.16)
    return scene


def scene_for_job(job_id):
    return next((s for s in bpy.data.scenes if job_id in s.get('meshdock_jobs', [])), None)


def own_job(scene, job_id):
    # Bind asynchronous imports to the scene where generation was requested.
    get_runtime()._preview_job_ids.add(job_id)
    if scene_for_job(job_id) is None:
        scene['meshdock_jobs'] = list(dict.fromkeys([*scene.get('meshdock_jobs', []), job_id]))


def candidate_collection(job_id, candidate_id):
    job = get_runtime().service.get_job(job_id)
    candidate = next(c for c in job['candidates'] if c['id'] == candidate_id)
    return next((c for c in bpy.data.collections if c.get('meshdock_job_id')==job_id and c.get('meshdock_candidate_id')==candidate_id),None) or bpy.data.collections.get(candidate.get('imported_collection') or '')


def bounds(objects):
    corners = [o.matrix_world @ Vector(p) for o in objects
               if o.type in {'MESH', 'CURVE', 'SURFACE', 'FONT'} for p in o.bound_box]
    if not corners:
        return Vector((0, 0, 0)), 1.0
    low = Vector(tuple(min(p[i] for p in corners) for i in range(3)))
    high = Vector(tuple(max(p[i] for p in corners) for i in range(3)))
    return (low + high) / 2, max((high - low).length / 2, 0.01)


def focus(context, collection, reset=True):
    center, radius = bounds(collection.all_objects)
    for area in context.screen.areas:
        if area.type != 'VIEW_3D':
            continue
        space = area.spaces.active
        space.region_3d.view_location = center
        region = next((r for r in area.regions if r.type == 'WINDOW'), None)
        aspect = (region.width / max(region.height, 1)) if region else 1.5
        # Leave room for the two overlay sidebars, including smaller displays.
        space.region_3d.view_distance = radius * max(3.6, 5.6 / aspect)
        if reset:
            space.region_3d.view_rotation = Vector((1.1, -1.8, 0.85)).to_track_quat('Z', 'Y')
            space.region_3d.view_perspective = 'PERSP'
        space.clip_start = max(radius / 10000, 0.0001)
        space.clip_end = max(radius * 200, 1000)
        area.tag_redraw()


def select_model(context, job_id, candidate_id, frame=True):
    service = get_runtime().service
    own_job(context.scene, job_id)
    props = context.scene.meshdock
    props.selected_job_key = job_id
    props.last_job_id = job_id
    props.candidate_id = candidate_id
    collection = candidate_collection(job_id, candidate_id)
    if collection is None:
        service.import_candidate(job_id, candidate_id)
        collection = candidate_collection(job_id, candidate_id)
        from .process_target import capture_baseline
        capture_baseline(context,collection)
    # A prior-session result may exist in another scene. Link only for preview.
    if collection.name not in {c.name for c in context.scene.collection.children}:
        if not any(o.name in context.scene.objects for o in collection.all_objects):
            context.scene.collection.children.link(collection)
    if context.object and context.object.mode!='OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for obj in context.selected_objects:
        obj.select_set(False)
    selectable=[o for o in collection.all_objects if o.name in context.view_layer.objects and not o.hide_get()]
    for obj in selectable:
        obj.select_set(True)
    if selectable:
        context.view_layer.objects.active=next((o for o in selectable if o.type=='MESH'),selectable[0])
    context.view_layer.update()
    if frame:
        focus(context, collection)
    props['workbench_selected_key'] = job_id + '/' + candidate_id
    request_thumbnail(job_id, candidate_id)
    return collection


def add_to_source(context, job_id, candidate_id):
    if not is_preview(context.scene):
        raise ValueError('Open the AI workspace first')
    source = bpy.data.scenes.get(context.scene.get('meshdock_source', ''))
    if source is None:
        raise ValueError('The destination scene no longer exists')
    collection = candidate_collection(job_id, candidate_id)
    if collection is None:
        collection = select_model(context, job_id, candidate_id)
    copied = bpy.data.collections.new(collection.name + ' · Mesh Dock')
    source.collection.children.link(copied)
    mapping = {}
    materials = {}
    for obj in collection.all_objects:
        new = obj.copy()
        if obj.data is not None:
            new.data = obj.data.copy()
        if new.type == 'MESH':
            for index, material in enumerate(new.data.materials):
                if material:
                    if material not in materials:
                        materials[material] = material.copy()
                    new.data.materials[index] = materials[material]
        if new.animation_data and new.animation_data.action:
            new.animation_data.action = new.animation_data.action.copy()
        copied.objects.link(new)
        mapping[obj] = new
    for obj, new in mapping.items():
        new.parent = mapping.get(obj.parent)
        new.matrix_world = obj.matrix_world.copy()
        for modifier in new.modifiers:
            if hasattr(modifier, 'object') and modifier.object in mapping:
                modifier.object = mapping[modifier.object]
        constraints = list(new.constraints)
        if new.type == 'ARMATURE' and new.pose:
            constraints.extend(c for bone in new.pose.bones for c in bone.constraints)
        for constraint in constraints:
            if hasattr(constraint, 'target') and constraint.target in mapping:
                constraint.target = mapping[constraint.target]
    copied['meshdock_job'] = job_id
    copied['meshdock_candidate'] = candidate_id
    return source, copied


def thumbnail_path(job_id, candidate_id):
    service = get_runtime().service
    provider = service.candidate_preview_path(job_id, candidate_id)
    if provider:
        return provider
    directory = service.staging.job_dir(job_id) / 'thumbnails'
    directory.mkdir(exist_ok=True)
    filename = hashlib.sha256(candidate_id.encode('utf-8')).hexdigest() + '.png'
    return directory / filename


def get_thumbnail(job_id, candidate_id):
    key = job_id + '/' + candidate_id
    if key in _images:
        return _images[key]
    path = thumbnail_path(job_id, candidate_id)
    if path.is_file():
        try:
            image = bpy.data.images.load(str(path), check_existing=True)
            _images[key] = image
            return image
        except RuntimeError:
            _failed.add(key)
    request_thumbnail(job_id, candidate_id)
    return None


def request_thumbnail(job_id, candidate_id):
    item = (job_id, candidate_id)
    key = job_id + '/' + candidate_id
    if key not in _failed and key not in _images and item not in _pending:
        _pending.append(item)


def render_thumbnail(job_id, candidate_id):
    """Render actual geometry in a temporary scene; never alter the user's render setup."""
    collection = candidate_collection(job_id, candidate_id)
    if collection is None:
        return False
    scene = bpy.data.scenes.new('Mesh Dock · Thumbnail')
    camera_data = bpy.data.cameras.new('Mesh Dock · Thumbnail Camera')
    camera = bpy.data.objects.new('Mesh Dock · Thumbnail Camera', camera_data)
    try:
        scene.collection.children.link(collection)
        scene.collection.objects.link(camera)
        scene.camera = camera
        center, radius = bounds(collection.all_objects)
        direction = Vector((1.1, -1.8, 0.85)).normalized()
        camera.location = center + direction * radius * 3.5
        camera.rotation_euler = (center-camera.location).to_track_quat('-Z','Y').to_euler()
        camera_data.type = 'ORTHO'
        camera_data.ortho_scale = radius * 2.5
        camera_data.clip_end = max(radius * 20, 1000)
        scene.render.engine = 'BLENDER_WORKBENCH'
        scene.display.shading.light = 'STUDIO'
        scene.display.shading.color_type = 'MATERIAL'
        scene.display.shading.show_shadows = True
        scene.display.shading.show_cavity = True
        scene.display.shading.background_type = 'WORLD'
        scene.render.film_transparent = True
        scene.render.resolution_x = scene.render.resolution_y = 256
        scene.render.resolution_percentage = 100
        scene.render.image_settings.file_format = 'PNG'
        scene.render.filepath = str(thumbnail_path(job_id, candidate_id))
        bpy.ops.render.render(write_still=True, scene=scene.name)
        return True
    finally:
        bpy.data.scenes.remove(scene)
        bpy.data.objects.remove(camera, do_unlink=True)
        bpy.data.cameras.remove(camera_data)


def tick_thumbnails():
    if not _pending:
        return
    job_id, candidate_id = _pending.pop(0)
    try:
        if not thumbnail_path(job_id, candidate_id).is_file():
            if not render_thumbnail(job_id, candidate_id):
                return  # Unimported geometry is retried after selecting its card.
        get_thumbnail(job_id, candidate_id)
    except Exception:
        _failed.add(job_id + '/' + candidate_id)


def clear():
    _images.clear()
    _pending.clear()
    _failed.clear()
