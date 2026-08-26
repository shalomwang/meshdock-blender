from __future__ import annotations

import json
import re
import zlib

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty, StringProperty

from ..core.profiles import PROFILES
from ..core.models import AssetSpec, InputMode, ProviderChoice
from ..core.provider_ids import (
    COMPARE_CN, COMPARE_GLOBAL, HUNYUAN_DIRECT, REAL_PROVIDERS,
    TOKENHUB_CN, TOKENHUB_GLOBAL, TOKENHUB_PROVIDERS,
    TRIPO_CN, TRIPO_GLOBAL, TRIPO_PROVIDERS, provider_family,
)


def _tripo_provider_for_choice(provider: str) -> str | None:
    if provider in TRIPO_PROVIDERS:
        return provider
    return {COMPARE_CN: TRIPO_CN, COMPARE_GLOBAL: TRIPO_GLOBAL}.get(provider)


def _hunyuan_provider_for_choice(provider: str) -> str | None:
    return HUNYUAN_DIRECT if provider in {HUNYUAN_DIRECT, COMPARE_CN} else None


def _tokenhub_provider_for_choice(provider: str) -> str | None:
    if provider in TOKENHUB_PROVIDERS:
        return provider
    return TOKENHUB_GLOBAL if provider == COMPARE_GLOBAL else None


def _enum_item(identifier: str, label: str, description: str):
    if identifier in {"auto", "__auto__", "__none__", "Normal", "small_prop_v1"}:
        return (identifier, label, description, 0, 0)
    number = zlib.crc32(identifier.encode("utf-8")) & 0x7FFFFFFF
    return (identifier, label, description, 0, number or 1)


def _profile_items(_self, _context):
    return [_enum_item(profile.id, profile.label, "") for profile in PROFILES.values()]


_MODE_LABELS = {
    "text": ("Text Prompt", "Generate from a text prompt"),
    "image": ("Single Image", "Generate from one front reference"),
    "multiview": ("Multiple Views", "Generate from several provider-supported views"),
}
_PROVIDER_LABELS = {
    TRIPO_CN: "Tripo Mainland China",
    TRIPO_GLOBAL: "Tripo Global",
    HUNYUAN_DIRECT: "Hunyuan Direct",
    TOKENHUB_CN: "Tencent TokenHub China",
    TOKENHUB_GLOBAL: "Tencent TokenHub Global",
}
_MODEL_ITEMS = {
    TRIPO_CN: (
        ("v3.1-20260211", "v3.1", "Current v3.1 model"),
        ("v3.0-20250812", "v3.0", "v3.0 model"),
        ("v2.5-20250123", "v2.5", "Legacy model with fewer advanced options"),
        ("P1-20260311", "P1", "Precision/low-poly P-series model"),
    ),
    TRIPO_GLOBAL: (
        ("v3.1-20260211", "v3.1", "Current v3.1 model"),
        ("v3.0-20250812", "v3.0", "v3.0 model"),
        ("v2.5-20250123", "v2.5", "Legacy model with fewer advanced options"),
        ("P1-20260311", "P1", "Precision/low-poly P-series model"),
    ),
    HUNYUAN_DIRECT: (
        ("3.1", "3.1", "Supports up to eight input views"),
        ("3.0", "3.0", "Supports four cardinal input views"),
    ),
    TOKENHUB_CN: (
        ("hy-3d-3.1", "Hunyuan 3.1", "Hunyuan generation through this account"),
        ("hy-3d-3.0", "Hunyuan 3.0", "Hunyuan 3.0 through this account"),
        ("tripo-3d-3.1", "Tripo 3.1", "Tripo 3.1 through this account"),
        ("tripo-3d-p1", "Tripo P1", "Tripo P1 through this account"),
    ),
    TOKENHUB_GLOBAL: (
        ("hy-3d-3.1", "Hunyuan 3.1", "Hunyuan generation through this account"),
        ("hy-3d-3.0", "Hunyuan 3.0", "Hunyuan 3.0 through this account"),
        ("tripo-3d-3.1", "Tripo 3.1", "Tripo 3.1 through this account"),
        ("tripo-3d-p1", "Tripo P1", "Tripo P1 through this account"),
    ),
}
_PROCESS_LABELS = {
    "retopology": "Retopology", "uv": "UV Unwrap", "texture": "Texture",
    "segment": "Segment", "convert": "Convert", "rig_check": "Riggability Check",
    "rig": "Auto Rig", "animate": "Animate",
}
_ENUM_ITEM_CACHE: dict[tuple, list[tuple[str, str, str]]] = {}
_INPUT_UPDATE_GUARD: set[int] = set()
_TOPOLOGY_UPDATE_GUARD: set[int] = set()


def _cached_enum_items(key: tuple, items: list[tuple]):
    # Blender retains pointers to callback strings; keep each returned list alive.
    return _ENUM_ITEM_CACHE.setdefault(key, items)


def _with_valid_enum_default(items: list[tuple]) -> list[tuple]:
    """Blender initializes dynamic enums to numeric zero; bind it to item one."""
    if not items:
        return items
    first = items[0]
    items[0] = (first[0], first[1], first[2], first[3], 0)
    return items


def _split_generation_account(identifier: str) -> tuple[str | None, str | None]:
    if identifier == "mock":
        return "mock", None
    if "|" not in identifier:
        return None, None
    provider, profile_id = identifier.split("|", 1)
    if provider not in REAL_PROVIDERS or not re.fullmatch(r"[a-f0-9]{32}", profile_id):
        return None, None
    return provider, profile_id


