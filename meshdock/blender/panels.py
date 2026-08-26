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
    bl_category = "MeshDock"


def _draw_references(layout, props) -> None:
    references = layout.box()
    references.label(text="Reference Images")
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
        row = references.row(align=True)
        row.label(
            text=f"{item.view}: {item.filename} ({item.dimensions}, {item.format.upper()})",
            icon="IMAGE_DATA",
        )
        remove = row.operator("meshdock.remove_reference_image", text="", icon="X")
        remove.index = index


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
        current.label(text=f'{state} · {status["progress"] * 100:.0f}%')
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
    bl_label = "MeshDock"
    bl_idname = "AI3D_PT_asset_pipeline"

    def draw(self, context):
        layout = self.layout
        props = context.scene.meshdock
        runtime = get_runtime()

        profiles = [
            profile
            for provider in (*TRIPO_PROVIDERS, HUNYUAN_DIRECT, *TOKENHUB_PROVIDERS)
            for profile in runtime.credentials.list_profiles(provider)
        ]
        account_count = len(profiles)
        enabled_count = sum(bool(profile["enabled"]) for profile in profiles)
        accounts = layout.row(align=True)
        accounts.label(
            text=f'{enabled_count}/{account_count} {iface_("accounts enabled")}',
            icon="CHECKMARK" if enabled_count else "LOCKED",
        )
        accounts.operator("meshdock.manage_credentials", text="Manage", icon="PREFERENCES")

        create = layout.box()
        create.label(text="Create 3D Asset", icon="OUTLINER_OB_MESH")
        create.prop(props, "input_mode", expand=True)
        create.prop(props, "generation_account")
        if props.generation_account == "__none__":
            create.label(text="Add a compatible account to continue.", icon="ERROR")
            create.operator("meshdock.manage_credentials", text="Add Account", icon="ADD")
            return
        create.prop(props, "generation_model")
        create.prop(
            props, "prompt",
            text="Prompt" if props.input_mode == "text" else "Guidance (optional)",
        )
        if props.input_mode != "text":
            _draw_references(create, props)
        create.prop(props, "candidate_count", text="Quantity")
        disclosure = create.row()
        disclosure.prop(
            props, "show_generation_settings", text="Quality & Cost",
            icon="DISCLOSURE_TRI_DOWN" if props.show_generation_settings else "DISCLOSURE_TRI_RIGHT",
            emboss=False,
        )
        if props.show_generation_settings:
            settings = create.box()
            provider, _profile_id = selected_generation_account(props)
            _draw_provider_options(settings, props, provider)
            settings.prop(props, "asset_name", text="Asset ID")
            settings.prop(props, "asset_profile")
            settings.prop(props, "priority")
            settings.prop(props, "max_estimated_credits")
            if props.max_estimated_credits > 0:
                settings.prop(props, "allow_over_budget")
            settings.prop(
                props, "show_advanced_generation",
                icon="DISCLOSURE_TRI_DOWN" if props.show_advanced_generation else "DISCLOSURE_TRI_RIGHT",
            )
            if props.show_advanced_generation:
                settings.prop(props, "advanced_json", text="Extra Options (JSON)")
        action = create.row(align=True)
        action.operator("meshdock.create_and_generate", text="Generate", icon="ADD")
        try:
            estimate = generation_cost_estimate(props)
        except Exception:
            estimate = None
        if estimate is not None:
            cost = action.row(align=True)
            cost.alert = (
                props.max_estimated_credits > 0
                and estimate > props.max_estimated_credits
                and not props.allow_over_budget
            )
            if estimate == 0:
                cost.label(text=iface_("Free"), icon="INFO")
            else:
                cost.label(
                    text=f'{iface_("Estimated")} {estimate:g} {iface_("credits")}',
                    icon="INFO",
                )
        _draw_active_job(layout, props, runtime)


class AI3D_PT_candidate_review(_PipelinePanel, bpy.types.Panel):
    bl_label = "Generated Models"
    bl_idname = "AI3D_PT_candidate_review"
    bl_parent_id = "AI3D_PT_asset_pipeline"

    def draw(self, context):
        layout = self.layout
        props = context.scene.meshdock
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


class AI3D_PT_processing(_PipelinePanel, bpy.types.Panel):
    bl_label = "Processing"
    bl_idname = "AI3D_PT_processing"
    bl_parent_id = "AI3D_PT_asset_pipeline"
    bl_options = {"DEFAULT_CLOSED"}

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
