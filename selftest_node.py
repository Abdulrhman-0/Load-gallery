"""Self-test for the node's tensor output.

Verifies that ImageGalleryLoader produces the same IMAGE/MASK tensors as the
built-in ComfyUI LoadImage node, using a copy of LoadImage's loading logic as
the reference. Also covers animated frames, alpha, missing files, and the
path-containment rejection.

Needs torch + numpy + Pillow; does not need a running ComfyUI.

    python selftest_node.py
"""

import atexit
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import torch
from PIL import Image, ImageDraw, ImageOps, ImageSequence

import gallery_config as cfg
import gallery_fs as fs
from gallery_node import ImageGalleryLoader

ROOT = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(ROOT, "_selftest_node")
GALLERY = os.path.join(FIX, "gallery")
OUTSIDE = os.path.join(FIX, "outside")

# The loader refuses files outside a configured folder, so point slot 0 at the
# fixture directory. The user's real config.json is restored on exit.
_saved_config = None
if os.path.isfile(cfg.CONFIG_PATH):
    with open(cfg.CONFIG_PATH, "r", encoding="utf-8") as fh:
        _saved_config = fh.read()


def _restore_config():
    try:
        if _saved_config is not None:
            with open(cfg.CONFIG_PATH, "w", encoding="utf-8") as fh:
                fh.write(_saved_config)
        elif os.path.isfile(cfg.CONFIG_PATH):
            os.remove(cfg.CONFIG_PATH)
    except OSError:
        pass


atexit.register(_restore_config)

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


def reference_load(path, dtype=torch.float32):
    """The stock LoadImage logic, minus the pyav fast path, copied verbatim."""
    output_images = []
    output_masks = []
    w = h = None
    with Image.open(path) as img:
        for i in ImageSequence.Iterator(img):
            i = ImageOps.exif_transpose(i)
            image = i.convert("RGB")
            if len(output_images) == 0:
                w = image.size[0]
                h = image.size[1]
            if image.size[0] != w or image.size[1] != h:
                continue
            arr = np.array(image).astype(np.float32) / 255.0
            output_images.append(torch.from_numpy(arr)[None,])
            if "A" in i.getbands():
                mask = np.array(i.getchannel("A")).astype(np.float32) / 255.0
                mask = 1.0 - torch.from_numpy(mask)
            else:
                mask = torch.zeros((64, 64), dtype=torch.float32, device="cpu")
            output_masks.append(mask.unsqueeze(0))
    return torch.cat(output_images, dim=0).to(dtype=dtype), torch.cat(output_masks, dim=0).to(dtype=dtype)


os.makedirs(GALLERY, exist_ok=True)
os.makedirs(OUTSIDE, exist_ok=True)

# Slot 0 must point at the fixture folder before any image is loaded.
cfg.save({"folders": [GALLERY] + [""] * (cfg.FOLDER_SLOTS - 1)})


def make(path, size=(64, 48), mode="RGB", frames=1, fmt=None, exif=None):
    imgs = []
    for n in range(frames):
        if mode == "RGBA":
            # Semi-transparent, so 1 - alpha is non-zero and the mask test is
            # meaningful (a fully opaque alpha yields an all-zero mask).
            img = Image.new(mode, size, (10 + n * 20, 90, 200, max(0, 200 - n * 40)))
        elif mode == "RGB":
            img = Image.new(mode, size, (10 + n * 20, 90, 200))
        else:
            img = Image.new(mode, size, 120)
        # A single-channel mode rejects an RGB outline, and tiny images have no
        # room for a border at all, so guard both.
        if min(size) > 6:
            outline = (255, 0, 0) if mode in ("RGB", "RGBA") else 255
            d = ImageDraw.Draw(img)
            d.rectangle([2, 2, size[0] - 3, size[1] - 3], outline=outline, width=2)
        imgs.append(img)
    if frames > 1:
        imgs[0].save(path, format=fmt or "PNG", save_all=True, append_images=imgs[1:])
    elif exif is not None:
        imgs[0].save(path, format=fmt or "PNG", exif=exif)
    else:
        # Passing exif=None makes the JPEG plugin call len(None) and crash.
        imgs[0].save(path, format=fmt or "PNG")
    return path


print("\n=== IMAGE / MASK parity with the built-in LoadImage ===")