def selected_generation_account(self) -> tuple[str, str | None]:
    provider, profile_id = _split_generation_account(
        str(getattr(self, "generation_account", ""))
    )
    if provider:
        return provider, profile_id
    # Keep old saved scenes usable until a visible account is selected.
    legacy = str(getattr(self, "provider", ""))
    return (legacy if legacy in REAL_PROVIDERS else "auto"), None


def _model_advanced(provider: str, model: str) -> dict:
    if provider in TRIPO_PROVIDERS:
        return {"tripo": {"model": model}}
    if provider == HUNYUAN_DIRECT:
        return {"hunyuan_direct": {"model": model}}
    if provider in TOKENHUB_PROVIDERS:
        return {"tokenhub": {"model": model}}
    return {}


def _model_supports_mode(runtime, provider: str, model: str, mode: str) -> bool:
    adapter = runtime.service.providers.get(provider)
    if adapter is None:
        return False
    try:
        return mode in adapter.resolve_input_constraints(_model_advanced(provider, model))
    except Exception:
        return False


def generation_topology_limits(self) -> dict[str, object]:
    """Resolve the face-limit contract currently visible in the generation UI."""
    provider, _profile_id = selected_generation_account(self)
    model = str(getattr(self, "generation_model", ""))
    profile = PROFILES.get(str(getattr(self, "asset_profile", "")))
    preset = profile.triangle_hard_cap if profile else 5_000
    if provider in TRIPO_PROVIDERS:
        if model == "P1-20260311":
            return {
                "supported": True, "minimum": 50, "maximum": 20_000,
                "preset": min(max(preset, 50), 20_000),
                "note": "P1 is a dedicated low-poly model.",
            }
        if model in {"v3.0-20250812", "v3.1-20260211"} and self.tripo_smart_low_poly:
            maximum = 10_000 if self.tripo_quad else 20_000
            return {
                "supported": True, "minimum": 500, "maximum": maximum,
                "preset": min(max(preset, 500), maximum),
                "note": "Smart Low Poly uses a restricted face range.",
            }
        maximum = {
            "v2.5-20250123": 500_000,
            "v3.0-20250812": 2_000_000 if self.tripo_geometry_quality == "detailed" else 1_000_000,
            "v3.1-20260211": 2_000_000 if self.tripo_geometry_quality == "detailed" else 1_500_000,
        }.get(model, 1_500_000)
        if self.tripo_quad:
            maximum = min(maximum, 150_000)
        return {
            "supported": True, "minimum": 500, "maximum": maximum,
            "preset": min(max(preset, 500), maximum),
            "note": "The limit is a maximum; the provider may return fewer faces.",
        }
    is_hunyuan = provider == HUNYUAN_DIRECT or (
        provider in TOKENHUB_PROVIDERS and model.startswith("hy-3d-")
    )
    if is_hunyuan:
        if self.hunyuan_generate_type == "LowPoly":
            return {
                "supported": False,
                "reason": "Low Poly uses provider-controlled intelligent topology; Face Count is not accepted.",
            }
        minimum = 10_000 if self.hunyuan_generate_type == "Normal" else 3_000
        return {
            "supported": True, "minimum": minimum, "maximum": 1_500_000,
            "preset": min(max(preset, minimum), 1_500_000),
            "note": "Custom face count is billed as an additional Hunyuan option.",
        }
    return {"supported": False, "reason": "This model does not publish a face-limit parameter."}


def _clamp_generation_face_limit(self) -> None:
    identity = self.as_pointer() if hasattr(self, "as_pointer") else id(self)
    if identity in _TOPOLOGY_UPDATE_GUARD:
        return
    if hasattr(self, "is_property_set") and not self.is_property_set("generation_account"):
        return
    _TOPOLOGY_UPDATE_GUARD.add(identity)
    try:
        limits = generation_topology_limits(self)
        if not limits.get("supported"):
            if getattr(self, "use_custom_face_limit", False):
                self.use_custom_face_limit = False
            return
        value = int(getattr(self, "target_face_count", limits["preset"]))
        clamped = min(max(value, int(limits["minimum"])), int(limits["maximum"]))
        if value != clamped:
            self.target_face_count = clamped
    finally:
        _TOPOLOGY_UPDATE_GUARD.discard(identity)


def _topology_option_updated(self, context):
    _clamp_generation_face_limit(self)


