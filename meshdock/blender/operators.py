import json
from pathlib import Path

import bpy
from bpy.app.translations import pgettext_iface as iface_
from bpy.props import BoolProperty, EnumProperty, IntProperty, StringProperty
from bpy_extras.io_utils import ImportHelper

from ..core.errors import PipelineError
from .properties import (
    effective_advanced, effective_process_params, generation_constraints,
    selected_generation_account,
    _provider_input_updated,
)
from .runtime import get_runtime


class _PipelineOperator:
    def fail(self, exc: Exception):
        code = getattr(exc, "code", "error")
        self.report({"ERROR"}, f"{code}: {exc}")
        return {"CANCELLED"}


class AI3D_OT_manage_credentials(bpy.types.Operator, _PipelineOperator):
    bl_idname = "meshdock.manage_credentials"
    bl_label = "Manage Accounts"
    bl_description = "Add and manage provider accounts without exposing keys"
    bl_options = {"INTERNAL"}

    provider: EnumProperty(
        name="Service",
        items=[
            ("tripo_cn", "Tripo Mainland China", ""),
            ("tripo_global", "Tripo Global", ""),
            ("hunyuan_direct", "Hunyuan Direct", ""),
            ("tokenhub_cn", "Tencent TokenHub China", ""),
            ("tokenhub_global", "Tencent TokenHub Global", ""),
        ],
    )
    secret: StringProperty(name="Secret Key", subtype="PASSWORD", options={"SKIP_SAVE"})
    note: StringProperty(
        name="Note", description="Optional name to distinguish this account", maxlen=80,
        options={"SKIP_SAVE"},
    )
    def invoke(self, context, _event):
        return context.window_manager.invoke_props_dialog(self, width=520)

    def draw(self, _context):
        layout = self.layout
        layout.prop(self, "provider")
        profiles = get_runtime().credentials.list_profiles(self.provider)
        current = layout.box()
        current.label(text="Existing Accounts")
        if not profiles:
            current.label(text="No accounts configured", icon="INFO")
        source_labels = {"session": "Session", "stored": "Saved", "environment": "Environment"}
        for index, profile in enumerate(profiles, start=1):
            row = current.row(align=True)
            enabled = bool(profile["enabled"])
            icon = "CHECKBOX_HLT" if enabled else "CHECKBOX_DEHLT"
            label = str(profile["note"]) or f'{iface_("Profile")} {index}'
            source = iface_(source_labels.get(profile["source"], "Available"))
            row.label(text=f"{label} · {source}", icon=icon)
            toggle = row.operator(
                "meshdock.set_credential_profile_enabled",
                text="Disable" if enabled else "Use",
                icon="HIDE_ON" if enabled else "HIDE_OFF",
            )
            toggle.provider = self.provider
            toggle.profile_id = str(profile["id"])
            toggle.desired_enabled = not enabled
            if profile["source"] != "environment":
                remove = row.operator(
                    "meshdock.delete_credential_profile", text="Remove", icon="X"
                )
                remove.provider = self.provider
                remove.profile_id = str(profile["id"])
        layout.separator()
        layout.label(text="Add Account")
        layout.prop(self, "secret")
        layout.prop(self, "note")
        if get_runtime().credentials.persistence_available:
            layout.label(text="New accounts are saved securely on this device.", icon="LOCKED")
        else:
            layout.label(
                text="Device storage is unavailable; this account will last for this session.",
                icon="INFO",
            )

    def execute(self, context):
        try:
            credentials = get_runtime().credentials
            persist = credentials.persistence_available
            identifier = credentials.set(
                self.provider, self.secret, persist=persist, note=self.note
            )
            props = context.scene.meshdock
            setattr(props, f"{self.provider}_account_profile", identifier)
            props.generation_account = f"{self.provider}|{identifier}"
            self.secret = ""
            self.note = ""
            _provider_input_updated(context.scene.meshdock, context)
            self.report(
                {"INFO" if persist else "WARNING"},
                "Account saved" if persist else "Account added for this session",
            )
            return {"FINISHED"}
        except PipelineError as exc:
            self.secret = ""
            return self.fail(exc)