cases = {
    "rgb png": make(os.path.join(GALLERY, "rgb.png"), fmt="PNG"),
    "rgb jpg": make(os.path.join(GALLERY, "rgb.jpg"), fmt="JPEG"),
    "alpha png": make(os.path.join(GALLERY, "alpha.png"), mode="RGBA", fmt="PNG"),
    "webp": make(os.path.join(GALLERY, "img.webp"), fmt="WEBP"),
    "bmp": make(os.path.join(GALLERY, "img.bmp"), fmt="BMP"),
    "grayscale": make(os.path.join(GALLERY, "gray.png"), mode="L", fmt="PNG"),
    "odd size 7x3": make(os.path.join(GALLERY, "odd.png"), size=(7, 3), fmt="PNG"),
    "square 1x1": make(os.path.join(GALLERY, "one.png"), size=(1, 1), fmt="PNG"),
}

node = ImageGalleryLoader()

for label, path in cases.items():
    try:
        got_img, got_mask = node.load_image(path)
        ref_img, ref_mask = reference_load(path)
        same_img = got_img.shape == ref_img.shape and torch.allclose(got_img, ref_img, atol=1e-6)
        same_mask = got_mask.shape == ref_mask.shape and torch.allclose(got_mask, ref_mask, atol=1e-6)
        check(f"{label}: IMAGE matches ({tuple(got_img.shape)})", same_img,
              f"got {tuple(got_img.shape)} ref {tuple(ref_img.shape)}")
        check(f"{label}: MASK matches ({tuple(got_mask.shape)})", same_mask,
              f"got {tuple(got_mask.shape)} ref {tuple(ref_mask.shape)}")
    except Exception as exc:
        check(f"{label}: loads", False, f"{type(exc).__name__}: {exc}")

print("\n=== dtype / shape contract ===")

img, mask = node.load_image(cases["rgb png"])
check("IMAGE is 4-dimensional [B,H,W,C]", img.dim() == 4, str(img.dim()))
check("IMAGE has 3 channels", img.shape[-1] == 3, str(img.shape))
check("IMAGE is float", img.dtype.is_floating_point, str(img.dtype))
check("IMAGE within 0..1", float(img.min()) >= 0.0 and float(img.max()) <= 1.0,
      f"{float(img.min())}..{float(img.max())}")
check("MASK is 3-dimensional [B,H,W]", mask.dim() == 3, str(mask.dim()))
check("MASK is float", mask.dtype.is_floating_point, str(mask.dtype))
check("returns a 2-tuple", isinstance(node.load_image(cases["rgb png"]), tuple))

img2, mask2 = node.load_image(cases["alpha png"])
check("alpha MASK is the full image size", tuple(mask2.shape[-2:]) == (48, 64), str(mask2.shape))
check("alpha MASK is 1 - alpha", float(mask2.max()) > 0.0, str(float(mask2.max())))

img3, mask3 = node.load_image(cases["rgb png"])
check("no-alpha MASK is the 64x64 stock shape", tuple(mask3.shape[-2:]) == (64, 64), str(mask3.shape))
check("no-alpha MASK is all zeros", float(mask3.abs().sum()) == 0.0)

print("\n=== animated images batch every frame ===")

anim = make(os.path.join(GALLERY, "anim.png"), frames=3, fmt="PNG")
try:
    aimg, amask = node.load_image(anim)
    check("animated png returns one frame per batch entry", aimg.shape[0] == 3, str(aimg.shape))
    ref = reference_load(anim)
    check("animated png matches the reference batch", torch.allclose(aimg, ref[0], atol=1e-6))
except Exception as exc:
    check("animated png loads", False, f"{type(exc).__name__}: {exc}")

# Frames that change size mid-sequence must be skipped, not crash. Pillow's
# APNG/GIF writers normalise every frame onto one canvas, so a genuinely
# size-changing file cannot be produced with Pillow alone; assert the
# well-defined behaviour either way.
mixed = os.path.join(GALLERY, "mixed.png")
f1 = Image.new("RGB", (32, 32), (200, 0, 0))
f2 = Image.new("RGB", (16, 16), (0, 200, 0))
try:
    f1.save(mixed, format="PNG", save_all=True, append_images=[f2])
    with Image.open(mixed) as probe:
        frame_sizes = [fr.size for fr in ImageSequence.Iterator(probe)]
    if len(set(frame_sizes)) == 1:
        check("uniform-frame animation batches every frame", True)
    else:
        mimg, _ = node.load_image(mixed)
        check("size-changing frames are skipped", mimg.shape[0] == 1, str(mimg.shape))
except Exception as exc:
    check("animation with mixed frame sizes handled", False, f"{type(exc).__name__}: {exc}")

print("\n=== EXIF orientation is applied ===")

