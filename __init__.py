"""Image Gallery Loader - a read-only gallery replacement for ``Load Image``.

Drops into ``ComfyUI/custom_nodes/`` like any other custom node. It browses
folders you configure in its own settings window and loads the image you click.
It never writes, moves, renames or deletes anything; the only file it creates
for itself is ``config.json`` (plus a thumbnail cache in ``_thumbcache/``).
"""

from .gallery_node import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS
from . import gallery_routes

#: Tells ComfyUI to serve everything in ``web/`` at
#: ``/extensions/<module name>/`` and load the JavaScript automatically.
WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]

# Register the HTTP routes at import time. PromptServer.instance already exists
# by the time custom nodes are loaded, and add_routes() runs later, so the
# routes are picked up by both the plain and the /api prefixed tables.
_ROUTES_OK = gallery_routes.register()

if not _ROUTES_OK:  # pragma: no cover - only on an unusual server state
    import logging

    logging.getLogger(__name__).warning(
        "Image Gallery Loader: HTTP routes were not registered. The node will "
        "still load, but the gallery will not be able to list folders."
    )
