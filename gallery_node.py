"""The ``Image Gallery Loader`` node.

A read-only, gallery-style replacement for the built-in ``Load Image`` node.
It produces the same ``IMAGE`` / ``MASK`` pair with the same tensor shapes and
dtypes, so it can be swapped into an existing workflow without anything
downstream noticing.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any, Dict, Tuple

import numpy as np
import torch

try:  # running as the custom_nodes package (normal ComfyUI case)
    from . import gallery_config as cfg
    from . import gallery_fs as fs
except ImportError:  # pragma: no cover - direct import, e.g. the self-test
    import gallery_config as cfg
    import gallery_fs as fs


def _empty_mask(dtype: torch.dtype, device: torch.device) -> torch.Tensor:
    """The 64x64 "no mask" tensor used by ComfyUI's LoadImage.

    Kept identical (including the tiny 64x64 size) so that downstream nodes see
    exactly what they would see from the stock node when a file has no alpha.
    """
    return torch.zeros((64, 64), dtype=dtype, device=device)


class ImageGalleryLoader:
    """Browse configured folders and load the selected image."""

    CATEGORY = "image"
    DESCRIPTION = (
        "Load an image by browsing a gallery of folders configured in the node's "
        "settings. Read-only: the gallery only browses, previews and selects."
    )
    SEARCH_ALIASES = [
        "load image",
        "loadimage",
        "load images",
        "image gallery",
        "gallery",
        "gallery loader",
        "gallery view",
        "image loader",
        "image browser",
        "browse images",
        "image folder",
        "folder browser",
        "file browser",
        "open image",
        "select image",
        "image select",
        "image picker",
        "image preview",
        "local image",
        "thumbnail",
        "thumbnails",
        "picture",
        "photos",
    ]

    RETURN_TYPES = ("IMAGE", "MASK")
    RETURN_NAMES = ("image", "mask")
    OUTPUT_TOOLTIPS = (
        "The selected image, as a batch of shape [B, H, W, 3] in the range 0-1.",
        "The alpha channel as a mask (1 - alpha), or an empty mask if the image "
        "has no alpha channel.",
    )
    FUNCTION = "load_image"

    #: ``image_path`` is hidden by the JavaScript extension and driven by the
    #: gallery. It is a plain STRING so the value survives a workflow save.
    @classmethod
    def INPUT_TYPES(cls) -> Dict[str, Any]:
        return {
            "required": {
                "image_path": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": False,
                        "tooltip": "Absolute path of the selected image. Set by "
                        "the gallery widget.",
                    },
                ),
            },
        }

    # ------------------------------------------------------------------ #
    # execution
    # ------------------------------------------------------------------ #

    def load_image(self, image_path: str) -> Tuple[torch.Tensor, torch.Tensor]:
        from PIL import Image, ImageOps, ImageSequence

        try:
            import comfy.model_management as model_management

            dtype = model_management.intermediate_dtype()
            device = model_management.intermediate_device()
        except (ImportError, AttributeError):
            # Outside a full ComfyUI install (e.g. the standalone self-test or
            # CI) fall back to the stock CPU / float32 pair. In ComfyUI this
            # branch never runs.
            dtype = torch.float32
            device = torch.device("cpu")

        path = self._checked_path(image_path)

        output_images = []
        output_masks = []
        w = h = None

        with Image.open(path) as img:
            for frame in ImageSequence.Iterator(img):
                try:
                    frame = ImageOps.exif_transpose(frame)
                except Exception:
                    pass

                rgb = frame.convert("RGB")

                if not output_images:
                    w, h = rgb.size
                elif rgb.size != (w, h):
                    # Animated formats can change size mid-sequence; the stock
                    # node skips those frames, so we do too.
                    continue

                arr = np.array(rgb).astype(np.float32) / 255.0
                output_images.append(torch.from_numpy(arr)[None,])

                if "A" in frame.getbands():
                    alpha = np.array(frame.getchannel("A")).astype(np.float32) / 255.0
                    output_masks.append((1.0 - torch.from_numpy(alpha)).unsqueeze(0))
                else:
                    # unsqueeze(0) is required: the stock node batches every
                    # mask as [1, H, W]. Without it a no-alpha frame would be
                    # [64, 64] and cat() would mis-batch (or raise on a mix).
                    output_masks.append(
                        _empty_mask(torch.float32, torch.device("cpu")).unsqueeze(0)
                    )

        if not output_images:
            raise ValueError(
                f"Could not read any frames from '{os.path.basename(path)}'. "
                "The file may be corrupt or empty."
            )

        images = torch.cat(output_images, dim=0).to(device=device, dtype=dtype)
        masks = torch.cat(output_masks, dim=0).to(device=device, dtype=dtype)
        return (images, masks)

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _checked_path(image_path: str) -> str:
        """Re-validate the stored path on the backend before opening it.

        The front end already validated when the image was picked, but the
        value arrives from a workflow file which a user can edit by hand, so
        the containment check has to be repeated here.
        """
        if not image_path or not str(image_path).strip():
            raise ValueError(
                "No image selected. Click an image in the gallery to choose one."
            )

        target = fs.canonical(str(image_path))

        if not os.path.exists(target):
            raise ValueError(
                f"Image not found: '{os.path.basename(target)}'. It may have been "
                "moved or deleted since it was selected."
            )
        if not os.path.isfile(target):
            raise ValueError("The selected path is not a file.")

        ext = os.path.splitext(target)[1].lower()
        if ext not in fs.IMAGE_EXTS:
            raise ValueError(
                f"Unsupported image type '{ext or 'unknown'}'. Supported: "
                + ", ".join(sorted(fs.IMAGE_EXTS))
            )

        if not ImageGalleryLoader._inside_a_root(target):
            raise ValueError(
                "The selected image is not inside any configured folder. "
                "Add its folder in the node's settings to load it."
            )

        return target

    @staticmethod
    def _inside_a_root(target: str) -> bool:
        for status in cfg.roots():
            if fs.is_within(target, status.path):
                return True
        return False

    # ------------------------------------------------------------------ #
    # caching / validation
    # ------------------------------------------------------------------ #

    @classmethod
    def IS_CHANGED(cls, image_path: str) -> str:
        """Invalidate the cache when the file changes.

        The stock node hashes the entire file; we only need mtime + size, which
        is what actually changes when the user picks or replaces an image, and
        costs nothing on large folders.
        """
        try:
            path = fs.canonical(str(image_path or ""))
            st = os.stat(path)
            raw = f"{path}|{int(st.st_mtime)}|{st.st_size}"
        except (OSError, ValueError):
            raw = f"{image_path}|missing"
        return hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()

    @classmethod
    def VALIDATE_INPUTS(cls, image_path: str) -> Any:
        """Give a readable error before execution rather than a traceback."""
        if not image_path or not str(image_path).strip():
            # Not an error: an unconfigured node is a normal state to be in.
            return True
        try:
            cls._checked_path(image_path)
        except ValueError as exc:
            return str(exc)
        return True


NODE_CLASS_MAPPINGS: Dict[str, Any] = {
    "ImageGalleryLoader": ImageGalleryLoader,
}

NODE_DISPLAY_NAME_MAPPINGS: Dict[str, str] = {
    "ImageGalleryLoader": "Image Gallery Loader",
}
