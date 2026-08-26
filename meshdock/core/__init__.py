"""Provider-neutral domain model.

Imports stay intentionally light to avoid loading provider modules when Blender first
discovers the extension. Consumers should import from ``core.models`` or ``core.service``.
"""
