"""Native, keyboard-accessible popovers behind the visual workbench controls."""
import bpy
from bpy.props import StringProperty
from .runtime import get_runtime
from .properties import generation_cost_estimate, generation_constraints, selected_generation_account, generation_topology_limits
from ..core.provider_ids import TRIPO_PROVIDERS, HUNYUAN_DIRECT, TOKENHUB_PROVIDERS


def tr(zh, en):
    return zh if bpy.context.preferences.view.language in {'zh_HANS', 'zh_CN'} else en


def generation_blocker(props):
    if props.generation_account == '__none__':
        return tr('先连接生成账号', 'Connect an account first')
    if props.generation_model == '__none__':
        return tr('请选择可用模型', 'Choose an available model')
    if props.input_mode == 'text':
        return '' if props.prompt.strip() else tr('先描述你想生成的模型', 'Describe your model first')
    try:
        rule = generation_constraints(props)['constraints'][props.input_mode]
        from .properties import active_references
        refs=active_references(props)
        views = {r.view for r in refs}
        missing = set(rule['required_views']) - views
        if missing:
            labels={'front':tr('正面','front'),'back':tr('背面','back'),'left':tr('左侧','left'),'right':tr('右侧','right')}
            return tr('请添加：','Add: ')+', '.join(labels.get(v,v) for v in sorted(missing))
        if len(refs) < rule['min_images']:
            return tr('至少添加 ','Add at least ')+str(rule['min_images'])+tr(' 张参考图',' reference images')
    except Exception:
        return tr('请检查账号与输入设置', 'Check account and input settings')
    return ''


def open_field(context, name):
    """Show enum choices directly, without an intermediate settings popup."""
    props = context.scene.meshdock
    prop = props.bl_rna.properties.get(name)
    if prop is None or name.startswith('_'):
        return
    if prop.type == 'BOOLEAN':
        setattr(props, name, not getattr(props, name))
    elif prop.type == 'ENUM':
        def draw(menu, menu_context):
            from . import properties
            callbacks={'generation_account':'_generation_account_items','generation_model':'_generation_model_items',
                       'process_provider':'_process_provider_items','process_operation':'_process_operation_items',
                       'tripo_texture_quality':'_tripo_texture_quality_items','hunyuan_generate_type':'_hunyuan_generate_type_items',
                       'action_name':'_action_items','asset_profile':'_profile_items'}
            callback=getattr(properties,callbacks.get(name,''),None)
            items=callback(props,menu_context) if callback else [(item.identifier,item.name,item.description) for item in prop.enum_items]
            column=menu.layout.column()
            for item in items:
                if item and item[0]:
                    op=column.operator('wm.context_set_enum',text=bpy.app.translations.pgettext_iface(item[1]),
                                       icon='RADIOBUT_ON' if getattr(props,name)==item[0] else 'RADIOBUT_OFF')
                    op.data_path='scene.meshdock.'+name
                    op.value=item[0]
            if name == 'generation_account':
                menu.layout.separator()
                menu.layout.operator('meshdock.manage_credentials', text=tr('管理账号…', 'Manage accounts…'), icon='PREFERENCES')
        context.window_manager.popup_menu(draw, title=tr(prop.name, prop.name))
    else:
        bpy.ops.meshdock.workbench_options('INVOKE_DEFAULT', section='FIELD', field_name=name)


