"""Resolve and snapshot a scene processing target without changing source objects."""
from pathlib import Path
from contextlib import contextmanager
import bpy
from .runtime import get_runtime


@contextmanager
def preserve_window_context(context):
    window=context.window
    scene=window.scene if window else None
    layer=window.view_layer if window else None
    try:
        yield
    finally:
        if window and scene and scene.name in bpy.data.scenes:
            if window.scene!=scene: window.scene=scene
            if layer and layer.name in scene.view_layers and window.view_layer!=layer:
                window.view_layer=layer


def generated_target(context):
    obj=context.active_object
    if obj is None or not obj.select_get(): return None
    service=get_runtime().service
    owners=list(obj.users_collection)
    for collection in owners:
        job_id=collection.get('meshdock_job_id');candidate_id=collection.get('meshdock_candidate_id')
        if job_id and candidate_id:
            try:
                job=service.get_job(job_id)
                if any(c['id']==candidate_id for c in job['candidates']): return job_id,candidate_id
            except Exception: pass
    # Legacy files: no recent-history limit. Persist once after resolution.
    matches=service.find_candidate_collections({c.name for c in owners})
    if len(matches)==1:
        ids=matches[0]
        for collection in owners:
            job=service.get_job(ids[0])
            if any(c['id']==ids[1] and c.get('imported_collection')==collection.name for c in job['candidates']):
                collection['meshdock_job_id'],collection['meshdock_candidate_id']=ids
        return ids
    return None



def local_target(context):
    obj=context.active_object
    return obj if obj and obj.type=='MESH' and obj.select_get() and generated_target(context) is None else None


def export_snapshot_objects(context,objects,destination):
    with preserve_window_context(context):
        return _export_snapshot_objects(context,objects,destination)


def _export_snapshot_objects(context, objects, destination):
    """Export only evaluated duplicates, restoring the source selection and data."""
    objects=list(objects)
    if context.mode!='OBJECT':
        raise ValueError('请先退出编辑模式再提交处理 / Exit edit mode before processing')
    if not objects or any(o.type!='MESH' or o.name not in context.scene.objects for o in objects):
        raise ValueError('Select mesh objects in the source scene')
    context.view_layer.update()
    depsgraph=context.evaluated_depsgraph_get()
    previous_selected=list(context.selected_objects)
    previous_active=context.view_layer.objects.active
    collection=None;copies=[];meshes=[]
    try:
        # Never remove a temporary Scene while the viewport still holds its depsgraph.
        # A selection-only export of parentless duplicates provides the same isolation.
        for obj in sorted(objects,key=lambda item:item.name):
            mesh=bpy.data.meshes.new_from_object(obj.evaluated_get(depsgraph),preserve_all_data_layers=True,depsgraph=depsgraph)
            meshes.append(mesh)
            if not mesh.polygons: continue
            copy=bpy.data.objects.new(obj.name+' · snapshot',mesh);copies.append(copy)
            copy.matrix_world=obj.matrix_world.copy()
        if not copies: raise ValueError('The processing target has no mesh faces')
        collection=bpy.data.collections.new('Mesh Dock · Export snapshot')
        context.scene.collection.children.link(collection)
        for copy in copies: collection.objects.link(copy)
        context.view_layer.update()
        for obj in context.view_layer.objects: obj.select_set(False)
        for copy in copies: copy.select_set(True)
        context.view_layer.objects.active=copies[0]
        result=bpy.ops.export_scene.gltf(filepath=str(destination),export_format='GLB',
            use_selection=True,use_active_scene=True,export_apply=False,
            export_animations=False,export_skins=False,export_cameras=False,export_lights=False)
        if result!={'FINISHED'}: raise ValueError('Could not export the processing target')
        if not Path(destination).is_file(): raise ValueError('Snapshot file was not produced')
    finally:
        for copy in copies: bpy.data.objects.remove(copy,do_unlink=True)
        if collection: bpy.data.collections.remove(collection)
        for mesh in meshes: bpy.data.meshes.remove(mesh)
        context.view_layer.update()
        for obj in previous_selected:
            if obj.name in context.view_layer.objects: obj.select_set(True)
        if previous_active and previous_active.name in context.view_layer.objects:
            context.view_layer.objects.active=previous_active


