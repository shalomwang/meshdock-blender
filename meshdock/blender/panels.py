from __future__ import annotations

import bpy
from bpy.app.translations import pgettext_iface as iface_

from ..core.provider_ids import HUNYUAN_DIRECT, TOKENHUB_PROVIDERS, TRIPO_PROVIDERS
from .properties import (
    generation_constraints, generation_cost_estimate, generation_topology_limits,
    selected_generation_account, selected_process_provider,
)
from .runtime import get_runtime

_STATE_LABELS = {
    "draft": "Draft", "queued": "Queued", "submitted": "Submitted",
    "processing": "Running", "recovery_pending": "Recovery Pending",
    "candidates_ready": "Models Ready", "candidate_imported": "Models Imported",
    "approved": "Approved", "exported": "Exported", "cancelled": "Cancelled",
    "failed": "Failed", "pending": "Pending", "rejected": "Rejected",
}


class _PipelinePanel:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Mesh Dock"


def _draw_references(layout, props) -> None:
    references = layout.box()
    references.label(text="Reference Images")
    if props.input_mode == "text":
        references.label(text="Choose image input to add references.", icon="INFO")
        return
    try:
        constraint = generation_constraints(props)["constraints"][props.input_mode]
        views = constraint["allowed_views"]
        required = set(constraint["required_views"])
        references.label(
            text=f'{constraint["min_images"]}-{constraint["max_images"]} · '
                 f'{"/".join(fmt.upper() for fmt in constraint["formats"])}',
            icon="INFO",
        )
        if constraint.get("note"):
            references.label(text=iface_(constraint["note"]))
    except Exception:
        views = ["front"] if props.input_mode == "image" else ["front", "left", "back", "right"]
        required = {"front"}
    row = references.row(align=True)
    for index, view in enumerate(views):
        if index and index % 4 == 0:
            row = references.row(align=True)
        operator = row.operator(
            "meshdock.add_reference_image",
            text=("* " if view in required else "") + iface_(view.replace("_", " ").title()),
        )
        operator.view = view
    for index, item in enumerate(props.reference_images):
        card = references.box()
        if item.preview_image:
            preview = item.preview_image.preview
            if preview and preview.icon_id:
                card.template_icon(icon_value=preview.icon_id, scale=5)
        row = card.row(align=True)
        row.label(
            text=f"{item.view}: {item.filename} ({item.dimensions}, {item.format.upper()})",
            icon="IMAGE_DATA",
        )
        remove = row.operator("meshdock.remove_reference_image", text="", icon="X")
        remove.index = index
        button = card.operator("meshdock.preview_reference", text="Preview Reference", icon="ZOOM_IN")
        button.index = index
    if not props.reference_images:
        references.label(text="Choose a view above to add an image.", icon="IMAGE_DATA")


def _draw_provider_options(layout, props, provider: str) -> None:
    model = props.generation_model
    if provider in TRIPO_PROVIDERS:
        row = layout.row(align=True)
        row.enabled = not props.tripo_generate_parts
        row.prop(props, "tripo_texture")
        row.prop(props, "tripo_pbr")
        layout.prop(props, "tripo_export_uv")
        if model != "v2.5-20250123":
            layout.prop(props, "tripo_texture_quality")
        if model in {"v3.0-20250812", "v3.1-20260211"}:
            layout.prop(props, "tripo_geometry_quality")
            row = layout.row(align=True)
            row.enabled = not props.tripo_generate_parts
            row.prop(props, "tripo_quad")
            row.prop(props, "tripo_smart_low_poly")
            layout.prop(props, "tripo_generate_parts")
        if props.input_mode == "image":
            layout.prop(props, "tripo_enable_image_autofix")
            layout.prop(props, "tripo_texture_alignment")
            row = layout.row()
            row.enabled = props.tripo_texture
            row.prop(props, "tripo_orientation")
    elif provider == HUNYUAN_DIRECT or (
        provider in TOKENHUB_PROVIDERS and model.startswith("hy-3d-")
    ):
        layout.prop(props, "hunyuan_generate_type")
        row = layout.row(align=True)
        if props.hunyuan_generate_type != "Geometry":
            row.prop(props, "hunyuan_enable_pbr")
        if props.hunyuan_generate_type == "LowPoly":
            row.prop(props, "hunyuan_polygon_type")
        layout.prop(props, "hunyuan_result_format")
    elif provider in TOKENHUB_PROVIDERS:
        layout.label(
            text="Tencent currently documents text input for this TokenHub model.",
            icon="INFO",
        )

    if provider in TOKENHUB_PROVIDERS and model.startswith("tripo-3d-"):
        return
    topology = layout.box()
    topology.label(text="Topology", icon="MOD_DECIM")
    limits = generation_topology_limits(props)
    if not limits.get("supported"):
        topology.label(text=iface_(str(limits.get("reason", "Unavailable"))), icon="INFO")
        return
    topology.prop(props, "use_custom_face_limit")
    if props.use_custom_face_limit:
        topology.prop(props, "target_face_count")
        topology.label(
            text=(
                f'{iface_("Supported range")}: '
                f'{int(limits["minimum"]):,}–{int(limits["maximum"]):,}'
            ),
            icon="INFO",
        )
    else:
        topology.label(
            text=f'{iface_("Preset limit")}: {int(limits["preset"]):,}',
            icon="INFO",
        )
    note = str(limits.get("note", ""))
    if note:
        topology.label(text=iface_(note))