def effective_advanced(self) -> dict:
    try:
        value = json.loads(self.advanced_json or "{}")
    except Exception:
        value = {}
    if not isinstance(value, dict):
        value = {}
    selected_provider, selected_profile = selected_generation_account(self)
    model = str(getattr(self, "generation_model", ""))
    tripo_provider = _tripo_provider_for_choice(selected_provider)
    if tripo_provider:
        tripo = value.setdefault("tripo", {})
        tripo.update({
            "model": model or self.tripo_model,
            "texture": self.tripo_texture,
            "pbr": self.tripo_pbr,
            "export_uv": self.tripo_export_uv,
        })
        account_profile = selected_profile or getattr(self, f"{tripo_provider}_account_profile", "")
        if re.fullmatch(r"[a-f0-9]{32}", account_profile or ""):
            tripo["account_profile"] = account_profile
        if self.input_mode == "image":
            tripo.update({
                "enable_image_autofix": self.tripo_enable_image_autofix,
                "texture_alignment": self.tripo_texture_alignment,
                "orientation": self.tripo_orientation,
            })
        selected_model = model or self.tripo_model
        if selected_model in {"v3.0-20250812", "v3.1-20260211"}:
            tripo.update({
                "texture_quality": self.tripo_texture_quality,
                "geometry_quality": self.tripo_geometry_quality,
                "quad": self.tripo_quad,
                "smart_low_poly": self.tripo_smart_low_poly,
                "generate_parts": self.tripo_generate_parts,
            })
        elif selected_model == "P1-20260311":
            tripo["texture_quality"] = self.tripo_texture_quality
        if self.use_custom_face_limit:
            tripo["face_limit"] = int(self.target_face_count)
        else:
            tripo.pop("face_limit", None)
    hunyuan_provider = _hunyuan_provider_for_choice(selected_provider)
    if hunyuan_provider:
        hunyuan = value.setdefault("hunyuan_direct", {})
        hunyuan.update({
            "model": model or self.hunyuan_direct_model,
            "enable_pbr": self.hunyuan_enable_pbr,
            "generate_type": self.hunyuan_generate_type,
            "polygon_type": self.hunyuan_polygon_type,
        })
        account_profile = selected_profile or getattr(self, f"{hunyuan_provider}_account_profile", "")
        if re.fullmatch(r"[a-f0-9]{32}", account_profile or ""):
            hunyuan["account_profile"] = account_profile
        if self.hunyuan_result_format != "default":
            hunyuan["result_format"] = self.hunyuan_result_format
        else:
            hunyuan.pop("result_format", None)
        if self.hunyuan_generate_type == "LowPoly":
            hunyuan.pop("face_count", None)
        elif self.use_custom_face_limit:
            hunyuan["face_count"] = int(self.target_face_count)
        else:
            hunyuan.pop("face_count", None)
    tokenhub_provider = _tokenhub_provider_for_choice(selected_provider)
    if tokenhub_provider:
        tokenhub = value.setdefault("tokenhub", {})
        tokenhub_model = model or self.tokenhub_model
        tokenhub["model"] = tokenhub_model
        account_profile = selected_profile or getattr(self, f"{tokenhub_provider}_account_profile", "")
        if re.fullmatch(r"[a-f0-9]{32}", account_profile or ""):
            tokenhub["account_profile"] = account_profile
        if tokenhub_model.startswith("hy-3d-"):
            tokenhub.update({
                "enable_pbr": self.hunyuan_enable_pbr,
                "generate_type": self.hunyuan_generate_type,
                "polygon_type": self.hunyuan_polygon_type,
            })
            if self.hunyuan_result_format != "default":
                tokenhub["result_format"] = self.hunyuan_result_format
            if self.hunyuan_generate_type == "LowPoly":
                tokenhub.pop("face_count", None)
            elif self.use_custom_face_limit:
                tokenhub["face_count"] = int(self.target_face_count)
            else:
                tokenhub.pop("face_count", None)
    return value


def _hunyuan_generate_type_items(self, _context):
    values = (
        [("Normal", "Normal", "Standard textured generation"),
         ("Geometry", "Geometry", "Geometry-focused generation")]
        if (
            self.tokenhub_model == "hy-3d-3.1"
            if _tokenhub_provider_for_choice(self.provider)
            else self.hunyuan_direct_model == "3.1"
        ) else
        [("Normal", "Normal", "Standard textured generation"),
         ("LowPoly", "Low Poly", "Low-polygon generation"),
         ("Geometry", "Geometry", "Geometry-focused generation"),
         ("Sketch", "Sketch", "Sketch-style generation")]
    )
    return _cached_enum_items(
        ("hunyuan_generate", self.provider, self.hunyuan_direct_model, self.tokenhub_model),
        [_enum_item(*item) for item in values],
    )


def _hunyuan_model_updated(self, context):
    is_31 = (
        self.tokenhub_model == "hy-3d-3.1"
        if _tokenhub_provider_for_choice(self.provider)
        else self.hunyuan_direct_model == "3.1"
    )
    if is_31:
        self.hunyuan_generate_type = "Normal"
    _clamp_generation_face_limit(self)
    _provider_input_updated(self, context)


def _hunyuan_generate_type_updated(self, context):
    if self.hunyuan_generate_type == "LowPoly":
        self.use_custom_face_limit = False
    _clamp_generation_face_limit(self)
    _provider_input_updated(self, context)


def _tripo_parts_updated(self, _context):
    if self.tripo_generate_parts:
        self.tripo_texture = False
        self.tripo_pbr = False
        self.tripo_quad = False


def _tripo_texture_quality_items(self, _context):
    values = [
        ("detailed", "Detailed", "Detailed texture"),
        ("standard", "Standard", "Lower cost"),
    ]
    if str(getattr(self, "generation_model", "")) != "P1-20260311":
        values.append(("extreme", "Extreme", "Highest texture detail; exact cost unavailable"))
    return _cached_enum_items(
        ("tripo_texture_quality", str(getattr(self, "generation_model", ""))),
        _with_valid_enum_default([_enum_item(*item) for item in values]),
    )


def generation_constraints(self) -> dict:
    from .runtime import get_runtime
    provider, _profile_id = selected_generation_account(self)
    return get_runtime().service.generation_constraints(provider, effective_advanced(self))