class AI3D_OT_set_credential_profile_enabled(bpy.types.Operator, _PipelineOperator):
    bl_idname = "meshdock.set_credential_profile_enabled"
    bl_label = "Set Account Availability"
    bl_description = "Show or hide this account in generation account lists"
    bl_options = {"INTERNAL"}

    provider: StringProperty(options={"HIDDEN"})
    profile_id: StringProperty(options={"HIDDEN"})
    desired_enabled: BoolProperty(options={"HIDDEN"})

    def execute(self, context):
        try:
            get_runtime().credentials.set_enabled(
                self.provider, self.profile_id, self.desired_enabled
            )
            props = context.scene.meshdock
            if self.desired_enabled:
                setattr(props, f"{self.provider}_account_profile", self.profile_id)
                props.generation_account = f"{self.provider}|{self.profile_id}"
            else:
                if getattr(props, f"{self.provider}_account_profile", "") == self.profile_id:
                    setattr(props, f"{self.provider}_account_profile", "__auto__")
                _provider_input_updated(props, context)
            self.report(
                {"INFO"}, "Account enabled" if self.desired_enabled else "Account disabled"
            )
            return {"FINISHED"}
        except Exception as exc:
            return self.fail(exc)


class AI3D_OT_delete_credential_profile(bpy.types.Operator, _PipelineOperator):
    bl_idname = "meshdock.delete_credential_profile"
    bl_label = "Remove Account"
    bl_description = "Remove this account from Blender and the OS credential store"

    provider: StringProperty(options={"HIDDEN"})
    profile_id: StringProperty(options={"HIDDEN"})

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        try:
            get_runtime().credentials.delete_profile(self.profile_id)
            props = context.scene.meshdock
            if getattr(props, f"{self.provider}_account_profile", "") == self.profile_id:
                setattr(props, f"{self.provider}_account_profile", "__auto__")
            if props.generation_account == f"{self.provider}|{self.profile_id}":
                _provider_input_updated(props, context)
            self.report({"INFO"}, "Account removed")
            return {"FINISHED"}
        except Exception as exc:
            return self.fail(exc)


class AI3D_OT_create_and_generate(bpy.types.Operator, _PipelineOperator):
    bl_idname = "meshdock.create_and_generate"
    bl_label = "Create Job & Generate"
    bl_description = "Create a validated asset job and stage provider candidates"

    def invoke(self, context, _event):
        return context.window_manager.invoke_confirm(self, _event)

    def execute(self, context):
        props = context.scene.meshdock
        try:
            advanced = effective_advanced(props)
            service = get_runtime().service
            provider, _profile_id = selected_generation_account(props)
            job = service.create_job(
                {
                    "asset_name": props.asset_name,
                    "prompt": props.prompt,
                    "input_mode": props.input_mode,
                    "reference_images": {
                        item.view: item.reference_id for item in props.reference_images
                    },
                    "asset_profile": props.asset_profile,
                    "provider": provider,
                    "candidate_count": props.candidate_count,
                    "priority": props.priority,
                    "max_estimated_credits": props.max_estimated_credits,
                    "allow_over_budget": props.allow_over_budget,
                    "advanced": advanced,
                }
            )
            props.last_job_id = job["id"]
            generated = service.generate_candidates(job["id"])
            get_runtime().request_auto_import(job["id"])
            self.report({"INFO"}, f"Generation started: {generated['state']}")
            return {"FINISHED"}
        except Exception as exc:
            return self.fail(exc)


class AI3D_OT_create_batch(bpy.types.Operator, _PipelineOperator):
    bl_idname = "meshdock.create_batch"
    bl_label = "Create Batch"
    bl_description = "Create up to 20 persistent jobs from a JSON array"

    def invoke(self, context, event):
        if context.scene.meshdock.batch_generate:
            return context.window_manager.invoke_confirm(self, event)
        return self.execute(context)

    def execute(self, context):
        props = context.scene.meshdock
        try:
            specs = json.loads(props.batch_json or "[]")
            if not isinstance(specs, list) or not all(isinstance(item, dict) for item in specs):
                raise ValueError("Batch Specs must be a JSON array of objects")
            result = get_runtime().service.create_batch(specs, generate=props.batch_generate)
            if result["jobs"]:
                props.last_job_id = result["jobs"][-1]["id"]
            self.report({"INFO"}, f'Created {result["count"]} jobs')
            return {"FINISHED"}
        except Exception as exc:
            return self.fail(exc)


