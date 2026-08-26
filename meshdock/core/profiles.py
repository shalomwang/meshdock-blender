from __future__ import annotations

from dataclasses import asdict, dataclass

from .errors import ValidationError


@dataclass(frozen=True, slots=True)
class AssetProfile:
    id: str
    label: str
    triangle_target: int
    triangle_hard_cap: int
    material_target: int
    material_hard_cap: int
    texture_default: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


PRESET_CATALOG = {
    "name": "Built-in provider generation presets",
    "version": "1.0",
    "purpose": "Provider generation hints only; never used as an acceptance gate.",
}

PROFILES: dict[str, AssetProfile] = {
    item.id: item
    for item in (
        AssetProfile("small_prop_v1", "Small prop (<1m)", 2_000, 4_000, 1, 2, "512-1K"),
        AssetProfile("standard_prop_v1", "Standard prop / furniture", 8_000, 15_000, 2, 4, "1K-2K"),
        AssetProfile("environment_piece_v1", "Environment or architecture piece", 15_000, 30_000, 3, 6, "up to 2K"),
        AssetProfile("high_detail_asset_v1", "High-detail standalone asset", 35_000, 70_000, 4, 8, "2K-4K"),
        AssetProfile("character_v1", "Humanoid character", 50_000, 100_000, 6, 12, "2K-4K"),
        AssetProfile("creature_v1", "Creature / non-humanoid character", 50_000, 100_000, 6, 12, "2K-4K"),
    )
}


def get_profile(profile_id: str) -> AssetProfile:
    try:
        return PROFILES[profile_id]
    except KeyError as exc:
        raise ValidationError(f"unknown asset_profile: {profile_id}") from exc


def public_profiles() -> list[dict[str, object]]:
    return [profile.to_dict() for profile in PROFILES.values()]