def export_snapshot(context,obj,destination):
    export_snapshot_objects(context,[obj],destination)


def collection_meshes(context,collection):
    return [o for o in collection.all_objects if o.type=='MESH' and o.name in context.scene.objects]


def snapshot_digest(path):
    import hashlib
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def editing_signature(objects):
    """Include editable modifier/shader settings that a GLB approximation can omit."""
    import hashlib,json,array
    visited=set();images={}
    def plain(value):
        if value is None or isinstance(value,(str,int,float,bool)): return value
        if isinstance(value,bpy.types.ID): return value.name_full
        if isinstance(value,dict): return {str(k):plain(v) for k,v in value.items()}
        if hasattr(value,'to_dict'): return plain(value.to_dict())
        try: return [plain(v) for v in value]
        except TypeError: return str(value)
    def settings(value):
        result={}
        for prop in value.bl_rna.properties:
            if prop.is_readonly or prop.type not in {'STRING','BOOLEAN','INT','FLOAT','ENUM'}: continue
            try: result[prop.identifier]=plain(getattr(value,prop.identifier))
            except (AttributeError,TypeError): pass
        return result
    def image_state(image):
        key=image.as_pointer()
        if key in images: return images[key]
        digest=hashlib.sha256()
        if image.packed_file: digest.update(image.packed_file.data)
        path=Path(bpy.path.abspath(image.filepath)) if image.filepath else None
        if path and path.is_file():
            with path.open('rb') as stream:
                for chunk in iter(lambda:stream.read(1048576),b''): digest.update(chunk)
        if image.is_dirty and image.has_data:
            pixels=array.array('f',[0.0])*len(image.pixels)
            image.pixels.foreach_get(pixels);digest.update(pixels.tobytes())
        images[key]=[settings(image),digest.hexdigest()]
        return images[key]
    def tree_state(tree):
        if not tree: return None
        key=tree.as_pointer()
        if key in visited: return tree.name_full
        visited.add(key)
        nodes=[]
        for node in tree.nodes:
            item={'type':node.bl_idname,'settings':settings(node),
                  'inputs':[(socket.identifier,plain(socket.default_value)) for socket in node.inputs if hasattr(socket,'default_value')],
                  'outputs':[(socket.identifier,plain(socket.default_value)) for socket in node.outputs if hasattr(socket,'default_value')]}
            if getattr(node,'image',None): item['image']=image_state(node.image)
            if getattr(node,'node_tree',None): item['group']=tree_state(node.node_tree)
            if getattr(node,'color_ramp',None):
                item['ramp']=[settings(node.color_ramp),[(e.position,list(e.color)) for e in node.color_ramp.elements]]
            if getattr(node,'mapping',None) and hasattr(node.mapping,'curves'):
                item['curves']=[[list(point.location) for point in curve.points] for curve in node.mapping.curves]
            nodes.append(item)
        return [nodes,[(link.from_node.name,link.from_socket.identifier,link.to_node.name,link.to_socket.identifier) for link in tree.links]]
    result=[]
    for obj in sorted(objects,key=lambda item:item.name):
        modifiers=[]
        for mod in obj.modifiers:
            try: custom={str(k):plain(v) for k,v in mod.items()}
            except TypeError: custom={}
            item=[mod.type,settings(mod),custom]
            if getattr(mod,'node_group',None): item.append(tree_state(mod.node_group))
            modifiers.append(item)
        materials=[None if mat is None else [settings(mat),tree_state(mat.node_tree)] for mat in (slot.material for slot in obj.material_slots)]
        result.append([obj.name,modifiers,materials,[settings(c) for c in obj.constraints]])
    return hashlib.sha256(json.dumps(result,sort_keys=True,ensure_ascii=False,default=str).encode('utf-8')).hexdigest()


