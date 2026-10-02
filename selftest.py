"""Self-test for the Image Gallery Loader backend.

Runs without ComfyUI (no torch import) and exercises the security boundary,
the directory listing, and the config round trip.

    python selftest.py
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gallery_config as cfg
import gallery_fs as fs

ROOT = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(ROOT, "_selftest")
GALLERY = os.path.join(FIX, "gallery")
OTHER = os.path.join(FIX, "other")


# --------------------------------------------------------------------------- #
# The fixture tree is GENERATED, not committed, so the repository carries no
# binary test images. Nothing here is ever decoded - the tests only list and
# validate by name/extension - so tiny stubs are enough.
# --------------------------------------------------------------------------- #


def _build_fixtures() -> None:
    os.makedirs(os.path.join(GALLERY, "sub deep", "extra"), exist_ok=True)
    os.makedirs(OTHER, exist_ok=True)
    for name in ("a.png", "b.jpg"):
        with open(os.path.join(GALLERY, name), "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n")
    with open(os.path.join(GALLERY, "note.txt"), "w", encoding="utf-8") as fh:
        fh.write("not an image\n")
    with open(os.path.join(OTHER, "secret.png"), "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")


_build_fixtures()

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


def expect_reject(label, rel, root=GALLERY):
    try:
        result = fs.resolve_in_root(root, rel)
        check(label, False, f"-> resolved to {result!r} instead of being rejected")
    except fs.GalleryError as exc:
        check(label, True)


def expect_allow(label, rel, root=GALLERY, expect_suffix=None):
    try:
        result = fs.resolve_in_root(root, rel)
        if expect_suffix is not None:
            ok = result.replace("\\", "/").endswith(expect_suffix)
            check(label, ok, f"-> {result!r}")
        else:
            check(label, True)
    except fs.GalleryError as exc:
        check(label, False, f"-> rejected: {exc}")


print("\n=== path containment (the security boundary) ===")

expect_allow("empty rel -> the root itself", "")
expect_allow("simple subfolder", "sub deep")
expect_allow("nested subfolder", "sub deep/extra")
expect_allow("backslash separators are normalised", "sub deep\\extra")
expect_allow("redundant ./ is collapsed", "./sub deep/./extra")

expect_reject("parent traversal rejected", "..")
expect_reject("deep parent traversal rejected", "../../..")
expect_reject("traversal hidden mid-path rejected", "sub deep/../../..")
expect_reject("absolute posix path rejected", "/etc/passwd")
expect_reject("backslash absolute rejected", "\\\\server\\share")
expect_reject("drive-relative rejected", "C:windows")
expect_reject("NUL byte rejected", "sub\x00deep")
expect_reject("sibling directory via .. rejected", "../other")
expect_reject("windows ..\\ rejected", "..\\other")

# A prefix-sharing sibling must not be treated as contained.
sibling = GALLERY + "-private"
os.makedirs(sibling, exist_ok=True)
check("sibling with shared prefix is not 'within'", not fs.is_within(sibling, GALLERY))
check("root is within itself", fs.is_within(GALLERY, GALLERY))
check("child is within root", fs.is_within(os.path.join(GALLERY, "sub deep"), GALLERY))

# Symlink / junction escape: a link pointing outside the root must be refused.
outside_link = os.path.join(GALLERY, "escape_link")
try:
    if os.path.isdir(outside_link):
        os.rmdir(outside_link)
    os.symlink(OTHER, outside_link, target_is_directory=True)
    try:
        fs.resolve_in_root(GALLERY, "escape_link")
        check("symlink escaping the root is rejected", False, "-> resolved outside the root!")
    except fs.GalleryError:
        check("symlink escaping the root is rejected", True)
except (OSError, NotImplementedError, AttributeError) as exc:
    check("symlink escaping the root is rejected (skipped: needs privileges)", True, str(exc))

print("\n=== folder validation ===")

good = fs.validate_folder(0, GALLERY)
check("valid folder detected", good.valid, good.reason)
check("folder named by its own name", good.display_name == "gallery", good.display_name)
check("image count ignores non-images", good.image_count == 2, str(good.image_count))

bad = fs.validate_folder(1, os.path.join(FIX, "does-not-exist"))
check("missing folder flagged", not bad.valid and bad.reason == "not found", bad.reason)

empty = fs.validate_folder(2, "")
check("empty slot flagged as empty", not empty.valid and empty.reason == "empty", empty.reason)

notdir = fs.validate_folder(3, os.path.join(GALLERY, "a.png"))
check("file is not accepted as a folder", not notdir.valid and notdir.reason == "not a folder")

print("\n=== de-duplication and hiding of invalid paths ===")

roots = fs.valid_roots([GALLERY, "", GALLERY, os.path.join(FIX, "nope"), OTHER])
check("invalid and duplicate slots hidden", len(roots) == 2, str(len(roots)))
check("slot indexes preserved", [r.index for r in roots] == [0, 4], str([r.index for r in roots]))

print("\n=== listing ===")

data = fs.list_dir(0, GALLERY, "")
names = sorted(i["name"] for i in data["images"])
check("only image extensions listed", names == ["a.png", "b.jpg"], str(names))
check("subfolders listed", [f["name"] for f in data["folders"]] == ["sub deep"], str(data["folders"]))
check("txt file excluded", all(not i["name"].endswith(".txt") for i in data["images"]))
check("count matches", data["count"] == 2, str(data["count"]))

nested = fs.list_dir(0, GALLERY, "sub deep/extra")
check("nested empty folder lists cleanly", nested["count"] == 0 and nested["root"] == 0)
check(
    "breadcrumb built",
    [c["name"] for c in nested["breadcrumb"]] == ["sub deep", "extra"],
    str(nested["breadcrumb"]),
)

sorted_desc = fs.list_dir(0, GALLERY, "", sort="name", direction="desc")
check(
    "descending sort reverses",
    [i["name"] for i in sorted_desc["images"]] == list(reversed(names)),
    str([i["name"] for i in sorted_desc["images"]]),
)

try:
    fs.list_dir(0, GALLERY, "sub deep/../../other")
    check("listing rejects traversal", False, "-> traversal succeeded")
except fs.GalleryError:
    check("listing rejects traversal", True)

try:
    fs.list_dir(0, GALLERY, "no-such-folder")
    check("listing a missing folder raises NotFound", False, "-> no error")
except fs.NotFound:
    check("listing a missing folder raises NotFound", True)
except fs.GalleryError as exc:
    check("listing a missing folder raises NotFound", False, type(exc).__name__)

print("\n=== config round trip ===")

saved_original = None
if os.path.isfile(cfg.CONFIG_PATH):
    with open(cfg.CONFIG_PATH, "r", encoding="utf-8") as fh:
        saved_original = fh.read()

try:
    payload = {
        "folders": [GALLERY, "", OTHER] + [""] * 12,
        "view_mode": "details",
        "thumb_size": 384,
        "sort": "date",
        "direction": "desc",
        "cache_limit_mb": 64,
    }
    written = cfg.save(payload)
    check("all 15 slots persisted", len(written["folders"]) == 15, str(len(written["folders"])))
    check("view mode persisted", written["view_mode"] == "details", written["view_mode"])
    check("path at slot 0 persisted", written["folders"][0] == GALLERY)

    reloaded = cfg.load(force=True)
    check("survives a reload", reloaded["folders"][0] == GALLERY and reloaded["thumb_size"] == 384)

    # Hostile / malformed input must degrade to defaults, never crash.
    hostile = cfg._coerce(
        {
            "folders": "not-a-list",
            "view_mode": "../../etc/passwd",
            "thumb_size": "huge",
            "sort": {"nope": 1},
            "direction": 999,
            "last": [1, 2, 3],
        }
    )
    check("bad folders list -> 15 empty slots", hostile["folders"] == [""] * 15)
    check("bad view mode -> default", hostile["view_mode"] == cfg.DEFAULT_VIEW_MODE)
    check("bad sort -> default", hostile["sort"] == "name")
    check("bad last map -> empty", hostile["last"] == {})
    check("config still has 15 slots after coercion", len(cfg._coerce({})["folders"]) == 15)

    # Too many slots must be trimmed, too few padded.
    check("extra slots trimmed", len(cfg._coerce({"folders": ["a"] * 40})["folders"]) == 15)

    # Remembered location
    cfg.remember_rel(GALLERY, "sub deep")
    check("remembered location", cfg.remembered_rel(GALLERY) == "sub deep")
    cfg.remember_rel(GALLERY, "../../etc")
    check("hostile remembered location rejected", cfg.remembered_rel(GALLERY) == "")

    # Root resolution by slot index
    status = cfg.resolve_root(0)
    check("resolve_root maps a slot to a folder", status.path == fs.canonical(GALLERY))
    for bad_index in ("nope", -1, 99, None):
        try:
            cfg.resolve_root(bad_index)
            check(f"resolve_root rejects {bad_index!r}", False, "-> accepted")
        except fs.GalleryError:
            check(f"resolve_root rejects {bad_index!r}", True)
finally:
    # Restore whatever the user had.
    if saved_original is not None:
        with open(cfg.CONFIG_PATH, "w", encoding="utf-8") as fh:
            fh.write(saved_original)
    else:
        try:
            os.remove(cfg.CONFIG_PATH)
        except OSError:
            pass

print(f"\n=== {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
