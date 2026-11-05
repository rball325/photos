import tkinter as tk
from tkinter import messagebox
from tkinter import filedialog
from tkinter import simpledialog
from tkinter import ttk
from PIL import Image, ImageTk, ImageOps, IptcImagePlugin, PngImagePlugin
import os, json, threading, datetime, calendar, re, shutil, math, stat, subprocess
import xml.etree.ElementTree as ET
import piexif

CONFIG_PATH = "config.json"

PREFIX_DATE_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})_')
JPEG_EXTENSIONS = (".jpg", ".jpeg")


def parse_date_from_prefix(prefix):
    """Extract a leading YYYY-MM-DD_ date from a rename prefix, if present."""
    match = PREFIX_DATE_RE.match(prefix)
    if not match:
        return None
    try:
        return datetime.date(*(int(g) for g in match.groups()))
    except ValueError:
        return None


def replace_prefix_date(prefix, date):
    """Put date at the front of prefix, replacing a leading YYYY-MM-DD if there is
    one and keeping whatever follows it (e.g. "1975-12-25_family" -> "<date>_family")."""
    prefix = prefix.strip()
    match = re.match(r'\d{4}-\d{2}-\d{2}(?=_|$)', prefix)
    rest = prefix[match.end():] if match else ("_" + prefix if prefix else "")
    return date.strftime("%Y-%m-%d") + (rest or "_")


def set_jpeg_date_taken(path, date):
    """Write date into JPEG EXIF date tags by editing only the EXIF segment
    in place -- the compressed image data is never touched/re-encoded."""
    exif_date = date.strftime("%Y:%m:%d 00:00:00")
    exif_dict = load_exif(path)
    exif_dict["0th"][piexif.ImageIFD.DateTime] = exif_date
    exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = exif_date
    exif_dict["Exif"][piexif.ExifIFD.DateTimeDigitized] = exif_date
    write_exif(path, exif_dict)


# EXIF Orientation value that shows the image turned a further 90° clockwise.
ROTATE_CW = {1: 6, 6: 3, 3: 8, 8: 1, 2: 7, 7: 4, 4: 5, 5: 2}
ROTATE_CCW = {after: before for before, after in ROTATE_CW.items()}


def rotate_jpeg(path, clockwise=True):
    """Rotate a JPEG 90° by updating its EXIF Orientation tag, so the
    compressed image data is never re-encoded."""
    exif_dict = load_exif(path)
    current = exif_dict["0th"].get(piexif.ImageIFD.Orientation, 1)
    turn = ROTATE_CW if clockwise else ROTATE_CCW
    exif_dict["0th"][piexif.ImageIFD.Orientation] = turn.get(current, turn[1])
    write_exif(path, exif_dict)


def load_exif(path):
    try:
        return piexif.load(path)
    except Exception:
        return {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}


def write_exif(path, exif_dict):
    """Replace only the EXIF segment of a JPEG in place."""
    # Photos are often kept read-only; lift that just long enough to write the
    # EXIF segment, then put the original permissions back.
    mode = os.stat(path).st_mode
    if not mode & stat.S_IWUSR:
        os.chmod(path, mode | stat.S_IWUSR)
    try:
        piexif.insert(piexif.dump(exif_dict), path)
    finally:
        if not mode & stat.S_IWUSR:
            os.chmod(path, mode)


THUMB_DIR = ".thumbs"
_thumb_cache_warned = set()  # folders whose cache couldn't be written, so we warn once


def thumb_cache_path(path):
    """Cached thumbnail for path: a hidden .thumbs folder beside it, named thumb_<file>."""
    directory, name = os.path.split(path)
    return os.path.join(directory, THUMB_DIR, "thumb_" + name)


def source_key(path):
    """Size and modification time of the photo, stored in its cached thumbnail so
    any edit (a rotation, an EXIF date) or a different file under the same name
    is noticed and the thumbnail rebuilt."""
    st = os.stat(path)
    return f"{st.st_size}:{st.st_mtime_ns}"


def read_cached_thumb(path, key):
    try:
        with Image.open(thumb_cache_path(path)) as img:
            stored = img.info.get("comment") or img.info.get("source")
            if isinstance(stored, bytes):
                stored = stored.decode("ascii", "replace")
            if stored != key:
                return None
            img.load()
            return img
    except Exception:
        return None


def write_cached_thumb(path, img, key):
    cache = thumb_cache_path(path)
    tmp = f"{cache}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        if path.lower().endswith(".png"):
            info = PngImagePlugin.PngInfo()
            info.add_text("source", key)
            img.save(tmp, "PNG", pnginfo=info)
        else:
            rgb = img if img.mode in ("RGB", "L") else img.convert("RGB")
            rgb.save(tmp, "JPEG", quality=85, comment=key)
        os.replace(tmp, cache)
    except OSError as e:
        directory = os.path.dirname(path)
        if directory not in _thumb_cache_warned:
            _thumb_cache_warned.add(directory)
            print(f"⚠️ Could not cache thumbnails in {directory}: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass


def move_cached_thumb(old_path, new_path):
    """Carry a cached thumbnail along when its photo is renamed or moved."""
    old_cache = thumb_cache_path(old_path)
    if not os.path.exists(old_cache):
        return
    try:
        new_cache = thumb_cache_path(new_path)
        os.makedirs(os.path.dirname(new_cache), exist_ok=True)
        shutil.move(old_cache, new_cache)
    except OSError as e:
        print(f"⚠️ Could not move cached thumbnail {old_cache}: {e}")


TRASH_DIR = ".trash"
TREE_PLACEHOLDER = "\x01"  # stand-in child that gives an unopened tree row its arrow
LOG_FILE_RE = re.compile(r"(restored\d*_)?(rename|move)_log_.*\.json$")


def move_to_trash(directory, name):
    """Move a photo or folder into the hidden .trash folder beside it, instead of
    deleting it. A plain rename, so read-only photos keep their mode. Returns the
    name it was given there, suffixed if that name was already taken."""
    trash = os.path.join(directory, TRASH_DIR)
    os.makedirs(trash, exist_ok=True)
    base, ext = (os.path.splitext(name) if os.path.isfile(os.path.join(directory, name)) else (name, ""))
    final, n = name, 1
    while os.path.exists(os.path.join(trash, final)):
        n += 1
        final = f"{base} ({n}){ext}"
    os.rename(os.path.join(directory, name), os.path.join(trash, final))
    return final


def remove_cached_thumb(path):
    try:
        os.remove(thumb_cache_path(path))
    except OSError:
        pass


XMP_NS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dc": "http://purl.org/dc/elements/1.1/",
}


def read_photo_metadata(path):
    """Return (date_taken, keywords) for display. date_taken is a string or None.
    Keywords come mainly from XMP dc:subject (what digiKam, Lightroom, darktable,
    Windows "Tags" etc. read and filter on), merged with legacy IPTC Keywords and
    Windows XPKeywords so tags written by older tools still show up."""
    date_taken, keywords = None, []
    with Image.open(path) as img:
        exif = img.getexif()
        raw_date = exif.get_ifd(0x8769).get(36867) or exif.get(306)  # DateTimeOriginal, DateTime
        if raw_date:
            date_part, _, time_part = str(raw_date).strip().partition(" ")
            date_taken = date_part.replace(":", "-")
            if time_part and time_part != "00:00:00":
                date_taken += f" {time_part}"

        xmp = img.info.get("xmp")
        if xmp:
            try:
                root = ET.fromstring(xmp)
                keywords += [li.text.strip() for li in root.iterfind(".//dc:subject//rdf:li", XMP_NS)
                             if li.text and li.text.strip()]
            except ET.ParseError:
                pass

        iptc = IptcImagePlugin.getiptcinfo(img) or {}
        iptc_kw = iptc.get((2, 25), [])
        for kw in iptc_kw if isinstance(iptc_kw, list) else [iptc_kw]:
            keywords.append(kw.decode("utf-8", "replace").strip())

        xp = exif.get(0x9C9E)  # XPKeywords: UTF-16LE, ';'-separated
        if xp:
            xp = bytes(xp).decode("utf-16-le", "replace").rstrip("\x00")
            keywords += [k.strip() for k in xp.split(";") if k.strip()]

    seen = set()
    keywords = [k for k in keywords if k and not (k.lower() in seen or seen.add(k.lower()))]
    return date_taken, keywords


def edit_keywords(paths, add=(), remove=()):
    """Add and/or remove keywords on JPEGs with exiftool, which rewrites only the
    metadata. Keywords go in XMP dc:subject (what digiKam, Lightroom, Windows
    etc. filter on) and IPTC Keywords (for older tools). Returns exiftool's summary."""
    args = ["-q", "-overwrite_original_in_place", "-charset", "iptc=UTF8", "-codedcharacterset=utf8"]
    for kw in list(remove) + list(add):
        args += [f"-XMP-dc:Subject-={kw}", f"-IPTC:Keywords-={kw}"]
    for kw in add:  # removing first means a keyword already there isn't duplicated
        args += [f"-XMP-dc:Subject+={kw}", f"-IPTC:Keywords+={kw}"]
    # Photos are often kept read-only; lift that just long enough to write.
    modes = {p: os.stat(p).st_mode for p in paths}
    for p, mode in modes.items():
        if not mode & stat.S_IWUSR:
            os.chmod(p, mode | stat.S_IWUSR)
    try:
        result = subprocess.run(["exiftool", "-@", "-"], input="\n".join(args + list(paths)),
                                capture_output=True, text=True)
    finally:
        for p, mode in modes.items():
            if not mode & stat.S_IWUSR:
                os.chmod(p, mode)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"exiftool exited with {result.returncode}")
    return result.stderr.strip()


def find_next_index(directory, prefix):
    """Scan an existing directory for files already named <prefix><digits><ext>
    and return the next unused index, so a batch moved there won't collide."""
    pattern = re.compile(r'^' + re.escape(prefix) + r'(\d+)\.[^.]+$', re.IGNORECASE)
    max_idx = 0
    if os.path.isdir(directory):
        for f in os.listdir(directory):
            match = pattern.match(f)
            if match:
                max_idx = max(max_idx, int(match.group(1)))
    return max_idx + 1


DATE_PATTERNS = [
    (re.compile(r'\b(\d{4})[-_](\d{2})[-_](\d{2})\b'), 'ymd'),
    (re.compile(r'\b(\d{4})(\d{2})(\d{2})\b'), 'ymd'),
    (re.compile(r'\b(\d{4})[-_](\d{2})\b'), 'ym'),
    (re.compile(r'\b(\d{4})\b'), 'y'),
    (re.compile(r'\b(\d{3}0)s\b'), 'y'),  # decade folder, e.g. "1950s" -> 1950-01-01
]


def infer_date_from_path(path):
    """Scan path components leaf-first for a date, falling back to just a year (Jan 1)."""
    parts = [p for p in os.path.normpath(path).split(os.sep) if p]
    for part in reversed(parts):
        for pattern, kind in DATE_PATTERNS:
            match = pattern.search(part)
            if not match:
                continue
            try:
                if kind == 'ymd':
                    year, month, day = (int(g) for g in match.groups())
                    return datetime.date(year, month, day)
                elif kind == 'ym':
                    year, month = (int(g) for g in match.groups())
                    return datetime.date(year, month, 1)
                else:
                    year = int(match.group(1))
                    if not (1800 <= year <= 2100):
                        continue
                    return datetime.date(year, 1, 1)
            except ValueError:
                continue
    return None