class MESHDOCK_OT_workbench_options(bpy.types.Operator):
    bl_idname = 'meshdock.workbench_options'
    bl_label = 'Mesh Dock'
    bl_description = 'Open settings for this control'
    section: StringProperty(default='INPUT')
    field_name: StringProperty(default='')

    def invoke(self, context, event):
        return context.window_manager.invoke_popup(self, width=400)

    def draw(self, context):
        p = context.scene.meshdock
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        section = self.section
        if section == 'FIELD':
            if self.field_name in p.bl_rna.properties and not self.field_name.startswith('_'):
                layout.prop(p,self.field_name)
        elif section == 'INPUT':
            layout.label(text=tr('描述模型', 'Describe your model'), icon='TEXT')
            layout.prop(p, 'prompt', text='')
        elif section == 'SERVICE':
            layout.label(text=tr('生成服务', 'Generation service'), icon='WORLD')
            layout.prop(p, 'generation_account')
            layout.prop(p, 'generation_model')
            layout.operator('meshdock.manage_credentials', text=tr('管理账号', 'Manage accounts'), icon='PREFERENCES')
        elif section == 'QUALITY':
            layout.label(text=tr('质量与输出', 'Quality & output'), icon='SHADING_RENDERED')
            layout.operator('meshdock.workbench_options', text=tr('几何与拓扑…', 'Geometry & topology…'), icon='MESH_DATA').section='GEOMETRY'
            layout.operator('meshdock.workbench_options', text=tr('材质与贴图…', 'Material & textures…'), icon='MATERIAL').section='MATERIAL'
            layout.prop(p, 'asset_profile')
            layout.separator()
            layout.operator('meshdock.workbench_options', text=tr('费用与高级设置…', 'Budget & advanced…'), icon='PREFERENCES').section='ADVANCED'
        elif section == 'GEOMETRY':
            provider=selected_generation_account(p)[0]
            layout.label(text=tr('几何与拓扑', 'Geometry & topology'), icon='MESH_DATA')
            if provider in TRIPO_PROVIDERS and p.generation_model in {'v3.0-20250812','v3.1-20260211'}:
                layout.prop(p,'tripo_geometry_quality',expand=True)
                layout.prop(p,'tripo_smart_low_poly')
                row=layout.row();row.enabled=not p.tripo_generate_parts
                row.prop(p,'tripo_quad')
                layout.prop(p,'tripo_generate_parts')
            elif provider==HUNYUAN_DIRECT or (provider in TOKENHUB_PROVIDERS and p.generation_model.startswith('hy-3d-')):
                layout.prop(p,'hunyuan_generate_type')
                if p.hunyuan_generate_type=='LowPoly':
                    layout.prop(p,'hunyuan_polygon_type')
            limits=generation_topology_limits(p)
            if limits.get('supported'):
                layout.separator()
                layout.prop(p,'use_custom_face_limit')
                if p.use_custom_face_limit:
                    layout.prop(p,'target_face_count')
                    layout.label(text=f'{int(limits["minimum"]):,} – {int(limits["maximum"]):,}',icon='INFO')
            else:
                layout.label(text=tr('当前模型使用服务商默认拓扑', 'This model uses provider topology defaults'),icon='INFO')
        elif section == 'MATERIAL':
            provider=selected_generation_account(p)[0]
            layout.label(text=tr('材质与贴图', 'Material & textures'),icon='MATERIAL')
            if provider in TRIPO_PROVIDERS:
                col=layout.column();col.enabled=not p.tripo_generate_parts
                col.prop(p,'tripo_texture');col.prop(p,'tripo_pbr')
                layout.prop(p,'tripo_export_uv')
                if p.generation_model!='v2.5-20250123':
                    row=layout.row();row.enabled=p.tripo_texture and not p.tripo_generate_parts
                    row.prop(p,'tripo_texture_quality')
                if p.input_mode=='image':
                    layout.separator()
                    layout.prop(p,'tripo_enable_image_autofix')
                    layout.prop(p,'tripo_texture_alignment')
                    row=layout.row();row.enabled=p.tripo_texture
                    row.prop(p,'tripo_orientation')
            elif provider==HUNYUAN_DIRECT or (provider in TOKENHUB_PROVIDERS and p.generation_model.startswith('hy-3d-')):
                if p.hunyuan_generate_type!='Geometry':
                    layout.prop(p,'hunyuan_enable_pbr')
                layout.prop(p,'hunyuan_result_format')
            else:
                layout.label(text=tr('当前模型使用服务商默认材质', 'This model uses provider material defaults'),icon='INFO')
        elif section == 'ADVANCED':
            layout.label(text=tr('费用与高级设置', 'Budget & advanced'), icon='PREFERENCES')
            layout.prop(p, 'priority')
            layout.prop(p, 'max_estimated_credits')
            if p.max_estimated_credits:
                layout.prop(p, 'allow_over_budget')
            layout.prop(p, 'show_advanced_generation')
            if p.show_advanced_generation:
                layout.prop(p, 'advanced_json')
        elif section == 'REFERENCES':
            from .panels import _draw_references
            _draw_references(layout, p)
        elif section == 'PROCESS':
            layout.operator('meshdock.prepare_game_asset', text=tr('规范化副本', 'Normalize a copy'), icon='MODIFIER')
            layout.separator()
            from .panels import AI3D_PT_processing
            AI3D_PT_processing.draw(self, context)
        elif section == 'ANIMATE':
            from .panels import AI3D_PT_character_preview
            AI3D_PT_character_preview.draw(self, context)
        elif section == 'TOOLS':
            layout.operator('meshdock.workbench_options', text=tr('历史与批量任务…', 'History & batch…'), icon='TIME').section='HISTORY'
            layout.operator('meshdock.manage_credentials', icon='PREFERENCES')
            layout.operator('meshdock.create_support_bundle', icon='TEXT')
            layout.operator('meshdock.open_candidate_folder', icon='FILE_FOLDER')
        elif section == 'HISTORY':
            from .panels import AI3D_PT_pipeline_tools
            AI3D_PT_pipeline_tools.draw(self, context)
        elif section == 'ERROR':
            try:
                job = get_runtime().service.get_job(p.last_job_id)
                error = job.get('error') or {}
                code = error.get('code', '')
                help_text = {
                    'provider_authentication_failed': tr('账号授权失败，请检查或更换密钥。', 'Check or replace the account key.'),
                    'provider_rate_limited': tr('服务繁忙，请稍后重试。', 'Service is busy. Retry shortly.'),
                    'provider_timeout': tr('等待超时，可以稍后恢复任务。', 'The request timed out. Resume later.'),
                    'provider_download_failed': tr('下载失败，请检查网络后重试。', 'Check your network and retry.'),
                }.get(code, tr('任务未完成，请查看原因并重试。', 'The task did not finish. Review the error and retry.'))
                layout.label(text=help_text, icon='ERROR')
                message = str(error.get('message', ''))
                for start in range(0, min(len(message), 400), 42):
                    layout.label(text=message[start:start+42])
                layout.operator('meshdock.retry_job', icon='FILE_REFRESH')
                layout.operator('meshdock.manage_credentials', icon='PREFERENCES')
            except Exception:
                layout.label(text=tr('暂无错误详情', 'No error details'))
        elif section == 'HELP':
            for text in [tr('1 · 图片或文字 → 生成', '1 · Image or text → Generate'),
                         tr('2 · 点击资产卡片 → 预览', '2 · Click an asset card → Preview'),
                         tr('3 · 加入场景，或直接导出', '3 · Add to scene, or export'),
                         tr('中键旋转 · 滚轮缩放 · 左键选择', 'MMB orbit · Wheel zoom · Click select'),
                         tr('悬停显示提示，Esc 关闭弹窗', 'Hover for hints. Esc closes popovers')]:
                layout.label(text=text)

    def execute(self, context):
        return {'FINISHED'}


