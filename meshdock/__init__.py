"""Blender extension entry point and importable asset-pipeline package."""

from __future__ import annotations

bl_info = {
    "name": "Mesh Dock",
    "author": "Shalom Wang",
    "version": (0, 10, 0),
    "blender": (5, 2, 0),
    "location": "View3D > Sidebar > Mesh Dock",
    "description": "Visual AI workbench with Tripo, Hunyuan and TokenHub",
    "category": "Import-Export",
}

__version__ = "0.10.0"


def register() -> None:
    from .blender.registration import register as blender_register

    blender_register()


def unregister() -> None:
    from .blender.registration import unregister as blender_unregister

    blender_unregister()