class AI3D_OT_purge_archived_jobs(bpy.types.Operator, _PipelineOperator):
    bl_idname = "meshdock.purge_archived_jobs"
    bl_label = "Purge Archived Jobs"
    bl_description = "Permanently remove archived staging directories older than the selected age"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        props = context.scene.meshdock
        try:
            result = get_runtime().service.purge_archived_jobs(props.purge_older_than_days)
            self.report({"INFO"}, f'Removed {result["removed_count"]} archived jobs')
            return {"FINISHED"}
        except Exception as exc:
            return self.fail(exc)


class AI3D_OT_create_support_bundle(bpy.types.Operator, _PipelineOperator):
    bl_idname = "meshdock.create_support_bundle"
    bl_label = "Create Safe Support Snapshot"
    bl_description = "Write a local diagnostic JSON without keys, paths, prompts, references, or provider URLs"

    def execute(self, _context):
        try:
            result = get_runtime().service.create_support_bundle()
            self.report({"INFO"}, f'Created {result["filename"]}')
            return {"FINISHED"}
        except Exception as exc:
            return self.fail(exc)


class AI3D_OT_add_reference_image(bpy.types.Operator, ImportHelper, _PipelineOperator):
    bl_idname = "meshdock.add_reference_image"
    bl_label = "Add Reference Image"
    bl_description = "Copy an image into managed staging and expose only an opaque id to jobs"

    filename_ext = ".png"
    filter_glob: StringProperty(default="*.png;*.jpg;*.jpeg;*.webp", options={"HIDDEN"})
    view: EnumProperty(
        name="View",
        items=[
            ("front", "Front", "Primary/front view"), ("left", "Left", "Left view"),
            ("right", "Right", "Right view"), ("back", "Back", "Back view"),
            ("top", "Top", "Top view"), ("bottom", "Bottom", "Bottom view"),
            ("left_front", "Left Front", "Left-front 45 degree view"),
            ("right_front", "Right Front", "Right-front 45 degree view"),
        ],
        default="front",
    )

    def execute(self, context):
        props = context.scene.meshdock
        try:
            reference = get_runtime().service.register_reference_image(Path(self.filepath), self.view)
            constraint = generation_constraints(props)["constraints"].get(props.input_mode)
            if constraint is None or self.view not in constraint["allowed_views"]:
                get_runtime().service.remove_reference_image(str(reference["id"]))
                raise ValueError(f"{self.view} is not supported by the selected provider/model")
            if reference["format"] not in constraint["formats"]:
                get_runtime().service.remove_reference_image(str(reference["id"]))
                raise ValueError(
                    f'{str(reference["format"]).upper()} is not supported; use '
                    + ", ".join(str(item).upper() for item in constraint["formats"])
                )
            if constraint["max_total_bytes"] and int(reference["bytes"]) > int(constraint["max_total_bytes"]):
                get_runtime().service.remove_reference_image(str(reference["id"]))
                raise ValueError("Reference exceeds the provider image-size limit")
            existing_index = next(
                (i for i, item in enumerate(props.reference_images) if item.view == self.view), None
            )
            if existing_index is not None:
                existing = props.reference_images[existing_index]
                get_runtime().service.remove_reference_image(existing.reference_id)
                props.reference_images.remove(existing_index)
            item = props.reference_images.add()
            item.reference_id = reference["id"]
            item.view = reference["view"]
            item.filename = reference["filename"]
            item.dimensions = f'{reference["width"]}×{reference["height"]}'
            item.format = str(reference["format"])
            item.bytes = int(reference["bytes"])
            item.width = int(reference["width"])
            item.height = int(reference["height"])
            _provider_input_updated(props, context)
            self.report({"INFO"}, f"Registered {self.view} reference")
            return {"FINISHED"}
        except Exception as exc:
            return self.fail(exc)