class DatePickerDialog(tk.Toplevel):
    def __init__(self, master, initial=None):
        super().__init__(master)
        self.title("Choose Date")
        self.resizable(False, False)
        self.result = None
        self._initial = initial
        self._date = (initial or datetime.date.today()).replace(day=1)

        entry_frame = tk.Frame(self)
        tk.Label(entry_frame, text="Date (YYYY-mm-dd):").pack(side="left")
        self.entry_var = tk.StringVar(value=(initial or datetime.date.today()).strftime("%Y-%m-%d"))
        entry = tk.Entry(entry_frame, textvariable=self.entry_var, width=12)
        entry.pack(side="left", padx=5)
        entry.bind("<Return>", lambda e: self._accept_entry())
        tk.Button(entry_frame, text="Use", command=self._accept_entry).pack(side="left")
        entry_frame.pack(padx=5, pady=5)

        nav_frame = tk.Frame(self)
        tk.Button(nav_frame, text="<", width=2, command=self._prev_month).pack(side="left")
        self.month_label = tk.Label(nav_frame, width=15, anchor="center")
        self.month_label.pack(side="left")
        tk.Button(nav_frame, text=">", width=2, command=self._next_month).pack(side="left")
        nav_frame.pack(pady=2)

        self.cal_frame = tk.Frame(self)
        self.cal_frame.pack(padx=5, pady=5)

        self._render_calendar()
        self.transient(master)
        self.grab_set()

    def _accept_entry(self):
        text = self.entry_var.get().strip()
        try:
            picked = datetime.datetime.strptime(text, "%Y-%m-%d").date()
        except ValueError:
            messagebox.showerror("Invalid Date", "Enter a date as YYYY-mm-dd.")
            return
        self.result = picked
        self.destroy()

    def _shift_month(self, delta):
        year, month = self._date.year, self._date.month + delta
        if month == 0:
            year, month = year - 1, 12
        elif month == 13:
            year, month = year + 1, 1
        self._date = self._date.replace(year=year, month=month, day=1)
        self._render_calendar()

    def _prev_month(self):
        self._shift_month(-1)

    def _next_month(self):
        self._shift_month(1)

    def _render_calendar(self):
        for widget in self.cal_frame.winfo_children():
            widget.destroy()
        self.month_label.config(text=self._date.strftime("%B %Y"))
        for col, name in enumerate(["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]):
            tk.Label(self.cal_frame, text=name, width=3).grid(row=0, column=col)
        cal = calendar.Calendar(firstweekday=0)
        for row, week in enumerate(cal.monthdayscalendar(self._date.year, self._date.month), start=1):
            for col, day in enumerate(week):
                if day == 0:
                    tk.Label(self.cal_frame, text="", width=3).grid(row=row, column=col)
                else:
                    current = self._date.replace(day=day) == self._initial
                    tk.Button(self.cal_frame, text=str(day), width=3,
                              relief="sunken" if current else "raised",
                              bg="#cde" if current else None,
                              command=lambda d=day: self._pick_day(d)).grid(row=row, column=col)

    def _pick_day(self, day):
        self.result = self._date.replace(day=day)
        self.destroy()


class ImageViewer(tk.Toplevel):
    def __init__(self, master, image_path):
        super().__init__(master)
        self.title(os.path.basename(image_path))
        self.geometry("800x600")
        self._image = ImageOps.exif_transpose(Image.open(image_path))
        self._scale = 1.0
        self._offset = [0.0, 0.0]
        self._drag_start = None
        self._photo = None
        self._zoom_var = tk.StringVar(value="100%")

        toolbar = tk.Frame(self)
        tk.Button(toolbar, text="−", width=2, command=self.zoom_out).pack(side="left", padx=2)
        tk.Button(toolbar, text="+", width=2, command=self.zoom_in).pack(side="left", padx=2)
        tk.Button(toolbar, text="Fit", command=self.reset_zoom).pack(side="left", padx=2)
        tk.Label(toolbar, textvariable=self._zoom_var, width=6, anchor="w").pack(side="left", padx=5)
        toolbar.pack(side="top", fill="x", pady=2)

        self._canvas = tk.Canvas(self, bg="black", cursor="fleur")
        self._canvas.pack(fill="both", expand=True)

        self._canvas.bind("<MouseWheel>", self._on_mousewheel)
        self._canvas.bind("<Button-4>", self._on_scroll_linux)
        self._canvas.bind("<Button-5>", self._on_scroll_linux)
        self._canvas.bind("<ButtonPress-1>", self._on_pan_start)
        self._canvas.bind("<B1-Motion>", self._on_pan_move)
        # Redraw on resize so newly exposed areas get filled; skipped until the first fit.
        self._canvas.bind("<Configure>", lambda e: self._photo and self._redraw())

        self.after(100, self._fit_to_window)

    def _fit_to_window(self):
        cw = self._canvas.winfo_width()
        ch = self._canvas.winfo_height()
        if cw <= 1 or ch <= 1:
            self.after(50, self._fit_to_window)
            return
        iw, ih = self._image.size
        self._scale = min(cw / iw, ch / ih, 1.0)
        self._offset = [cw / 2.0, ch / 2.0]
        self._redraw()

    def _redraw(self):
        # Only crop and scale the part of the image that's visible on the canvas.
        # Scaling the whole image at high zoom produced bitmaps far beyond X11's
        # 32767px limit (and many GB of RAM), freezing or crashing the app.
        self._canvas.delete("all")
        self._zoom_var.set(f"{int(self._scale * 100)}%")
        iw, ih = self._image.size
        s = self._scale
        cw, ch = self._canvas.winfo_width(), self._canvas.winfo_height()
        left = self._offset[0] - iw * s / 2
        top = self._offset[1] - ih * s / 2
        # Visible region in source-image pixels, padded out to whole pixels.
        sx0 = max(0, math.floor(-left / s))
        sy0 = max(0, math.floor(-top / s))
        sx1 = min(iw, math.ceil((cw - left) / s))
        sy1 = min(ih, math.ceil((ch - top) / s))
        if sx1 <= sx0 or sy1 <= sy0:
            return
        out_w = max(1, round((sx1 - sx0) * s))
        out_h = max(1, round((sy1 - sy0) * s))
        resized = self._image.resize((out_w, out_h), Image.Resampling.LANCZOS,
                                     box=(sx0, sy0, sx1, sy1))
        self._photo = ImageTk.PhotoImage(resized)
        self._canvas.create_image(round(left + sx0 * s), round(top + sy0 * s),
                                  image=self._photo, anchor="nw")

    def _zoom_at(self, factor, cx, cy):
        old = self._scale
        self._scale = max(0.05, min(self._scale * factor, 20.0))
        r = self._scale / old
        self._offset[0] = cx + (self._offset[0] - cx) * r
        self._offset[1] = cy + (self._offset[1] - cy) * r
        self._redraw()

    def _on_mousewheel(self, event):
        self._zoom_at(1.1 if event.delta > 0 else 1 / 1.1, event.x, event.y)

    def _on_scroll_linux(self, event):
        self._zoom_at(1.1 if event.num == 4 else 1 / 1.1, event.x, event.y)

    def zoom_in(self):
        self._zoom_at(1.25, self._canvas.winfo_width() / 2, self._canvas.winfo_height() / 2)

    def zoom_out(self):
        self._zoom_at(1 / 1.25, self._canvas.winfo_width() / 2, self._canvas.winfo_height() / 2)

    def reset_zoom(self):
        self._fit_to_window()

    def _on_pan_start(self, event):
        self._drag_start = (event.x, event.y)

    def _on_pan_move(self, event):
        if self._drag_start:
            self._offset[0] += event.x - self._drag_start[0]
            self._offset[1] += event.y - self._drag_start[1]
            self._drag_start = (event.x, event.y)
            self._redraw()


class KeywordsWindow(tk.Toplevel):
    """Modeless window for adding or removing EXIF subject keywords on the selected
    photos, from a keyword list kept in config.json."""

    def __init__(self, sorter):
        super().__init__(sorter)
        self.sorter = sorter
        self.title("EXIF Subjects")
        self.minsize(260, 300)
        self.transient(sorter)  # stay above the main window while working in the grid

        list_frame = tk.Frame(self)
        self.listbox = tk.Listbox(list_frame, selectmode="multiple", exportselection=False, height=12)
        scrollbar = tk.Scrollbar(list_frame, command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scrollbar.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        list_frame.pack(fill="both", expand=True, padx=5, pady=(5, 0))

        edit_frame = tk.Frame(self)
        self.new_entry = tk.Entry(edit_frame)
        self.new_entry.pack(side="left", fill="x", expand=True)
        self.new_entry.bind("<Return>", lambda e: self.add_to_list())
        tk.Button(edit_frame, text="+", width=2, command=self.add_to_list).pack(side="left", padx=(5, 0))
        tk.Button(edit_frame, text="−", width=2, command=self.delete_from_list).pack(side="left", padx=(2, 0))
        edit_frame.pack(fill="x", padx=5, pady=5)

        path_frame = tk.Frame(self)
        self.from_path = tk.BooleanVar(value=sorter.keywords_from_path)
        self.from_path.trace_add("write", lambda *a: setattr(sorter, "keywords_from_path", self.from_path.get()))
        tk.Checkbutton(path_frame, text="From Path", variable=self.from_path).pack(side="left")
        self.root_label = tk.Label(path_frame, anchor="w", fg="#555")
        self.root_label.pack(side="left", fill="x", expand=True)
        tk.Button(path_frame, text="Root...", command=self.choose_photo_root).pack(side="left")
        path_frame.pack(fill="x", padx=5)
        self.update_root_label()

        self.target_label = tk.Label(self, anchor="w")
        self.target_label.pack(fill="x", padx=5)
        apply_frame = tk.Frame(self)
        tk.Button(apply_frame, text="Add to Photos", command=lambda: self.apply(add=True)).pack(side="left", padx=2)
        tk.Button(apply_frame, text="Remove from Photos", command=lambda: self.apply(add=False)).pack(side="left", padx=2)
        apply_frame.pack(pady=5)
        self.status_label = tk.Label(self, anchor="w", wraplength=320, justify="left")
        self.status_label.pack(fill="x", padx=5, pady=(0, 5))

        self.listbox.bind("<<ListboxSelect>>", lambda e: self.remember_highlights())
        self.listbox.bind("<Button-1>", self.clear_on_empty_click)
        self.refresh_list(select=set(sorter.keyword_highlights))
        self.update_target_label()

    def clear_on_empty_click(self, event):
        # Clicks below the last keyword clear the highlights instead of toggling
        # the nearest one, as a Listbox otherwise would.
        bbox = self.listbox.bbox(self.listbox.nearest(event.y))
        if bbox and event.y <= bbox[1] + bbox[3]:
            return
        self.listbox.selection_clear(0, "end")
        self.remember_highlights()
        return "break"

    def refresh_list(self, select=()):
        self.listbox.delete(0, "end")
        for kw in self.sorter.subject_keywords:
            self.listbox.insert("end", kw)
            if kw in select:
                self.listbox.selection_set("end")
        self.remember_highlights()

    def remember_highlights(self):
        # Saved to config.json with everything else, so they come back next time.
        self.sorter.keyword_highlights = self.checked()

    def checked(self):
        return [self.listbox.get(i) for i in self.listbox.curselection()]

    def add_to_list(self):
        kw = self.new_entry.get().strip()
        if not kw:
            return
        keywords = self.sorter.subject_keywords
        if kw.lower() not in (k.lower() for k in keywords):
            keywords.append(kw)
            keywords.sort(key=str.lower)
            self.sorter.save_config()
        self.new_entry.delete(0, "end")
        self.refresh_list(select=set(self.checked()) | {kw})

    def delete_from_list(self):
        checked = self.checked()
        if not checked:
            self.status_label.config(text="Highlight keywords in the list to delete them.")
            return
        # Only the list changes; photos keep any keywords already written to them.
        self.sorter.subject_keywords[:] = [k for k in self.sorter.subject_keywords if k not in checked]
        self.sorter.save_config()
        self.refresh_list()

    def update_root_label(self):
        root = self.sorter.photo_root
        self.root_label.config(text=f"below {root.replace(os.path.expanduser('~'), '~', 1)}" if root else "(no root set)")

    def choose_photo_root(self):
        folder = filedialog.askdirectory(parent=self, title="Root of the photo tree",
                                         initialdir=self.sorter.photo_root or self.sorter.image_directory)
        if folder:
            self.sorter.photo_root = os.path.normpath(folder)
            self.sorter.save_config()
            self.update_root_label()
            self.sorter.refresh_tree()

    def path_keywords(self):
        """Folder names from just below the photo root down to the current folder,
        or None if the current folder isn't inside the root."""
        root = self.sorter.photo_root
        rel = os.path.relpath(os.path.normpath(self.sorter.image_directory), root) if root else os.pardir
        if rel == os.pardir or rel.startswith(os.pardir + os.sep):
            return None
        return [] if rel == os.curdir else rel.split(os.sep)

    def update_target_label(self):
        # Polled so the label follows selection changes in the main window.
        targets, selected = self.sorter.keyword_targets()
        what = "selected photo(s)" if selected else "photo(s) — all of them, nothing is selected"
        self.target_label.config(text=f"Applies to {len(targets)} {what}")
        self.after(300, self.update_target_label)

    def apply(self, add):
        keywords = self.checked()
        if self.from_path.get():
            path_kws = self.path_keywords()
            if path_kws is None:
                self.status_label.config(text="This folder isn't inside the photo root. Use Root... to set it.")
                return
            keywords += [k for k in path_kws if k.lower() not in (c.lower() for c in keywords)]
        if not keywords:
            self.status_label.config(text="Highlight one or more keywords in the list, or tick From Path, first.")
            return
        targets, selected = self.sorter.keyword_targets()
        if not targets:
            self.status_label.config(text="No photos loaded.")
            return
        names = [d["filename"] for d in targets]
        jpegs = [os.path.join(self.sorter.image_directory, n) for n in names if n.lower().endswith(JPEG_EXTENSIONS)]
        verb = "Add" if add else "Remove"
        if not selected and not messagebox.askyesno(
                f"{verb} Keywords", f"Nothing is selected.\n{verb} {', '.join(keywords)} "
                f"{'to' if add else 'from'} all {len(names)} photos?", parent=self):
            return
        if not jpegs:
            self.status_label.config(text="Keywords can only be written to JPEGs.")
            return
        self.config(cursor="watch"); self.update_idletasks()
        try:
            summary = edit_keywords(jpegs, add=keywords if add else (), remove=() if add else keywords)
        except FileNotFoundError:
            messagebox.showerror("EXIF", "exiftool isn't installed.", parent=self)
            return
        except Exception as e:
            messagebox.showerror("EXIF", f"Could not write keywords:\n{e}", parent=self)
            return
        finally:
            self.config(cursor="")
        skipped = len(names) - len(jpegs)
        self.status_label.config(
            text=f"{'Added' if add else 'Removed'} {', '.join(keywords)} {'to' if add else 'from'} {len(jpegs)} photo(s)."
                 + (f" Skipped {skipped} non-JPEG." if skipped else ""))
        if summary:
            print(summary)


class DragDropSorter(tk.Tk):

    def load_config(self):
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
                self.image_directory = config.get("image_directory", "")
                self.move_destination = config.get("move_destination", "")
                self.subject_keywords = config.get("subject_keywords", [])
                self.saved_prefix = config.get("prefix", "")
                self.saved_selections = config.get("selections", {})
                self.keyword_highlights = config.get("keyword_highlights", [])
                self.photo_root = config.get("photo_root", "")
                self.keywords_from_path = config.get("keywords_from_path", False)
        else:
            self.image_directory = ""
            self.move_destination = ""
            self.subject_keywords = []
            self.saved_prefix = ""
            self.saved_selections = {}
            self.keyword_highlights = []
            self.photo_root = ""
            self.keywords_from_path = False
        if not self.photo_root and os.path.isdir(os.path.expanduser("~/Photos")):
            self.photo_root = os.path.expanduser("~/Photos")

    def save_config(self):
        if hasattr(self, "prefix_entry"):
            self.saved_prefix = self.prefix_entry.get()
        # Selections are remembered per folder. Record this folder's only once it has
        # fully loaded, so a save made while switching folders or mid-load doesn't
        # wipe the remembered one.
        if self.grid_directory and hasattr(self, "on_disk_order"):
            folder = os.path.normpath(self.grid_directory)
            files = [d["filename"] for d in self.image_data if d["label"] in self.selected_widgets]
            if files:
                self.saved_selections[folder] = files
            else:
                self.saved_selections.pop(folder, None)
        self.saved_selections = {f: names for f, names in self.saved_selections.items() if os.path.isdir(f)}
        with open(CONFIG_PATH, "w") as f:
            json.dump({
                "image_directory": self.image_directory,
                "move_destination": self.move_destination,
                "subject_keywords": self.subject_keywords,
                "prefix": self.saved_prefix,
                "selections": self.saved_selections,
                "keyword_highlights": self.keyword_highlights,
                "photo_root": self.photo_root,
                "keywords_from_path": self.keywords_from_path,
            }, f)

    def __init__(self):
        super().__init__()
        self.title("Photo DnD Grid Sorter")
        self.geometry("960x720")
        self.protocol("WM_DELETE_WINDOW", self.on_app_close)
        self.thumb_size = (120, 120)
        self.cell_size = 140  # thumb + selection border + padding
        self.image_directory = ""
        self.max_columns = 6
        self.image_data = []
        self.image_files = []
        self.folder_data = []
        self.folder_names = []
        self.grid_row_offset = 0
        self.load_generation = 0
        self.metadata_cache = {}  # path -> (mtime, (date_taken, keywords))
        self.tooltip = None
        self.tooltip_after = None
        self.keywords_window = None
        self.grid_directory = None  # folder whose thumbnails are in the grid
        self.selected_widgets = []
        self.last_clicked_label = None
        self.dragged_widget = None
        self._is_dragging = False
        self._folder_drag = None
        self.cut_files = None  # (folder, filenames) waiting for Ctrl-V

        self.load_config()
        self.hide_hidden_files_in_dialogs()

        self.create_ui()
        self.folder_entry.insert(0, self.image_directory)
        self.prefix_entry.insert(0, self.saved_prefix)
        #self.start_thumbnail_loading()

    def create_ui(self):
        menubar = tk.Menu(self)
        menubar.add_command(label="EXIF", command=self.open_keywords_window)
        self.config(menu=menubar)

        # Folder tree on the left, everything else on the right; the sash can be dragged.
        panes = tk.PanedWindow(self, orient="horizontal", sashwidth=5)
        panes.pack(fill="both", expand=True)
        panes.add(self.build_folder_tree(panes), width=220, minsize=120)
        main = tk.Frame(panes)
        panes.add(main, minsize=300)

        source_frame = tk.Frame(main)
        
        self.folder_label = tk.Label(source_frame, text="Image Folder:")
        self.folder_label.pack(side="left")

        self.folder_entry = tk.Entry(source_frame, width=50)
        self.folder_entry.pack(side="left", expand=True, fill="x")

        self.load_button = tk.Button(source_frame, text="Load", command=self.load_from_entry)
        self.load_button.pack(side="left")

        self.browse_button = tk.Button(source_frame, text="Browse...", command=self.choose_directory)
        self.browse_button.pack(side="left")

        source_frame.pack(pady=5)

        prefix_frame = tk.Frame(main)
        tk.Label(prefix_frame, text="Filename Prefix:").pack(side="left")
        self.prefix_entry = tk.Entry(prefix_frame)
        self.prefix_entry.pack(side="left", padx=5)
        tk.Button(prefix_frame, text="📅", command=self.choose_date_prefix).pack(side="left")
        tk.Button(prefix_frame, text="Auto", command=self.auto_set_prefix).pack(side="left", padx=(5, 0))
        prefix_frame.pack(pady=5)

        tk.Button(main, text="Preview Rename", command=self.preview_order).pack(pady=2)
        tk.Button(main, text="Rename & Save Order", command=self.save_order).pack(pady=2)
        tk.Button(main, text="Move & Rename To...", command=self.move_and_rename_to_folder).pack(pady=2)
        tk.Button(main, text="Restore From Log", command=self.restore_from_log).pack(pady=2)

        self.loading_label = tk.Label(main, text="Click 'Load' to load thumbnails")
        self.loading_label.pack(pady=5)

        # Thumbnails are placed directly on the canvas as window items rather than
        # gridded into one inner Frame: X11 caps window size at 32767px, and a Frame
        # tall enough for ~1500 thumbnails made Tk abort with BadAlloc.
        self.canvas = tk.Canvas(main)
        self.scrollbar = tk.Scrollbar(main, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.configure(yscrollincrement=10)
        self.grid_menu = tk.Menu(self, tearoff=0)
        self.canvas.bind("<Button-3>", self.show_grid_menu)

        self.canvas.bind_all("<MouseWheel>", self.on_mousewheel)       # Windows & macOS
        self.canvas.bind_all("<Button-4>", self.on_mousewheel_linux)   # Linux scroll up
        self.canvas.bind_all("<Button-5>", self.on_mousewheel_linux)   # Linux scroll down
        self.bind_all("<Button-1>", self.clear_selection_on_background, add="+")
        # Drags are tracked here rather than on the pressed thumbnail: auto-scroll can
        # carry that thumbnail out of view, and Tk unmaps it, which ends its pointer
        # grab and would cut off its motion and release events mid-drag.
        self.bind_all("<B1-Motion>", self.on_drag_motion)
        self.bind_all("<ButtonRelease-1>", self.on_button_release)
        self.bind_all("<Delete>", self.delete_selected_thumbnails)        
        self.bind_all("<KeyPress-r>", self.rotate_thumbnail_under_cursor)
        self.bind_all("<KeyPress-R>", lambda e: self.rotate_thumbnail_under_cursor(e, clockwise=False))
        self.bind_all("<Control-a>", self.select_all)
        self.bind_all("<Control-A>", self.select_all)
        self.bind_all("<Control-x>", self.cut_selection)
        self.bind_all("<Control-X>", self.cut_selection)
        self.bind_all("<Control-v>", self.paste_cut)
        self.bind_all("<Control-V>", self.paste_cut)
        self.bind_all("<Home>", lambda e: self.scroll_grid_to(e, 0.0))
        self.bind_all("<End>", lambda e: self.scroll_grid_to(e, 1.0))
        self.bind_all("<Prior>", lambda e: self.scroll_grid_page(-1))
        self.bind_all("<Next>", lambda e: self.scroll_grid_page(1))

    def choose_date_prefix(self):
        # Open on the date already in the prefix field, if there is one.
        prefix = self.prefix_entry.get().strip()
        if prefix and not prefix.endswith("_"):
            prefix += "_"
        dialog = DatePickerDialog(self, initial=parse_date_from_prefix(prefix))
        self.wait_window(dialog)
        if dialog.result:
            new_prefix = replace_prefix_date(self.prefix_entry.get(), dialog.result)
            self.prefix_entry.delete(0, tk.END)
            self.prefix_entry.insert(0, new_prefix)

    def auto_set_prefix(self):
        if not self.image_directory:
            messagebox.showinfo("Auto Prefix", "Load an image folder first.")
            return
        date = infer_date_from_path(self.image_directory)
        if date is None:
            messagebox.showinfo("Auto Prefix", "Couldn't determine a date from the folder path.")
            return
        self.prefix_entry.delete(0, tk.END)
        self.prefix_entry.insert(0, date.strftime("%Y-%m-%d") + "_")

    def choose_directory(self):
        initial = self.folder_entry.get().strip() or self.image_directory
        kwargs = {"initialdir": initial} if initial and os.path.isdir(initial) else {}
        selected_dir = filedialog.askdirectory(**kwargs)
        if selected_dir:
            # Only fill in the entry; Load switches image_directory. Changing it here
            # would leave the grid showing the old folder's files under the new path.
            self.folder_entry.delete(0, tk.END)
            self.folder_entry.insert(0, selected_dir)

    def load_from_entry(self):
        path = self.folder_entry.get()
        if os.path.exists(path):
            if not self.check_unsaved_changes():
                return
            self.image_directory = path
            self.save_config()
            self.clear_grid()
            self.start_thumbnail_loading()

    def navigate_to_folder(self, folder_name):
        if not self.check_unsaved_changes():
            return
        current = self.image_directory.rstrip(os.sep)
        if folder_name == "..":
            new_dir = os.path.dirname(current)
        else:
            new_dir = os.path.join(current, folder_name)
        if not os.path.isdir(new_dir):
            return
        self.image_directory = new_dir
        self.folder_entry.delete(0, tk.END)
        self.folder_entry.insert(0, new_dir)
        self.save_config()
        self.clear_grid()
        self.start_thumbnail_loading()

    def clear_grid(self):
        self.hide_tooltip()
        self.canvas.delete("all")
        for widget in self.canvas.winfo_children():
            widget.destroy()
        self.image_data.clear()
        self.folder_data = []
        self.selected_widgets.clear()
        self.last_clicked_label = None
        self.grid_directory = None
        if hasattr(self, 'on_disk_order'):
            del self.on_disk_order

    def set_on_disk_order(self):
        self.on_disk_order = [d["filename"] for d in self.image_data]
        #print(f"Set ODO to: {self.on_disk_order}")

    def check_unsaved_changes(self):
        # check for unsaved reordering, if loading is complete
        if hasattr(self, 'on_disk_order'):
            current_order = [f["filename"] for f in self.image_data]
            if self.on_disk_order != current_order:
                return messagebox.askyesno(
                    "Pending Changes",
                    "You’ve reordered files but haven’t renamed them.\nLeave without saving?"
                )
        return True

    def on_app_close(self):
        if not self.check_unsaved_changes():
            return
        try:
            self.save_config()
        except Exception as e:
            print(f"⚠️ Could not save settings: {e}")
        self.destroy()

    WHEEL_STEP = 3        # canvas units (yscrollincrement=10px) per wheel notch
    FAST_WHEEL_STEP = 30  # with Ctrl held: about two rows per notch

    def wheel_step(self, event):
        return self.FAST_WHEEL_STEP if event.state & 0x0004 else self.WHEEL_STEP

    def on_mousewheel(self, event):
        self.hide_tooltip()  # the grid moves under the pointer without a <Leave>
        self.canvas.yview_scroll(int(-1 * (event.delta / 120) * self.wheel_step(event)), "units")

    def on_mousewheel_linux(self, event):
        self.hide_tooltip()
        if event.num == 4:
            self.canvas.yview_scroll(-self.wheel_step(event), "units")
        elif event.num == 5:
            self.canvas.yview_scroll(self.wheel_step(event), "units")

    def scroll_grid_to(self, event, fraction):
        if isinstance(event.widget, tk.Entry):
            return  # Home/End move the cursor in the prefix or folder field
        self.hide_tooltip()
        self.canvas.yview_moveto(fraction)

    def scroll_grid_page(self, direction):
        self.hide_tooltip()
        self.canvas.yview_scroll(direction, "pages")

    def clear_selection_on_background(self, event):
        # Clear on clicks in empty space or on plain labels in the main window, but not
        # on buttons, text fields, the scrollbar or other windows, which act on the
        # selection or are used alongside it.
        widget = event.widget
        if not isinstance(widget, tk.Misc) or widget.winfo_toplevel() is not self:
            return
        if widget.winfo_class() not in ("Canvas", "Frame", "Label", "Tk"):
            return
        if any(widget is d["label"] for d in self.image_data):
            return
        self.canvas.focus_set()  # take focus from the entries so keys reach the grid
        for lbl in self.selected_widgets:
            lbl.config(relief="raised", bd=2)
        self.selected_widgets.clear()
        self.last_clicked_label = None

    def select_all(self, event):
        if isinstance(event.widget, tk.Entry) or event.widget.winfo_toplevel() is not self:
            return  # Ctrl-A in a text field or another window keeps its usual meaning
        self.selected_widgets = [d["label"] for d in self.image_data]
        for lbl in self.selected_widgets:
            lbl.config(relief="solid", bd=4, highlightbackground="blue")
        self.last_clicked_label = None

    def hide_hidden_files_in_dialogs(self):
        """Make Tk's own file/folder dialogs (used on X11) hide dotfiles, with a
        "Show Hidden" checkbox to reveal them. The settings live in tkfbox.tcl,
        which is only sourced when a dialog first opens -- and sourcing it resets
        them -- so force it to load with a deliberately invalid call first."""
        if self.tk.call("tk", "windowingsystem") != "x11":
            return
        try:
            self.tk.call("tk_getOpenFile", "-badoption")
        except tk.TclError:
            pass
        self.tk.call("set", "::tk::dialog::file::showHiddenBtn", 1)
        self.tk.call("set", "::tk::dialog::file::showHiddenVar", 0)

    def build_folder_tree(self, parent):
        """A folders-only tree of the photo root. Rows are keyed by their full path;
        clicking one opens it, and photos or folders can be dropped onto it."""
        frame = tk.Frame(parent)
        self.tree = ttk.Treeview(frame, show="tree", selectmode="browse")
        scrollbar = tk.Scrollbar(frame, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.tag_configure("drop", background="#9cc3ee")
        self.tree.bind("<<TreeviewOpen>>", lambda e: self.fill_tree_node(self.tree.focus()))
        self.tree.bind("<Button-1>", self.press_tree)
        self._tree_drop_item = None
        self.after_idle(self.refresh_tree)
        return frame

    def tree_root(self):
        return os.path.normpath(self.photo_root or os.path.expanduser("~"))

    @staticmethod
    def subfolders(path):
        try:
            return sorted(e.name for e in os.scandir(path) if e.is_dir() and not e.name.startswith("."))
        except OSError:
            return []

    def fill_tree_node(self, path):
        """List a row's subfolders. Rows that have any get a placeholder child so
        they show an expand arrow; their own children are listed when opened."""
        if not path:
            return
        self.tree.delete(*self.tree.get_children(path))
        for name in self.subfolders(path):
            child = os.path.join(path, name)
            self.tree.insert(path, "end", iid=child, text=f"📁 {name}")
            if self.subfolders(child):
                self.tree.insert(child, "end", iid=os.path.join(child, TREE_PLACEHOLDER))

    def tree_items(self, parent=""):
        for item in self.tree.get_children(parent):
            yield item
            yield from self.tree_items(item)

    def refresh_tree(self):
        """Rebuild the tree from disk, keeping open the rows that were open."""
        opened = {i for i in self.tree_items() if self.tree.item(i, "open")}
        self.tree.delete(*self.tree.get_children(""))
        self._tree_drop_item = None
        root = self.tree_root()
        if not os.path.isdir(root):
            return
        self.tree.insert("", "end", iid=root, text=f"📁 {os.path.basename(root) or root}", open=True)
        self.fill_tree_node(root)
        for path in sorted(opened, key=len):  # parents before their children
            if path != root and self.tree.exists(path):
                self.tree.item(path, open=True)
                self.fill_tree_node(path)
        self.reveal_in_tree()

    def reveal_in_tree(self):
        """Open the rows down to the folder on screen and select it."""
        root, current = self.tree_root(), os.path.normpath(self.image_directory or "")
        if not self.tree.exists(root) or not (current == root or current.startswith(root + os.sep)):
            self.tree.selection_set(())
            return
        path = root
        for part in os.path.relpath(current, root).split(os.sep):
            if part == ".":
                break
            if not self.tree.item(path, "open"):
                self.tree.item(path, open=True)
                self.fill_tree_node(path)
            path = os.path.join(path, part)
            if not self.tree.exists(path):
                return
        self.tree.selection_set(path)
        self.tree.see(path)

    def tree_folder_at(self, x_root, y_root):
        """The tree row under a screen point, if any, as a folder path."""
        x, y = x_root - self.tree.winfo_rootx(), y_root - self.tree.winfo_rooty()
        if not (0 <= x < self.tree.winfo_width() and 0 <= y < self.tree.winfo_height()):
            return None
        item = self.tree.identify_row(y)
        return item if item and os.path.basename(item) != TREE_PLACEHOLDER else None

    def press_tree(self, event):
        item = self.tree.identify_row(event.y)
        if not item or "indicator" in self.tree.identify_element(event.x, event.y):
            return  # the expand arrow just opens or closes the row
        self.hide_tooltip()
        self._folder_drag = {"path": item, "label": None, "movable": item != self.tree_root(),
                             "start": (event.x_root, event.y_root), "moved": False}

    def drop_target_at(self, x_root, y_root):
        """The folder under a screen point, as a full path: a tree row or a folder tile."""
        item = self.tree_folder_at(x_root, y_root)
        if item:
            return item
        tile = self.folder_at(x_root, y_root)
        return os.path.normpath(os.path.join(self.image_directory, tile["name"])) if tile else None

    def highlight_drop(self, path):
        """Light up the folder tile and tree row for path (None clears them)."""
        for data in self.folder_data:
            here = os.path.normpath(os.path.join(self.image_directory, data["name"]))
            data["label"].config(bg="#9cc3ee" if here == path else "#dde8f5")
        if self._tree_drop_item != path:
            if self._tree_drop_item and self.tree.exists(self._tree_drop_item):
                self.tree.item(self._tree_drop_item, tags=())
            if path and self.tree.exists(path):
                self.tree.item(path, tags=("drop",))
            self._tree_drop_item = path

    def update_drop_highlight(self):
        if self._folder_drag:
            target = self.folder_drop_target()
        else:
            target = self.drop_target_at(*self._drag_pointer)
            if target == os.path.normpath(self.image_directory):
                target = None  # the photos are already there
        self.highlight_drop(target)

    def folder_path_changed(self, old_path, new_path):
        """Follow a folder that was renamed or moved: remembered selections, the cut
        list, and the folder on screen if it was that folder or inside it."""
        old_path, new_path = os.path.normpath(old_path), os.path.normpath(new_path)
        def follow(f):
            f = os.path.normpath(f)
            if f == old_path or f.startswith(old_path + os.sep):
                return os.path.normpath(os.path.join(new_path, os.path.relpath(f, old_path)))
            return f
        self.saved_selections = {follow(f): names for f, names in self.saved_selections.items()}
        self.cut_folder_moved(old_path, new_path)
        if self.image_directory and follow(self.image_directory) != os.path.normpath(self.image_directory):
            self.image_directory = follow(self.image_directory)
            if self.grid_directory:
                self.grid_directory = self.image_directory
            self.folder_entry.delete(0, tk.END)
            self.folder_entry.insert(0, self.image_directory)
            self.save_config()

    def list_folders(self, entries):
        folders = [d for d in entries if os.path.isdir(os.path.join(self.image_directory, d))]
        parent = os.path.dirname(self.image_directory.rstrip(os.sep))
        if parent and parent != self.image_directory.rstrip(os.sep):
            folders = [".."] + folders
        return folders

    def start_thumbnail_loading(self):
        entries = sorted(e for e in os.listdir(self.image_directory) if not e.startswith("."))
        self.folder_names = self.list_folders(entries)
        self.image_files = [f for f in entries
                            if f.lower().endswith((".jpg", ".jpeg", ".png"))]
        self.total_files = len(self.image_files)
        self.grid_row_offset = 0
        if self.folder_names:
            self.grid_row_offset = (len(self.folder_names) + self.max_columns - 1) // self.max_columns
            self.add_folder_widgets()
        # Bump the generation so a loader still running for a previous folder stops,
        # and any thumbnails it already queued via after() are discarded.
        self.load_generation += 1
        self.grid_directory = self.image_directory
        self.refresh_tree()
        threading.Thread(target=self.load_thumbnails_thread, daemon=True,
                         args=(self.load_generation, self.image_directory, self.image_files)).start()

    def add_folder_widgets(self):
        self.folder_data = []
        for idx, name in enumerate(self.folder_names):
            row = idx // self.max_columns
            column = idx % self.max_columns
            display = ".. (parent)" if name == ".." else name
            lbl = tk.Label(self.canvas, text=f"📁 {display}", width=14, height=6,
                           wraplength=100, justify="center",
                           bg="#dde8f5", bd=2, relief="raised")
            item = self.canvas.create_window(*self.cell_center(row, column), window=lbl)
            # Opened on release rather than press, so a folder can be dragged instead.
            lbl.bind("<Button-1>", lambda e, n=name, l=lbl: self.press_folder(e, n, l))
            lbl.bind("<Button-3>", self.show_grid_menu)
            self.folder_data.append({"name": name, "label": lbl, "item": item})
        self.update_scrollregion()

    def refresh_folders(self):
        """Rebuild the folder tiles after one is added, shifting the photos to fit."""
        for data in self.folder_data:
            self.canvas.delete(data["item"])
            data["label"].destroy()
        entries = sorted(e for e in os.listdir(self.image_directory) if not e.startswith("."))
        self.folder_names = self.list_folders(entries)
        self.grid_row_offset = (len(self.folder_names) + self.max_columns - 1) // self.max_columns
        self.add_folder_widgets()
        self.redraw_grid()
        self.refresh_tree()

    def show_grid_menu(self, event):
        if not self.grid_directory:
            return
        self.grid_menu.delete(0, "end")
        self.grid_menu.add_command(label="New Folder...", command=self.create_folder)
        folder = self.folder_at(event.x_root, event.y_root)
        if self.selected_widgets:
            self.grid_menu.add_command(label=f"Cut {len(self.selected_widgets)} Photo(s)", accelerator="Ctrl+X",
                                       command=self.cut_selection)
        if self.cut_files:
            count = len(self.cut_files[1])
            if folder:
                shown = "Parent Folder" if folder["name"] == ".." else f"'{folder['name']}'"
                self.grid_menu.add_command(label=f"Paste {count} Photo(s) into {shown}",
                                           command=lambda n=folder["name"]: self.paste_cut(folder_name=n))
            else:
                self.grid_menu.add_command(label=f"Paste {count} Photo(s) Here", accelerator="Ctrl+V",
                                           command=self.paste_cut)
        if folder and folder["name"] != "..":
            self.grid_menu.add_command(label=f"Rename Folder '{folder['name']}'...",
                                       command=lambda n=folder["name"]: self.rename_folder(n))
            self.grid_menu.add_command(label=f"Delete Folder '{folder['name']}'...",
                                       command=lambda n=folder["name"]: self.delete_folder(n))
        self.grid_menu.tk_popup(event.x_root, event.y_root)

    def delete_folder(self, name):
        """Move a folder into .trash, asking first if it or any subfolder holds files
        other than thumbnail caches and rename/move logs."""
        path = os.path.join(self.image_directory, name)
        photos = folders = others = 0
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames[:] = [d for d in dirnames if d != THUMB_DIR]
            folders += len(dirnames)
            for f in filenames:
                if LOG_FILE_RE.match(f):
                    continue  # this app's own logs, including retired restored_* ones
                if f.lower().endswith((".jpg", ".jpeg", ".png")):
                    photos += 1
                else:
                    others += 1
        # Subfolders alone (empty, or holding only logs) aren't worth a warning.
        if photos or others:
            contents = ", ".join(f"{n} {what}" for n, what in (
                (photos, "photo(s)"), (folders, "subfolder(s)"), (others, "other file(s)")) if n)
            if not messagebox.askyesno(
                    "Delete Folder",
                    f"'{name}' is not empty. It contains {contents}, counting everything inside its subfolders.\n\n"
                    f"Move the folder and everything in it to {TRASH_DIR}?",
                    icon="warning", default="no"):
                return
        try:
            final = move_to_trash(self.image_directory, name)
        except Exception as e:
            messagebox.showerror("Delete Folder", f"Could not move '{name}' to {TRASH_DIR}:\n{e}")
            return
        trash = os.path.join(self.image_directory, TRASH_DIR)
        self.write_move_log("", trash, [{"original": name, "final": final, "destination": trash}])
        self.loading_label.config(text=f"Moved folder '{name}' to {TRASH_DIR}.")
        self.refresh_folders()

    def ask_folder_name(self, title, initial=""):
        """Ask for a folder name; None if cancelled or not a usable name."""
        name = simpledialog.askstring(title, "Folder name:", initialvalue=initial, parent=self)
        name = (name or "").strip()
        if not name:
            return None
        if os.sep in name or name.startswith("."):
            messagebox.showerror(title, "Folder names can't contain '/' or start with '.'.")
            return None
        return name

    def rename_folder(self, name):
        new_name = self.ask_folder_name("Rename Folder", name)
        if not new_name or new_name == name:
            return
        old_path = os.path.join(self.image_directory, name)
        new_path = os.path.join(self.image_directory, new_name)
        # Checked by hand: os.rename would silently replace an empty folder of that name.
        if os.path.exists(new_path) and not os.path.samefile(old_path, new_path):
            messagebox.showerror("Rename Folder", f"'{new_name}' already exists.")
            return
        try:
            os.rename(old_path, new_path)
        except OSError as e:
            messagebox.showerror("Rename Folder", f"Could not rename '{name}':\n{e}")
            return
        self.folder_path_changed(old_path, new_path)
        # Logged as a move within this folder, so Restore From Log renames it back.
        self.write_move_log("", self.image_directory, [
            {"original": name, "final": new_name, "destination": self.image_directory}])
        self.loading_label.config(text=f"Renamed folder '{name}' to '{new_name}'.")
        self.refresh_folders()

    def create_folder(self):
        name = self.ask_folder_name("New Folder")
        if not name:
            return
        try:
            os.mkdir(os.path.join(self.image_directory, name))
        except FileExistsError:
            messagebox.showerror("New Folder", f"'{name}' already exists.")
            return
        except OSError as e:
            messagebox.showerror("New Folder", f"Could not create '{name}':\n{e}")
            return
        self.refresh_folders()

    def folder_at(self, x_root, y_root):
        """The folder tile under a screen point, if any."""
        x = self.canvas.canvasx(x_root - self.canvas.winfo_rootx())
        y = self.canvas.canvasy(y_root - self.canvas.winfo_rooty())
        if not (0 <= x_root - self.canvas.winfo_rootx() < self.canvas.winfo_width()
                and 0 <= y_root - self.canvas.winfo_rooty() < self.canvas.winfo_height()):
            return None
        column, row = int(x // self.cell_size), int(y // self.cell_size)
        index = row * self.max_columns + column
        if 0 <= column < self.max_columns and 0 <= row < self.grid_row_offset and index < len(self.folder_data):
            return self.folder_data[index]
        return None

    def move_to_folder(self, group, dest_dir):
        """Move photos dragged onto a folder tile or tree row, keeping their names.
        Logged like Move & Rename, so Restore From Log puts them back."""
        moved = set(self.move_files(self.image_directory, [d["filename"] for d in group], dest_dir))
        self.remove_moved_from_grid([d for d in group if d["filename"] in moved])
        self.loading_label.config(text=f"Moved {len(moved)} file(s) to {os.path.basename(dest_dir)}.")

    def move_files(self, src_dir, names, dest_dir):
        """Move files between folders keeping their names, logged in src_dir so
        Restore From Log there puts them back. Returns the names that moved."""
        moved, log_entries = [], []
        for name in names:
            old_path = os.path.join(src_dir, name)
            new_path = os.path.join(dest_dir, name)
            if os.path.exists(new_path) and not messagebox.askyesno(
                    "Conflict Warning",
                    f"'{name}' already exists in {os.path.basename(dest_dir)}.\nOverwrite?"):
                continue
            try:
                shutil.move(old_path, new_path)
                move_cached_thumb(old_path, new_path)
            except Exception as e:
                print(f"⚠️ Move failed for {name}: {e}")
                continue
            log_entries.append({"original": name, "final": name, "destination": dest_dir})
            moved.append(name)
        self.write_move_log("", dest_dir, log_entries, log_dir=src_dir)
        return moved

    def cut_selection(self, event=None):
        if event is not None and (isinstance(event.widget, tk.Entry) or event.widget.winfo_toplevel() is not self):
            return  # Ctrl-X in a text field or another window keeps its usual meaning
        if not hasattr(self, "on_disk_order"):
            return
        names = [d["filename"] for d in self.image_data if d["label"] in self.selected_widgets]
        if not names:
            self.loading_label.config(text="Select photos to cut first.")
            return
        self.cut_files = (os.path.normpath(self.image_directory), names)
        self.loading_label.config(text=f"Cut {len(names)} photo(s). Open another folder and press Ctrl-V to move them there.")

    def cut_files_renamed(self, directory, renames):
        """Keep the cut list pointing at its photos after they're renamed in place."""
        if self.cut_files and self.cut_files[0] == os.path.normpath(directory):
            src_dir, names = self.cut_files
            self.cut_files = (src_dir, [renames.get(n, n) for n in names])

    def cut_folder_moved(self, old_path, new_path):
        """Keep the cut list pointing at its folder after it (or a parent) moves."""
        if not self.cut_files:
            return
        old_path, (src_dir, names) = os.path.normpath(old_path), self.cut_files
        if src_dir == old_path or src_dir.startswith(old_path + os.sep):
            src_dir = os.path.normpath(os.path.join(new_path, os.path.relpath(src_dir, old_path)))
            self.cut_files = (src_dir, names)

    def paste_cut(self, event=None, folder_name=None):
        """Move the cut photos into the folder on screen, or into folder_name below it."""
        if event is not None and (isinstance(event.widget, tk.Entry) or event.widget.winfo_toplevel() is not self):
            return
        if not self.cut_files:
            self.loading_label.config(text="Nothing to paste. Select photos and press Ctrl-X first.")
            return
        if self.is_loading():
            return
        src_dir, names = self.cut_files
        dest_dir = os.path.normpath(os.path.join(self.image_directory, folder_name or ""))
        if dest_dir == src_dir:
            self.loading_label.config(text="The cut photos are already in this folder.")
            return
        here = os.path.normpath(self.image_directory)
        # Pasting here reloads the grid to show the new photos, which drops any unsaved order.
        if dest_dir == here and not self.check_unsaved_changes():
            return
        present = [n for n in names if os.path.exists(os.path.join(src_dir, n))]
        if not present:
            self.cut_files = None
            messagebox.showwarning("Paste", f"None of the {len(names)} cut photo(s) are in\n{src_dir}\n"
                                   "any more. Nothing was moved.")
            return
        moved = self.move_files(src_dir, present, dest_dir)
        self.cut_files = None
        missing = len(names) - len(present)
        note = f" {missing} could not be found." if missing else ""
        if src_dir == here:  # pasted into a folder tile while still viewing their folder
            self.remove_moved_from_grid([d for d in self.image_data if d["filename"] in set(moved)])
        if dest_dir == here:
            self.clear_grid()
            self.saved_selections[here] = moved  # reselected once the reload finishes
            self.start_thumbnail_loading()
            print(f"Pasted {len(moved)} photo(s) from {src_dir}.{note}")
        else:
            shown = "the parent folder" if folder_name == ".." else folder_name
            self.loading_label.config(text=f"Moved {len(moved)} photo(s) to {shown}.{note}")

    def load_thumbnails_thread(self, generation, directory, files):
        for idx, file in enumerate(files):
            if generation != self.load_generation:
                return
            img_path = os.path.join(directory, file)
            try:
                img = self.load_thumbnail(img_path)
            except Exception as e:
                print(f"Failed to open file {file}: {e}")
                continue
            try:
                self.after(0, self.add_thumbnail_to_grid, generation, file, img, idx)
            except Exception:
                pass
        self.after(0, self.finish_loading, generation)

    def load_thumbnail(self, path):
        # Read the key before the photo so an edit made while we work leaves the cache stale.
        key = source_key(path)
        img = read_cached_thumb(path, key)
        if img is None:
            img = self.make_thumbnail(path)
            write_cached_thumb(path, img, key)
        return img

    def make_thumbnail(self, path):
        img = Image.open(path)
        img.thumbnail(self.thumb_size, Image.Resampling.LANCZOS)
        # Turn the thumbnail upright per its EXIF Orientation, as other viewers do.
        return ImageOps.exif_transpose(img)

    def finish_loading(self, generation):
        if generation != self.load_generation:
            return
        self.set_on_disk_order()
        self.restore_selection()
        self.loading_label.config(text=f"✅ {self.total_files} thumbnails loaded.")

    def restore_selection(self):
        """Re-select the photos that were selected when this folder was last open."""
        names = set(self.saved_selections.get(os.path.normpath(self.grid_directory), []))
        if not names:
            return
        self.selected_widgets = [d["label"] for d in self.image_data if d["filename"] in names]
        for lbl in self.selected_widgets:
            lbl.config(relief="solid", bd=4, highlightbackground="blue")

    def is_loading(self):
        # on_disk_order is only set once loading completes (see clear_grid / finish_loading).
        if not self.load_generation or hasattr(self, 'on_disk_order'):
            return False
        messagebox.showinfo("Still Loading", "Wait for thumbnails to finish loading.")
        return True

    def add_thumbnail_to_grid(self, generation, file, img, idx):
        if generation != self.load_generation:
            return
        # PhotoImage must be created on the main thread (Tcl/Tk is not thread-safe),
        # so the background loader hands us a plain PIL Image to convert here.
        photo = ImageTk.PhotoImage(img)
        position = len(self.image_data)
        row = self.grid_row_offset + position // self.max_columns
        column = position % self.max_columns
        lbl = tk.Label(self.canvas, image=photo, bd=2, relief="raised")
        lbl.image = photo
        item = self.canvas.create_window(*self.cell_center(row, column), window=lbl)

        lbl.bind("<Button-1>", lambda e, l=lbl: self.handle_click_and_drag(e, l))
        lbl.bind("<Double-Button-1>", lambda e, f=file: self.open_image_viewer(f))
        lbl.bind("<Enter>", lambda e, l=lbl: self.schedule_tooltip(l))
        lbl.bind("<Leave>", self.hide_tooltip)
        lbl.bind("<Button-3>", self.show_grid_menu)

        self.image_data.append({
            "filename": file,
            "photo": photo,
            "label": lbl,
            "item": item,
            "row": row,
            "column": column
        })
        self.update_scrollregion()
        self.loading_label.config(text=f"Loading {len(self.image_data)} of {self.total_files}...")

    def open_keywords_window(self):
        if self.keywords_window and self.keywords_window.winfo_exists():
            self.keywords_window.deiconify()
            self.keywords_window.lift()
            return
        self.keywords_window = KeywordsWindow(self)

    def keyword_targets(self):
        """Photos the EXIF window acts on: the selection (in grid order), else all.
        Returns (photos, whether they're a selection)."""
        selected = [d for d in self.image_data if d["label"] in self.selected_widgets]
        return (selected, True) if selected else (list(self.image_data), False)

    def open_image_viewer(self, filename):
        path = os.path.join(self.image_directory, filename)
        ImageViewer(self, path)

    def delete_selected_thumbnails(self, event=None):
        if not self.selected_widgets:
            messagebox.showwarning(
                title="⚠️ Delete",
                message="Nothing selected!")

            return

        to_delete = [d for d in self.image_data if d["label"] in self.selected_widgets]
        trash = os.path.join(self.image_directory, TRASH_DIR)
        trashed, log_entries = [], []
        for data in to_delete:
            path = os.path.join(self.image_directory, data["filename"])
            try:
                final = move_to_trash(self.image_directory, data["filename"])
                move_cached_thumb(path, os.path.join(trash, final))
                print(f"🗑️ Moved {path} to {TRASH_DIR}")
            except Exception as e:
                print(f"⚠️ Could not move {path} to {TRASH_DIR}: {e}")
                continue
            log_entries.append({"original": data["filename"], "final": final, "destination": trash})
            trashed.append(data)
        self.remove_moved_from_grid(trashed)
        if trashed:
            self.loading_label.config(text=f"Moved {len(trashed)} photo(s) to {TRASH_DIR}.")
        # Logged like a move, so Restore From Log brings them back.
        self.write_move_log("", trash, log_entries)

    def create_drag_cursor(self, event):
        if getattr(self, "drag_overlay", None):
            return
        
        count = len(self.selected_widgets)
        if count == 1:
            img_data = next((d for d in self.image_data if d["label"] == self.selected_widgets[0]), None)
            if img_data:
                self.drag_overlay = tk.Toplevel(self)
                self.drag_overlay.overrideredirect(True)
                self.drag_overlay.attributes("-topmost", True)
                img = img_data["photo"]
                label = tk.Label(self.drag_overlay, image=img, bd=0)
                label.pack()
        elif count > 1:
            self.drag_overlay = tk.Toplevel(self)
            self.drag_overlay.overrideredirect(True)
            self.drag_overlay.attributes("-topmost", True)
            tk.Label(self.drag_overlay, text="📦", font=("Arial", 24)).pack()
        
    def move_drag_cursor(self, event):
        if hasattr(self, 'drag_overlay') and self.drag_overlay:
            x = event.x_root + 10
            y = event.y_root + 10
            self.drag_overlay.geometry(f"+{x}+{y}")

    def destroy_drag_cursor(self, event):
        if hasattr(self, 'drag_overlay') and self.drag_overlay:
            self.drag_overlay.destroy()
            self.drag_overlay = None

    def schedule_tooltip(self, label):
        self.hide_tooltip()
        if not self._is_dragging:
            self.tooltip_after = self.after(500, self.show_tooltip, label)

    def show_tooltip(self, label):
        self.tooltip_after = None
        data = next((d for d in self.image_data if d["label"] is label), None)
        if data is None or not label.winfo_exists():
            return
        # Look up the filename now rather than at bind time: renames change it.
        path = os.path.join(self.image_directory, data["filename"])
        try:
            mtime = os.stat(path).st_mtime
            cached = self.metadata_cache.get(path)
            if cached and cached[0] == mtime:
                date_taken, keywords = cached[1]
            else:
                date_taken, keywords = read_photo_metadata(path)
                self.metadata_cache[path] = (mtime, (date_taken, keywords))
        except Exception as e:
            date_taken, keywords = None, []
            print(f"⚠️ Could not read metadata for {data['filename']}: {e}")

        text = (f"{data['filename']}\n"
                f"Date: {date_taken or '(none)'}\n"
                f"Keywords: {', '.join(keywords) if keywords else '(none)'}")
        self.tooltip = tk.Toplevel(self)
        self.tooltip.overrideredirect(True)
        self.tooltip.attributes("-topmost", True)
        tk.Label(self.tooltip, text=text, justify="left", bg="#ffffe0", fg="black",
                 bd=1, relief="solid", padx=6, pady=4, wraplength=320).pack()
        x, y = self.winfo_pointerxy()
        self.tooltip.geometry(f"+{x + 16}+{y + 16}")

    def hide_tooltip(self, event=None):
        if self.tooltip_after:
            self.after_cancel(self.tooltip_after)
            self.tooltip_after = None
        if self.tooltip:
            self.tooltip.destroy()
            self.tooltip = None

    def rotate_thumbnail_under_cursor(self, event, clockwise=True):
        if isinstance(event.widget, tk.Entry):
            return  # typing an 'r' into the prefix or folder field
        widget = self.winfo_containing(*self.winfo_pointerxy())
        data = next((d for d in self.image_data if d["label"] is widget), None)
        if data is None:
            return
        path = os.path.join(self.image_directory, data["filename"])
        if not data["filename"].lower().endswith(JPEG_EXTENSIONS):
            self.loading_label.config(text=f"⚠️ Can only rotate JPEGs: {data['filename']}")
            return
        try:
            rotate_jpeg(path, clockwise)
            img = self.load_thumbnail(path)
        except Exception as e:
            print(f"⚠️ Could not rotate {data['filename']}: {e}")
            self.loading_label.config(text=f"⚠️ Could not rotate {data['filename']}: {e}")
            return
        photo = ImageTk.PhotoImage(img)
        data["label"].config(image=photo)
        data["label"].image = photo
        data["photo"] = photo
        self.loading_label.config(text=f"{'↻' if clockwise else '↺'} Rotated {data['filename']}")

    def handle_click_and_drag(self, event, label):
        self.canvas.focus_set()  # take focus from the entries so keys reach the grid
        self.hide_tooltip()
        if not hasattr(self, 'on_disk_order'):
            return
        self.dragged_widget = label
        self._drag_pressed = True
        self._press_point = (event.x_root, event.y_root)
        self.handle_click(event, label)

    def on_drag_motion(self, event):
        if getattr(self, "_drag_pressed", False):
            self.handle_drag_motion(event, self.dragged_widget)
        elif getattr(self, "_folder_drag", None):
            self.handle_folder_drag_motion(event)

    def on_button_release(self, event):
        if getattr(self, "_folder_drag", None):
            self.finish_folder_drag(event)
        elif getattr(self, "_drag_pressed", False):
            self._drag_pressed = False
            self.finish_drag(event)
        else:
            self.destroy_drag_cursor(event)

    def press_folder(self, event, name, label):
        self.hide_tooltip()
        # The parent tile can be dropped onto, but not moved.
        self._folder_drag = {"path": os.path.normpath(os.path.join(self.image_directory, name)),
                             "label": label, "movable": name != "..",
                             "start": (event.x_root, event.y_root), "moved": False}

    def handle_folder_drag_motion(self, event):
        drag = self._folder_drag
        if not drag["movable"]:
            return
        if not drag["moved"]:
            dx, dy = event.x_root - drag["start"][0], event.y_root - drag["start"][1]
            if dx * dx + dy * dy < 36:
                return  # small jitter during a click
            drag["moved"] = True
            self.drag_overlay = tk.Toplevel(self)
            self.drag_overlay.overrideredirect(True)
            self.drag_overlay.attributes("-topmost", True)
            tk.Label(self.drag_overlay, text=f"📁 {os.path.basename(drag['path'])}", bg="#dde8f5", bd=1, relief="solid",
                     padx=6, pady=4).pack()
        self.move_drag_cursor(event)
        self._drag_pointer = (event.x_root, event.y_root)
        self.update_drop_highlight()
        if not getattr(self, "_autoscroll_after", None):
            self.auto_scroll()

    def folder_drop_target(self):
        """Where the dragged folder would go: not into itself, anything inside it,
        or the folder it's already in."""
        target = self.drop_target_at(*self._drag_pointer)
        src = self._folder_drag["path"]
        if not target or target == src or target.startswith(src + os.sep) or target == os.path.dirname(src):
            return None
        return target

    def finish_folder_drag(self, event):
        drag = self._folder_drag
        self._drag_pointer = (event.x_root, event.y_root)
        if drag["moved"]:
            target = self.folder_drop_target()
            self._folder_drag = None
            self.destroy_drag_cursor(event)
            self.highlight_drop(None)
            if target:
                self.move_folder(drag["path"], target)
            return
        self._folder_drag = None
        # A click (no drag) opens the folder, if released over the same tile or row.
        if drag["label"]:
            over = self.folder_at(event.x_root, event.y_root)
            released_on_it = over and over["label"] is drag["label"]
        else:
            released_on_it = self.tree_folder_at(event.x_root, event.y_root) == drag["path"]
        if released_on_it and drag["path"] != os.path.normpath(self.image_directory):
            self.navigate_to_folder(drag["path"])

    def move_folder(self, src, dest_dir):
        """Move a folder into another one. Logged like photo moves in the folder it
        left, so Restore From Log there puts it back."""
        name = os.path.basename(src)
        if os.path.exists(os.path.join(dest_dir, name)):
            messagebox.showerror("Move Folder", f"'{name}' already exists in {os.path.basename(dest_dir)}.")
            return
        try:
            shutil.move(src, dest_dir)
        except Exception as e:
            messagebox.showerror("Move Folder", f"Could not move '{name}':\n{e}")
            return
        self.folder_path_changed(src, os.path.join(dest_dir, name))
        self.write_move_log("", dest_dir, [{"original": name, "final": name, "destination": dest_dir}],
                            log_dir=os.path.dirname(src))
        self.refresh_folders()
        self.loading_label.config(text=f"Moved folder '{name}' into {os.path.basename(dest_dir)}.")

    def handle_drag_motion(self, event, label):
        if not self._is_dragging:
            dx, dy = event.x_root - self._press_point[0], event.y_root - self._press_point[1]
            if dx * dx + dy * dy < 36:
                return  # small jitter during a click isn't a drag
        self._is_dragging = True
        self.dragged_widget = label  # Prep drag target
        self.create_drag_cursor(event)  # Show drag visual
        self.move_drag_cursor(event)   # Move overlay
        self._drag_pointer = (event.x_root, event.y_root)
        self.update_drop_highlight()
        if not getattr(self, "_autoscroll_after", None):
            self.auto_scroll()

    def handle_click(self, event, label):
        # The shift-click anchor is kept as a label and both ends are looked up now:
        # positions change whenever photos are reordered, moved out or deleted.
        positions = {d["label"]: i for i, d in enumerate(self.image_data)}
        ctrl_pressed = (event.state & 0x0004) != 0
        shift_pressed = (event.state & 0x0001) != 0

        if ctrl_pressed:
            if label in self.selected_widgets:
                self.selected_widgets.remove(label)
                label.config(relief="raised", bd=2)
            else:
                self.selected_widgets.append(label)
                label.config(relief="solid", bd=4, highlightbackground="blue")
            self.last_clicked_label = label

        elif shift_pressed:
            if self.last_clicked_label in positions:
                i1, i2 = sorted((positions[self.last_clicked_label], positions[label]))
                for i in range(i1, i2 + 1):
                    lbl = self.image_data[i]["label"]
                    if lbl not in self.selected_widgets:
                        self.selected_widgets.append(lbl)
                        lbl.config(relief="solid", bd=4, highlightbackground="blue")
            else:
                self.selected_widgets = [label]
                label.config(relief="solid", bd=4, highlightbackground="blue")
                self.last_clicked_label = label

        else:
            # If already selected, don’t reset — just prep for drag
            if label in self.selected_widgets:
                self.last_clicked_label = label
            else:
                for lbl in self.selected_widgets:
                    lbl.config(relief="raised", bd=2)
                self.selected_widgets = [label]
                label.config(relief="solid", bd=4, highlightbackground="blue")
                self.last_clicked_label = label

    def finish_drag(self, event):
        self.canvas.unbind("<Motion>")
        self.destroy_drag_cursor(event)
        self.highlight_drop(None)
        if not self._is_dragging:
            return
        self._is_dragging = False
        if not hasattr(self, "on_disk_order"):
            return
        group = [d for d in self.image_data if d["label"] in self.selected_widgets]
        if not group:
            return
        dest = self.drop_target_at(event.x_root, event.y_root)
        if dest:
            if dest != os.path.normpath(self.image_directory):
                self.move_to_folder(group, dest)
            return
        # Hit-test against the fixed cell layout in canvas coordinates; widget
        # winfo_x/y aren't kept current for items scrolled out of view.
        px, py = event.x_root - self.canvas.winfo_rootx(), event.y_root - self.canvas.winfo_rooty()
        if not (0 <= px < self.canvas.winfo_width() and 0 <= py < self.canvas.winfo_height()):
            return  # dropped outside the grid
        column = int(self.canvas.canvasx(px) // self.cell_size)
        row = int(self.canvas.canvasy(py) // self.cell_size) - self.grid_row_offset
        if not 0 <= column < self.max_columns or row < 0:
            return
        position = row * self.max_columns + column
        remaining = [d for d in self.image_data if d["label"] not in self.selected_widgets]
        if position < len(self.image_data):
            target = self.image_data[position]["label"]
            if target in self.selected_widgets:
                return  # dropped back onto the selection: leave the order alone
            target_idx = next(i for i, d in enumerate(remaining) if d["label"] is target)
        else:
            target_idx = len(remaining)  # dropped past the last photo: move to the end
        self.image_data = remaining[:target_idx] + group + remaining[target_idx:]
        self.redraw_grid()

    def set_dragged_widget(self, label):
        self.dragged_widget = label
        pass

    def cell_center(self, row, column):
        half = self.cell_size / 2
        return column * self.cell_size + half, row * self.cell_size + half

    def update_scrollregion(self):
        rows = self.grid_row_offset + (len(self.image_data) + self.max_columns - 1) // self.max_columns
        self.canvas.configure(scrollregion=(0, 0, self.max_columns * self.cell_size, rows * self.cell_size))

    def redraw_grid(self):
        row = self.grid_row_offset
        column = 0
        for data in self.image_data:
            self.canvas.coords(data["item"], *self.cell_center(row, column))
            data["row"], data["column"] = row, column
            column += 1
            if column >= self.max_columns:
                column = 0
                row += 1
        self.update_scrollregion()

    def preview_order(self):
        prefix = self.prefix_entry.get().strip()
        if prefix and not prefix.endswith("_"):
            prefix += "_"
        preview_text = ""
        for idx, data in enumerate(self.image_data, start=1):
            ext = os.path.splitext(data["filename"])[1].lower()
            new_name = f"{prefix}{idx:03d}{ext}"
            preview_text += f"{data['filename']} → {new_name}\n"
        messagebox.showinfo("Rename Preview", preview_text)
        if messagebox.askyesno("Confirm Rename", "Proceed with renaming these files?"):
            self.save_order()

    def save_order(self):
        if self.is_loading():
            return
        prefix = self.prefix_entry.get().strip()
        if prefix and not prefix.endswith("_"):
            prefix += "_"
        prefix_date = parse_date_from_prefix(prefix)
        log_entries, temp_names = [], []

        for idx, data in enumerate(self.image_data, start=1):
            old_path = os.path.join(self.image_directory, data["filename"])
            ext = os.path.splitext(data["filename"])[1].lower()
            temp_name = f"_temp_{idx:03d}{ext}"
            temp_path = os.path.join(self.image_directory, temp_name)
            try:
                os.rename(old_path, temp_path)
                move_cached_thumb(old_path, temp_path)
            except Exception as e:
                print(f"⚠️ Rename failed: {e}")
                continue
            data["filename"] = temp_name
            temp_names.append((temp_path, ext))
            log_entries.append({"original": os.path.basename(old_path), "temporary": temp_name})

        for idx, (temp_path, ext) in enumerate(temp_names, start=1):
            final_name = f"{prefix}{idx:03d}{ext}"
            final_path = os.path.join(self.image_directory, final_name)
            if os.path.exists(final_path) and os.path.basename(temp_path) != final_name:
                overwrite = messagebox.askyesno("Conflict Warning",
                    f"File '{final_name}' already exists.\nOverwrite?")
                if not overwrite:
                    continue
            try:
                os.rename(temp_path, final_path)
                move_cached_thumb(temp_path, final_path)
            except Exception as e:
                print(f"⚠️ Final rename failed: {e}")
                continue
            self.image_data[idx - 1]["filename"] = final_name
            log_entries[idx - 1]["final"] = final_name

            if prefix_date and ext in JPEG_EXTENSIONS:
                try:
                    set_jpeg_date_taken(final_path, prefix_date)
                    log_entries[idx - 1]["exif_date"] = prefix_date.isoformat()
                except Exception as e:
                    print(f"⚠️ Could not set EXIF date for {final_name}: {e}")

        self.set_on_disk_order()
        self.cut_files_renamed(self.image_directory,
                               {e["original"]: e["final"] for e in log_entries if "final" in e})
        self.loading_label.config(text=f"File renames complete.")

        # Save JSON log
        timestamp = datetime.datetime.now().isoformat(timespec="seconds").replace(":", "-")
        log_name = f"rename_log_{timestamp}.json"
        log_path = os.path.join(self.image_directory, log_name)
        try:
            with open(log_path, "w") as f:
                json.dump({
                    "timestamp": timestamp,
                    "prefix": prefix,
                    "files": log_entries
                }, f, indent=2)
            print(f"📝 Rename log saved: {log_name}")
        except Exception as e:
            print(f"⚠️ Log save failed: {e}")

        self.redraw_grid()

    def move_and_rename_to_folder(self):
        if self.is_loading():
            return
        if not self.image_data:
            messagebox.showinfo("Move & Rename", "No images loaded.")
            return
        # Move just the selection when there is one (in grid order), else everything.
        to_move = [d for d in self.image_data if d["label"] in self.selected_widgets] \
            or list(self.image_data)
        which = "selected " if len(to_move) < len(self.image_data) else ""

        prefix = self.prefix_entry.get().strip()
        if not prefix and not messagebox.askyesno(
            "No Prefix",
            "The filename prefix is blank, so the files will be named 001, 002, ...\n"
            "with no date.\n\nMove them anyway?",
            icon="warning", default="no"
        ):
            return
        if prefix and not prefix.endswith("_"):
            prefix += "_"

        initial = self.move_destination if self.move_destination and os.path.isdir(self.move_destination) \
            else (self.folder_entry.get().strip() or self.image_directory)
        kwargs = {"initialdir": initial} if initial and os.path.isdir(initial) else {}
        dest_dir = filedialog.askdirectory(title="Choose destination folder", **kwargs)
        if not dest_dir:
            return
        if os.path.normpath(dest_dir) == os.path.normpath(self.image_directory):
            messagebox.showerror("Move & Rename", "Destination must be a different folder.")
            return

        self.move_destination = dest_dir
        self.save_config()

        if not messagebox.askyesno(
            "Confirm Move",
            f"Move and rename {len(to_move)} {which}file(s) to:\n{dest_dir}?"
        ):
            return

        start_idx = find_next_index(dest_dir, prefix)
        prefix_date = parse_date_from_prefix(prefix)
        moved, log_entries = [], []

        for offset, data in enumerate(to_move):
            idx = start_idx + offset
            old_path = os.path.join(self.image_directory, data["filename"])
            ext = os.path.splitext(data["filename"])[1].lower()
            final_name = f"{prefix}{idx:03d}{ext}"
            final_path = os.path.join(dest_dir, final_name)

            if os.path.exists(final_path):
                overwrite = messagebox.askyesno(
                    "Conflict Warning",
                    f"File '{final_name}' already exists in destination.\nOverwrite?"
                )
                if not overwrite:
                    continue

            try:
                shutil.move(old_path, final_path)
                move_cached_thumb(old_path, final_path)
            except Exception as e:
                print(f"⚠️ Move failed for {data['filename']}: {e}")
                continue

            if prefix_date and ext in JPEG_EXTENSIONS:
                try:
                    set_jpeg_date_taken(final_path, prefix_date)
                except Exception as e:
                    print(f"⚠️ Could not set EXIF date for {final_name}: {e}")

            log_entries.append({
                "original": data["filename"],
                "final": final_name,
                "destination": dest_dir,
            })
            moved.append(data)

        self.remove_moved_from_grid(moved)
        self.loading_label.config(text=f"Moved {len(moved)} file(s) to {dest_dir}.")
        self.write_move_log(prefix, dest_dir, log_entries)

    def remove_moved_from_grid(self, moved):
        for data in moved:
            self.canvas.delete(data["item"])
            data["label"].destroy()
            self.image_data.remove(data)
            if data["label"] in self.selected_widgets:
                self.selected_widgets.remove(data["label"])
        self.last_clicked_label = None
        self.redraw_grid()
        # Drop the moved files from the saved order without marking any unsaved
        # reordering of the files left behind as saved.
        moved_names = {d["filename"] for d in moved}
        self.on_disk_order = [f for f in self.on_disk_order if f not in moved_names]

    def write_move_log(self, prefix, dest_dir, log_entries, log_dir=None):
        """Write a move log into log_dir (the folder the files left; by default the
        one on screen), where Restore From Log looks for it."""
        log_dir = log_dir or self.image_directory
        if log_entries:
            timestamp = datetime.datetime.now().isoformat(timespec="seconds").replace(":", "-")
            log_name, n = f"move_log_{timestamp}.json", 1
            # Several moves can land in the same second; don't let one log replace another.
            while os.path.exists(os.path.join(log_dir, log_name)):
                n += 1
                log_name = f"move_log_{timestamp}_{n}.json"
            log_path = os.path.join(log_dir, log_name)
            try:
                with open(log_path, "w") as f:
                    json.dump({
                        "timestamp": timestamp,
                        "prefix": prefix,
                        "destination": dest_dir,
                        "files": log_entries
                    }, f, indent=2)
                print(f"📝 Move log saved: {log_name}")
            except Exception as e:
                print(f"⚠️ Log save failed: {e}")

    def restore_from_log(self):
        """Undo the most recent rename or move made from this folder, using its log."""
        if self.is_loading():
            return
        logs = [f for f in os.listdir(self.image_directory)
                if f.startswith(("rename_log_", "move_log_")) and f.endswith(".json")]
        if not logs:
            messagebox.showerror("Restore Error", "No rename or move logs found.")
            return
        # Both log types share the timestamp suffix, so order by that, not by prefix;
        # the timestamp only has 1s resolution, so break ties by file mtime.
        log_name = max(logs, key=lambda f: (f.split("_log_", 1)[1],
                                            os.path.getmtime(os.path.join(self.image_directory, f))))
        log_path = os.path.join(self.image_directory, log_name)
        try:
            with open(log_path, "r") as f:
                log = json.load(f)
        except Exception:
            messagebox.showerror("Error", f"Could not read {log_name}")
            return

        entries = log.get("files", [])
        is_move = log_name.startswith("move_log_")
        if is_move:
            prompt = (f"Move {len(entries)} file(s) back from:\n{log.get('destination', '')}\n"
                      f"and restore their original names?\n\n({log_name})")
        else:
            prompt = f"Restore original filenames from {log_name}?"
        if not messagebox.askyesno("Confirm Restore", prompt):
            return

        restored, renames = 0, {}
        for entry in entries:
            if is_move:
                current = os.path.join(entry.get("destination", log.get("destination", "")), entry["final"])
            else:
                current = os.path.join(self.image_directory, entry.get("final", entry["temporary"]))
            original = os.path.join(self.image_directory, entry["original"])
            if not os.path.exists(current) and os.path.exists(original):
                restored += 1  # already put back by an earlier, partial restore
                continue
            if os.path.exists(original) and current != original:
                overwrite = messagebox.askyesno("Conflict Warning",
                    f"File '{entry['original']}' already exists.\nOverwrite?")
                if not overwrite:
                    continue
            try:
                shutil.move(current, original) if is_move else os.rename(current, original)
                move_cached_thumb(current, original)
                restored += 1
                if not is_move:
                    renames[os.path.basename(current)] = entry["original"]
            except Exception as e:
                print(f"⚠️ Could not restore {os.path.basename(current)}: {e}")

        self.cut_files_renamed(self.image_directory, renames)
        # Retire a fully-restored log so it isn't offered again: once a move is undone,
        # later moves reuse those destination names, and replaying the old log would
        # pull the new files back. A partial restore keeps the log so it can be retried.
        if restored == len(entries):
            retired, n = f"restored_{log_name}", 1
            while os.path.exists(os.path.join(self.image_directory, retired)):
                n += 1
                retired = f"restored{n}_{log_name}"
            try:
                os.rename(log_path, os.path.join(self.image_directory, retired))
            except Exception as e:
                print(f"⚠️ Could not retire {log_name}: {e}")
            messagebox.showinfo("Restore Complete", f"Restored {restored} files from {log_name}")
        else:
            messagebox.showwarning("Restore Incomplete",
                f"Restored {restored} of {len(entries)} files from {log_name}.\n"
                "The log was kept so the rest can be retried.")
        self.clear_grid()
        self.start_thumbnail_loading()

    def auto_scroll(self):
        """Scroll while a drag is held near (or past) the top or bottom edge of the
        grid, repeating on a timer so the pointer needn't keep moving."""
        self._autoscroll_after = None
        if not (getattr(self, "_drag_pressed", False) or getattr(self, "_folder_drag", None)):
            return
        px, py = self._drag_pointer[0] - self.tree.winfo_rootx(), self._drag_pointer[1] - self.tree.winfo_rooty()
        if 0 <= px < self.tree.winfo_width():  # over the folder tree: scroll it instead
            edge = 20
            step = -1 if py < edge else 1 if py > self.tree.winfo_height() - edge else 0
            if step:
                self.tree.yview_scroll(step, "units")
                self.update_drop_highlight()
                self._autoscroll_after = self.after(80, self.auto_scroll)
            return
        canvas_height = self.canvas.winfo_height()
        distance_from_top = self._drag_pointer[1] - self.canvas.winfo_rooty()
        distance_from_bottom = canvas_height - distance_from_top
        edge_threshold = canvas_height / 6

        speed = 0
        if distance_from_top < edge_threshold:
            speed = -min(5, 1 + int((edge_threshold - distance_from_top) / 30))
        elif distance_from_bottom < edge_threshold:
            speed = min(5, 1 + int((edge_threshold - distance_from_bottom) / 30))
        if speed == 0:
            return
        self.canvas.yview_scroll(speed, "units")
        self.update_drop_highlight()
        self._autoscroll_after = self.after(50, self.auto_scroll)

if __name__ == "__main__":
    app = DragDropSorter()
    app.mainloop()