def generation_cost_estimate(self) -> float | None:
    """Estimate from provider-published rules without reading a credential."""
    from .runtime import get_runtime

    provider, _profile_id = selected_generation_account(self)
    adapter = get_runtime().service.providers.get(provider)
    if adapter is None:
        return None
    mode = InputMode(str(self.input_mode))
    references: dict[str, str] = {}
    if mode == InputMode.IMAGE:
        references = {"front": "0" * 32}
    elif mode == InputMode.MULTIVIEW:
        references = {"front": "0" * 32, "right": "1" * 32}
    spec = AssetSpec(
        asset_name="cost_estimate",
        prompt="cost estimate" if mode == InputMode.TEXT else "",
        input_mode=mode,
        reference_images=references,
        asset_profile=str(self.asset_profile),
        provider=ProviderChoice(provider),
        candidate_count=int(self.candidate_count),
        advanced=effective_advanced(self),
    )
    adapter.validate_advanced(spec)
    return adapter.estimate_generation_credits(spec)


def _input_mode_items(self, _context):
    return _cached_enum_items(
        ("input", "all"), [_enum_item(mode, *label) for mode, label in _MODE_LABELS.items()]
    )


def _provider_items(self, _context):
    labels = {
        "auto": ("Automatic", "Use the first configured compatible service"),
        TRIPO_CN: ("Tripo Mainland China", "Generate with the Tripo mainland service"),
        HUNYUAN_DIRECT: ("Hunyuan Direct", "Generate with the direct Hunyuan 3D API"),
        TOKENHUB_CN: ("Tencent TokenHub China", "Use Tencent's China model gateway"),
        COMPARE_CN: ("Compare Tripo CN + Hunyuan", "Compare Tripo mainland and direct Hunyuan"),
        TRIPO_GLOBAL: ("Tripo Global", "Generate with the Tripo global service"),
        TOKENHUB_GLOBAL: ("Tencent TokenHub Global", "Use Tencent's international model gateway"),
        COMPARE_GLOBAL: ("Compare Tripo + TokenHub", "Compare Tripo global and TokenHub global"),
    }
    try:
        from .runtime import get_runtime

        runtime = get_runtime()
        model_options = {provider: {
            "tripo": {"model": self.tripo_model}
        } for provider in TRIPO_PROVIDERS}
        model_options[HUNYUAN_DIRECT] = {
            "hunyuan_direct": {"model": self.hunyuan_direct_model}
        }
        model_options.update({provider: {
            "tokenhub": {"model": self.tokenhub_model}
        } for provider in TOKENHUB_PROVIDERS})
        compatible: list[str] = []
        available: list[str] = []
        for provider in REAL_PROVIDERS:
            adapter = runtime.service.providers.get(provider)
            if adapter and self.input_mode in adapter.resolve_input_constraints(model_options[provider]):
                compatible.append(provider)
                if adapter.status().available:
                    available.append(provider)
        ids: list[str] = []
        if available:
            ids.append("auto")
        for identifier in (TRIPO_CN, HUNYUAN_DIRECT, TOKENHUB_CN):
            if identifier in compatible:
                ids.append(identifier)
        if {TRIPO_CN, HUNYUAN_DIRECT}.issubset(compatible):
            ids.append(COMPARE_CN)
        for identifier in (TRIPO_GLOBAL, TOKENHUB_GLOBAL):
            if identifier in compatible:
                ids.append(identifier)
        if {TRIPO_GLOBAL, TOKENHUB_GLOBAL}.issubset(compatible):
            ids.append(COMPARE_GLOBAL)
        if not ids:
            ids = [item for item in REAL_PROVIDERS if item in compatible]
        return _cached_enum_items(
            ("providers", self.input_mode, *ids),
            [_enum_item(identifier, *labels[identifier]) for identifier in ids],
        )
    except Exception:
        ids = ["auto", *REAL_PROVIDERS, COMPARE_CN, COMPARE_GLOBAL]
        return _cached_enum_items(
            ("providers", "fallback", self.input_mode),
            [_enum_item(identifier, *labels[identifier]) for identifier in ids],
        )


def _generation_account_items(self, _context):
    """Configured accounts compatible with the selected generation method."""
    try:
        from bpy.app.translations import pgettext_iface as iface_
        from .runtime import get_runtime

        runtime = get_runtime()
        source_labels = {
            "session": "This session", "stored": "Saved", "environment": "Environment",
        }
        items: list[tuple] = []
        signature: list[tuple] = []
        for provider in REAL_PROVIDERS:
            models = [
                model for model, _label, _description in _MODEL_ITEMS[provider]
                if _model_supports_mode(runtime, provider, model, self.input_mode)
            ]
            if not models:
                continue
            profiles = runtime.credentials.list_profiles(provider)
            for index, profile in enumerate(profiles, start=1):
                if not profile["enabled"]:
                    continue
                profile_id = str(profile["id"])
                note = str(profile["note"]).strip() or f'{iface_("Profile")} {index}'
                provider_label = iface_(_PROVIDER_LABELS[provider])
                source = iface_(source_labels.get(str(profile["source"]), "Available"))
                identifier = f"{provider}|{profile_id}"
                items.append(_enum_item(
                    identifier, f"{note} · {provider_label}",
                    f"{source} · {provider_label}",
                ))
                signature.append((
                    provider, profile_id, note, str(profile["source"]), bool(profile["enabled"]),
                ))
        if not items:
            items = [_enum_item("__none__", iface_("No compatible accounts"), iface_("Add an account first"))]
        return _cached_enum_items(
            ("generation_accounts", self.input_mode, *signature),
            _with_valid_enum_default(items),
        )
    except Exception:
        return _cached_enum_items(
            ("generation_accounts", "fallback", self.input_mode),
            _with_valid_enum_default([
                _enum_item("__none__", "No compatible accounts", "Add an account first")
            ]),
        )