def _draw_active_job(layout, props, runtime) -> None:
    current = layout.box()
    current.label(text="Current Job", icon="TIME")
    current.prop(props, "last_job_id", text="")
    if not props.last_job_id or props.last_job_id == "__none__":
        current.label(text="Select a job to view or import its models.", icon="INFO")
        return
    try:
        status = runtime.service.get_job(props.last_job_id)
        state = iface_(_STATE_LABELS.get(status["state"], status["state"]))
        current.progress(factor=max(0.0, min(1.0, status["progress"])), type="BAR", text=state)
        if status["state"] in {"queued", "submitted", "processing"}:
            controls = current.row(align=True)
            if status.get("paused"):
                controls.operator("meshdock.unpause_job", text="Continue", icon="PLAY")
            else:
                controls.operator("meshdock.pause_job", text="Pause", icon="PAUSE")
            controls.operator("meshdock.cancel_job", text="Cancel", icon="CANCEL")
        elif status["state"] == "recovery_pending":
            current.operator("meshdock.resume_job", icon="FILE_REFRESH")
        elif status["state"] in {"failed", "cancelled"}:
            current.operator("meshdock.retry_job", icon="DUPLICATE")
        if status["state"] == "candidates_ready":
            ready = current.box()
            if runtime.auto_import_status(props.last_job_id) == "pending":
                ready.label(text="Downloaded; importing models automatically...", icon="IMPORT")
            else:
                ready.label(text="Downloaded models are ready", icon="CHECKMARK")
                ready.operator("meshdock.import_all_models", icon="IMPORT")
        elif status["state"] == "candidate_imported":
            current.label(text="Generated models are in the scene", icon="CHECKMARK")
    except Exception:
        current.label(text="Status temporarily unavailable", icon="ERROR")


class AI3D_PT_asset_pipeline(_PipelinePanel, bpy.types.Panel):
    bl_label = "Mesh Dock"
    bl_idname = "AI3D_PT_asset_pipeline"

    def draw(self, context):
        layout = self.layout
        props = context.scene.meshdock
        runtime = get_runtime()

        if context.workspace.name != "AI":
            entry = layout.row()
            entry.scale_y = 1.4
            entry.operator("meshdock.open_ai_workspace", icon="WORKSPACE")
        layout.operator("meshdock.manage_credentials", text="Manage Accounts", icon="PREFERENCES")
        layout.prop(props,'native_controls',text='原生控件 / Native controls')
        if props.native_controls:
            layout.prop(props,'workbench_tab',expand=True)
            if props.workbench_tab=='CREATE':
                layout.prop(props,'generation_account');layout.prop(props,'generation_model')
                layout.prop(props,'input_mode',expand=True)
                if props.input_mode=='text': layout.prop(props,'prompt')
                else: _draw_references(layout,props)
                _draw_provider_options(layout,props,selected_generation_account(props)[0])
                layout.prop(props,'candidate_count')
                layout.operator('meshdock.workbench_generate',text='生成 / Generate')
            elif props.workbench_tab=='MODELS':
                from .workflow import process_fields,process_blocker,target_summary
                layout.label(text=target_summary(context))
                layout.prop(props,'process_operation');layout.prop(props,'process_provider')
                if props.process_operation=='retopology':
                    names=('process_face_limit','process_quad','process_bake') if selected_process_provider(props) in TRIPO_PROVIDERS else ('process_face_level','process_polygon_type')
                    for name in names: layout.prop(props,name)
                else:
                    for name,label in process_fields(props): layout.prop(props,name,text=label)
                blocker=process_blocker(context,props)
                if blocker: layout.label(text=blocker,icon='INFO')
                row=layout.row();row.enabled=not blocker;row.operator('meshdock.process_candidate')
                layout.prop(props,'action_name')
                row=layout.row();row.enabled=props.action_name not in ('','__none__');row.operator('meshdock.preview_character_action')
            else:
                layout.prop(props,'last_job_id');layout.prop(props,'candidate_id')
                layout.operator('meshdock.export_reviewed_asset')
        return


