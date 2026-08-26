from __future__ import annotations

import re
import hashlib
import html
import math
from pathlib import Path
from typing import Any

import bpy
from mathutils import Vector

from ..core.errors import CandidateNotFoundError, ValidationError
from ..core.models import AssetJob


def _pascal_case(value: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", value)
    return "".join(word[:1].upper() + word[1:] for word in words) or "GeneratedAsset"


def _candidate(job: AssetJob, candidate_id: str):
    for item in job.candidates:
        if item.id == candidate_id:
            return item
    raise CandidateNotFoundError("candidate does not exist")


def _ensure_child(parent: bpy.types.Collection, name: str) -> bpy.types.Collection:
    collection = bpy.data.collections.get(name)
    if collection is None:
        collection = bpy.data.collections.new(name)
    if collection.name not in {child.name for child in parent.children}:
        parent.children.link(collection)
    return collection


def import_candidate(job: AssetJob, candidate_id: str) -> str:
    candidate = _candidate(job, candidate_id)
    source = Path(candidate.local_model_path)
    suffix = source.suffix.lower()
    if suffix not in {".obj", ".glb", ".gltf", ".fbx", ".stl", ".usd", ".usdz"} or not source.is_file():
        raise ValidationError("candidate must be a supported staged 3D file")

    staging = bpy.data.collections.get("MeshDock_Staging") or bpy.data.collections.get("AI_Staging")
    if staging is None:
        staging = bpy.data.collections.new("MeshDock_Staging")
        bpy.context.scene.collection.children.link(staging)
    job_collection = _ensure_child(staging, f"Job_{job.id[:8]}_{_pascal_case(job.spec.asset_name)}")
    collection_name = f"Candidate_{job.id[:8]}_{_pascal_case(candidate_id)}"
    candidate_collection = _ensure_child(job_collection, collection_name)

    before = set(bpy.data.objects)
    before_actions = {action.name for action in bpy.data.actions}
    if suffix == ".obj":
        bpy.ops.wm.obj_import(filepath=str(source), forward_axis="NEGATIVE_Z", up_axis="Y")
    elif suffix in {".glb", ".gltf"}:
        bpy.ops.import_scene.gltf(filepath=str(source), import_scene_as_collection=False)
    elif suffix == ".fbx":
        bpy.ops.wm.fbx_import(filepath=str(source))
    elif suffix == ".stl":
        bpy.ops.wm.stl_import(filepath=str(source))
    else:
        bpy.ops.wm.usd_import(filepath=str(source))
    imported = set(bpy.data.objects) - before
    if not imported:
        raise ValidationError("Blender imported no objects from the staged candidate")
    asset_prefix = _pascal_case(job.spec.asset_name)
    for index, obj in enumerate(sorted(imported, key=lambda item: item.name), start=1):
        for owner in list(obj.users_collection):
            owner.objects.unlink(obj)
        candidate_collection.objects.link(obj)
        obj.name = f"{asset_prefix}Candidate{candidate_id.removeprefix('mock_')}_{index:02d}"
        if obj.type == "MESH":
            obj.data.name = f"{obj.name}Mesh"
    candidate_collection["meshdock_action_names"] = sorted(
        action.name for action in bpy.data.actions if action.name not in before_actions
    )
    return candidate_collection.name


def arrange_candidate_collections(job: AssetJob, candidate_ids: list[str]) -> dict[str, Any]:
    """Lay generated models out left-to-right with size-aware spacing."""
    entries: list[tuple[bpy.types.Collection, list[bpy.types.Object], float, float]] = []
    for candidate_id in candidate_ids:
        candidate = _candidate(job, candidate_id)
        collection = bpy.data.collections.get(candidate.imported_collection or "")
        if collection is None:
            continue
        objects = list(collection.all_objects)
        points = [
            obj.matrix_world @ Vector(corner)
            for obj in objects if hasattr(obj, "bound_box")
            for corner in obj.bound_box
        ]
        if not points:
            continue
        minimum = min(point.x for point in points)
        maximum = max(point.x for point in points)
        entries.append((collection, objects, minimum, maximum))
    if not entries:
        return {"arranged": 0, "collections": []}

    widest = max(maximum - minimum for _collection, _objects, minimum, maximum in entries)
    gap = max(0.5, widest * 0.25)
    cursor = 0.0
    names: list[str] = []
    for collection, objects, minimum, maximum in entries:
        width = max(maximum - minimum, 0.01)
        target_center = cursor + width / 2.0
        offset = target_center - (minimum + maximum) / 2.0
        object_set = set(objects)
        roots = [obj for obj in objects if obj.parent not in object_set]
        for obj in roots:
            obj.matrix_world.translation.x += offset
        collection["meshdock_layout_offset_x"] = offset
        names.append(collection.name)
        cursor += width + gap
    bpy.context.view_layer.update()
    return {"arranged": len(names), "collections": names, "gap": gap}


def candidate_actions(job: AssetJob, candidate_id: str) -> list[str]:
    candidate = _candidate(job, candidate_id)
    collection = bpy.data.collections.get(candidate.imported_collection or "")
    if collection is None:
        return []
    stored = collection.get("meshdock_action_names", collection.get("ai3d_action_names", []))
    names = [str(name) for name in stored if bpy.data.actions.get(str(name)) is not None]
    if names:
        return names
    # Older imported candidates did not store an action list. Keep fallback scoped to
    # actions already assigned to armatures in this candidate collection.
    return sorted({
        obj.animation_data.action.name
        for obj in collection.all_objects
        if obj.type == "ARMATURE" and obj.animation_data and obj.animation_data.action
    })


def activate_candidate_action(job: AssetJob, candidate_id: str, action_name: str) -> dict[str, Any]:
    candidate = _candidate(job, candidate_id)
    collection = bpy.data.collections.get(candidate.imported_collection or "")
    if collection is None:
        raise ValidationError("import the character candidate before previewing animation")
    action = bpy.data.actions.get(action_name)
    if action is None or action_name not in candidate_actions(job, candidate_id):
        raise ValidationError("the selected action does not belong to this candidate")
    armatures = [obj for obj in collection.all_objects if obj.type == "ARMATURE"]
    if not armatures:
        raise ValidationError("candidate contains no armature")
    armature = next(
        (obj for obj in armatures if obj.animation_data and obj.animation_data.action == action),
        armatures[0],
    )
    animation_data = armature.animation_data_create()
    animation_data.action = action
    start, end = (int(value) for value in action.frame_range)
    scene = bpy.context.scene
    scene.frame_start = start
    scene.frame_end = max(start, end)
    scene.frame_set(start)
    bpy.ops.object.select_all(action="DESELECT")
    armature.select_set(True)
    bpy.context.view_layer.objects.active = armature
    return {
        "candidate_id": candidate_id,
        "action": action.name,
        "armature": armature.name,
        "frame_start": start,
        "frame_end": max(start, end),
    }


def prepare_game_asset(
    job: AssetJob, candidate_id: str, options: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Create and normalize a non-destructive Blender copy of an imported candidate."""
    settings = {
        "flatten_hierarchy": False,
        "apply_transforms": True,
        "ground": True,
        "target_height": 0.0,
        "ensure_uv": False,
        "smooth": True,
        "triangulate": False,
        "rename": False,
        **dict(options or {}),
    }
    candidate = _candidate(job, candidate_id)
    source = bpy.data.collections.get(candidate.imported_collection or "")
    if source is None:
        raise ValidationError("the imported candidate collection no longer exists")
    if any(obj.type == "ARMATURE" for obj in source.all_objects):
        raise ValidationError("armature assets require the character preparation workflow")
    parent = next((owner for owner in bpy.data.collections if source.name in {child.name for child in owner.children}), None)
    if parent is None:
        parent = bpy.context.scene.collection
    collection = bpy.data.collections.new(f"{source.name}_Normalized")
    parent.children.link(collection)
    object_map: dict[bpy.types.Object, bpy.types.Object] = {}
    for original in source.all_objects:
        duplicate = original.copy()
        if original.data is not None and hasattr(original.data, "copy"):
            duplicate.data = original.data.copy()
        duplicate.animation_data_clear()
        collection.objects.link(duplicate)
        object_map[original] = duplicate
    for original, duplicate in object_map.items():
        duplicate.parent = object_map.get(original.parent)
        duplicate.matrix_world = original.matrix_world.copy()

    objects = list(collection.all_objects)
    meshes = [obj for obj in objects if obj.type == "MESH"]
    if not meshes:
        raise ValidationError("candidate collection contains no meshes")

    scene = bpy.context.scene
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.scale_length = 1.0
    scene.unit_settings.length_unit = "METERS"

    operations = ["duplicate"]
    if settings["flatten_hierarchy"]:
        for obj in objects:
            if obj.parent is not None:
                world = obj.matrix_world.copy()
                obj.parent = None
                obj.matrix_world = world
        operations.append("flatten_hierarchy")

    target_height = float(settings["target_height"])
    if target_height > 0:
        corners = [obj.matrix_world @ Vector(corner) for obj in meshes for corner in obj.bound_box]
        height = max(v.z for v in corners) - min(v.z for v in corners)
        if height > 1e-8:
            factor = target_height / height
            for obj in (item for item in objects if item.parent is None):
                obj.location *= factor
                obj.scale *= factor
            operations.append("target_height")

    if settings["apply_transforms"]:
        bpy.ops.object.select_all(action="DESELECT")
        for obj in meshes:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = meshes[0]
        bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
        operations.append("apply_rotation_scale")

    if settings["ground"]:
        minimum_z = min((obj.matrix_world @ Vector(corner)).z for obj in meshes for corner in obj.bound_box)
        for root in [obj for obj in objects if obj.parent is None]:
            root.location.z -= minimum_z
        operations.append("ground_z0")

    asset_name = _pascal_case(job.spec.asset_name)
    for mesh_index, obj in enumerate(meshes, start=1):
        if settings["rename"]:
            obj.name = f"{asset_name}{mesh_index:02d}"
            obj.data.name = f"{obj.name}Mesh"
        if settings["ensure_uv"]:
            _ensure_uv0(obj)
        if settings["smooth"]:
            for polygon in obj.data.polygons:
                polygon.use_smooth = True
        if settings["triangulate"]:
            triangulate = next((modifier for modifier in obj.modifiers if modifier.type == "TRIANGULATE"), None)
            if triangulate is None:
                triangulate = obj.modifiers.new(name="ExportTriangulate", type="TRIANGULATE")
            if obj.modifiers[-1] != triangulate:
                obj.modifiers.move(obj.modifiers.find(triangulate.name), len(obj.modifiers) - 1)
    if settings["ensure_uv"]:
        operations.append("ensure_uv0")
    if settings["smooth"]:
        operations.append("smooth_shading")
    if settings["triangulate"]:
        operations.append("triangulate_modifier")
    if settings["rename"]:
        operations.append("generic_names")

    return {
        "job_id": job.id,
        "candidate_id": candidate_id,
        "prepared": True,
        "mesh_count": len(meshes),
        "derived_collection": collection.name,
        "operations": operations,
    }


def set_candidate_visibility(job: AssetJob, candidate_id: str | None) -> None:
    for candidate in job.candidates:
        if not candidate.imported_collection:
            continue
        collection = bpy.data.collections.get(candidate.imported_collection)
        if collection is None:
            continue
        visible = candidate_id is None or candidate.id == candidate_id
        collection.hide_viewport = not visible
        collection.hide_render = not visible


def delete_candidate_scene(job: AssetJob, candidate_id: str) -> None:
    candidate = _candidate(job, candidate_id)
    collection = bpy.data.collections.get(candidate.imported_collection or "")
    if collection is None:
        return
    for obj in list(collection.all_objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    bpy.data.collections.remove(collection)


def render_review_pack(
    job: AssetJob, candidate_id: str, destination: Path, *, frames: int = 8
) -> dict[str, Any]:
    candidate = _candidate(job, candidate_id)
    collection = bpy.data.collections.get(candidate.imported_collection or "")
    if collection is None:
        raise ValidationError("the imported candidate collection no longer exists")
    render_objects = [obj for obj in collection.all_objects if obj.type in {"MESH", "CURVE", "SURFACE", "ARMATURE"}]
    meshes = [obj for obj in render_objects if obj.type in {"MESH", "CURVE", "SURFACE"}]
    if not meshes:
        raise ValidationError("candidate contains no renderable geometry")

    corners = [obj.matrix_world @ Vector(corner) for obj in meshes for corner in obj.bound_box]
    minimum = Vector((min(v.x for v in corners), min(v.y for v in corners), min(v.z for v in corners)))
    maximum = Vector((max(v.x for v in corners), max(v.y for v in corners), max(v.z for v in corners)))
    center = (minimum + maximum) * 0.5
    radius = max((maximum - minimum).length * 0.65, 0.5)
    destination.mkdir(parents=True, exist_ok=True)

    scene = bpy.context.scene
    render = scene.render
    previous = {
        "engine": scene.render.engine,
        "resolution_x": render.resolution_x,
        "resolution_y": render.resolution_y,
        "resolution_percentage": render.resolution_percentage,
        "filepath": render.filepath,
        "file_format": render.image_settings.file_format,
        "film_transparent": render.film_transparent,
        "camera": scene.camera,
    }
    hidden = {obj: obj.hide_render for obj in bpy.data.objects}
    temporary_collection = bpy.data.collections.new(f"MeshDock_Review_{job.id[:8]}")
    scene.collection.children.link(temporary_collection)
    created_objects: list[bpy.types.Object] = []
    created_data: list[Any] = []
    files: list[str] = []
    try:
        for obj in bpy.data.objects:
            obj.hide_render = obj not in render_objects

        camera_data = bpy.data.cameras.new("MeshDock_ReviewCamera")
        camera = bpy.data.objects.new("MeshDock_ReviewCamera", camera_data)
        temporary_collection.objects.link(camera)
        created_objects.append(camera)
        created_data.append(camera_data)
        camera_data.lens = 52
        scene.camera = camera

        for name, energy, size, position in (
            ("Key", 1100.0, radius * 3.0, center + Vector((radius * 2.5, -radius * 3.0, radius * 3.0))),
            ("Fill", 650.0, radius * 2.5, center + Vector((-radius * 2.5, -radius * 1.5, radius * 1.5))),
        ):
            light_data = bpy.data.lights.new(f"MeshDock_Review{name}", type="AREA")
            light_data.energy = energy
            light_data.shape = "DISK"
            light_data.size = max(size, 1.0)
            light = bpy.data.objects.new(f"MeshDock_Review{name}", light_data)
            light.location = position
            light.rotation_euler = (center - light.location).to_track_quat("-Z", "Y").to_euler()
            temporary_collection.objects.link(light)
            created_objects.append(light)
            created_data.append(light_data)

        render.resolution_x = 640
        render.resolution_y = 640
        render.resolution_percentage = 100
        render.image_settings.file_format = "PNG"
        render.film_transparent = False
        distance = radius * 3.2
        elevation = math.radians(18.0)
        for index in range(frames):
            angle = math.tau * index / frames
            camera.location = center + Vector((
                math.sin(angle) * distance * math.cos(elevation),
                -math.cos(angle) * distance * math.cos(elevation),
                distance * math.sin(elevation),
            ))
            camera.rotation_euler = (center - camera.location).to_track_quat("-Z", "Y").to_euler()
            filename = f"view_{index + 1:02d}.png"
            render.filepath = str(destination / filename)
            bpy.ops.render.render(write_still=True)
            files.append(filename)

        index_name = "review_sheet.html"
        cards = "\n".join(
            f'<figure><img src="{html.escape(filename)}"><figcaption>{i * 360 / frames:.0f}°</figcaption></figure>'
            for i, filename in enumerate(files)
        )
        document = f"""<!doctype html><meta charset=\"utf-8\"><title>{html.escape(job.spec.asset_name)}</title>
<style>body{{background:#17191d;color:#eee;font:14px system-ui;margin:24px}}main{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}}figure{{margin:0;background:#262a30;padding:8px}}img{{width:100%;display:block}}figcaption{{padding-top:6px;text-align:center}}</style>
<h1>{html.escape(job.spec.asset_name)} · {html.escape(candidate.label)}</h1><main>{cards}</main>"""
        (destination / index_name).write_text(document, encoding="utf-8")
        raw = (destination / index_name).read_bytes()
        return {
            "filename": index_name,
            "format": "review_pack",
            "candidate_id": candidate_id,
            "frames": files,
            "bytes": len(raw) + sum((destination / name).stat().st_size for name in files),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "staging_only": True,
        }
    finally:
        for obj, was_hidden in hidden.items():
            if obj.name in bpy.data.objects:
                obj.hide_render = was_hidden
        for obj in created_objects:
            if obj.name in bpy.data.objects:
                bpy.data.objects.remove(obj, do_unlink=True)
        for data in created_data:
            if isinstance(data, bpy.types.Camera) and data.name in bpy.data.cameras:
                bpy.data.cameras.remove(data)
            elif isinstance(data, bpy.types.Light) and data.name in bpy.data.lights:
                bpy.data.lights.remove(data)
        if temporary_collection.name in bpy.data.collections:
            bpy.data.collections.remove(temporary_collection)
        scene.render.engine = previous["engine"]
        render.resolution_x = previous["resolution_x"]
        render.resolution_y = previous["resolution_y"]
        render.resolution_percentage = previous["resolution_percentage"]
        render.filepath = previous["filepath"]
        render.image_settings.file_format = previous["file_format"]
        render.film_transparent = previous["film_transparent"]
        scene.camera = previous["camera"]


def _ensure_uv0(obj: bpy.types.Object) -> None:
    if not obj.data.uv_layers:
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.mode_set(mode="EDIT")
        try:
            bpy.ops.mesh.select_all(action="SELECT")
            bpy.ops.uv.smart_project()
        finally:
            bpy.ops.object.mode_set(mode="OBJECT")
    if obj.data.uv_layers:
        obj.data.uv_layers[0].name = "UV0"


def export_reviewed_asset(
    job: AssetJob,
    candidate_id: str,
    destination: Path,
    export_format: str = "glb",
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    settings = {"include_animations": True, "apply_modifiers": True, **dict(options or {})}
    candidate = _candidate(job, candidate_id)
    collection = bpy.data.collections.get(candidate.imported_collection or "")
    if collection is None:
        raise ValidationError("the approved candidate collection no longer exists")
    export_objects = [obj for obj in collection.all_objects if obj.type in {"MESH", "ARMATURE"}]
    if not export_objects:
        raise ValidationError("approved candidate contains no exportable objects")
    destination.parent.mkdir(parents=True, exist_ok=True)
    previous_selected = list(bpy.context.selected_objects)
    previous_active = bpy.context.view_layer.objects.active
    try:
        bpy.ops.object.select_all(action="DESELECT")
        for obj in export_objects:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = export_objects[0]
        if export_format in {"glb", "gltf"}:
            bpy.ops.export_scene.gltf(
                filepath=str(destination), export_format="GLB" if export_format == "glb" else "GLTF_SEPARATE",
                use_selection=True, export_apply=bool(settings["apply_modifiers"]),
                export_cameras=False, export_lights=False,
                export_animations=bool(settings["include_animations"]),
            )
        elif export_format == "fbx":
            bpy.ops.export_scene.fbx(
                filepath=str(destination), use_selection=True,
                use_mesh_modifiers=bool(settings["apply_modifiers"]),
                bake_anim=bool(settings["include_animations"]), add_leaf_bones=False,
            )
        elif export_format == "obj":
            bpy.ops.wm.obj_export(
                filepath=str(destination), export_selected_objects=True,
                apply_modifiers=bool(settings["apply_modifiers"]), export_materials=True,
            )
        elif export_format == "stl":
            bpy.ops.wm.stl_export(
                filepath=str(destination), export_selected_objects=True,
                apply_modifiers=bool(settings["apply_modifiers"]),
            )
        elif export_format == "usd":
            bpy.ops.wm.usd_export(
                filepath=str(destination), selected_objects_only=True,
                export_animation=bool(settings["include_animations"]),
                export_materials=True,
            )
        else:
            raise ValidationError("unsupported Blender export format")
    finally:
        bpy.ops.object.select_all(action="DESELECT")
        for obj in previous_selected:
            if obj.name in bpy.data.objects:
                obj.select_set(True)
        if previous_active and previous_active.name in bpy.data.objects:
            bpy.context.view_layer.objects.active = previous_active
    raw = destination.read_bytes()
    return {
        "filename": destination.name,
        "format": export_format,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "staging_only": bool(settings.get("_staging_only", True)),
    }
