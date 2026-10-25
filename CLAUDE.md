# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running scripts

Virtual environment is at `/home/rball/myenv`. Run scripts with:

```bash
/home/rball/myenv/bin/python <script>.py
```

No test suite exists. Verify changes by running the app directly.

## Building the photo_sorter executable

```bash
/home/rball/myenv/bin/pyinstaller photo_sorter.spec
```

Output goes to `dist/`. The spec bundles all PIL submodules and produces a single-file console-less executable.

## Codebase overview

All tools are standalone Tkinter GUI scripts with no shared state between them.

| Script | Purpose |
|---|---|
| `photo_sorter.py` | Main app: drag-and-drop grid sorter + sequential renamer |
| `resize.py` | Batch resizes image trees to 1080px on the short side |
| `dups.py` | Scans a directory tree for duplicate filenames |
| `flatten.py` | Copies all files from a tree into a single flat directory |
| `sync2.py` | Syncs two photo directories; converts PNG→JPEG on copy |
| `jpeg2jpg.py` | CLI script — rename `.jpeg` → `.jpg` (hardcoded path, edit before use) |

`scrolledtextpair.py` and `texthandler.py` are shared helpers used by the older tools (`dups`, `flatten`, `sync2`): a paired two-pane ScrolledText widget and a `logging.Handler` subclass that writes log records into a tk Text widget.

## photo_sorter.py architecture

Two classes in one file:

**`ImageViewer(tk.Toplevel)`** — double-click viewer. Loads the full-resolution PIL image, displays it on a Canvas, supports mouse-wheel zoom centered on cursor and click-drag pan. Self-contained; opened by `DragDropSorter.open_image_viewer()`.

**`DragDropSorter(tk.Tk)`** — main window. Key state:
- `image_data` — list of dicts `{filename, photo, label, row, column}`. This is the canonical display order; all reorder operations mutate this list then call `redraw_grid()`.
- `on_disk_order` — snapshot of filenames at last save/load, used to detect unsaved reordering on exit. Only exists after loading completes.
- `selected_widgets` — list of currently selected `tk.Label`s.

**Threading**: thumbnail loading runs in a daemon thread (`load_thumbnails_thread`). All UI updates from that thread go through `self.after(0, ...)` to stay on the main thread.

**Rename flow**: two-phase to avoid collisions — first rename all files to `_temp_NNN.ext`, then rename temps to `prefix_NNN.ext`. A `rename_log_*.json` is written into the image directory after each rename and can be used by "Restore From Log" to reverse the operation.

**Config**: `config.json` (photo_sorter) and `photo_resizer_settings.json` (resize.py) are written relative to CWD, not the script directory.