def geometry_signature(context,objects):
    """Hash evaluated geometry directly; avoid a GLB encode just to detect edits."""
    import hashlib,array,json
    context.view_layer.update();graph=context.evaluated_depsgraph_get();digest=hashlib.sha256()
    def values(collection,property,width=1,kind='f'):
        data=array.array(kind,[0])* (len(collection)*width)
        if data: collection.foreach_get(property,data)
        digest.update(data.tobytes())
    for obj in sorted(objects,key=lambda o:o.name):
        digest.update(json.dumps([obj.name,[list(row) for row in obj.matrix_world]],ensure_ascii=False).encode())
        digest.update(str([(g.name,g.index) for g in obj.vertex_groups]).encode())
        if obj.vertex_groups:
            for vertex in obj.data.vertices:
                digest.update(str([(g.group,g.weight) for g in vertex.groups]).encode())
        if obj.data.shape_keys:
            for key in obj.data.shape_keys.key_blocks:
                digest.update(str((key.name,key.value,key.mute)).encode());values(key.data,'co',3)
        evaluated=obj.evaluated_get(graph)
        mesh=evaluated.to_mesh(preserve_all_data_layers=True,depsgraph=graph)
        try:
            for mesh in (obj.data,mesh):
                values(mesh.vertices,'co',3);values(mesh.edges,'vertices',2,'i')
                values(mesh.loops,'vertex_index',1,'i')
                for prop in ('loop_start','loop_total','material_index','use_smooth'): values(mesh.polygons,prop,1,'i')
                for layer in mesh.uv_layers:
                    digest.update(layer.name.encode());values(layer.data,'uv',2)
                # Every supported mesh attribute matters, including custom normals and colors.
                types={'FLOAT':('value',1,'f'),'INT':('value',1,'i'),'BOOLEAN':('value',1,'i'),
                       'INT32_2D':('value',2,'i'),'INT8':('value',1,'i'),'QUATERNION':('value',4,'f'),'FLOAT_VECTOR':('vector',3,'f'),'FLOAT2':('vector',2,'f'),'FLOAT_COLOR':('color',4,'f'),'BYTE_COLOR':('color',4,'f')}
                for attribute in mesh.attributes:
                    if attribute.name.startswith(('.select','.uv_select')): continue
                    digest.update((attribute.name+attribute.domain+attribute.data_type).encode())
                    if attribute.data_type not in types: raise ValueError('Unknown mesh attribute state')
                    values(attribute.data,*types[attribute.data_type])
                digest.update(str([(m.name if m else None) for m in mesh.materials]).encode())
        finally: evaluated.to_mesh_clear()
    return digest.hexdigest()


def capture_baseline(context,collection):
    """Capture a conservative state fingerprint after initial placement."""
    try:
        objects=collection_meshes(context,collection)
        collection['meshdock_baseline_glb']=geometry_signature(context,objects)
        collection['meshdock_baseline_edit_state']=editing_signature(objects)
        collection['meshdock_baseline_version']=2
    except Exception:
        for key in ('meshdock_baseline_glb','meshdock_baseline_edit_state','meshdock_baseline_version'):
            if key in collection: del collection[key]


def prepare_processing_source(context,obj,ids=None,credit_limit=0.0):
    """Reuse a generated source only when the exported current state matches its baseline."""
    import tempfile
    service=get_runtime().service
    collection=None
    if ids:
        from .workbench_preview import candidate_collection
        collection=candidate_collection(*ids)
        if collection is None or obj.name not in collection.all_objects:
            raise ValueError('The generated result no longer contains the confirmed target')
    objects=collection_meshes(context,collection) if collection else [obj]
    if collection and collection.get('meshdock_baseline_version')==2:
        try:
            if (collection.get('meshdock_baseline_glb')==geometry_signature(context,objects) and
                collection.get('meshdock_baseline_edit_state')==editing_signature(objects)): return ids
        except Exception: pass
    with tempfile.TemporaryDirectory(prefix='scene-snapshot-',dir=service.staging.root) as directory:
        destination=Path(directory)/'source.glb'
        export_snapshot_objects(context,objects,destination)
        if collection and collection.get('meshdock_baseline_version')!=2 and collection.get('meshdock_baseline_glb')==snapshot_digest(destination):
            try:
                if collection.get('meshdock_baseline_edit_state')==editing_signature(objects): return ids
            except Exception:
                pass  # Unknown editable state is a fresh upload, never assumed unchanged.
        job=service.register_local_model(destination,collection.name if collection else obj.name,
                                         max_estimated_credits=credit_limit)
        return job['id'],job['candidates'][0]['id']


def stage_local_target(context,obj,credit_limit=0.0):
    ids=prepare_processing_source(context,obj,credit_limit=credit_limit)
    return get_runtime().service.get_job(ids[0])
