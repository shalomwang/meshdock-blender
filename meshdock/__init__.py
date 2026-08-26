"""Blender extension entry point and importable asset-pipeline package."""

from __future__ import annotations

bl_info = {
    "name": "MeshDock",
    "author": "Shalom Wang",
    "version": (0, 9, 6),
    "blender": (5, 2, 0),
    "location": "View3D > Sidebar > MeshDock",
    "description": "High-level AI asset jobs with Tripo, Hunyuan and TokenHub adapters",
    "category": "Import-Export",
}

__version__ = "0.9.6"


def register() -> None:
    from .blender.registration import register as blender_register

    blender_register()


def unregister() -> None:
    from .blender.registration import unregister as blender_unregister

    blender_unregister()
