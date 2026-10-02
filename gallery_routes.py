"""HTTP routes backing the gallery UI.

Route paths are registered **without** the ``/api`` prefix: ComfyUI mirrors
every non-static route under ``/api`` in ``PromptServer.add_routes()``, and the
front end calls both through ``api.fetchApi()``.

Every handler that touches the filesystem funnels through
``gallery_fs.resolve_in_root()`` and is dispatched to a worker thread so a slow
network share cannot stall the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable, Dict, Optional

from aiohttp import web

try:  # running as the custom_nodes package (normal ComfyUI case)
    from . import gallery_config as cfg
    from . import gallery_fs as fs
except ImportError:  # pragma: no cover - direct import, e.g. the self-test
    import gallery_config as cfg
    import gallery_fs as fs

logger = logging.getLogger(__name__)

ROUTE_PREFIX = "/image_gallery"


async def _to_thread(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a blocking filesystem/PIL call off the event loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)


def _json_error(exc: fs.GalleryError) -> web.Response:
    return web.json_response({"error": exc.message}, status=exc.status)


def _json_error_str(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #


async def get_config(request: web.Request) -> web.Response:
    """Everything the gallery and settings window need on open."""

    def build() -> Dict[str, Any]:
        data = cfg.load(force=True)
        # Re-validate so the UI reflects reality (folders may have been moved
        # or deleted since the config was written).
        statuses = [st.as_dict() for st in fs.resolve(data["folders"])]
        roots = cfg.root_list_payload()
        return {
            "folders": data["folders"],
            "folder_status": statuses,
            "roots": roots,
            "view_mode": data["view_mode"],
            "view_modes": list(cfg.VIEW_MODES),
            "thumb_size": data["thumb_size"],
            "sort": data["sort"],
            "direction": data["direction"],
            "show_folders": data["show_folders"],
            "slots": cfg.FOLDER_SLOTS,
            "supported_extensions": sorted(fs.IMAGE_EXTS),
            "config_path": cfg.CONFIG_PATH,
        }

    try:
        return web.json_response(await _to_thread(build))
    except fs.GalleryError as exc:
        return _json_error(exc)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("image_gallery: failed to read config")
        return _json_error_str(f"Could not read configuration: {exc}", 500)


async def post_config(request: web.Request) -> web.Response:
    """Save the 15 folder paths and/or the preferred view mode."""
    try:
        body = await request.json()
    except Exception:
        return _json_error_str("Request body must be valid JSON.")

    if not isinstance(body, dict):
        return _json_error_str("Request body must be a JSON object.")

    def write() -> Dict[str, Any]:
        current = dict(cfg.load())

        folders = body.get("folders")
        if folders is not None:
            if not isinstance(folders, list):
                raise fs.GalleryError("'folders' must be a list of strings.")
            clean = ["" if f is None else str(f).strip() for f in folders]
            clean = clean[: cfg.FOLDER_SLOTS]
            clean += [""] * (cfg.FOLDER_SLOTS - len(clean))
            current["folders"] = clean

        for key in ("view_mode", "sort", "direction"):
            if key in body and body[key] is not None:
                current[key] = body[key]
        for key in ("thumb_size", "cache_limit_mb"):
            if key in body and body[key] is not None:
                current[key] = body[key]
        if "show_folders" in body and body["show_folders"] is not None:
            current["show_folders"] = bool(body["show_folders"])

        saved = cfg.save(current)

        statuses = [st.as_dict() for st in fs.resolve(saved["folders"])]
        for status in statuses:
            # Report the folder's own name so the UI can show "cats", not
            # "C:\\Users\\me\\Pictures\\cats".
            path = status.get("path") or ""
            base = os.path.basename(path.rstrip("\\/")) if path else ""
            status["name"] = base or (path or status.get("raw", ""))
        return {
            "ok": True,
            "folders": saved["folders"],
            "folder_status": statuses,
            "roots": cfg.root_list_payload(),
            "view_mode": saved["view_mode"],
            "config_path": cfg.CONFIG_PATH,
        }

    try:
        return web.json_response(await _to_thread(write))
    except fs.GalleryError as exc:
        return _json_error(exc)
    except OSError as exc:
        logger.exception("image_gallery: failed to write config")
        return _json_error_str(f"Could not save configuration: {exc}", 500)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("image_gallery: unexpected error saving config")
        return _json_error_str(f"Could not save configuration: {exc}", 500)


# --------------------------------------------------------------------------- #
# validate a single path (used by the settings window as you type)
# --------------------------------------------------------------------------- #


async def get_validate(request: web.Request) -> web.Response:
    """Check one arbitrary path typed into the settings window.

    Deliberately reports nothing but existence/type/count, and never lists the
    contents, so it cannot be used to enumerate an unconfigured location.
    """
    raw = request.rel_url.query.get("path", "")
    if not raw.strip():
        return web.json_response({"valid": False, "reason": "empty", "name": ""})

    index_raw = request.rel_url.query.get("index", "0")
    try:
        index = int(index_raw)
    except (TypeError, ValueError):
        index = 0

    def check() -> Dict[str, Any]:
        status = fs.validate_folder(index, raw)
        payload = status.as_dict()
        payload["name"] = status.display_name if status.valid else ""
        return payload

    try:
        return web.json_response(await _to_thread(check))
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("image_gallery: validate failed")
        return _json_error_str(f"Could not check that path: {exc}", 500)


# --------------------------------------------------------------------------- #
# listing
# --------------------------------------------------------------------------- #


async def get_list(request: web.Request) -> web.Response:
    """List subfolders and images inside a configured root."""
    query = request.rel_url.query
    root_index = query.get("root", "")
    rel = query.get("path", "")
    sort = query.get("sort", cfg.load()["sort"])
    direction = query.get("direction", cfg.load()["direction"])

    if sort not in fs.SORT_MODES:
        sort = "name"
    if direction not in ("asc", "desc"):
        direction = "asc"

    def listing() -> Dict[str, Any]:
        root = cfg.resolve_root(root_index)  # raises for bad/unconfigured slots
        data = fs.list_dir(root.index, root.path, rel, sort=sort, direction=direction)
        data["root_name"] = root.display_name
        data["root_path"] = root.path
        cfg.remember_rel(root.path, data["rel"])
        return data

    try:
        return web.json_response(await _to_thread(listing))
    except fs.GalleryError as exc:
        return _json_error(exc)
    except Exception as exc:
        logger.exception("image_gallery: listing failed")
        return _json_error_str(f"Could not list that folder: {exc}", 500)


# --------------------------------------------------------------------------- #
# thumbnails
# --------------------------------------------------------------------------- #


async def get_thumb(request: web.Request) -> web.Response:
    """Return a downscaled thumbnail for one image.

    Corrupt or undecodable images return a generated placeholder with
    ``X-Placeholder: 1`` instead of an error, so one bad file cannot blank the
    gallery. Missing files return 404 so the tile can be marked stale.
    """
    query = request.rel_url.query
    root_index = query.get("root", "")
    rel = query.get("path", "")
    size = query.get("size", "256")

    try:
        size = max(32, min(int(size), 1024))
    except (TypeError, ValueError):
        size = 256

    def render() -> Any:
        root = cfg.resolve_root(root_index)
        src = fs.resolve_in_root(root.path, rel)  # containment boundary
        # Only ever thumbnail files we would list in the first place.
        if os.path.splitext(src)[1].lower() not in fs.IMAGE_EXTS:
            raise fs.PathRejected("Unsupported file type.")
        result = fs.thumbnail(src, size, cfg.CACHE_DIR)
        # Original pixel size, so the Details view can show dimensions without
        # ever downloading the full image. None for placeholders.
        dims = None if result[2] else fs.image_size(src)
        return result, dims

    try:
        (data, mime, placeholder, from_cache), dims = await _to_thread(render)
    except fs.GalleryError as exc:
        return _json_error(exc)
    except Exception as exc:
        logger.exception("image_gallery: thumbnail failed")
        return _json_error_str(f"Could not build a thumbnail: {exc}", 500)

    headers = {
        "Cache-Control": "private, max-age=60",
        "X-Placeholder": "1" if placeholder else "0",
        "X-Cache": "hit" if from_cache else "miss",
    }
    if dims:
        headers["X-Img-W"], headers["X-Img-H"] = str(dims[0]), str(dims[1])
    return web.Response(body=data, content_type=mime, headers=headers)


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #


def register() -> bool:
    """Attach the routes to the running ComfyUI server.

    Called from ``__init__`` at import time. Any failure here is logged and
    swallowed: the node itself must still register even if the routes cannot.
    """
    try:
        from server import PromptServer
    except Exception as exc:  # pragma: no cover - server not importable
        logger.warning("image_gallery: PromptServer unavailable (%s); routes not added.", exc)
        return False

    instance = getattr(PromptServer, "instance", None)
    if instance is None:
        logger.warning("image_gallery: PromptServer has no instance yet; routes not added.")
        return False

    routes = instance.routes
    try:
        routes.get(f"{ROUTE_PREFIX}/config")(get_config)
        routes.post(f"{ROUTE_PREFIX}/config")(post_config)
        routes.get(f"{ROUTE_PREFIX}/validate")(get_validate)
        routes.get(f"{ROUTE_PREFIX}/list")(get_list)
        routes.get(f"{ROUTE_PREFIX}/thumb")(get_thumb)
    except Exception as exc:
        logger.warning("image_gallery: could not register routes: %s", exc)
        return False

    logger.info("image_gallery: registered %s/* routes", ROUTE_PREFIX)
    return True