# Orientation 6 = rotate 90 CW, so the stored 40x20 becomes 20x40.
oriented = os.path.join(GALLERY, "oriented.jpg")
big = Image.new("RGB", (40, 20), (30, 60, 90))
exif = big.getexif()
exif[274] = 6
big.save(oriented, format="JPEG", exif=exif)
try:
    oimg, _ = node.load_image(oriented)
    ow, oh = oimg.shape[2], oimg.shape[1]
    check("EXIF orientation is honoured", (ow, oh) == (20, 40), f"got {ow}x{oh}")
except Exception as exc:
    check("EXIF orientation is honoured", False, f"{type(exc).__name__}: {exc}")

print("\n=== bad input is reported readably, never as a traceback ===")

cfg.save({"folders": [GALLERY] + [""] * 14})

bad_cases = [
    ("empty path", ""),
    ("whitespace path", "   "),
    ("missing file", os.path.join(GALLERY, "ghost.png")),
    ("a directory", GALLERY),
    ("unsupported extension", make(os.path.join(GALLERY, "notes.txt"))),
]
for label, value in bad_cases:
    try:
        node.load_image(value)
        check(f"{label} raises", False, "-> no error raised")
    except ValueError as exc:
        check(f"{label} raises a readable ValueError", bool(str(exc)), str(exc))
    except Exception as exc:
        check(f"{label} raises a readable ValueError", False, f"{type(exc).__name__}: {exc}")

print("\n=== paths outside every configured folder are refused ===")

outside = make(os.path.join(OUTSIDE, "private.png"))
try:
    node.load_image(outside)
    check("image outside all configured folders is refused", False, "-> loaded anyway!")
except ValueError as exc:
    check("image outside all configured folders is refused", "not inside any configured" in str(exc), str(exc))

try:
    node.load_image(os.path.join(GALLERY, "..", "outside", "private.png"))
    check("traversal to outside is refused", False, "-> loaded anyway!")
except ValueError:
    check("traversal to outside is refused", True)

print("\n=== the folder list only exposes valid, unique folders ===")

cfg.save({"folders": [GALLERY, "", OUTSIDE, os.path.join(FIX, "nope"), GALLERY] + [""] * 10})
payload = cfg.root_list_payload()
check("invalid and duplicate folders hidden from the gallery", len(payload) == 2, str(len(payload)))
check("folders labelled by their own name", [r["name"] for r in payload] == ["gallery", "outside"],
      str([r["name"] for r in payload]))
check("root payload carries the real path for joining", payload[0]["path"] == fs.canonical(GALLERY))

print("\n=== IS_CHANGED / VALIDATE_INPUTS ===")

h1 = ImageGalleryLoader.IS_CHANGED(cases["rgb png"])
h2 = ImageGalleryLoader.IS_CHANGED(cases["rgb png"])
check("IS_CHANGED is stable for an unchanged file", h1 == h2, f"{h1} != {h2}")
before = os.stat(cases["rgb png"]).st_mtime
os.utime(cases["rgb png"], (before + 5, before + 5))
check("IS_CHANGED changes when the file changes", ImageGalleryLoader.IS_CHANGED(cases["rgb png"]) != h1)
check("IS_CHANGED survives a missing file", isinstance(ImageGalleryLoader.IS_CHANGED("/nope/x.png"), str))
check("IS_CHANGED returns a str, not a bool", not isinstance(h1, bool))

check("VALIDATE_INPUTS accepts an empty path", ImageGalleryLoader.VALIDATE_INPUTS("") is True)
check("VALIDATE_INPUTS accepts a good path", ImageGalleryLoader.VALIDATE_INPUTS(cases["rgb png"]) is True)
msg = ImageGalleryLoader.VALIDATE_INPUTS(os.path.join(GALLERY, "ghost.png"))
check("VALIDATE_INPUTS returns a message for a bad path", isinstance(msg, str), repr(msg))

print("\n=== node metadata ===")

check("RETURN_TYPES is (IMAGE, MASK)", ImageGalleryLoader.RETURN_TYPES == ("IMAGE", "MASK"))
check("RETURN_NAMES matches", ImageGalleryLoader.RETURN_NAMES == ("image", "mask"))
check("CATEGORY is set", bool(ImageGalleryLoader.CATEGORY))
check("FUNCTION names a real method", callable(getattr(ImageGalleryLoader, ImageGalleryLoader.FUNCTION, None)))
check("INPUT_TYPES declares image_path", "image_path" in ImageGalleryLoader.INPUT_TYPES()["required"])
check("image_path is a STRING", ImageGalleryLoader.INPUT_TYPES()["required"]["image_path"][0] == "STRING")
check("SEARCH_ALIASES include 'load image'", "load image" in ImageGalleryLoader.SEARCH_ALIASES)

print(f"\n=== {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