def _generation_model_items(self, _context):
    provider, _profile_id = selected_generation_account(self)
    values = list(_MODEL_ITEMS.get(provider, ()))
    try:
        from .runtime import get_runtime

        runtime = get_runtime()
        values = [
            item for item in values
            if _model_supports_mode(runtime, provider, item[0], self.input_mode)
        ]
    except Exception:
        pass
    if not values:
        values = [("__none__", "No compatible models", "Choose another account")]
    return _cached_enum_items(
        ("generation_models", provider, self.input_mode, *(item[0] for item in values)),
        _with_valid_enum_default([_enum_item(*item) for item in values]),
    )


def _sync_generation_selection(self) -> None:
    provider, profile_id = selected_generation_account(self)
    self.provider = provider
    if profile_id:
        setattr(self, f"{provider}_account_profile", profile_id)
    model = str(getattr(self, "generation_model", ""))
    if provider in TRIPO_PROVIDERS and model in {item[0] for item in _MODEL_ITEMS[provider]}:
        self.tripo_model = model
    elif provider == HUNYUAN_DIRECT and model in {item[0] for item in _MODEL_ITEMS[provider]}:
        self.hunyuan_direct_model = model
    elif provider in TOKENHUB_PROVIDERS and model in {item[0] for item in _MODEL_ITEMS[provider]}:
        self.tokenhub_model = model


def _generation_account_updated(self, context):
    provider, profile_id = _split_generation_account(str(self.generation_account))
    if provider and profile_id:
        try:
            from .runtime import get_runtime
            get_runtime().credentials.select(provider, profile_id)
        except Exception:
            pass
    _sync_generation_selection(self)
    _clamp_generation_face_limit(self)
    _provider_input_updated(self, context)


def _generation_model_updated(self, context):
    _sync_generation_selection(self)
    if self.generation_model == "P1-20260311" and self.tripo_texture_quality == "extreme":
        self.tripo_texture_quality = "detailed"
    _clamp_generation_face_limit(self)
    _hunyuan_model_updated(self, context)


def _credential_items(self, provider: str):
    try:
        from .runtime import get_runtime

        profiles = get_runtime().credentials.list_profiles(provider)
        signature = tuple(
            (str(item["id"]), str(item["note"]), str(item["source"]), bool(item["enabled"]))
            for item in profiles
        )
        items = [_enum_item("__auto__", "Automatic", "Use the first enabled profile")]
        source_labels = {"session": "This session", "stored": "Saved", "environment": "Environment"}
        for index, item in enumerate(profiles, start=1):
            if not item["enabled"]:
                continue
            label = str(item["note"]) or f"Profile {index}"
            source = source_labels.get(str(item["source"]), "Available")
            items.append(_enum_item(str(item["id"]), label, source))
        return _cached_enum_items(("accounts", provider, signature), items)
    except Exception:
        return _cached_enum_items(
            ("accounts", provider, "fallback"),
            [_enum_item("__auto__", "Automatic", "Use the first enabled profile")],
        )


def _tripo_cn_account_items(self, _context):
    return _credential_items(self, TRIPO_CN)


def _tripo_global_account_items(self, _context):
    return _credential_items(self, TRIPO_GLOBAL)


def _hunyuan_direct_account_items(self, _context):
    return _credential_items(self, HUNYUAN_DIRECT)


def _tokenhub_cn_account_items(self, _context):
    return _credential_items(self, TOKENHUB_CN)


def _tokenhub_global_account_items(self, _context):
    return _credential_items(self, TOKENHUB_GLOBAL)


def _account_updated(self, _context):
    from .runtime import get_runtime

    for provider in REAL_PROVIDERS:
        attribute = f"{provider}_account_profile"
        identifier = getattr(self, attribute, "")
        if re.fullmatch(r"[a-f0-9]{32}", identifier or ""):
            try:
                get_runtime().credentials.select(provider, identifier)
            except Exception:
                pass


def _provider_input_updated(self, _context):
    identity = self.as_pointer()
    if identity in _INPUT_UPDATE_GUARD:
        return
    _INPUT_UPDATE_GUARD.add(identity)
    try:
        accounts = [item[0] for item in _generation_account_items(self, _context)]
        if accounts and self.generation_account not in accounts:
            self.generation_account = accounts[0]
        models = [item[0] for item in _generation_model_items(self, _context)]
        if models and self.generation_model not in models:
            self.generation_model = models[0]
        _sync_generation_selection(self)
        constraints = generation_constraints(self)
        if self.input_mode not in constraints["modes"]:
            return
        constraint = constraints["constraints"][self.input_mode]
        allowed = set(constraint["allowed_views"])
        formats = set(constraint["formats"])
        for index in reversed(range(len(self.reference_images))):
            item = self.reference_images[index]
            invalid_dimension = (
                (constraint["min_dimension"] and min(item.width, item.height) < constraint["min_dimension"])
                or (constraint["max_dimension"] and max(item.width, item.height) > constraint["max_dimension"])
            )
            invalid_bytes = (
                constraint.get("max_file_bytes", 0)
                and item.bytes > constraint["max_file_bytes"]
            )
            if item.view not in allowed or item.format not in formats or invalid_dimension or invalid_bytes:
                self.reference_images.remove(index)
        maximum = int(constraint["max_images"])
        required = set(constraint["required_views"])
        while len(self.reference_images) > maximum:
            removable = next(
                (index for index in reversed(range(len(self.reference_images)))
                 if self.reference_images[index].view not in required),
                len(self.reference_images) - 1,
            )
            self.reference_images.remove(removable)
        byte_limit = int(constraint["max_total_bytes"])
        while byte_limit and sum(item.bytes for item in self.reference_images) > byte_limit:
            removable = next(
                (index for index in reversed(range(len(self.reference_images)))
                 if self.reference_images[index].view not in required),
                None,
            )
            if removable is None:
                break
            self.reference_images.remove(removable)
    except Exception:
        return
    finally:
        _INPUT_UPDATE_GUARD.discard(identity)