class AI3D_OT_remove_reference_image(bpy.types.Operator, _PipelineOperator):
    bl_idname = "meshdock.remove_reference_image"
    bl_label = "Remove Reference Image"
    bl_description = "Remove an unused managed reference from this Blender session"

    index: IntProperty(options={"HIDDEN"})

    def execute(self, context):
        props = context.scene.meshdock
        try:
            item = props.reference_images[self.index]
            get_runtime().service.remove_reference_image(item.reference_id)
            props.reference_images.remove(self.index)
            return {"FINISHED"}
        except Exception as exc:
            return self.fail(exc)


class AI3D_OT_cancel_job(bpy.types.Operator, _PipelineOperator):
    bl_idname = "meshdock.cancel_job"
    bl_label = "Cancel Job"
    bl_description = "Stop local generation polling and downloads for the active job"

    def execute(self, context):
        props = context.scene.meshdock
        try:
            get_runtime().service.cancel_job(props.last_job_id)
            self.report({"INFO"}, "Job cancelled locally")
            return {"FINISHED"}
        except Exception as exc:
            return self.fail(exc)

class AI3D_OT_resume_job(bpy.types.Operator, _PipelineOperator):
    bl_idname = "meshdock.resume_job"
    bl_label = "Resume Provider Tasks"
    bl_description = "Resume polling and download for already-submitted provider tasks without submitting new work"

    def execute(self, context):
        try:
            get_runtime().service.resume_job(context.scene.meshdock.last_job_id)
            self.report({"INFO"}, "Provider task recovery started")
            return {"FINISHED"}
        except Exception as exc:
            return self.fail(exc)


class AI3D_OT_retry_job(bpy.types.Operator, _PipelineOperator):
    bl_idname = "meshdock.retry_job"
    bl_label = "Retry as New Job"
    bl_description = "Create and submit a new billable job using the same specification"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        props = context.scene.meshdock
        try:
            retried = get_runtime().service.retry_job(props.last_job_id)
            props.last_job_id = retried["id"]
            self.report({"INFO"}, "Replacement generation job queued")
            return {"FINISHED"}
        except Exception as exc:
            return self.fail(exc)


class AI3D_OT_archive_job(bpy.types.Operator, _PipelineOperator):
    bl_idname = "meshdock.archive_job"
    bl_label = "Archive Job"
    bl_description = "Hide a completed job from the active job list without deleting its files"

    def execute(self, context):
        try:
            props = context.scene.meshdock
            service = get_runtime().service
            service.archive_job(props.last_job_id)
            remaining = service.list_jobs(limit=1)["jobs"]
            props.last_job_id = remaining[0]["id"] if remaining else "__none__"
            self.report({"INFO"}, "Job hidden; restore it from Job History")
            return {"FINISHED"}
        except Exception as exc:
            return self.fail(exc)


class AI3D_OT_restore_archived_job(bpy.types.Operator, _PipelineOperator):
    bl_idname = "meshdock.restore_archived_job"
    bl_label = "Restore Hidden Job"
    bl_description = "Return a hidden job to the active job list without changing its files"

    def execute(self, context):
        props = context.scene.meshdock
        try:
            restored = get_runtime().service.restore_job(props.archived_job_id)
            props.last_job_id = restored["id"]
            self.report({"INFO"}, "Job restored")
            return {"FINISHED"}
        except Exception as exc:
            return self.fail(exc)


class AI3D_OT_import_candidate(bpy.types.Operator, _PipelineOperator):
    bl_idname = "meshdock.import_candidate"
    bl_label = "Import Candidate"
    bl_description = "Import one staged candidate into an isolated Blender Collection"
    bl_options = {"UNDO"}

    def execute(self, context):
        props = context.scene.meshdock
        try:
            if props.last_job_id in {"", "__none__"}:
                raise ValueError("Select a valid job before importing a candidate")
            if props.candidate_id in {"", "__none__"}:
                raise ValueError("Select a downloaded candidate before importing")
            job = get_runtime().service.import_candidate(props.last_job_id, props.candidate_id)
            self.report({"INFO"}, f"Imported {props.candidate_id} for job {job['id'][:8]}")
            return {"FINISHED"}
        except Exception as exc:
            return self.fail(exc)