class AI3D_PT_candidate_review(_PipelinePanel, bpy.types.Panel):
    bl_label = "Generated Models"
    bl_idname = "AI3D_PT_candidate_review"
    bl_parent_id = "AI3D_PT_asset_pipeline"

    @classmethod
    def poll(cls, context):
        return context.scene.meshdock.workbench_tab == "MODELS"

    def draw(self, context):
        layout = self.layout
        props = context.scene.meshdock
        if props.last_job_id in {"", "__none__"}:
            layout.label(text="Generate a model to see your results here.", icon="INFO")
            return
        layout.prop(props, "candidate_id", text="Model")
        row = layout.row(align=True)
        row.enabled = props.last_job_id not in {"", "__none__"}
        row.operator("meshdock.import_all_models", icon="IMPORT")
        row.operator("meshdock.open_candidate_folder", text="Open Folder", icon="FILE_FOLDER")
        row = layout.row(align=True)
        row.enabled = props.last_job_id not in {"", "__none__"} and props.candidate_id not in {"", "__none__"}
        row.operator("meshdock.prepare_game_asset", text="Normalize", icon="MODIFIER")
        row.operator("meshdock.show_candidate", text="Show Model", icon="HIDE_OFF")
        row.operator("meshdock.show_all_candidates", text="Show All", icon="RESTRICT_VIEW_OFF")
        viewport = layout.box()
        viewport.label(text="Model Preview", icon="SHADING_SOLID")
        viewport.prop(context.space_data.shading, "type", expand=True)
        viewport.prop(context.space_data.overlay, "show_wireframes", text="Wireframe")
        viewport.prop(context.space_data.overlay, "show_stats", text="Statistics")
        export = layout.box()
        export.label(text="Export", icon="EXPORT")
        export.prop(props, "export_format", text="Format")
        export.prop(props, "export_directory", text="Folder")
        try:
            job = get_runtime().service.get_job(props.last_job_id)
            candidate = next((c for c in job["candidates"] if c["id"] == props.candidate_id), {})
        except Exception:
            job, candidate = {}, {}
        confirm = export.row()
        confirm.enabled = bool(candidate.get("imported"))
        confirm.operator("meshdock.approve_candidate", text="Use This Model", icon="CHECKMARK")
        action = export.row()
        action.scale_y = 1.3
        action.enabled = bool(candidate.get("imported")) and job.get("selected_candidate_id") == props.candidate_id
        action.operator("meshdock.export_reviewed_asset", icon="EXPORT")
        if not action.enabled:
            export.label(text="Import and confirm this model before exporting.", icon="INFO")


