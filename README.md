# Image Gallery Loader

A read-only, gallery-style replacement for ComfyUI's built-in **Load Image** node.

It browses folders you configure in its own settings window and loads the image you
click. It never writes, moves, renames, copies or deletes anything; the only file it
creates for itself is `config.json` (plus a thumbnail cache in `_thumbcache/`).

The node produces the same `IMAGE` / `MASK` pair, with the same tensor shapes and
dtypes, as the stock `LoadImage` node, so it can be dropped into an existing workflow
without anything downstream noticing.

## Features

- In-node, mobile-gallery-style browser with folder cards, breadcrumbs and a filter box.
- Four view modes: large grid, medium grid, small grid, and a details table
  (name, type, size, modified, dimensions).
- Up to **15 configurable folders**; empty or invalid paths are hidden automatically.
- Sorting by name / date / size / type, ascending or descending.
- Thumbnails generated server-side (WebP) and cached on disk with an LRU-style prune.
- Format support: PNG, JPG/JPEG, WebP, BMP, animated images (all frames are batched),
  and EXIF orientation is honoured.
- Corrupt or unreadable files show a generated placeholder instead of breaking the grid.

## Installation

```bash
cd ComfyUI/custom_nodes
git clone <this-repo> Comfyui_Ai_Node_Loadgallery
```

Then restart ComfyUI (or use **Refresh** in the ComfyUI menu). No extra dependencies
beyond what ComfyUI already ships (Pillow, numpy, torch, aiohttp).

Add an **Image Gallery Loader** node, click the gear icon, and paste folder paths.

## How it works

The front end (`web/`) talks to five read-only HTTP routes registered under
`/image_gallery/*`:

| Route | Purpose |
| --- | --- |
| `GET  /image_gallery/config` | Folders, validation status, view preferences |
| `POST /image_gallery/config` | Save folder paths and preferences |
| `GET  /image_gallery/validate` | Validate one path as you type in settings |
| `GET  /image_gallery/list` | List subfolders and images in a configured root |
| `GET  /image_gallery/thumb` | Downscaled thumbnail (and original dimensions) |

Every filesystem access funnels through `gallery_fs.resolve_in_root()`, which applies
two independent checks:

1. **Lexical** — reject absolute paths, drive-relative paths (`C:foo`), UNC paths and
   any `..` segment.
2. **Canonical** — resolve the real path and re-compare, which catches symlinks and
   NTFS junctions pointing outside the root.

The same containment check is repeated on the backend in `gallery_node.py`, because a
path stored in a workflow file can be edited by hand.

## Project layout

```
__init__.py          # node + route registration (ComfyUI entry point)
gallery_node.py      # the ImageGalleryLoader node (tensor output)
gallery_routes.py    # aiohttp routes
gallery_fs.py        # filesystem helpers, path containment, thumbnails
gallery_config.py    # config.json read/write (the only writer)
web/gallery.js       # front-end: in-node gallery + settings window
web/gallery.css      # front-end styles
selftest.py          # path containment, listing, config round trip
selftest_thumbs.py   # thumbnail pipeline (needs Pillow)
selftest_node.py     # IMAGE/MASK parity with the stock LoadImage
```

## Tests

The self-tests run standalone (they do not need a running ComfyUI):

```bash
python selftest.py
python selftest_thumbs.py
python selftest_node.py
```

All fixtures are generated on the fly into `_selftest*/` folders, so the repository
ships no binary test images and nothing extra needs cleaning up (those folders are
git-ignored).

`selftest_node.py` exercises the real loader; outside a full ComfyUI install it falls
back to a CPU / `float32` intermediate dtype so it can run in CI. It backs up and
restores your `config.json` around the run.

## Notes

- The gallery is intentionally **read-only**. It cannot be used to modify your files,
  and the front end blocks file drops.
- ComfyUI binds to `127.0.0.1` by default. If you expose it to a network, be aware that
  the settings validator reports basic file/folder existence for any path it is given.
- `config.json` and `_thumbcache/` are runtime state and are git-ignored.

## License

MIT — it is free to download, use, edit, remix, publish and redistribute, including
for commercial projects. Just keep the copyright notice (see `LICENSE`). No warranty.

You are encouraged to fork it, tweak it, and share your changes. A link back or a
mention is appreciated but not required.