def _process_capabilities(self) -> dict:
    if not self.last_job_id or self.last_job_id == "__none__" or self.candidate_id == "__none__":
        return {}
    from .runtime import get_runtime
    return get_runtime().service.available_process_operations(
        self.last_job_id, self.candidate_id, "auto"
    )["operations"]


def _process_operation_items(self, _context):
    try:
        operations = _process_capabilities(self)
        if operations:
            return _cached_enum_items(
                ("process", *operations),
                [_enum_item(key, _PROCESS_LABELS.get(key, key.title()), "Available for selected candidate") for key in operations],
            )
    except Exception:
        pass
    return _cached_enum_items(
        ("process", "none"),
        [_enum_item("__none__", "No compatible operations", "Select a compatible candidate")],
    )


def _process_provider_items(self, _context):
    try:
        providers = _process_capabilities(self).get(self.process_operation, {}).get("providers", [])
        items = [_enum_item("auto", "Auto", "Use a compatible provider")]
        items.extend(_enum_item(item, item.title(), f"Use {item.title()}") for item in providers)
        return _cached_enum_items(("process_provider", *providers), items)
    except Exception:
        return _cached_enum_items(
            ("process_provider", "fallback"),
            [_enum_item("auto", "Auto", "Use a compatible provider")],
        )


def selected_process_provider(self) -> str | None:
    """Resolve Auto exactly like the ordered provider list shown by the UI."""
    try:
        providers = _process_capabilities(self).get(
            self.process_operation, {}
        ).get("providers", [])
        requested = str(getattr(self, "process_provider", "auto"))
        if requested != "auto" and requested in providers:
            return requested
        return providers[0] if providers else None
    except Exception:
        return None


def _sync_process_controls(self, defaults: dict) -> None:
    if self.process_operation != "retopology":
        return
    provider = selected_process_provider(self)
    if provider in TRIPO_PROVIDERS:
        self.process_face_limit = min(
            max(int(defaults.get("face_limit", 5_000)), 1_000), 20_000
        )
        self.process_quad = bool(defaults.get("quad", False))
        self.process_bake = bool(defaults.get("bake", True))
    elif provider in TOKENHUB_PROVIDERS:
        polygon = str(defaults.get("polygon_type", "triangle"))
        level = str(defaults.get("face_level", "medium"))
        self.process_polygon_type = (
            polygon if polygon in {"triangle", "quadrilateral"} else "triangle"
        )
        self.process_face_level = (
            level if level in {"low", "medium", "high"} else "medium"
        )


