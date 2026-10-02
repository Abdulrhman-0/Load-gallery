"""Persistent configuration for the Image Gallery Loader node.

This is the ONLY place in the package that writes a user-visible file, and the
only file it writes is ``config.json`` living next to this module. No image,
folder, or any other file is ever created, modified, moved or deleted.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Any, Dict, List, Optional

try:  # running as the custom_nodes package (normal ComfyUI case)
    from . import gallery_fs as fs
except ImportError:  # pragma: no cover - direct import, e.g. the self-test
    import gallery_fs as fs

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(PACKAGE_DIR, "config.json")
CACHE_DIR = os.path.join(PACKAGE_DIR, "_thumbcache")

#: Number of path inputs in the settings window.
FOLDER_SLOTS = 15

DEFAULT_VIEW_MODE = "medium"
VIEW_MODES = ("large", "medium", "small", "details")

_lock = threading.RLock()
_cache: Optional[Dict[str, Any]] = None


def default_config() -> Dict[str, Any]:
    return {
        "version": 1,
        "folders": [""] * FOLDER_SLOTS,
        "view_mode": DEFAULT_VIEW_MODE,
        "thumb_size": 256,
        "sort": "name",
        "direction": "asc",
        "show_folders": True,
        "last": {},  # {root_path_key: rel}
        "cache_limit_mb": 512,
    }


def _coerce(raw: Any) -> Dict[str, Any]:
    """Merge arbitrary on-disk JSON into a valid config. Never raises."""
    cfg = default_config()
    if not isinstance(raw, dict):
        return cfg

    folders = raw.get("folders")
    if isinstance(folders, list):
        # Keep slot positions stable; pad/trim to exactly FOLDER_SLOTS.
        cleaned = ["" if f is None else str(f) for f in folders][:FOLDER_SLOTS]
        cleaned += [""] * (FOLDER_SLOTS - len(cleaned))
        cfg["folders"] = cleaned
    elif isinstance(folders, dict):
        # Tolerate {index: path} shaped files from hand editing.
        for k, v in folders.items():
            try:
                i = int(k)
            except (TypeError, ValueError):
                continue
            if 0 <= i < FOLDER_SLOTS:
                cfg["folders"][i] = "" if v is None else str(v)

    vm = str(raw.get("view_mode", "")).lower()
    cfg["view_mode"] = vm if vm in VIEW_MODES else DEFAULT_VIEW_MODE

    try:
        cfg["thumb_size"] = max(64, min(int(raw.get("thumb_size", 256)), 1024))
    except (TypeError, ValueError):
        pass

    srt = str(raw.get("sort", "name")).lower()
    cfg["sort"] = srt if srt in fs.SORT_MODES else "name"

    cfg["direction"] = "desc" if str(raw.get("direction", "asc")).lower() == "desc" else "asc"
    cfg["show_folders"] = bool(raw.get("show_folders", True))

    last = raw.get("last")
    if isinstance(last, dict):
        cfg["last"] = {
            str(k): str(v) for k, v in last.items() if isinstance(k, str) and isinstance(v, str)
        }

    try:
        cfg["cache_limit_mb"] = max(0, min(int(raw.get("cache_limit_mb", 512)), 10240))
    except (TypeError, ValueError):
        pass

    return cfg


def load(force: bool = False) -> Dict[str, Any]:
    """Read config.json, falling back to defaults on any problem."""
    global _cache
    with _lock:
        if _cache is not None and not force:
            return _cache
        cfg = default_config()
        try:
            if os.path.isfile(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                    cfg = _coerce(json.load(fh))
        except (OSError, ValueError, UnicodeDecodeError):
            # A corrupt or unreadable config must not stop ComfyUI from
            # starting; we simply fall back to defaults.
            cfg = default_config()
        _cache = cfg
        return cfg


def save(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Atomically persist a config. Returns the coerced value written.

    Written via a temp file + ``os.replace`` so a crash mid-write cannot leave
    a truncated config.json behind.
    """
    global _cache
    clean = _coerce(cfg)
    with _lock:
        os.makedirs(PACKAGE_DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=".config-", suffix=".json.tmp", dir=PACKAGE_DIR
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(clean, fh, indent=2, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, CONFIG_PATH)
        except BaseException:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            raise
        _cache = clean
    return clean


def update_fields(**fields: Any) -> Dict[str, Any]:
    """Read-modify-write a subset of the config."""
    with _lock:
        cfg = dict(load())
        for key, value in fields.items():
            if value is not None:
                cfg[key] = value
        return save(cfg)


# --------------------------------------------------------------------------- #
# convenience accessors used by the routes
# --------------------------------------------------------------------------- #


def folders() -> List[str]:
    return list(load()["folders"])


def roots() -> List[fs.FolderStatus]:
    """Valid, de-duplicated configured folders (what the gallery may show)."""
    return fs.valid_roots(folders())


def root_list_payload() -> List[Dict[str, Any]]:
    """The gallery's folder list: only valid folders, named by folder name."""
    return [
        {
            "index": st.index,
            "name": st.display_name,
            "path": st.path,
            "image_count": st.image_count,
        }
        for st in roots()
    ]


def resolve_root(root_index: Any) -> fs.FolderStatus:
    """Map a client-supplied root index onto a validated folder.

    The index refers to the *slot* number in config, not to a position in the
    filtered root list, so it stays stable when earlier slots become invalid.
    """
    try:
        idx = int(root_index)
    except (TypeError, ValueError):
        raise fs.PathRejected("Invalid folder slot.")

    if not 0 <= idx < FOLDER_SLOTS:
        raise fs.PathRejected("Invalid folder slot.")

    status = fs.validate_folder(idx, folders()[idx])
    if not status.valid:
        raise fs.RootNotConfigured(
            f"Folder slot {idx + 1} is not available ({status.reason or 'invalid'})."
        )
    return status


def remembered_rel(root_path: str) -> str:
    """Last browsed subfolder for this root, if still valid."""
    rel = load()["last"].get(fs.path_key(root_path), "")
    try:
        return fs.clean_rel(rel)
    except fs.GalleryError:
        return ""


def remember_rel(root_path: str, rel: str) -> None:
    """Record the last browsed subfolder. Silent on failure.

    A value that fails validation is treated as "go back to the root", not as
    "keep the previous value": a stale journal entry from a tampered request
    must not survive.
    """
    try:
        cleaned = fs.clean_rel(rel)
    except fs.GalleryError:
        cleaned = ""

    try:
        with _lock:
            cfg = dict(load())
            last = dict(cfg.get("last") or {})
            key = fs.path_key(root_path)
            if cleaned:
                last[key] = cleaned
            else:
                last.pop(key, None)
            # Bound the map so a long-lived install cannot grow it forever.
            if len(last) > 64:
                last = dict(list(last.items())[-64:])
            cfg["last"] = last
            save(cfg)
    except Exception:
        pass


def cache_limit_bytes() -> int:
    try:
        return int(load().get("cache_limit_mb", 512)) * 1024 * 1024
    except (TypeError, ValueError):
        return 512 * 1024 * 1024
