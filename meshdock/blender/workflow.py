"""Task-oriented presentation shared by the visual and native panels."""
from .workbench_controls import tr
from .platform_links import PLATFORM_LABELS

OPERATIONS={'retopology':('重拓扑','Retopology'),'uv':('展开 UV','Unwrap UV'),'texture':('生成贴图','Texture'),
            'segment':('拆分部件','Segment'),'convert':('转换格式','Convert'),'rig_check':('检查可绑定性','Check riggability'),
            'rig':('自动绑定','Rig'),'animate':('生成动画','Generate animation')}
def operation_label(key): return tr(*OPERATIONS.get(key,(key,key)))
def provider_label(key): return PLATFORM_LABELS.get(key,tr('自动选择','Automatic') if key=='auto' else key)

def process_fields(p):
    from .properties import selected_process_provider
    tripo=(selected_process_provider(p) or '').startswith('tripo_')
    op=p.process_operation
    if op=='texture':
        return [('proc_prompt',tr('外观描述','Appearance')),('proc_pbr','PBR'),
                ('proc_quality',tr('贴图质量','Texture quality'))] if tripo else [
                ('proc_prompt',tr('外观描述','Appearance')),('proc_pbr','PBR'),
                ('proc_keep_uv',tr('保留 UV','Keep UV')),('proc_texture_size',tr('贴图尺寸','Texture size'))]
    if op=='segment': return [('proc_granularity',tr('拆分粒度','Granularity'))] if tripo else [
            ('proc_staged',tr('分阶段生成','Staged generation')),('proc_post',tr('后处理','Post-process'))]
    if op=='convert': return [('proc_format',tr('输出格式','Output format'))]
    if op=='rig': return []  # Provider-safe humanoid defaults; expert settings remain optional.
    if op=='animate': return [('proc_animation',tr('动作预设','Motion preset')),('proc_in_place',tr('原地动作','In place'))] if tripo else [
            ('proc_motion_prompt',tr('动作描述','Motion description')),('proc_duration',tr('时长（秒）','Duration (s)'))]
    return []

def friendly_params(p):
    from .properties import selected_process_provider
    tripo=(selected_process_provider(p) or '').startswith('tripo_')
    op=p.process_operation
    if op=='texture':
        result={'texture_quality':p.proc_quality,'pbr':p.proc_pbr} if tripo else {
                'enable_pbr':p.proc_pbr,'enable_keep_uv':p.proc_keep_uv,'texture_size':int(p.proc_texture_size)}
        if p.proc_prompt.strip(): result.update({'texture_prompt':{'text':p.proc_prompt.strip()}} if tripo else {'prompt':p.proc_prompt.strip()})
        return result
    if op=='segment': return {'segmentation_granularity':p.proc_granularity} if tripo else {'enable_staged_generation':p.proc_staged,'enable_post_process':p.proc_post}
    if op=='convert': return {'format':p.proc_format}
    if op=='animate': return {'animation':p.proc_animation,'animate_in_place':p.proc_in_place} if tripo else {'prompt':p.proc_motion_prompt,'duration':p.proc_duration}
    return {}

def target_summary(context):
    from .process_target import generated_target,collection_meshes
    from .workbench_preview import candidate_collection
    obj=context.active_object
    ids=generated_target(context)
    if ids:
        col=candidate_collection(*ids)
        count=len(collection_meshes(context,col)) if col else 0
        return tr('整个结果','Whole result')+f' · {count} '+tr('个部件','parts')
    return tr('仅活动物体','Active object only')

def process_blocker(context,p):
    from .properties import _process_capabilities
    from .process_target import generated_target,local_target
    if context.mode!='OBJECT': return tr('请按 Tab 返回物体模式','Press Tab to return to Object Mode')
    obj=context.active_object
    if obj is None or not obj.select_get(): return tr('请在场景中选择模型','Select a model in the scene')
    if not (generated_target(context) or local_target(context)): return tr('请选择网格模型','Select a mesh model')
    caps=_process_capabilities(p)
    if p.process_operation not in caps:
        from .runtime import get_runtime
        available=[a for a in get_runtime().service.providers.values() if a.status().available and a.capabilities().postprocess]
        if not available: return tr('请到平台添加并启用处理账号','Add and enable a processing account in Platforms')
        return tr('当前模型格式或来源不支持此操作','This model format or source does not support this operation')
    return ''


def rig_check_result(job, candidate_id):
    """Return only diagnostics for the currently selected source result."""
    if not job: return None
    for artifact in reversed(job.get('artifacts', [])):
        if artifact.get('kind') == 'rig_check' and artifact.get('source_candidate_id') == candidate_id:
            return artifact.get('diagnostics', {}).get('riggable')
    return None
