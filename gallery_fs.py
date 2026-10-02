"""Filesystem helpers for the Image Gallery Loader node.

Everything in this module is READ-ONLY. The only writes anywhere in this
package happen in ``gallery_config.save()`` (our own config file) and in the
thumbnail cache directory (``_thumbcache``).

The security model is:

    config.json  ->  list of "roots"
    every request -> (root_index, relative_path)
    resolve()    -> absolute path, guaranteed to sit inside that root

``resolve()`` is the single choke point: no route handler is allowed to touch
the filesystem without going through it first.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Extensions we will list. Deliberately a small, explicit allow-list.
IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp"})

# Sort modes offered by the UI.
SORT_MODES = ("name", "date", "size", "type")

# Sentinel used for "the requested thing is gone" so callers can distinguish it
# from a permissions/other error.
_THUMB_FORMAT = "WEBP"
_THUMB_MIME = "image/webp"

logger = logging.getLogger(__name__)

# Throttle: run prune_cache at most once every 5 minutes.
_PRUNE_INTERVAL = 300  # seconds
_last_prune: float = 0.0


class GalleryError(Exception):
    """Base class for errors we intend to report to the client as 4xx."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


class RootNotConfigured(GalleryError):
    def __init__(self, message: str = "That folder slot is not configured."):
        super().__init__(message, 404)


class PathRejected(GalleryError):
    def __init__(self, message: str = "That path is outside the configured folders."):
        super().__init__(message, 403)


class NotFound(GalleryError):
    def __init__(self, message: str = "Not found."):
        super().__init__(message, 404)


# --------------------------------------------------------------------------- #
# low level helpers
# --------------------------------------------------------------------------- #


def normalize(p: str) -> str:
    """Expand ``~``/env vars and make the path absolute-ish, without touching disk."""
    if not p:
        return ""
    p = os.path.expanduser(os.path.expandvars(p.strip().strip('"')))
    return p


def canonical(p: str) -> str:
    """Return the real (symlink/junction resolved) absolute path.

    Never raises: falls back to ``abspath`` when the path does not exist yet,
    which is what we want when the user has typed a folder that is not there.
    """
    p = normalize(p)
    if not p:
        return ""
    try:
        return os.path.realpath(p)
    except OSError:
        return os.path.abspath(p)


def _fold(p: str) -> str:
    """Case/normalisation key for comparisons (Windows is case-insensitive)."""
    return os.path.normcase(os.path.normpath(p))


def path_key(p: str) -> str:
    return _fold(canonical(p))


def is_within(child: str, parent: str) -> bool:
    """True when ``child`` is ``parent`` or lives underneath it.

    ``commonpath`` is used rather than a ``startswith`` string test so that
    ``/srv/pics`` does not appear to contain ``/srv/pics-private``.
    """
    if not child or not parent:
        return False
    a, b = _fold(child), _fold(parent)
    if a == b:
        return True
    try:
        return os.path.commonpath([a, b]) == b
    except ValueError:
        # Different drives on Windows, or mixed absolute/relative.
        return False


# --------------------------------------------------------------------------- #
# folders
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FolderStatus:
    """Result of validating one of the 15 configured paths."""

    index: int
    raw: str
    path: str
    valid: bool
    reason: str = ""
    image_count: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "raw": self.raw,
            "path": self.path,
            "valid": self.valid,
            "reason": self.reason,
            "image_count": self.image_count,
        }

    @property
    def display_name(self) -> str:
        """The folder's own name, e.g. ``C:\\art\\cats`` -> ``cats``."""
        base = os.path.basename(self.path.rstrip("\\/")) if self.path else ""
        # A drive root ("C:\\") has no basename.
        return base or (self.path or self.raw)


def count_images(folder: str, limit: int = 5000) -> int:
    """Cheap count of image files directly inside ``folder``."""
    n = 0
    try:
        with os.scandir(folder) as it:
            for entry in it:
                if entry.is_file() and os.path.splitext(entry.name)[1].lower() in IMAGE_EXTS:
                    n += 1
                    if n >= limit:
                        break
    except OSError:
        return 0
    return n