class AI3D_OT_import_all_models(bpy.types.Operator, _PipelineOperator):
    bl_idname = "meshdock.import_all_models"
    bl_label = "Import All Models"
    bl_description = "Import every generated model and arrange them with size-aware spacing"
    bl_options = {"UNDO"}

    def execute(self, context):
        props = context.scene.meshdock
        try:
            if props.last_job_id in {"", "__none__"}:
                raise ValueError("Select a valid job first")
            result = get_runtime().import_all_models(props.last_job_id)
            self.report({"INFO"}, f'Imported {result["model_count"]} generated model(s)')
            return {"FINISHED"}
        except Exception as exc:
            return self.fail(exc)


class AI3D_OT_open_candidate_folder(bpy.types.Operator, _PipelineOperator):
    bl_idname = "meshdock.open_candidate_folder"
    bl_label = "Open Candidate Folder"
    bl_description = "Open the local folder where this candidate was downloaded"

    def execute(self, context):
        props = context.scene.meshdock
        try:
            if props.last_job_id in {"", "__none__"} or props.candidate_id in {"", "__none__"}:
                raise ValueError("Select a downloaded candidate first")
            model_path = get_runtime().service.candidate_local_path(
                props.last_job_id, props.candidate_id
            )
            if not model_path.is_file():
                raise ValueError("The downloaded candidate file is missing")
            bpy.ops.wm.path_open(filepath=str(model_path.parent))
            return {"FINISHED"}
        except Exception as exc:
            return self.fail(exc)


class AI3D_OT_prepare_game_asset(bpy.types.Operator, _PipelineOperator):
    bl_idname = "meshdock.prepare_game_asset"
    bl_label = "Normalize Candidate"
    bl_description = "Apply optional Blender normalization to the imported candidate"
    bl_options = {"UNDO"}

    def execute(self, context):
        props = context.scene.meshdock
        try:
            options = json.loads(props.normalize_json or "{}")
            if not isinstance(options, dict):
                raise ValueError("Normalize Options must be a JSON object")
            result = get_runtime().service.normalize_candidate(
                props.last_job_id, props.candidate_id, options
            )
            if result.get("result_candidate_id"):
                props.candidate_id = result["result_candidate_id"]
            self.report({"INFO"}, f"Normalized {result['mesh_count']} meshes")
            return {"FINISHED"}
        except Exception as exc:
            return self.fail(exc)


class AI3D_OT_pause_job(bpy.types.Operator, _PipelineOperator):
    bl_idname = "meshdock.pause_job"
    bl_label = "Pause Local Orchestration"
    bl_description = "Pause queueing, polling and downloads locally; remote provider work may continue"

    def execute(self, context):
        try:
            get_runtime().service.pause_job(context.scene.meshdock.last_job_id)
            return {"FINISHED"}
        except Exception as exc:
            return self.fail(exc)


class AI3D_OT_unpause_job(bpy.types.Operator, _PipelineOperator):
    bl_idname = "meshdock.unpause_job"
    bl_label = "Continue Local Orchestration"

    def execute(self, context):
        try:
            get_runtime().service.resume_paused_job(context.scene.meshdock.last_job_id)
            return {"FINISHED"}
        except Exception as exc:
            return self.fail(exc)


class AI3D_OT_update_job_priority(bpy.types.Operator, _PipelineOperator):
    bl_idname = "meshdock.update_job_priority"
    bl_label = "Apply Queue Priority"

    def execute(self, context):
        props = context.scene.meshdock
        try:
            get_runtime().service.set_job_priority(props.last_job_id, props.priority)
            return {"FINISHED"}
        except Exception as exc:
            return self.fail(exc)