class AI3D_PT_processing(_PipelinePanel, bpy.types.Panel):
    bl_label = "Processing"
    bl_idname = "AI3D_PT_processing"
    bl_parent_id = "AI3D_PT_asset_pipeline"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return context.scene.meshdock.workbench_tab == "MODELS"

    def draw(self, context):
        layout = self.layout
        props = context.scene.meshdock
        runtime = get_runtime()
        layout.prop(props, "process_operation")
        if props.process_operation != "__none__":
            layout.prop(props, "process_provider")
            if props.process_operation == "retopology":
                provider = selected_process_provider(props)
                controls = layout.box()
                controls.label(text="Retopology Settings", icon="MOD_DECIM")
                if provider in TRIPO_PROVIDERS:
                    controls.prop(props, "process_face_limit")
                    row = controls.row(align=True)
                    row.prop(props, "process_quad")
                    row.prop(props, "process_bake")
                    controls.label(text="Supported range: 1,000–20,000", icon="INFO")
                elif provider in TOKENHUB_PROVIDERS:
                    controls.prop(props, "process_face_level", expand=True)
                    controls.prop(props, "process_polygon_type")
                    controls.label(
                        text="Face Level is provider-defined, not an exact face count.",
                        icon="INFO",
                    )
            layout.prop(
                props, "show_advanced_process",
                icon="DISCLOSURE_TRI_DOWN" if props.show_advanced_process else "DISCLOSURE_TRI_RIGHT",
            )
            if props.show_advanced_process:
                layout.prop(props, "process_json")
            layout.operator("meshdock.process_candidate", icon="MODIFIER")
        if props.last_process_task_id and props.last_job_id != "__none__":
            try:
                process = runtime.service.get_process_status(props.last_job_id, props.last_process_task_id)
                layout.label(text=f'{process["state"]} · {process["progress"] * 100:.0f}%')
                if process["state"] in {"queued", "processing"}:
                    row = layout.row(align=True)
                    paused = bool(process.get("paused"))
                    row.operator(
                        "meshdock.unpause_process" if paused else "meshdock.pause_process",
                        text="Continue" if paused else "Pause", icon="PLAY" if paused else "PAUSE",
                    )
                    row.operator("meshdock.cancel_process", text="Cancel", icon="CANCEL")
                elif process["state"] == "recovery_pending":
                    row = layout.row(align=True)
                    row.operator("meshdock.resume_process", icon="FILE_REFRESH")
                    row.operator("meshdock.cancel_process", icon="CANCEL")
            except Exception:
                pass


class AI3D_PT_character_preview(_PipelinePanel, bpy.types.Panel):
    bl_label = "Character Animation Preview"
    bl_idname = "AI3D_PT_character_preview"
    bl_parent_id = "AI3D_PT_asset_pipeline"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return context.scene.meshdock.workbench_tab == "MODELS"

    def draw(self, context):
        props = context.scene.meshdock
        self.layout.prop(props, "action_name")
        row = self.layout.row(align=True)
        row.operator("meshdock.preview_character_action", icon="PLAY")
        row.operator("meshdock.stop_character_preview", icon="PAUSE")


class AI3D_PT_pipeline_tools(_PipelinePanel, bpy.types.Panel):
    bl_label = "Batch & Diagnostics"
    bl_idname = "AI3D_PT_pipeline_tools"
    bl_parent_id = "AI3D_PT_asset_pipeline"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return context.scene.meshdock.workbench_tab == "TOOLS"

    def draw(self, context):
        layout = self.layout
        props = context.scene.meshdock
        runtime = get_runtime()
        layout.label(text="Batch Queue")
        layout.prop(props, "batch_json")
        layout.prop(props, "batch_generate")
        layout.operator("meshdock.create_batch", icon="SEQ_STRIP_DUPLICATE")
        hidden = layout.box()
        hidden.label(text="Job History", icon="PACKAGE")
        hidden.prop(props, "archived_job_id", text="Hidden Job")
        restore = hidden.row(align=True)
        restore.enabled = props.archived_job_id not in {"", "__none__"}
        restore.operator("meshdock.restore_archived_job", icon="LOOP_BACK")
        try:
            current = runtime.service.get_job(props.last_job_id)
            if current["state"] not in {"queued", "submitted", "processing"}:
                hidden.operator("meshdock.archive_job", text="Hide Current Job", icon="HIDE_ON")
        except Exception:
            pass
        hidden.label(text="Hiding never downloads or deletes files.", icon="INFO")
        row = layout.row(align=True)
        row.prop(props, "purge_older_than_days", text="Archived Days")
        row.operator("meshdock.purge_archived_jobs", text="Purge", icon="TRASH")
        layout.separator()
        layout.label(
            text="MCP bridge is running" if runtime.bridge.running else "MCP bridge is stopped",
            icon="LINKED" if runtime.bridge.running else "UNLINKED",
        )
        layout.operator("meshdock.create_support_bundle", icon="TEXT")
        layout.label(text="Snapshot excludes keys, paths, prompts and URLs.", icon="INFO")