def validate_folder(index: int, raw: str) -> FolderStatus:
    """Check a single configured path. Never raises."""
    raw = (raw or "").strip()
    if not raw:
        return FolderStatus(index, "", "", False, "empty")

    path = canonical(raw)
    try:
        if not os.path.exists(path):
            return FolderStatus(index, raw, path, False, "not found")
        if not os.path.isdir(path):
            return FolderStatus(index, raw, path, False, "not a folder")
        if not os.access(path, os.R_OK):
            return FolderStatus(index, raw, path, False, "permission denied")
    except OSError as exc:
        return FolderStatus(index, raw, path, False, f"unreadable: {exc.strerror or exc}")

    return FolderStatus(index, raw, path, True, "", count_images(path))


def resolve(paths: Iterable[str]) -> List[FolderStatus]:
    """Validate every configured slot, in order."""
    return [validate_folder(i, p) for i, p in enumerate(paths)]


def valid_roots(paths: Iterable[str]) -> List[FolderStatus]:
    """Only the usable folders, in slot order, de-duplicated by real path."""
    seen: set = set()
    out: List[FolderStatus] = []
    for st in resolve(paths):
        if not st.valid:
            continue
        key = _fold(st.path)
        if key in seen:
            continue
        seen.add(key)
        out.append(st)
    return out


# --------------------------------------------------------------------------- #
# path resolution (the security boundary)
# --------------------------------------------------------------------------- #

_DRIVE_RE = re.compile(r"^[a-zA-Z]:")


def clean_rel(rel: Optional[str]) -> str:
    """Normalise a client-supplied relative path, or reject it.

    Accepts ``""``, ``"sub/dir"``, ``"sub\\dir"``. Rejects absolute paths,
    drive-relative paths (``C:foo``), UNC paths, and any ``..`` segment.
    """
    if rel is None:
        return ""
    rel = str(rel).replace("\\", "/").strip()

    # Strip leading slashes but remember they were there: "//server/share" and
    # "/etc/passwd" both become "/" after lstrip, which we must reject.
    if rel.startswith("/") or rel.startswith("//"):
        raise PathRejected("Absolute paths are not allowed.")

    if _DRIVE_RE.match(rel):
        raise PathRejected("Absolute paths are not allowed.")

    parts = [seg for seg in rel.split("/") if seg not in ("", ".")]
    for seg in parts:
        if seg == "..":
            raise PathRejected("Parent directory traversal is not allowed.")
        if "\x00" in seg:
            raise PathRejected("Invalid path.")

    return "/".join(parts)


def resolve_in_root(root: str, rel: Optional[str]) -> str:
    """Join ``rel`` onto ``root`` and prove the result stays inside ``root``.

    Two checks are applied because either alone can be defeated:
      1. lexical - reject ``..``/absolute/drive-relative input;
      2. canonical - realpath the result and re-compare, which catches
         symlinks and NTFS junctions pointing outside the root.
    """
    root_real = canonical(root)
    if not root_real:
        raise PathRejected("Root folder is not configured.")
    if not os.path.isdir(root_real):
        raise NotFound("That folder no longer exists.")

    rel_clean = clean_rel(rel)
    if not rel_clean:
        return root_real

    target = os.path.join(root_real, *rel_clean.split("/"))

    # canonical() uses realpath, so this resolves symlinks too. realpath() on a
    # path containing a symlink to outside the root will escape, which is
    # exactly what we want to detect.
    target_real = canonical(target)
    if not is_within(target_real, root_real):
        raise PathRejected("That path is outside the configured folder.")

    return target_real


# --------------------------------------------------------------------------- #
# listing
# --------------------------------------------------------------------------- #


def _sort_key(mode: str):
    if mode == "date":
        return lambda e: (-(e["mtime"] or 0), e["_fold"])
    if mode == "size":
        return lambda e: (-(e["size"] or 0), e["_fold"])
    if mode == "type":
        return lambda e: (e["ext"], e["_fold"])
    return lambda e: e["_fold"]