class AI3D_OT_show_candidate(bpy.types.Operator, _PipelineOperator):
    bl_idname = "meshdock.show_candidate"
    bl_label = "Show Candidate Only"
    bl_description = "Show only the selected candidate collection"

    def execute(self, context):
        props = context.scene.meshdock
        try:
            get_runtime().service.set_candidate_visibility(props.last_job_id, props.candidate_id)
            return {"FINISHED"}
        except Exception as exc:
            return self.fail(exc)


class AI3D_OT_show_all_candidates(bpy.types.Operator, _PipelineOperator):
    bl_idname = "meshdock.show_all_candidates"
    bl_label = "Show All Candidates"
    bl_description = "Restore visibility for all imported candidate collections in this job"

    def execute(self, context):
        try:
            props = context.scene.meshdock
            get_runtime().service.set_candidate_visibility(props.last_job_id, None)
            return {"FINISHED"}
        except Exception as exc:
            return self.fail(exc)


class AI3D_OT_reject_candidate(bpy.types.Operator, _PipelineOperator):
    bl_idname = "meshdock.reject_candidate"
    bl_label = "Reject Candidate"
    bl_description = "Mark this candidate as rejected without deleting its staged files"

    def execute(self, context):
        props = context.scene.meshdock
        try:
            get_runtime().service.reject_candidate(
                props.last_job_id, props.candidate_id, props.candidate_note
            )
            self.report({"INFO"}, "Candidate rejected")
            return {"FINISHED"}
        except Exception as exc:
            return self.fail(exc)


class AI3D_OT_render_review_pack(bpy.types.Operator, _PipelineOperator):
    bl_idname = "meshdock.render_review_pack"
    bl_label = "Render Review Pack"
    bl_description = "Render eight consistent turntable views and a local HTML review sheet"

    def execute(self, context):
        props = context.scene.meshdock
        try:
            result = get_runtime().service.render_review_pack(props.last_job_id, props.candidate_id)
            self.report({"INFO"}, f'Rendered {len(result["artifact"]["frames"])} review views')
            return {"FINISHED"}
        except Exception as exc:
            return self.fail(exc)


class AI3D_OT_delete_candidate(bpy.types.Operator, _PipelineOperator):
    bl_idname = "meshdock.delete_candidate"
    bl_label = "Delete Candidate"
    bl_description = "Delete this unselected candidate collection and its staged model/preview files"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        props = context.scene.meshdock
        try:
            get_runtime().service.delete_candidate(props.last_job_id, props.candidate_id)
            self.report({"INFO"}, "Candidate deleted")
            return {"FINISHED"}
        except Exception as exc:
            return self.fail(exc)


class AI3D_OT_regenerate_candidate(bpy.types.Operator, _PipelineOperator):
    bl_idname = "meshdock.regenerate_candidate"
    bl_label = "Regenerate Candidate"
    bl_description = "Create one new billable generation job from this candidate's original specification"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        props = context.scene.meshdock
        try:
            result = get_runtime().service.regenerate_candidate(props.last_job_id, props.candidate_id)
            props.last_job_id = result["id"]
            self.report({"INFO"}, "Candidate regeneration queued as a new job")
            return {"FINISHED"}
        except Exception as exc:
            return self.fail(exc)


class AI3D_OT_process_candidate(bpy.types.Operator, _PipelineOperator):
    bl_idname = "meshdock.process_candidate"
    bl_label = "Start Provider Process"
    bl_description = "Submit an immutable provider post-processing artifact"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        props = context.scene.meshdock
        try:
            params = effective_process_params(props)
            task = get_runtime().service.submit_candidate_process(
                props.last_job_id, props.candidate_id, props.process_operation,
                props.process_provider, params,
            )
            props.last_process_task_id = task["id"]
            self.report({"INFO"}, f'{props.process_operation} queued')
            return {"FINISHED"}
        except Exception as exc:
            return self.fail(exc)


class AI3D_OT_cancel_process(bpy.types.Operator, _PipelineOperator):
    bl_idname = "meshdock.cancel_process"
    bl_label = "Cancel Provider Process"
    bl_description = "Stop local polling/download for the active process task"

    def execute(self, context):
        props = context.scene.meshdock
        try:
            get_runtime().service.cancel_process_task(props.last_job_id, props.last_process_task_id)
            return {"FINISHED"}
        except Exception as exc:
            return self.fail(exc)


