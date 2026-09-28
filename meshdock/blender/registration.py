from __future__ import annotations

import bpy

from .operators import (
    AI3D_OT_add_reference_image,
    AI3D_OT_set_credential_profile_enabled,
    AI3D_OT_approve_candidate,
    AI3D_OT_archive_job,
    AI3D_OT_cancel_job,
    AI3D_OT_cancel_process,
    AI3D_OT_pause_job,
    AI3D_OT_unpause_job,
    AI3D_OT_update_job_priority,
    AI3D_OT_pause_process,
    AI3D_OT_unpause_process,
    AI3D_OT_regenerate_candidate,
    AI3D_OT_create_and_generate,
    AI3D_OT_create_batch,
    AI3D_OT_create_support_bundle,
    AI3D_OT_delete_candidate,
    AI3D_OT_delete_credential_profile,
    AI3D_OT_export_reviewed_asset,
    AI3D_OT_import_candidate,
    AI3D_OT_import_all_models,
    AI3D_OT_open_candidate_folder,
    AI3D_OT_prepare_game_asset,
    AI3D_OT_preview_character_action,
    AI3D_OT_process_candidate,
    AI3D_OT_purge_archived_jobs,
    AI3D_OT_reject_candidate,
    AI3D_OT_render_review_pack,
    AI3D_OT_restore_archived_job,
    AI3D_OT_remove_reference_image,
    AI3D_OT_resume_job,
    AI3D_OT_resume_process,
    AI3D_OT_retry_job,
    AI3D_OT_manage_credentials,
    AI3D_OT_show_all_candidates,
    AI3D_OT_show_candidate,
    AI3D_OT_stop_character_preview,
)
from .panels import (
    AI3D_PT_asset_pipeline, AI3D_PT_candidate_review, AI3D_PT_character_preview,
    AI3D_PT_pipeline_tools, AI3D_PT_processing,
)
from .properties import (
    AI3D_PG_asset_pipeline, AI3D_PG_reference_image, register_properties, unregister_properties,
)
from .runtime import get_runtime, start_runtime, stop_runtime
from .translations import register_translations, unregister_translations
from .workspace import CLASSES as WORKSPACE_CLASSES
from .workbench_controls import CLASSES as CONTROL_CLASSES
from .account_checks import CLASSES as ACCOUNT_CLASSES
from .task_controls import CLASSES as TASK_CLASSES
from . import workbench

CLASSES = (
    AI3D_PG_reference_image,
    AI3D_PG_asset_pipeline,
    AI3D_OT_manage_credentials,
    AI3D_OT_set_credential_profile_enabled,
    AI3D_OT_delete_credential_profile,
    AI3D_OT_add_reference_image,
    AI3D_OT_create_batch,
    AI3D_OT_purge_archived_jobs,
    AI3D_OT_create_support_bundle,
    AI3D_OT_remove_reference_image,
    AI3D_OT_show_candidate,
    AI3D_OT_show_all_candidates,
    AI3D_OT_reject_candidate,
    AI3D_OT_render_review_pack,
    AI3D_OT_delete_candidate,
    AI3D_OT_process_candidate,
    AI3D_OT_cancel_process,
    AI3D_OT_pause_process,
    AI3D_OT_unpause_process,
    AI3D_OT_resume_process,
    AI3D_OT_preview_character_action,
    AI3D_OT_stop_character_preview,
    AI3D_OT_cancel_job,
    AI3D_OT_pause_job,
    AI3D_OT_unpause_job,
    AI3D_OT_update_job_priority,
    AI3D_OT_archive_job,
    AI3D_OT_restore_archived_job,
    AI3D_OT_create_and_generate,
    AI3D_OT_import_candidate,
    AI3D_OT_import_all_models,
    AI3D_OT_open_candidate_folder,
    AI3D_OT_prepare_game_asset,
    AI3D_OT_approve_candidate,
    AI3D_OT_resume_job,
    AI3D_OT_retry_job,
    AI3D_OT_regenerate_candidate,
    AI3D_OT_export_reviewed_asset,
    *WORKSPACE_CLASSES,
    *CONTROL_CLASSES,
    *TASK_CLASSES,
    *ACCOUNT_CLASSES,
    *workbench.CLASSES,
    AI3D_PT_asset_pipeline,
)


def _drain_bridge() -> float:
    try:
        runtime = get_runtime()
        runtime.bridge.drain_on_main_thread()
        runtime.drain_blender_tasks()
        runtime.drain_auto_imports()
        workbench.ensure_controllers()
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
    except RuntimeError:
        return 1.0
    return 0.1


def _restore_scene_tasks():
    get_runtime().restore_scene_tasks()
    return None


def register() -> None:
    register_translations()
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    register_properties()
    start_runtime()
    bpy.app.timers.register(_restore_scene_tasks, first_interval=0.1)
    workbench.start()
    if not bpy.app.timers.is_registered(_drain_bridge):
        bpy.app.timers.register(_drain_bridge, first_interval=0.1, persistent=True)


def unregister() -> None:
    if bpy.app.timers.is_registered(_restore_scene_tasks):
        bpy.app.timers.unregister(_restore_scene_tasks)
    if bpy.app.timers.is_registered(_drain_bridge):
        bpy.app.timers.unregister(_drain_bridge)
    workbench.stop()
    stop_runtime()
    unregister_properties()
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    unregister_translations()