def list_dir(
    root_index: int,
    root_path: str,
    rel: str,
    sort: str = "name",
    direction: str = "asc",
) -> Dict[str, Any]:
    """List subfolders and images at ``rel`` inside ``root_path``.

    Unreadable entries are skipped rather than raising: a single locked file
    must not break the whole folder.
    """
    target = resolve_in_root(root_path, rel)
    rel_clean = clean_rel(rel)

    if not os.path.isdir(target):
        raise NotFound("That folder no longer exists.")

    folders: List[Dict[str, Any]] = []
    files: List[Dict[str, Any]] = []

    try:
        with os.scandir(target) as it:
            for entry in it:
                name = entry.name
                if name.startswith("."):
                    continue  # hidden / OS metadata
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if name.startswith("$"):  # $RECYCLE.BIN etc.
                            continue
                        rel_child = f"{rel_clean}/{name}".lstrip("/")
                        folders.append(
                            {
                                "name": name,
                                "rel": rel_child,
                                "has_children": None,  # filled lazily, see below
                            }
                        )
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue

                ext = os.path.splitext(name)[1].lower()
                if ext not in IMAGE_EXTS:
                    continue

                try:
                    st = entry.stat()
                    size, mtime = st.st_size, st.st_mtime
                except OSError:
                    size, mtime = 0, 0.0

                files.append(
                    {
                        "name": name,
                        "rel": f"{rel_clean}/{name}".lstrip("/"),
                        "ext": ext.lstrip("."),
                        "size": size,
                        "mtime": mtime,
                        "folder": False,
                        "_fold": name.casefold(),
                    }
                )
    except PermissionError as exc:
        raise GalleryError(f"Permission denied: {exc.filename or target}", 403) from exc
    except OSError as exc:
        raise GalleryError(f"Could not read folder: {exc.strerror or exc}", 500) from exc

    # Subfolders always sort by name: date/size are meaningless for a folder
    # row, and the grid keeps folders above images regardless of mode.
    folders.sort(key=lambda f: f["name"].casefold())

    reverse = direction == "desc"
    files.sort(key=_sort_key(sort), reverse=reverse)
    for f in files:
        f.pop("_fold", None)

    return {
        "root": root_index,
        "rel": rel_clean,
        "breadcrumb": _breadcrumb(rel_clean),
        "folders": folders,
        "images": files,
        "count": len(files),
        "sorted_by": sort,
        "direction": direction,
    }


def _breadcrumb(rel: str) -> List[Dict[str, str]]:
    """``"a/b/c"`` -> ``[{name:"a", rel:"a"}, ...]`` for the mobile-style back bar."""
    crumbs: List[Dict[str, str]] = []
    acc: List[str] = []
    for seg in [s for s in rel.split("/") if s]:
        acc.append(seg)
        crumbs.append({"name": seg, "rel": "/".join(acc)})
    return crumbs


# --------------------------------------------------------------------------- #
# thumbnails
# --------------------------------------------------------------------------- #

# A tiny 1x1 transparent GIF-ish placeholder is generated instead, so we never
# need to ship binary assets in the repo.
_PLACEHOLDER_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
    'viewBox="0 0 {w} {h}"><rect width="{w}" height="{h}" fill="#2b2b33"/>'
    '<g fill="none" stroke="#5a5a68" stroke-width="3" stroke-linecap="round">'
    '<path d="M{x1} {y1} l{w1} {h1}"/><path d="M{x2} {y2} l{w2} {h2}"/>'
    "</g></svg>"
)


