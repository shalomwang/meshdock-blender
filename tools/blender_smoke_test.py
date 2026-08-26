"""Background smoke test executed by Blender, not regular Python."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bpy  # noqa: E402
import meshdock  # noqa: E402
from meshdock.core.errors import ValidationError  # noqa: E402
from meshdock.blender.runtime import get_runtime  # noqa: E402
from meshdock.blender.properties import (  # noqa: E402
    _candidate_items, _generation_account_items, _job_items, _provider_items,
    generation_topology_limits,
)


def main() -> None:
    staging_context = tempfile.TemporaryDirectory(prefix="meshdock-blender-smoke-")
    os.environ["MESHDOCK_DEV_MOCK"] = "1"
    os.environ["MESHDOCK_STAGING"] = staging_context.name
    meshdock.register()
    try:
        service = get_runtime().service
        props = bpy.context.scene.meshdock
        assert service.generation_constraints(
            "hunyuan_direct", {"hunyuan_direct": {"model": "3.0"}}
        )["constraints"]["multiview"]["max_images"] == 4
        assert service.generation_constraints(
            "hunyuan_direct", {"hunyuan_direct": {"model": "3.1"}}
        )["constraints"]["multiview"]["max_images"] == 8
        props.input_mode = "multiview"
        provider_ids = {item[0] for item in _provider_items(props, bpy.context)}
        assert "mock" not in provider_ids
        assert {"tripo_cn", "tripo_global", "hunyuan_direct"} <= provider_ids
        props.input_mode = "text"
        account_ids = {
            item[0] for item in _generation_account_items(props, bpy.context)
        }
        assert "mock" not in account_ids
        manager_properties = bpy.ops.meshdock.manage_credentials.get_rna_type().properties
        assert "remember" not in manager_properties.keys()
        toggle_properties = (
            bpy.ops.meshdock.set_credential_profile_enabled.get_rna_type().properties.keys()
        )
        assert "desired_enabled" in toggle_properties, list(toggle_properties)
        visibility_profile = get_runtime().credentials.set(
            "tokenhub_global", "smoke-session-secret", note="Visibility Smoke"
        )
        try:
            visible_accounts = {item[0] for item in _generation_account_items(props, bpy.context)}
            assert f"tokenhub_global|{visibility_profile}" in visible_accounts
            assert bpy.ops.meshdock.set_credential_profile_enabled(
                provider="tokenhub_global",
                profile_id=visibility_profile,
                desired_enabled=False,
            ) == {"FINISHED"}
            hidden_accounts = {item[0] for item in _generation_account_items(props, bpy.context)}
            assert f"tokenhub_global|{visibility_profile}" not in hidden_accounts
            assert bpy.ops.meshdock.set_credential_profile_enabled(
                provider="tokenhub_global",
                profile_id=visibility_profile,
                desired_enabled=True,
            ) == {"FINISHED"}
        finally:
            get_runtime().credentials.delete_profile(visibility_profile)
        assert props.candidate_count == 1
        p1_limits = generation_topology_limits(SimpleNamespace(
            generation_account=f'tripo_global|{"a" * 32}',
            generation_model="P1-20260311", asset_profile="small_prop_v1",
            tripo_smart_low_poly=False, tripo_quad=False,
            tripo_geometry_quality="standard", hunyuan_generate_type="Normal",
        ))
        assert p1_limits["minimum"] == 50
        assert p1_limits["maximum"] == 20_000
        smart_quad_limits = generation_topology_limits(SimpleNamespace(
            generation_account=f'tripo_cn|{"b" * 32}',
            generation_model="v3.1-20260211", asset_profile="small_prop_v1",
            tripo_smart_low_poly=True, tripo_quad=True,
            tripo_geometry_quality="standard", hunyuan_generate_type="Normal",
        ))
        assert smart_quad_limits["minimum"] == 500
        assert smart_quad_limits["maximum"] == 10_000
        bpy.context.preferences.view.language = "zh_HANS"
        bpy.context.preferences.view.use_translate_interface = True
        assert bpy.app.translations.pgettext_iface("Model") == "模型"
        assert bpy.app.translations.pgettext_iface("Small prop (<1m)") == "小型道具（<1 米）"
        assert bpy.app.translations.pgettext_iface("Manage Accounts", "Operator") == "管理账号"
        assert bpy.app.translations.pgettext_iface("Custom Face Limit") == "自定义面数上限"
        icon_items = bpy.types.UILayout.bl_rna.functions["operator"].parameters["icon"].enum_items
        valid_icons = {item.identifier for item in icon_items}
        blender_ui_dir = ROOT / "meshdock" / "blender"
        for source_path in blender_ui_dir.glob("*.py"):
            source = source_path.read_text(encoding="utf-8")
            literal_icons = re.findall(r'icon\s*=\s*["\']([A-Z0-9_]+)["\']', source)
            unknown_icons = sorted(set(literal_icons) - valid_icons)
            assert not unknown_icons, f"unsupported Blender icons in {source_path.name}: {unknown_icons}"
        try:
            get_runtime().dispatch("create_asset_job", {"spec": {
                "asset_name": "blocked_account_binding", "prompt": "blocked bridge request",
                "provider": "tripo", "advanced": {
                    "tripo": {"account_profile": "a" * 32}
                },
            }})
            raise AssertionError("bridge accepted an account profile binding")
        except ValidationError:
            pass
        created = service.create_job(
            {
                "asset_name": "smoke_test_crate",
                "prompt": "A clean stylized wooden crate for a game",
                "asset_profile": "small_prop_v1",
                "provider": "mock",
                "candidate_count": 2,
            }
        )
        generated = service.generate_candidates(created["id"])
        deadline = time.monotonic() + 5
        while generated["state"] in {"queued", "submitted", "processing"} and time.monotonic() < deadline:
            time.sleep(0.02)
            generated = service.get_job(created["id"])
        assert generated["state"] == "candidates_ready", generated
        props.last_job_id = created["id"]
        job_items = _job_items(props, bpy.context)
        assert job_items is _job_items(props, bpy.context)
        assert any(item[0] == created["id"] for item in job_items)
        candidate_items = _candidate_items(props, bpy.context)
        assert candidate_items is _candidate_items(props, bpy.context)
        assert candidate_items[0][0] == generated["candidates"][0]["id"]
        assert candidate_items[0][4] == 0
        assert props.candidate_id == generated["candidates"][0]["id"]
        import_result = get_runtime().import_all_models(created["id"])
        assert import_result["model_count"] == 2, import_result
        assert import_result["layout"]["arranged"] == 2, import_result
        imported = service.get_job(created["id"])
        assert imported["state"] == "candidate_imported", imported
        prepared = service.normalize_candidate(created["id"], generated["candidates"][0]["id"])
        assert prepared["prepared"] is True
        selected = service.select_candidate(created["id"], generated["candidates"][0]["id"])
        assert selected["state"] == "approved", selected
        exported = service.export_selected_asset(created["id"])
        assert exported["job"]["state"] == "exported", exported
        assert exported["artifact"]["format"] == "glb", exported
        assert bpy.data.collections.get("MeshDock_Staging") is not None
        print("MESHDOCK_SMOKE_BEGIN")
        print(json.dumps({"job": imported["id"], "artifact": exported["artifact"]}, ensure_ascii=False))
        print("MESHDOCK_SMOKE_END")
    finally:
        meshdock.unregister()
        os.environ.pop("MESHDOCK_DEV_MOCK", None)
        os.environ.pop("MESHDOCK_STAGING", None)
        staging_context.cleanup()


if __name__ == "__main__":
    main()
