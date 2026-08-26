from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..core.errors import ValidationError
from ..core.provider_ids import canonical_provider_id
from .os_vault import OsCredentialBackend, default_os_backend

_DEFAULT_REGISTRY = object()
_PROFILE_ID = re.compile(r"^[a-f0-9]{32}$")


@dataclass(frozen=True, slots=True)
class CredentialProfile:
    id: str
    provider: str
    note: str
    source: str
    persistent: bool
    created_at: str

    def public_dict(self, *, enabled: bool) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "note": self.note,
            "source": self.source,
            "persistent": self.persistent,
            "enabled": enabled,
        }


def _default_registry_path() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    destination = base / "MeshDock" / "credential_profiles.json"
    legacy = base / "AI3DAssetPipeline" / "credential_profiles.json"
    if not destination.exists() and legacy.is_file():
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy, destination)
        except OSError:
            return legacy
    return destination


class SessionCredentialVault:
    """Blender-owned multi-profile credential resolver.

    Secret values live only in Blender memory, the native OS credential backend, or a
    fixed provider environment variable. The optional JSON registry contains only
    opaque ids and user notes so Blender can present saved profiles without enumerating
    or serialising secrets.
    """

    _ENVIRONMENT_KEYS = {
        "tripo_cn": ("TRIPO_CN_API_KEY",),
        "tripo_global": ("TRIPO_GLOBAL_API_KEY", "TRIPO_API_KEY"),
        "hunyuan_direct": ("HUNYUAN_3D_API_KEY", "HUNYUAN_DIRECT_API_KEY"),
        "tokenhub_cn": ("TOKENHUB_CN_API_KEY", "TOKENHUB_API_KEY"),
        "tokenhub_global": ("TOKENHUB_INTL_API_KEY", "TOKENHUB_GLOBAL_API_KEY"),
        "tripo_webhook": ("TRIPO_WEBHOOK_SECRET",),
    }
    _MANAGED_PROVIDERS = frozenset({
        "tripo_cn", "tripo_global", "hunyuan_direct", "tokenhub_cn", "tokenhub_global",
    })
    _LEGACY_STORAGE_PROVIDERS = {
        "tripo_global": "tripo", "tokenhub_cn": "hunyuan",
    }

    def __init__(
        self,
        persistent: OsCredentialBackend | None = None,
        *,
        environment: Mapping[str, str] | None = None,
        registry_path: Path | None | object = _DEFAULT_REGISTRY,
    ) -> None:
        using_default_backend = persistent is None
        self._values: dict[str, str] = {}
        self._profiles: dict[str, CredentialProfile] = {}
        self._active: dict[str, str] = {}
        self._disabled: set[str] = set()
        self._lock = threading.RLock()
        self._persistent = persistent or default_os_backend()
        self._environment = os.environ if environment is None else environment
        if registry_path is _DEFAULT_REGISTRY:
            self._registry_path = _default_registry_path() if using_default_backend else None
        else:
            self._registry_path = Path(registry_path) if registry_path is not None else None
        self._load_registry()

    @property
    def persistence_available(self) -> bool:
        return self._persistent.available

    def set(
        self,
        provider: str,
        secret: str,
        *,
        persist: bool = False,
        note: str = "",
        profile_id: str | None = None,
    ) -> str:
        provider = str(canonical_provider_id(provider))
        self._validate_managed_provider(provider)
        value = secret.strip()
        if len(value) < 8:
            raise ValidationError("credential is unexpectedly short")
        if persist and not self.persistence_available:
            raise ValidationError("persistent OS credential storage is unavailable")
        cleaned_note = self._clean_note(note)
        identifier = profile_id or uuid.uuid4().hex
        if not _PROFILE_ID.fullmatch(identifier):
            raise ValidationError("credential profile id is invalid")
        created_at = datetime.now(UTC).isoformat()
        with self._lock:
            existing = self._profiles.get(identifier)
            if existing is not None and existing.provider != provider:
                raise ValidationError("credential profile belongs to another provider")
            profile = CredentialProfile(
                id=identifier,
                provider=provider,
                note=cleaned_note,
                source="stored" if persist else "session",
                persistent=persist,
                created_at=existing.created_at if existing else created_at,
            )
            if persist:
                self._persistent.set(self._storage_key(provider, identifier), value)
                self._values.pop(identifier, None)
            else:
                self._values[identifier] = value
                if existing and existing.persistent:
                    self._persistent.delete(self._storage_key(provider, identifier))
            self._profiles[identifier] = profile
            self._disabled.discard(identifier)
            self._active[provider] = identifier
            self._save_registry()
        return identifier

    def list_profiles(self, provider: str | None = None) -> list[dict[str, Any]]:
        if provider is not None:
            provider = str(canonical_provider_id(provider))
            self._validate_managed_provider(provider)
            providers = (provider,)
        else:
            providers = tuple(sorted(self._MANAGED_PROVIDERS))
        with self._lock:
            profiles = [
                profile
                for profile in self._profiles.values()
                if profile.provider in providers and self._profile_is_configured(profile)
            ]
            # Python dictionaries preserve insertion order, as does the persisted
            # registry list.  Keep that user-visible account order stable within
            # each provider instead of relying on wall-clock timestamps, which can
            # collide or move backwards after a system clock adjustment.
            result = [
                profile.public_dict(enabled=profile.id not in self._disabled)
                for profile in sorted(profiles, key=lambda item: item.provider)
            ]
            for item_provider in providers:
                if self._legacy_value(item_provider):
                    identifier = self._legacy_id(item_provider)
                    result.append(
                        CredentialProfile(identifier, item_provider, "", "stored", True, "").public_dict(
                            enabled=identifier not in self._disabled
                        )
                    )
                if self._environment_value(item_provider):
                    identifier = self._environment_id(item_provider)
                    result.append(
                        CredentialProfile(identifier, item_provider, "", "environment", False, "").public_dict(
                            enabled=identifier not in self._disabled
                        )
                    )
            return result

    def select(self, provider: str, profile_id: str) -> None:
        provider = str(canonical_provider_id(provider))
        self._validate_managed_provider(provider)
        self._secret_for_profile(provider, profile_id)
        with self._lock:
            self._active[provider] = profile_id

    def set_enabled(self, provider: str, profile_id: str, enabled: bool) -> None:
        """Control whether a profile is offered for new work without deleting it."""
        provider = str(canonical_provider_id(provider))
        self._validate_managed_provider(provider)
        self._secret_for_profile(provider, profile_id)
        with self._lock:
            if enabled:
                self._disabled.discard(profile_id)
            else:
                self._disabled.add(profile_id)
                if self._active.get(provider) == profile_id:
                    self._active.pop(provider, None)
            self._save_registry()

    def configured(self, provider: str) -> bool:
        provider = str(canonical_provider_id(provider))
        self._validate_provider(provider)
        if provider == "tripo_webhook":
            return bool(self._environment_value(provider) or self._legacy_value(provider))
        return any(profile["enabled"] for profile in self.list_profiles(provider))

    def use(self, provider: str, profile_id: str | None = None) -> str:
        """Return a secret only to provider adapter code; never expose via bridge calls."""
        provider = str(canonical_provider_id(provider))
        self._validate_provider(provider)
        if provider == "tripo_webhook":
            value = self._legacy_value(provider) or self._environment_value(provider)
            if value:
                return value
            raise ValidationError("webhook credential is not configured")

        with self._lock:
            selected = profile_id or self._active.get(provider)
            if profile_id is None and selected in self._disabled:
                selected = None
        if selected:
            return self._secret_for_profile(provider, selected)

        profiles = self.list_profiles(provider)
        for source in ("session", "stored", "environment"):
            for profile in profiles:
                if profile["source"] == source and profile["enabled"]:
                    return self._secret_for_profile(provider, str(profile["id"]))
        raise ValidationError(f"{provider} is not configured")

    def delete_profile(self, profile_id: str) -> None:
        with self._lock:
            environment_provider = next(
                (item for item in self._MANAGED_PROVIDERS if profile_id == self._environment_id(item)),
                None,
            )
            if environment_provider:
                raise ValidationError("environment credentials are managed outside Blender")
            legacy_provider = next(
                (item for item in self._MANAGED_PROVIDERS if profile_id == self._legacy_id(item)),
                None,
            )
            if legacy_provider:
                provider = legacy_provider
                self._persistent.delete(provider)
                old_provider = self._LEGACY_STORAGE_PROVIDERS.get(provider)
                if old_provider:
                    self._persistent.delete(old_provider)
            else:
                profile = self._profiles.get(profile_id)
                if profile is None:
                    raise ValidationError("credential profile does not exist")
                self._values.pop(profile_id, None)
                if profile.persistent:
                    self._persistent.delete(self._storage_key(profile.provider, profile.id))
                    legacy_provider = self._LEGACY_STORAGE_PROVIDERS.get(profile.provider)
                    if legacy_provider:
                        self._persistent.delete(self._storage_key(legacy_provider, profile.id))
                provider = profile.provider
                self._profiles.pop(profile_id, None)
            if self._active.get(provider) == profile_id:
                self._active.pop(provider, None)
            self._disabled.discard(profile_id)
            self._save_registry()

    def delete_persisted(self, provider: str) -> None:
        """Backward-compatible removal of all Blender-managed profiles for a provider."""
        provider = str(canonical_provider_id(provider))
        self._validate_managed_provider(provider)
        for profile in list(self.list_profiles(provider)):
            if profile["source"] != "environment":
                self.delete_profile(str(profile["id"]))

    def clear(self) -> None:
        with self._lock:
            session_ids = {
                identifier for identifier, profile in self._profiles.items()
                if not profile.persistent
            }
            for identifier in session_ids:
                self._profiles.pop(identifier, None)
                self._values.pop(identifier, None)
                self._disabled.discard(identifier)
            self._active = {
                provider: identifier for provider, identifier in self._active.items()
                if identifier not in session_ids
            }
            self._save_registry()

    @classmethod
    def _validate_provider(cls, provider: str) -> None:
        if provider not in cls._ENVIRONMENT_KEYS:
            raise ValidationError("unsupported credential provider")

    @classmethod
    def _validate_managed_provider(cls, provider: str) -> None:
        if provider not in cls._MANAGED_PROVIDERS:
            raise ValidationError("unsupported credential provider")

    @staticmethod
    def _clean_note(note: str) -> str:
        cleaned = " ".join(str(note).split())
        if len(cleaned) > 80:
            raise ValidationError("credential note exceeds 80 characters")
        return cleaned

    @staticmethod
    def _storage_key(provider: str, profile_id: str) -> str:
        return f"profile/{provider}/{profile_id}"

    @staticmethod
    def _legacy_id(provider: str) -> str:
        return uuid.uuid5(uuid.NAMESPACE_URL, f"ai3d-legacy:{provider}").hex

    @staticmethod
    def _environment_id(provider: str) -> str:
        return uuid.uuid5(uuid.NAMESPACE_URL, f"ai3d-environment:{provider}").hex

    def _profile_is_configured(self, profile: CredentialProfile) -> bool:
        if profile.id in self._values:
            return True
        if not profile.persistent:
            return False
        try:
            return bool(self._persistent_profile_value(profile.provider, profile.id))
        except Exception:
            return False

    def _secret_for_profile(self, provider: str, profile_id: str) -> str:
        if profile_id == self._environment_id(provider):
            value = self._environment_value(provider)
        elif profile_id == self._legacy_id(provider):
            value = self._legacy_value(provider)
        else:
            with self._lock:
                profile = self._profiles.get(profile_id)
                if profile is None or profile.provider != provider:
                    raise ValidationError("credential profile is unavailable")
                value = self._values.get(profile_id)
                if value is None and profile.persistent:
                    try:
                        value = self._persistent_profile_value(provider, profile_id)
                    except Exception as exc:
                        raise ValidationError("credential profile could not be read") from exc
        if value:
            return value
        raise ValidationError("credential profile is unavailable")

    def _legacy_value(self, provider: str) -> str | None:
        try:
            value = self._persistent.get(provider)
            if not value and provider in self._LEGACY_STORAGE_PROVIDERS:
                value = self._persistent.get(self._LEGACY_STORAGE_PROVIDERS[provider])
        except Exception:
            return None
        return str(value).strip() if value and len(str(value).strip()) >= 8 else None

    def _environment_value(self, provider: str) -> str | None:
        for key in self._ENVIRONMENT_KEYS[provider]:
            try:
                value = str(self._environment.get(key, "")).strip()
            except Exception:
                continue
            if len(value) >= 8:
                return value
        return None

    def _persistent_profile_value(self, provider: str, profile_id: str) -> str | None:
        value = self._persistent.get(self._storage_key(provider, profile_id))
        if value:
            return value
        legacy_provider = self._LEGACY_STORAGE_PROVIDERS.get(provider)
        if legacy_provider:
            return self._persistent.get(self._storage_key(legacy_provider, profile_id))
        return None

    def _load_registry(self) -> None:
        if self._registry_path is None or not self._registry_path.is_file():
            return
        try:
            value = json.loads(self._registry_path.read_text(encoding="utf-8"))
            items = value.get("profiles", []) if isinstance(value, dict) else []
            disabled = value.get("disabled_profile_ids", []) if isinstance(value, dict) else []
            self._disabled = {
                str(identifier) for identifier in disabled
                if _PROFILE_ID.fullmatch(str(identifier))
            }
            for raw in items[:100]:
                if not isinstance(raw, dict):
                    continue
                identifier = str(raw.get("id", ""))
                provider = str(canonical_provider_id(raw.get("provider", "")))
                if not _PROFILE_ID.fullmatch(identifier) or provider not in self._MANAGED_PROVIDERS:
                    continue
                self._profiles[identifier] = CredentialProfile(
                    id=identifier,
                    provider=provider,
                    note=self._clean_note(str(raw.get("note", ""))),
                    source="stored",
                    persistent=True,
                    created_at=str(raw.get("created_at", ""))[:64],
                )
        except Exception:
            self._profiles.clear()
            self._disabled.clear()

    def _save_registry(self) -> None:
        if self._registry_path is None:
            return
        persistent = [
            {
                "id": profile.id,
                "provider": profile.provider,
                "note": profile.note,
                "created_at": profile.created_at,
            }
            for profile in self._profiles.values() if profile.persistent
        ]
        destination = self._registry_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "schema": 2,
                    "profiles": persistent,
                    "disabled_profile_ids": sorted(self._disabled),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(destination)