class MESHDOCK_OT_workbench_export(bpy.types.Operator):
    bl_idname = 'meshdock.workbench_export'
    bl_label = 'Export Model'
    bl_description = 'Export the model currently shown in the preview'
    job_id: StringProperty(default='', options={'HIDDEN','SKIP_SAVE'})
    candidate_id: StringProperty(default='', options={'HIDDEN','SKIP_SAVE'})

    def invoke(self, context, event):
        if not self.job_id: self.job_id=context.scene.meshdock.last_job_id
        if not self.candidate_id: self.candidate_id=context.scene.meshdock.candidate_id
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        p = context.scene.meshdock
        self.layout.label(text=tr('导出此生成结果', 'Export this generated result'), icon='OUTLINER_OB_MESH')
        self.layout.prop(p, 'export_format', expand=True)
        self.layout.prop(p, 'export_directory')
        if not p.export_directory:
            self.layout.label(text=tr('未选目录时保存到 Mesh Dock 资产目录', 'Uses the Mesh Dock asset folder by default'), icon='INFO')

    def execute(self, context):
        p = context.scene.meshdock
        try:
            from .workbench_preview import candidate_collection
            job_id=self.job_id or p.last_job_id
            candidate_id=self.candidate_id or p.candidate_id
            collection=candidate_collection(job_id,candidate_id)
            if not collection or not collection.all_objects:
                raise ValueError(tr('模型已从场景移除，请先点击历史卡片重新导入', 'Model was removed; click its history card to import it first'))
            if context.object and context.object.mode!='OBJECT': bpy.ops.object.mode_set(mode='OBJECT')
            p.last_job_id=job_id;p.candidate_id=candidate_id
            get_runtime().service.select_candidate(job_id, candidate_id)
            return bpy.ops.meshdock.export_reviewed_asset('EXEC_DEFAULT')
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class MESHDOCK_OT_workbench_generate(bpy.types.Operator):
    bl_idname = 'meshdock.workbench_generate'
    bl_label = 'Generate Model'
    bl_description = 'Review estimated cost and generate a model'

    def invoke(self, context, event):
        blocker = generation_blocker(context.scene.meshdock)
        if blocker:
            self.report({'WARNING'}, blocker)
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=380)

    def draw(self, context):
        p = context.scene.meshdock
        layout = self.layout
        layout.label(text=tr('生成新模型', 'Generate a new model'), icon='OUTLINER_OB_MESH')
        layout.prop(p, 'candidate_count', text=tr('数量', 'Quantity'))
        estimate = generation_cost_estimate(p)
        layout.label(text=(tr('预计费用：', 'Estimated cost: ') + f'{estimate:g} '+tr('积分','credits')
                          if estimate is not None else tr('费用由服务商确定', 'Cost is determined by the provider')), icon='INFO')
        layout.label(text=tr('结果会自动加入当前场景。', 'Results are automatically added to the current scene.'))

    def execute(self, context):
        blocker = generation_blocker(context.scene.meshdock)
        if blocker:
            self.report({'WARNING'}, blocker)
            return {'CANCELLED'}
        return bpy.ops.meshdock.create_and_generate('EXEC_DEFAULT')


CLASSES = (MESHDOCK_OT_workbench_options, MESHDOCK_OT_workbench_export,
           MESHDOCK_OT_workbench_generate)