def _placeholder_png(size: int) -> bytes:
    """Generate a neutral 'broken image' tile. Safe to call with no PIL state."""
    from io import BytesIO

    from PIL import Image, ImageDraw

    img = Image.new("RGB", (size, size), (43, 43, 51))
    d = ImageDraw.Draw(img)
    inset = max(4, size // 8)
    d.rectangle(
        [inset, inset, size - inset - 1, size - inset - 1],
        outline=(96, 96, 110),
        width=max(1, size // 64),
    )
    # a little mountain + sun, drawn with primitives so no font is required
    mid = size // 2
    d.line(
        [
            (inset + 2, size - inset - 2),
            (mid - size // 8, mid),
            (mid + size // 16, size - inset - size // 6),
            (size - inset - 2, size - inset - 2),
        ],
        fill=(120, 120, 138),
        width=max(1, size // 48),
    )
    r = max(2, size // 16)
    d.ellipse(
        [size - inset - r * 3, inset + r, size - inset - r, inset + r * 3],
        fill=(120, 120, 138),
    )
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def thumbnail(src: str, size: int, cache_dir: str) -> Tuple[bytes, str, bool, bool]:
    """Return ``(bytes, mime, is_placeholder, from_cache)`` for ``src``.

    Raises ``NotFound`` if the source image is gone. Corrupt/unreadable images
    yield a placeholder rather than an error, so one bad file cannot blank the
    gallery.
    """
    size = max(32, min(int(size or 256), 1024))

    if not os.path.isfile(src):
        raise NotFound("That image no longer exists.")

    try:
        st = os.stat(src)
        stamp = f"{int(st.st_mtime)}:{st.st_size}"
    except OSError:
        raise NotFound("That image is no longer readable.")

    cache_name = hashlib.sha1(
        f"{path_key(src)}|{stamp}|{size}|{_THUMB_FORMAT}".encode("utf-8", "surrogatepass")
    ).hexdigest()
    cache_path = os.path.join(cache_dir, f"{cache_name}.webp")

    if os.path.isfile(cache_path):
        try:
            with open(cache_path, "rb") as fh:
                return fh.read(), _THUMB_MIME, False, True
        except OSError:
            pass  # fall through and regenerate

    try:
        data = _render_thumb(src, size)
    except NotFound:
        raise
    except Exception:
        # Corrupt, truncated, unsupported-variant, zero-byte, etc.
        return _placeholder_png(size), "image/png", True, False

    _write_cache(cache_path, data, cache_dir)
    _maybe_prune(cache_dir)
    return data, _THUMB_MIME, False, False


def image_size(src: str) -> Optional[Tuple[int, int]]:
    """``(width, height)`` of the source image, read from the header only.

    Returns ``None`` when the file cannot be decoded. Cheap: PIL reads only the
    header for ``.size`` and never decodes the pixels, so this is safe to call
    on large images (used by the Details view for its Dimensions column).
    """
    try:
        from PIL import Image

        with Image.open(src) as img:
            return int(img.size[0]), int(img.size[1])
    except Exception:
        return None


def _render_thumb(src: str, size: int) -> bytes:
    from io import BytesIO

    from PIL import Image, ImageOps

    with Image.open(src) as img:
        # Animated formats: show the first frame only.
        try:
            img.seek(0)
        except (EOFError, ValueError, OSError):
            pass
        # EXIF orientation, so portrait photos are not sideways in the grid.
        try:
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
        img.thumbnail((size, size), Image.Resampling.LANCZOS)
        if img.mode == "RGBA":
            # WebP keeps alpha, but compositing on the panel colour avoids
            # transparent PNGs looking broken against a dark background.
            bg = Image.new("RGB", img.size, (28, 28, 34))
            bg.paste(img, mask=img.getchannel("A"))
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")

        buf = BytesIO()
        img.save(buf, format=_THUMB_FORMAT, quality=82, method=4)
        return buf.getvalue()


def _write_cache(cache_path: str, data: bytes, cache_dir: str) -> None:
    """Best-effort cache write. A failure here must never fail the request."""
    try:
        os.makedirs(cache_dir, exist_ok=True)
        tmp = f"{cache_path}.{os.getpid()}.tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, cache_path)
    except OSError:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except (OSError, NameError, UnboundLocalError):
            pass


def _maybe_prune(cache_dir: str) -> None:
    """Run ``prune_cache`` at most once every ``_PRUNE_INTERVAL`` seconds."""
    global _last_prune
    now = time.monotonic()
    if now - _last_prune < _PRUNE_INTERVAL:
        return
    _last_prune = now
    try:
        # Import lazily to avoid a circular import (gallery_config imports us).
        try:
            from . import gallery_config as _cfg
        except ImportError:
            import gallery_config as _cfg  # type: ignore[no-redef]
        limit = _cfg.cache_limit_bytes()
    except Exception:
        limit = 512 * 1024 * 1024
    removed = prune_cache(cache_dir, limit)
    if removed:
        logger.info("image_gallery: pruned %d cached thumbnails", removed)


def prune_cache(cache_dir: str, max_bytes: int = 512 * 1024 * 1024) -> int:
    """Trim the thumbnail cache to ``max_bytes``, oldest-accessed first.

    Returns the number of files removed. Called opportunistically; never raises.
    """
    if max_bytes <= 0:
        return 0
    removed = 0
    try:
        entries = []
        total = 0
        with os.scandir(cache_dir) as it:
            for e in it:
                if not e.is_file():
                    continue
                try:
                    st = e.stat()
                except OSError:
                    continue
                entries.append((st.st_atime, st.st_size, e.path))
                total += st.st_size
        if total <= max_bytes:
            return 0
        entries.sort()  # oldest access first
        for _atime, fsize, path in entries:
            if total <= max_bytes:
                break
            try:
                os.remove(path)
                total -= fsize
                removed += 1
            except OSError:
                continue
    except OSError:
        return removed
    return removed
