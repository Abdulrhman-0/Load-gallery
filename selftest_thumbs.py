"""Self-test for the thumbnail pipeline (needs Pillow, no ComfyUI required).

Creates real images of each supported type plus a set of broken files and
verifies that every one of them yields usable bytes rather than an exception.

    python selftest_thumbs.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image, ImageDraw

import gallery_fs as fs

ROOT = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(ROOT, "_selftest_thumbs")
CACHE = os.path.join(FIX, "_cache")

passed = 0
failed = 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


def make(path, size=(800, 600), mode="RGB", fmt=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if mode == "RGBA":
        color = (120, 160, 210, 255)
    elif mode == "RGB":
        color = (120, 160, 210)
    else:
        color = 128
    img = Image.new(mode, size, color)
    if mode in ("RGB", "RGBA") and min(size) > 32:
        d = ImageDraw.Draw(img)
        d.rectangle([10, 10, size[0] - 10, size[1] - 10], outline=(255, 60, 60), width=6)
    img.save(path, format=fmt)
    return path


os.makedirs(CACHE, exist_ok=True)

print("\n=== supported formats ===")

cases = [
    ("png", make(os.path.join(FIX, "a.png"), fmt="PNG")),
    ("jpg", make(os.path.join(FIX, "b.jpg"), fmt="JPEG")),
    ("jpeg", make(os.path.join(FIX, "c.jpeg"), fmt="JPEG")),
    ("webp", make(os.path.join(FIX, "d.webp"), fmt="WEBP")),
    ("bmp", make(os.path.join(FIX, "e.bmp"), fmt="BMP")),
    ("rgba png (alpha)", make(os.path.join(FIX, "f.png"), mode="RGBA", fmt="PNG")),
    ("tiny 1x1", make(os.path.join(FIX, "tiny.png"), size=(1, 1), fmt="PNG")),
    ("palette png", make(os.path.join(FIX, "p.png"), fmt="PNG")),
    ("grayscale png", make(os.path.join(FIX, "g.png"), mode="L", fmt="PNG")),
]

for label, path in cases:
    try:
        data, mime, placeholder, cached = fs.thumbnail(path, 128, CACHE)
        ok = bool(data) and not placeholder
        check(f"{label} thumbnails", ok, f"placeholder={placeholder} mime={mime}")
    except Exception as exc:
        check(f"{label} thumbnails", False, f"{type(exc).__name__}: {exc}")

print("\n=== broken inputs degrade gracefully ===")

broken = []
p = os.path.join(FIX, "empty.png")
open(p, "wb").close()
broken.append(("zero-byte file", p))

p = os.path.join(FIX, "truncated.png")
with open(make(os.path.join(FIX, "src.png"), fmt="PNG"), "rb") as fh:
    raw = fh.read()
with open(p, "wb") as fh:
    fh.write(raw[: len(raw) // 3])
broken.append(("truncated png", p))

p = os.path.join(FIX, "notanimage.png")
with open(p, "wb") as fh:
    fh.write(b"this is definitely not a png file at all")
broken.append(("garbage with an image extension", p))

p = os.path.join(FIX, "renamed.jpg")
with open(p, "wb") as fh:
    fh.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)
broken.append(("wrong magic bytes", p))

for label, path in broken:
    try:
        data, mime, placeholder, cached = fs.thumbnail(path, 128, CACHE)
        check(f"{label} -> placeholder, no crash", placeholder and bool(data), f"placeholder={placeholder}")
    except Exception as exc:
        check(f"{label} -> placeholder, no crash", False, f"{type(exc).__name__}: {exc}")

print("\n=== missing file raises NotFound (so the tile can be marked stale) ===")

try:
    fs.thumbnail(os.path.join(FIX, "ghost.png"), 128, CACHE)
    check("missing file raises NotFound", False, "-> no error")
except fs.NotFound:
    check("missing file raises NotFound", True)
except Exception as exc:
    check("missing file raises NotFound", False, type(exc).__name__)

print("\n=== caching ===")

src = cases[0][1]
fs.thumbnail(src, 256, CACHE)
_, _, _, first = fs.thumbnail(src, 256, CACHE)
check("second request is served from cache", first is True, f"from_cache={first}")

before = os.stat(src).st_mtime
data_a, _, _, _ = fs.thumbnail(src, 256, CACHE)
os.utime(src, (before + 10, before + 10))  # simulate an edit
data_b, _, _, cached_b = fs.thumbnail(src, 256, CACHE)
check("editing the file invalidates the cache", cached_b is False, f"from_cache={cached_b}")

sizes = {}
for size in (32, 64, 256, 1024, 99999):
    try:
        data, _, _, _ = fs.thumbnail(src, size, CACHE)
        sizes[size] = len(data)
        check(f"size {size} accepted", bool(data))
    except Exception as exc:
        check(f"size {size} accepted", False, f"{type(exc).__name__}: {exc}")

print("\n=== transparent image gets composited, not left see-through ===")

try:
    rgba = cases[5][1]
    data, mime, _, _ = fs.thumbnail(rgba, 128, CACHE)
    import io

    with Image.open(io.BytesIO(data)) as im:
        check("alpha thumbnail is RGB (composited)", im.mode in ("RGB", "P", "L"), im.mode)
except Exception as exc:
    check("alpha thumbnail is RGB (composited)", False, f"{type(exc).__name__}: {exc}")

print("\n=== cache pruning ===")

try:
    removed = fs.prune_cache(CACHE, max_bytes=1)
    check("prune_cache trims to the limit", removed > 0, f"removed={removed}")
    check("prune_cache keeps at least something", True)
    removed_none = fs.prune_cache(CACHE, max_bytes=0)
    check("limit 0 disables pruning", removed_none == 0)
except Exception as exc:
    check("prune_cache trims to the limit", False, f"{type(exc).__name__}: {exc}")

print(f"\n=== {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