def effective_process_params(self) -> dict:
    """Merge the friendly process controls over expert JSON."""
    try:
        value = json.loads(self.process_json or "{}")
    except Exception as exc:
        raise ValueError("Process Options must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("Process Options must be a JSON object")
    if self.process_operation != "retopology":
        return value
    provider = selected_process_provider(self)
    if provider in TRIPO_PROVIDERS:
        value.update({
            "face_limit": int(self.process_face_limit),
            "quad": bool(self.process_quad),
            "bake": bool(self.process_bake),
        })
    elif provider in TOKENHUB_PROVIDERS:
        value.update({
            "polygon_type": str(self.process_polygon_type),
            "face_level": str(self.process_face_level),
        })
    return value


def _process_operation_updated(self, _context):
    try:
        operation = _process_capabilities(self).get(self.process_operation, {})
        defaults = operation.get("defaults", {})
        self.process_json = json.dumps(defaults, separators=(",", ":"))
        providers = operation.get("providers", [])
        if self.process_provider not in {"auto", *providers}:
            self.process_provider = "auto"
        _sync_process_controls(self, defaults)
    except Exception:
        return


def _process_provider_updated(self, _context):
    try:
        operation = _process_capabilities(self).get(self.process_operation, {})
        _sync_process_controls(self, operation.get("defaults", {}))
    except Exception:
        return


def _candidate_updated(self, context):
    try:
        operations = _process_capabilities(self)
        target = next(iter(operations), "__none__")
        if self.process_operation not in operations:
            self.process_operation = target
        elif target != "__none__":
            _process_operation_updated(self, context)
    except Exception:
        return


def _job_updated(self, context):
    try:
        candidates = _candidate_items(self, context)
        target = candidates[0][0]
        if self.candidate_id not in {item[0] for item in candidates}:
            self.candidate_id = target
        _candidate_updated(self, context)
    except Exception:
        return

def _job_items(_self, _context):
    try:
        from .runtime import get_runtime
        jobs = get_runtime().service.list_jobs(limit=100)["jobs"]
        if jobs:
            return [
                _enum_item(job["id"], f'{job["spec"]["asset_name"]} · {job["state"]}', job["id"])
                for job in jobs
            ]
    except Exception:
        pass
    return [_enum_item("__none__", "No jobs", "Create a generation job first")]


def _candidate_items(self, _context):
    try:
        from .runtime import get_runtime
        if self.last_job_id and self.last_job_id != "__none__":
            candidates = get_runtime().service.get_job(self.last_job_id)["candidates"]
            if candidates:
                return [
                    _enum_item(item["id"], f'{item["label"]} · {item["format"].upper()}', item["provider"])
                    for item in candidates
                ]
    except Exception:
        pass
    return [_enum_item("__none__", "No candidates", "Wait for generation to finish")]


def _action_items(self, _context):
    try:
        from .runtime import get_runtime
        if self.last_job_id != "__none__" and self.candidate_id != "__none__":
            names = get_runtime().service.list_candidate_actions(
                self.last_job_id, self.candidate_id
            )["actions"]
            if names:
                return [_enum_item(name, name, "Imported character animation") for name in names]
    except Exception:
        pass
    return [_enum_item("__none__", "No Actions", "Import an animated character candidate first")]


class AI3D_PG_reference_image(bpy.types.PropertyGroup):
    reference_id: StringProperty(name="Reference ID", options={"HIDDEN"})
    view: StringProperty(name="View")
    filename: StringProperty(name="File")
    dimensions: StringProperty(name="Dimensions")
    format: StringProperty(name="Format", options={"HIDDEN"})
    bytes: IntProperty(name="Bytes", options={"HIDDEN"})
    width: IntProperty(name="Width", options={"HIDDEN"})
    height: IntProperty(name="Height", options={"HIDDEN"})


class AI3D_PG_asset_pipeline(bpy.types.PropertyGroup):
    asset_name: StringProperty(name="Asset Name", default="sample_prop", maxlen=64)
    prompt: StringProperty(name="Prompt", default="A clean stylized game prop", maxlen=4000)
    input_mode: EnumProperty(
        name="Generation Method",
        items=[(mode, *label) for mode, label in _MODE_LABELS.items()],
        update=_provider_input_updated,
        default="text",
    )
    asset_profile: EnumProperty(
        name="Generation Preset", items=_profile_items, update=_topology_option_updated,
    )
    generation_account: EnumProperty(
        name="Account", items=_generation_account_items,
        update=_generation_account_updated,
    )
    generation_model: EnumProperty(
        name="Model", items=_generation_model_items,
        update=_generation_model_updated,
    )
    provider: EnumProperty(
        name="Service",
        items=_provider_items,
        update=_provider_input_updated,
    )
    tripo_model: EnumProperty(
        name="Model", update=_provider_input_updated,
        items=[
            ("v3.1-20260211", "v3.1", "Current v3.1 model"),
            ("v3.0-20250812", "v3.0", "v3.0 model"),
            ("v2.5-20250123", "v2.5", "Legacy model with fewer advanced options"),
            ("P1-20260311", "P1", "Precision/low-poly P-series model"),
        ], default="v3.1-20260211",
    )
    hunyuan_direct_model: EnumProperty(
        name="Model", update=_hunyuan_model_updated,
        items=[
            ("3.1", "3.1", "Supports up to eight input views"),
            ("3.0", "3.0", "Supports four cardinal input views"),
        ], default="3.1",
    )
    tokenhub_model: EnumProperty(
        name="Model", update=_hunyuan_model_updated,
        items=[
            ("hy-3d-3.1", "Hunyuan 3.1", "Hunyuan generation through TokenHub"),
            ("hy-3d-3.0", "Hunyuan 3.0", "Hunyuan 3.0 through TokenHub"),
            ("tripo-3d-3.1", "Tripo 3.1", "Tripo 3.1 through TokenHub"),
            ("tripo-3d-p1", "Tripo P1", "Tripo P1 through TokenHub"),
        ], default="hy-3d-3.1",
    )
    tripo_cn_account_profile: EnumProperty(
        name="Account", items=_tripo_cn_account_items, update=_account_updated,
    )
    tripo_global_account_profile: EnumProperty(
        name="Account", items=_tripo_global_account_items, update=_account_updated,
    )
    hunyuan_direct_account_profile: EnumProperty(
        name="Account", items=_hunyuan_direct_account_items, update=_account_updated,
    )
    tokenhub_cn_account_profile: EnumProperty(
        name="Account", items=_tokenhub_cn_account_items, update=_account_updated,
    )
    tokenhub_global_account_profile: EnumProperty(
        name="Account", items=_tokenhub_global_account_items, update=_account_updated,
    )
    tripo_texture: BoolProperty(name="Generate Texture", default=True)
    tripo_pbr: BoolProperty(name="Generate PBR", default=True)
    tripo_export_uv: BoolProperty(name="Export UV", default=True)
    tripo_enable_image_autofix: BoolProperty(
        name="Auto-fix Input Image", default=False,
        description="Ask Tripo to enhance a low-quality single input image",
    )
    tripo_texture_alignment: EnumProperty(
        name="Texture Alignment",
        items=[("original_image", "Original Image", "Prioritize source colors"),
               ("geometry", "Geometry", "Prioritize generated geometry")],
        default="original_image",
    )
    tripo_orientation: EnumProperty(
        name="Orientation",
        items=[("default", "Automatic", "Automatically orient the model"),
               ("align_image", "Align to Image", "Match the source image viewpoint")],
        default="default",
    )
    tripo_texture_quality: EnumProperty(
        name="Texture Quality",
        items=_tripo_texture_quality_items,
    )
    tripo_geometry_quality: EnumProperty(
        name="Geometry Quality",
        items=[("standard", "Standard", "Standard geometry"), ("detailed", "Detailed", "Detailed geometry")],
        default="standard", update=_topology_option_updated,
    )
    tripo_quad: BoolProperty(
        name="Quad Output", default=False, update=_topology_option_updated,
    )
    tripo_smart_low_poly: BoolProperty(
        name="Smart Low Poly", default=False, update=_topology_option_updated,
    )
    tripo_generate_parts: BoolProperty(
        name="Generate Parts", default=False, update=_tripo_parts_updated,
    )
    hunyuan_enable_pbr: BoolProperty(name="Generate PBR", default=True)
    hunyuan_generate_type: EnumProperty(
        name="Generation Type", items=_hunyuan_generate_type_items,
        update=_hunyuan_generate_type_updated,
    )
    hunyuan_polygon_type: EnumProperty(
        name="Polygon Type",
        items=[("triangle", "Triangles", "Triangle mesh"),
               ("quadrilateral", "Quads", "Quadrilateral mesh")], default="triangle",
    )
    hunyuan_result_format: EnumProperty(
        name="Extra Output Format",
        items=[("default", "Default GLB", "Use the normal GLB output"),
               ("FBX", "FBX", "Also request FBX"), ("STL", "STL", "Also request STL"),
               ("USDZ", "USDZ", "Also request USDZ")], default="default",
    )
    use_custom_face_limit: BoolProperty(
        name="Custom Face Limit", default=False, update=_topology_option_updated,
        description="Override the preset/provider face limit when the selected model supports it",
    )
    target_face_count: IntProperty(
        name="Maximum Faces", default=5_000, min=48, max=2_000_000,
        update=_topology_option_updated,
    )
    candidate_count: IntProperty(name="Candidates", default=1, min=1, max=4)
    priority: IntProperty(name="Priority", default=50, min=0, max=100)
    max_estimated_credits: FloatProperty(name="Credit Limit", default=0.0, min=0.0)
    allow_over_budget: BoolProperty(name="Allow Over Limit", default=False)
    batch_json: StringProperty(name="Batch Specs (JSON)", default="[]", maxlen=32768)
    batch_generate: BoolProperty(name="Generate Immediately", default=False)
    purge_older_than_days: IntProperty(name="Purge Archived Older Than", default=30, min=0, max=3650)
    last_job_id: EnumProperty(name="Job", items=_job_items, update=_job_updated)
    candidate_id: EnumProperty(name="Candidate", items=_candidate_items, update=_candidate_updated)
    candidate_note: StringProperty(name="Review Note", default="", maxlen=500)
    process_operation: EnumProperty(
        name="Process",
        items=_process_operation_items,
        update=_process_operation_updated,
    )
    process_provider: EnumProperty(
        name="Service",
        items=_process_provider_items,
        update=_process_provider_updated,
    )
    process_face_limit: IntProperty(
        name="Target Faces", default=5_000, min=1_000, max=20_000,
        description="Maximum face count requested from Tripo retopology",
    )
    process_quad: BoolProperty(
        name="Quad Output", default=False,
        description="Request quad topology instead of triangles",
    )
    process_bake: BoolProperty(
        name="Bake Textures", default=True,
        description="Bake the source appearance onto the retopologized mesh",
    )
    process_polygon_type: EnumProperty(
        name="Polygon Type",
        items=[
            ("triangle", "Triangles", "Triangle topology"),
            ("quadrilateral", "Quads", "Quadrilateral topology"),
        ],
        default="triangle",
    )
    process_face_level: EnumProperty(
        name="Face Level",
        items=[
            ("low", "Low", "Lowest provider-defined topology density"),
            ("medium", "Medium", "Balanced provider-defined topology density"),
            ("high", "High", "Highest provider-defined topology density"),
        ],
        default="medium",
    )
    process_json: StringProperty(name="Process Options (JSON)", default="{}", maxlen=4096)
    last_process_task_id: StringProperty(name="Process Task", default="", options={"HIDDEN"})
    action_name: EnumProperty(name="Animation Action", items=_action_items)
    advanced_json: StringProperty(name="Extra Options (JSON)", default="{}", maxlen=4096)
    show_generation_settings: BoolProperty(name="Quality & Cost", default=False)
    show_advanced_generation: BoolProperty(name="Advanced JSON", default=False)
    show_advanced_process: BoolProperty(name="Expert Process JSON", default=False)
    normalize_json: StringProperty(
        name="Normalize Options (JSON)",
        default='{"apply_transforms":true,"ground":true,"ensure_uv":false,"triangulate":false}',
        maxlen=4096,
    )
    export_format: EnumProperty(
        name="Export Format",
        items=[
            ("glb", "GLB", "Binary glTF"), ("gltf", "glTF", "Separate glTF"),
            ("fbx", "FBX", "Autodesk FBX"), ("obj", "OBJ", "Wavefront OBJ"),
            ("stl", "STL", "STL geometry"), ("usd", "USD", "Universal Scene Description"),
        ],
        default="glb",
    )
    export_directory: StringProperty(name="Export Directory", subtype="DIR_PATH", default="")
    export_json: StringProperty(
        name="Export Options (JSON)", default='{"include_animations":true,"apply_modifiers":true}', maxlen=4096
    )
    reference_images: CollectionProperty(type=AI3D_PG_reference_image)


def register_properties() -> None:
    bpy.types.Scene.meshdock = PointerProperty(type=AI3D_PG_asset_pipeline)


def unregister_properties() -> None:
    del bpy.types.Scene.meshdock
