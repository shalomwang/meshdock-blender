"""Isolated Blender benchmark: real editable/evaluated geometry, no provider calls."""
import sys,time,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import bpy
import numpy as np
from meshdock.blender.process_target import geometry_signature,editing_signature
results=[]
for n in (316,1000):
    mesh=bpy.data.meshes.new('BenchmarkGrid')
    side=n+1;vertices=side*side;faces=n*n
    mesh.vertices.add(vertices)
    co=np.zeros((vertices,3),dtype=np.float32)
    co[:,0]=np.tile(np.arange(side),side);co[:,1]=np.repeat(np.arange(side),side)
    mesh.vertices.foreach_set('co',co.ravel())
    base=(np.repeat(np.arange(n),n)*side+np.tile(np.arange(n),n)).astype(np.int32)
    indices=np.column_stack((base,base+1,base+side+1,base+side)).ravel()
    mesh.loops.add(faces*4);mesh.loops.foreach_set('vertex_index',indices)
    mesh.polygons.add(faces);mesh.polygons.foreach_set('loop_start',np.arange(faces,dtype=np.int32)*4)
    mesh.polygons.foreach_set('loop_total',np.full(faces,4,dtype=np.int32));mesh.update()
    obj=bpy.data.objects.new('Benchmark',mesh);bpy.context.scene.collection.objects.link(obj)
    start=time.perf_counter();baseline=geometry_signature(bpy.context,[obj]);elapsed=time.perf_counter()-start
    start=time.perf_counter();assert geometry_signature(bpy.context,[obj])==baseline;recheck=time.perf_counter()-start
    mesh.vertices[0].co.z=.1;mesh.update();assert geometry_signature(bpy.context,[obj])!=baseline
    results.append({'vertices':vertices,'quad_faces':faces,'initial_seconds':round(elapsed,3),'unchanged_check_seconds':round(recheck,3),'edit_detected':True})
    bpy.data.objects.remove(obj,do_unlink=True);bpy.data.meshes.remove(mesh)
(ROOT/'build/fingerprint-benchmark.json').write_text(json.dumps(results,indent=2))
print(json.dumps(results))