class AI3D_OT_resume_process(bpy.types.Operator, _PipelineOperator):
    bl_idname = "meshdock.resume_process"
    bl_label = "Resume Provider Process"
    bl_description = "Resume polling and download for an already-submitted provider task"

    def execute(self, context):
        props = context.scene.meshdock
        try:
            get_runtime().service.resume_process_task(
                props.last_job_id, props.last_process_task_id
            )
            self.report({"INFO"}, "Provider processing resumed")
            return {"FINISHED"}
        except Exception as exc:
            return self.fail(exc)


class AI3D_OT_pause_process(bpy.types.Operator, _PipelineOperator):
    bl_idname = "meshdock.pause_process"
    bl_label = "Pause Local Process Polling"

    def execute(self, context):
        props = context.scene.meshdock
        try:
            get_runtime().service.pause_process_task(props.last_job_id, props.last_process_task_id)
            return {"FINISHED"}
        except Exception as exc:
            return self.fail(exc)


class AI3D_OT_unpause_process(bpy.types.Operator, _PipelineOperator):
    bl_idname = "meshdock.unpause_process"
    bl_label = "Continue Local Process Polling"

    def execute(self, context):
        props = context.scene.meshdock
        try:
            get_runtime().service.resume_paused_process_task(
                props.last_job_id, props.last_process_task_id
            )
            return {"FINISHED"}
        except Exception as exc:
            return self.fail(exc)


class AI3D_OT_preview_character_action(bpy.types.Operator, _PipelineOperator):
    bl_idname = "meshdock.preview_character_action"
    bl_label = "Preview Animation"
    bl_description = "Activate the selected imported Action and play it in the Blender timeline"

    def execute(self, context):
        props = context.scene.meshdock
        try:
            if props.action_name == "__none__":
                raise ValueError("No candidate animation Action is available")
            result = get_runtime().service.activate_candidate_action(
                props.last_job_id, props.candidate_id, props.action_name
            )
            if context.screen and not context.screen.is_animation_playing:
                bpy.ops.screen.animation_play()
            self.report(
                {"INFO"},
                f'Previewing {result["action"]} ({result["frame_start"]}-{result["frame_end"]})',
            )
            return {"FINISHED"}
        except Exception as exc:
            return self.fail(exc)


class AI3D_OT_stop_character_preview(bpy.types.Operator, _PipelineOperator):
    bl_idname = "meshdock.stop_character_preview"
    bl_label = "Stop Preview"
    bl_description = "Stop Blender timeline playback"

    def execute(self, context):
        if context.screen and context.screen.is_animation_playing:
            bpy.ops.screen.animation_play()
        return {"FINISHED"}


class AI3D_OT_approve_candidate(bpy.types.Operator, _PipelineOperator):
    bl_idname = "meshdock.approve_candidate"
    bl_label = "Select Candidate"
    bl_description = "Select the imported candidate for export; this action remains Blender-user controlled"

    def execute(self, context):
        props = context.scene.meshdock
        try:
            get_runtime().service.select_candidate(props.last_job_id, props.candidate_id)
            self.report({"INFO"}, "Candidate selected")
            return {"FINISHED"}
        except Exception as exc:
            return self.fail(exc)


class AI3D_OT_export_reviewed_asset(bpy.types.Operator, _PipelineOperator):
    bl_idname = "meshdock.export_reviewed_asset"
    bl_label = "Export Selected Asset"
    bl_description = "Export the selected candidate; external directories are Blender-user controlled"

    def execute(self, context):
        props = context.scene.meshdock
        try:
            options = json.loads(props.export_json or "{}")
            if not isinstance(options, dict):
                raise ValueError("Export Options must be a JSON object")
            destination_root = Path(bpy.path.abspath(props.export_directory)) if props.export_directory else None
            result = get_runtime().service.export_selected_asset(
                props.last_job_id, props.export_format, options, destination_root
            )
            self.report({"INFO"}, f"Exported {result['artifact']['filename']}")
            return {"FINISHED"}
        except Exception as exc:
            return self.fail(exc)
