import tkinter as tk
from tkinter import messagebox
from tkinter import filedialog
from tkinter import simpledialog
from tkinter import ttk
from tkinter import font as tkfont
from PIL import Image, ImageTk, ImageOps, ImageStat, IptcImagePlugin, PngImagePlugin
import os, json, threading, bisect, datetime, calendar, re, shutil, math, stat, subprocess, time
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
VIEW_FILE = ".photo_sorter_view.json"  # per-folder scroll positions of the grid and tree
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


def image_dimensions(path):
    """Width and height as displayed (EXIF-rotated photos swapped), read from
    the header only; None if it can't be read."""
    try:
        with Image.open(path) as img:
            w, h = img.size
            if img.getexif().get(0x0112) in (5, 6, 7, 8):  # turned 90°
                w, h = h, w
            return w, h
    except Exception:
        return None


def format_file_size(n):
    """Compact size: 940 KB, 1.3 MB."""
    if n < 1024 * 1024:
        return f"{max(1, round(n / 1024))} KB"
    return f"{n / (1024 * 1024):.1f} MB"


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


def stretch_channel(hist, clip):
    """A lookup table stretching one channel so the darkest and brightest clip
    fraction of its pixels become 0 and 255. Left alone if there's no range."""
    cut = sum(hist) * clip
    lo, acc = 0, 0
    while lo < 255 and acc + hist[lo] <= cut:
        acc += hist[lo]
        lo += 1
    hi, acc = 255, 0
    while hi > 0 and acc + hist[hi] <= cut:
        acc += hist[hi]
        hi -= 1
    if hi - lo < 10:
        return list(range(256))
    return [min(255, max(0, round((v - lo) * 255 / (hi - lo)))) for v in range(256)]


def neutral_midtones(rgb, strength):
    """Per-channel gamma that moves the midtones' average colour toward grey.
    strength 1 makes it fully grey; less keeps some of a scene's own warmth
    (lamplight, candles), which full strength would turn blue."""
    mask = rgb.convert("L").point(lambda v: 255 if 60 <= v <= 200 else 0)
    if not mask.getbbox():
        return rgb
    means = ImageStat.Stat(rgb, mask=mask).mean
    target = sum(means) / 3
    lut = []
    for m in means:
        gamma = math.log(target / 255) / math.log(min(max(m, 1), 254) / 255)
        gamma = min(1.8, max(0.55, gamma)) ** strength
        lut += [round(255 * (v / 255) ** gamma) for v in range(256)]
    return rgb.point(lut)


def auto_color_balance(img, clip=0.005, strength=0.5):
    """Correct a faded print's colour cast: stretch red, green and blue separately
    to the full range, then pull the midtones partway toward neutral. Each pass
    is moderate; running it again on the result corrects further."""
    if img.mode == "L":
        return img.point(stretch_channel(img.histogram(), clip))
    alpha = img.getchannel("A") if img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info else None
    rgb = img.convert("RGB")
    hist = rgb.histogram()
    lut = []
    for c in range(3):
        lut += stretch_channel(hist[c * 256:(c + 1) * 256], clip)
    fixed = neutral_midtones(rgb.point(lut), strength)
    if alpha is not None:
        fixed.putalpha(alpha.convert("L") if alpha.mode != "L" else alpha)
    return fixed


def save_color_balanced(path, passes=1):
    """Write the auto colour balanced version of a photo over it, keeping all of
    its metadata. The original goes into the folder's .trash. Returns the name
    it was given there."""
    directory, name = os.path.split(path)
    tmp = os.path.join(directory, f".colorfix_{os.getpid()}_{name}")
    try:
        with Image.open(path) as raw:
            # Corrected as stored, not as displayed, so the EXIF Orientation copied
            # across below still turns it upright.
            fixed = raw
            for _ in range(passes):
                fixed = auto_color_balance(fixed)
            kwargs = {}
            if raw.info.get("icc_profile"):
                kwargs["icc_profile"] = raw.info["icc_profile"]
            if raw.format == "JPEG":
                from PIL import JpegImagePlugin
                sampling = JpegImagePlugin.get_sampling(raw)
                kwargs.update(quality=95, **({"subsampling": sampling} if sampling >= 0 else {}))
            fixed.save(tmp, format=raw.format, **kwargs)
        result = subprocess.run(["exiftool", "-q", "-q", "-overwrite_original", "-tagsFromFile", path,
                                 "-all:all", "-ThumbnailImage=", tmp], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Could not copy metadata: {result.stderr.strip() or result.returncode}")
        mode = stat.S_IMODE(os.stat(path).st_mode)
        trashed = move_to_trash(directory, name)
        os.replace(tmp, path)
        os.chmod(path, mode)
        return trashed
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# --- Undo/redo history -------------------------------------------------------
#
# Every command that changes files records the steps it took. Undo runs them
# backwards, Redo forwards again. Steps record how to reverse themselves (a move
# moves back, a rotation turns back, old EXIF dates are written back) rather
# than keeping copies of photos, so undo costs no disk space. The history lasts
# for this run of the program.

UNDO_STORE_ROOT = os.path.join(os.path.expanduser("~"), ".cache", "photo_sorter", "undo")
EXIF_DATE_TAGS = (("0th", piexif.ImageIFD.DateTime),
                  ("Exif", piexif.ExifIFD.DateTimeOriginal),
                  ("Exif", piexif.ExifIFD.DateTimeDigitized))


def read_jpeg_dates(path):
    exif = load_exif(path)
    return [exif.get(ifd, {}).get(tag) for ifd, tag in EXIF_DATE_TAGS]


def write_jpeg_dates(path, values):
    exif = load_exif(path)
    for (ifd, tag), value in zip(EXIF_DATE_TAGS, values):
        if value is None:
            exif.get(ifd, {}).pop(tag, None)
        else:
            exif.setdefault(ifd, {})[tag] = value
    write_exif(path, exif)


class UndoError(Exception):
    """A step that can't be undone or redone because things have changed since."""


class MoveStep:
    """A file or folder moved or renamed from src to dst."""
    def __init__(self, src, dst):
        self.src, self.dst = os.path.normpath(src), os.path.normpath(dst)

    @staticmethod
    def _move(a, b):
        if not os.path.lexists(a):
            raise UndoError(f"{a} is no longer there")
        if os.path.lexists(b):
            raise UndoError(f"{b} is in the way")
        os.makedirs(os.path.dirname(b), exist_ok=True)
        is_file = os.path.isfile(a)
        shutil.move(a, b)
        if is_file:
            move_cached_thumb(a, b)
        # Don't leave behind a .trash that undo just emptied.
        left = os.path.dirname(a)
        if os.path.basename(left) == TRASH_DIR:
            shutil.rmtree(os.path.join(left, THUMB_DIR), ignore_errors=True)
            try:
                os.rmdir(left)
            except OSError:
                pass  # still holds other things
        return [a, b]

    def undo(self, store):
        return self._move(self.dst, self.src)

    def redo(self, store):
        return self._move(self.src, self.dst)


class CreateStep:
    """A file the command created (a log, a colour balanced photo). Undo puts it
    aside in the undo store; Redo brings it back."""
    def __init__(self, path):
        self.path = os.path.normpath(path)
        self.stash = None

    def undo(self, store):
        if not os.path.exists(self.path):
            raise UndoError(f"{self.path} is no longer there")
        os.makedirs(store, exist_ok=True)
        self.stash = os.path.join(store, f"{id(self)}_{os.path.basename(self.path)}")
        shutil.move(self.path, self.stash)
        return [self.path]

    def redo(self, store):
        if not self.stash or not os.path.exists(self.stash):
            raise UndoError(f"the undone copy of {self.path} is gone")
        if os.path.lexists(self.path):
            raise UndoError(f"{self.path} is in the way")
        shutil.move(self.stash, self.path)
        self.stash = None
        return [self.path]


class MkdirStep:
    def __init__(self, path):
        self.path = os.path.normpath(path)

    def undo(self, store):
        # The program's own clutter (thumbnail cache, scroll position) doesn't count.
        for junk in (THUMB_DIR, VIEW_FILE):
            junk = os.path.join(self.path, junk)
            if os.path.isdir(junk):
                shutil.rmtree(junk, ignore_errors=True)
            elif os.path.exists(junk):
                os.remove(junk)
        try:
            os.rmdir(self.path)
        except OSError:
            raise UndoError(f"folder {self.path} isn't empty")
        return [self.path]

    def redo(self, store):
        os.makedirs(self.path)
        return [self.path]


class RotateStep:
    def __init__(self, path, clockwise):
        self.path, self.clockwise = path, clockwise

    def undo(self, store):
        rotate_jpeg(self.path, not self.clockwise)
        return [self.path]

    def redo(self, store):
        rotate_jpeg(self.path, self.clockwise)
        return [self.path]


class ExifDateStep:
    def __init__(self, path, before, after):
        self.path, self.before, self.after = path, before, after

    def undo(self, store):
        write_jpeg_dates(self.path, self.before)
        return [self.path]

    def redo(self, store):
        write_jpeg_dates(self.path, self.after)
        return [self.path]


class KeywordsStep:
    """Keywords actually added to and removed from some photos (not ones that
    were already there, or already absent, so undo leaves those alone)."""
    def __init__(self, paths, added, removed):
        self.paths, self.added, self.removed = list(paths), list(added), list(removed)

    def undo(self, store):
        edit_keywords(self.paths, add=self.removed, remove=self.added)
        return self.paths

    def redo(self, store):
        edit_keywords(self.paths, add=self.added, remove=self.removed)
        return self.paths


class History:
    LIMIT = 200  # oldest commands drop off beyond this

    def __init__(self):
        self.undo_stack, self.redo_stack = [], []
        self.store = os.path.join(UNDO_STORE_ROOT, f"{os.getpid()}")
        # Clear out what earlier runs left behind if they didn't exit cleanly.
        try:
            for name in os.listdir(UNDO_STORE_ROOT):
                if name.isdigit() and not os.path.exists(f"/proc/{name}"):
                    shutil.rmtree(os.path.join(UNDO_STORE_ROOT, name), ignore_errors=True)
        except OSError:
            pass

    def record(self, label, steps):
        """Add a finished command. Steps are in the order they were done."""
        if not steps:
            return
        self.undo_stack.append((label, list(steps)))
        del self.undo_stack[:-self.LIMIT]
        self.redo_stack.clear()
        shutil.rmtree(self.store, ignore_errors=True)  # only undone creations lived there

    def undo_label(self):
        return self.undo_stack[-1][0] if self.undo_stack else None

    def redo_label(self):
        return self.redo_stack[-1][0] if self.redo_stack else None

    def _run(self, source, target, undo):
        """Undo (or redo) the latest command. Returns (label, steps, paths touched,
        problems). A step that fails is reported and the rest still run."""
        label, steps = source.pop()
        touched, problems = [], []
        for step in (reversed(steps) if undo else steps):
            try:
                touched += step.undo(self.store) if undo else step.redo(self.store)
            except Exception as e:
                problems.append(str(e))
        target.append((label, steps))
        return label, steps, touched, problems

    def undo(self):
        return self._run(self.undo_stack, self.redo_stack, undo=True)

    def redo(self):
        return self._run(self.redo_stack, self.undo_stack, undo=False)

    def close(self):
        shutil.rmtree(self.store, ignore_errors=True)


def color_balance_steps(path, trashed):
    """History steps for save_color_balanced: the original went to .trash and a
    new file took its place."""
    directory = os.path.dirname(path)
    return [MoveStep(path, os.path.join(directory, TRASH_DIR, trashed)), CreateStep(path)]


def read_all_metadata(path):
    """Every metadata tag in a photo as (group, tag, value) rows: from exiftool
    when it's installed, otherwise the EXIF that PIL can read."""
    try:
        result = subprocess.run(["exiftool", "-a", "-G1", path], capture_output=True, text=True, timeout=20)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        result = None
    if result and result.returncode == 0:
        rows = []
        for line in result.stdout.splitlines():
            m = re.match(r"\[([^\]]*)\]\s+(.*?)\s*: (.*)", line)
            if m:
                rows.append(m.groups())
        return rows, "exiftool"
    rows = []
    with Image.open(path) as img:
        exif = img.getexif()
        from PIL import ExifTags
        for ifd_name, tags in (("IFD0", exif), ("ExifIFD", exif.get_ifd(0x8769)), ("GPS", exif.get_ifd(0x8825))):
            names = ExifTags.GPSTAGS if ifd_name == "GPS" else ExifTags.TAGS
            for tag, value in tags.items():
                if isinstance(value, bytes):
                    value = f"({len(value)} bytes)"
                rows.append((ifd_name, names.get(tag, hex(tag)), str(value)))
        rows.insert(0, ("File", "Image Size", f"{img.width}x{img.height}"))
    return rows, "PIL (install exiftool for the full list)"


class MetadataWindow(tk.Toplevel):
    """All of a photo's EXIF/XMP/IPTC tags in a searchable, copyable list."""

    def __init__(self, master, path):
        super().__init__(master)
        name = os.path.basename(path)
        self.title(f"EXIF Data — {name}")
        self.geometry("720x600")
        try:
            master.config(cursor="watch"); master.update_idletasks()
            self._rows, source = read_all_metadata(path)
        except Exception as e:
            self._rows, source = [], f"could not read: {e}"
        finally:
            master.config(cursor="")

        top = tk.Frame(self)
        tk.Label(top, text="Filter:").pack(side="left")
        self._filter = tk.StringVar()
        entry = tk.Entry(top, textvariable=self._filter)
        entry.pack(side="left", fill="x", expand=True, padx=4)
        tk.Button(top, text="Copy All", command=self._copy).pack(side="left")
        top.pack(fill="x", padx=6, pady=4)
        self._status = tk.Label(self, anchor="w", fg="gray30")
        self._status.pack(fill="x", padx=6)
        self._source = source

        frame = tk.Frame(self)
        # The named font itself: a ("TkFixedFont", 10) tuple is read as a family
        # name, isn't found, and falls back to a proportional face.
        mono = tkfont.nametofont("TkFixedFont").copy()
        mono.configure(size=10)
        self._text = tk.Text(frame, wrap="none", font=mono)
        ys = tk.Scrollbar(frame, orient="vertical", command=self._text.yview)
        xs = tk.Scrollbar(frame, orient="horizontal", command=self._text.xview)
        self._text.config(yscrollcommand=ys.set, xscrollcommand=xs.set)
        ys.pack(side="right", fill="y"); xs.pack(side="bottom", fill="x")
        self._text.pack(fill="both", expand=True)
        frame.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self._text.tag_configure("group", foreground="#2060a0")

        self._filter.trace_add("write", lambda *a: self._show())
        self.bind("<Escape>", lambda e: self.destroy())
        self._show()
        entry.focus_set()

    def _lines(self):
        want = self._filter.get().strip().lower()
        rows = [r for r in self._rows if not want or want in " ".join(r).lower()]
        width_g = max((len(g) for g, _, _ in rows), default=0)
        width_t = max((len(t) for _, t, _ in rows), default=0)
        return rows, width_g, width_t

    def _show(self):
        rows, wg, wt = self._lines()
        self._text.config(state="normal")
        self._text.delete("1.0", "end")
        for group, tag, value in rows:
            self._text.insert("end", f"{group:<{wg}}  ", "group")
            self._text.insert("end", f"{tag:<{wt}}  {value}\n")
        self._text.config(state="disabled")
        self._status.config(text=f"{len(rows)} of {len(self._rows)} tags  ·  from {self._source}")

    def _copy(self):
        rows, wg, wt = self._lines()
        self.clipboard_clear()
        self.clipboard_append("\n".join(f"{g:<{wg}}  {t:<{wt}}  {v}" for g, t, v in rows))


class ImageViewer(tk.Toplevel):
    """Full-size view of one photo. Given the list of photos it was opened from,
    the Left and Right arrow keys step to the previous and next one."""

    open_viewers = set()

    def __init__(self, master, image_path, paths=None):
        super().__init__(master)
        self.geometry("800x600")
        ImageViewer.open_viewers.add(self)
        self.bind("<Destroy>", lambda e: e.widget is self and ImageViewer.open_viewers.discard(self))
        self._paths = list(paths) if paths else [image_path]
        self._index = self._paths.index(image_path) if image_path in self._paths else 0
        if image_path not in self._paths:
            self._paths.insert(0, image_path)
        self._image = None
        self._original = None  # the photo as on disk, while a correction is shown
        self._passes = 0       # times Auto Color Balance has been applied to it
        self._load()
        self._scale = 1.0
        self._offset = [0.0, 0.0]
        self._drag_start = None
        self._photo = None
        self._zoom_var = tk.StringVar(value="100%")

        menubar = tk.Menu(self)
        self._color_menu = tk.Menu(menubar, tearoff=0)
        self._color_menu.add_command(label="Auto Color Balance", accelerator="Ctrl+B",
                                     command=self.color_balance)
        self._color_menu.add_command(label="Show Original", command=self.show_original)
        self._color_menu.add_separator()
        self._color_menu.add_command(label="Save Color Balance...", command=self.save_color_balance)
        menubar.add_cascade(label="Color", menu=self._color_menu)
        self.config(menu=menubar)
        self._update_menu()

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
        self._context_menu = tk.Menu(self, tearoff=0)
        self._context_menu.add_command(label="Show EXIF Data...",
                                       command=lambda: MetadataWindow(self, self._paths[self._index]))
        self._canvas.bind("<Button-3>", lambda e: self._context_menu.tk_popup(e.x_root, e.y_root))
        # Redraw on resize so newly exposed areas get filled; skipped until the first fit.
        self._canvas.bind("<Configure>", lambda e: self._photo and self._redraw())
        self.bind("<Left>", lambda e: self.step(-1))
        self.bind("<Right>", lambda e: self.step(1))
        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<Control-b>", lambda e: self.color_balance())
        self.bind("<Control-B>", lambda e: self.color_balance())
        # The sorter binds these application-wide to act on its grid's selection;
        # stop them reaching it from here.
        for key in ("<Delete>", "<KeyPress-r>", "<KeyPress-R>", "<Control-x>", "<Control-X>",
                    "<Control-v>", "<Control-V>", "<Home>", "<End>", "<Prior>", "<Next>"):
            self.bind(key, lambda e: "break")
        self.focus_set()

        self.after(100, self._fit_to_window)

    def _load(self):
        path = self._paths[self._index]
        position = f"   ({self._index + 1} of {len(self._paths)})" if len(self._paths) > 1 else ""
        try:
            image = ImageOps.exif_transpose(Image.open(path))
        except Exception as e:
            print(f"⚠️ Could not open {path}: {e}")
            image = Image.new("RGB", (400, 300), "black")
            position += "   — could not open"
        if self._image is not None:
            self._image.close()
        if self._original is not None:
            self._original.close()
        self._image, self._original, self._passes = image, None, 0
        self._failed = "could not open" in position
        self._update_title()
        if hasattr(self, "_color_menu"):
            self._update_menu()

    def _update_title(self):
        path = self._paths[self._index]
        title = os.path.basename(path)
        if len(self._paths) > 1:
            title += f"   ({self._index + 1} of {len(self._paths)})"
        if self._failed:
            title += "   — could not open"
        elif self._passes:
            title += "   — color balanced" + (f" ×{self._passes}" if self._passes > 1 else "") + ", not saved"
        self.title(title)

    def _update_menu(self):
        corrected = self._passes > 0
        self._color_menu.entryconfig(0, label="Auto Color Balance" + (" Again" if corrected else ""))
        for label, enabled in ((0, not self._failed),
                               ("Show Original", corrected),
                               ("Save Color Balance...", corrected)):
            self._color_menu.entryconfig(label, state="normal" if enabled else "disabled")

    def color_balance(self):
        if self._failed:
            return "break"
        balanced = auto_color_balance(self._image)
        if self._original is None:
            self._original = self._image
        else:
            self._image.close()  # an earlier pass's result
        self._image = balanced
        self._passes += 1
        self._update_title()
        self._update_menu()
        self._redraw()
        return "break"

    def show_original(self):
        if self._original is None:
            return
        self._image.close()
        self._image, self._original, self._passes = self._original, None, 0
        self._update_title()
        self._update_menu()
        self._redraw()

    def save_color_balance(self):
        if self._failed or not self._passes:
            return
        path = self._paths[self._index]
        if not messagebox.askyesno(
                "Save Color Balance",
                f"Replace {os.path.basename(path)} with the color-balanced version?\n\n"
                f"The original will be kept in this folder's {TRASH_DIR}.", parent=self):
            return
        try:
            trashed = save_color_balanced(path, self._passes)
        except Exception as e:
            messagebox.showerror("Save Color Balance", f"Could not save:\n{e}", parent=self)
            return
        print(f"🎨 Color balanced {path} (original in {TRASH_DIR}/{trashed})")
        sorter = getattr(self.master, "sorter", self.master)
        sorter.history.record(f"Color Balance {os.path.basename(path)}", color_balance_steps(path, trashed))
        self._load()
        self._redraw()
        changed = getattr(self.master, "photo_changed", None)
        if changed:
            changed(path)

    def file_changed(self, path):
        """Show the photo again if something (Undo, Redo) changed it on disk."""
        if os.path.normpath(self._paths[self._index]) == os.path.normpath(path):
            self._load()
            if self._photo:
                self._redraw()

    def step(self, delta):
        index = self._index + delta
        if not 0 <= index < len(self._paths):
            self.bell()
            return "break"
        self._index = index
        self._load()
        if self._photo:  # once the window has had its first fit
            self._fit_to_window()
        return "break"

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
        return "break"  # don't also scroll the sorter's grid

    def _on_scroll_linux(self, event):
        self._zoom_at(1.1 if event.num == 4 else 1 / 1.1, event.x, event.y)
        return "break"

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
        # What each photo had, so undo reverses only what this actually changed.
        had = {}
        for p in jpegs:
            try:
                had[p] = {k.lower() for k in read_photo_metadata(p)[1]}
            except Exception:
                had[p] = set()
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
        groups = {}
        for p in jpegs:
            added = tuple(k for k in (keywords if add else ()) if k.lower() not in had[p])
            removed = tuple(k for k in (() if add else keywords) if k.lower() in had[p])
            if added or removed:
                groups.setdefault((added, removed), []).append(p)
        self.sorter.history.record(f"{'Add' if add else 'Remove'} Keyword(s) {', '.join(keywords)}",
                                   [KeywordsStep(ps, a, r) for (a, r), ps in groups.items()])
        skipped = len(names) - len(jpegs)
        self.status_label.config(
            text=f"{'Added' if add else 'Removed'} {', '.join(keywords)} {'to' if add else 'from'} {len(jpegs)} photo(s)."
                 + (f" Skipped {skipped} non-JPEG." if skipped else ""))
        if summary:
            print(summary)


TIMELINE_EXTENSIONS = (".jpg", ".jpeg", ".png")


def timeline_date(path):
    """When a photo was taken, for the timeline: the YYYY-MM-DD_ prefix the sorter
    names files with, else the EXIF date, else a date in the folder names.
    Returns a datetime.date or None."""
    date = parse_date_from_prefix(os.path.basename(path))
    if date:
        return date
    if path.lower().endswith(JPEG_EXTENSIONS):
        try:
            with Image.open(path) as img:
                exif = img.getexif()
                raw = exif.get_ifd(0x8769).get(36867) or exif.get(306)  # DateTimeOriginal, DateTime
            if raw:
                y, m, d = (int(p) for p in str(raw).strip()[:10].split(":"))
                return datetime.date(y, m, d)
        except Exception:
            pass
    return infer_date_from_path(os.path.dirname(path))


class TimelineWindow(tk.Toplevel):
    """Every photo under a folder (searched recursively), in date order, under a
    heading for each month. Thumbnails are drawn only for the rows near the view,
    so a whole library of tens of thousands of photos scrolls without trouble."""

    HEADER_HEIGHT = 34
    MARGIN_ROWS = 4  # rows drawn beyond the view, above and below

    def __init__(self, sorter, folder):
        super().__init__(sorter)
        self.sorter = sorter
        self.title("Timeline")
        self.geometry("1000x760")
        self.cell = sorter.cell_size
        self.items = []         # [{"path", "date"}] in timeline order
        self.rows = []          # [(y, first_item, end_item)] photo rows, top to bottom
        self.row_tops = []      # the y of each row, for bisecting
        self.row_firsts = []    # the first item of each row, for bisecting
        self.year_tops = []     # [(year label, y)] for the year list
        self.closed = False
        self.headers = []       # [(y, label, year)]
        self.columns = 0
        self.drawn = {}         # row index -> canvas item ids
        self.photos = {}        # item index -> PhotoImage (only near the view)
        self.cell_items = {}    # item index -> canvas image id, while its row is drawn
        self.wanted = []        # item indexes waiting for a thumbnail, most urgent last
        self.wanted_lock = threading.Condition()
        self.generation = 0
        self._render_after = None

        bar = tk.Frame(self)
        tk.Label(bar, text="Folder:").pack(side="left")
        self.folder_entry = tk.Entry(bar, width=20)
        self.folder_entry.insert(0, folder)
        self.folder_entry.pack(side="left", fill="x", expand=True, padx=4)
        self.folder_entry.bind("<Return>", lambda e: self.load())
        tk.Button(bar, text="Load", command=self.load).pack(side="left")
        tk.Button(bar, text="Browse...", command=self.browse).pack(side="left", padx=(4, 0))
        bar.pack(side="top", fill="x", padx=6, pady=5)

        self.status = tk.Label(self, anchor="w")
        self.status.pack(side="bottom", fill="x", padx=6, pady=(0, 3))

        body = tk.PanedWindow(self, orient="horizontal", sashwidth=5)
        body.pack(fill="both", expand=True)
        years = tk.Frame(body)
        self.year_list = tk.Listbox(years, activestyle="none", exportselection=False, width=12)
        year_scroll = tk.Scrollbar(years, command=self.year_list.yview)
        self.year_list.configure(yscrollcommand=year_scroll.set)
        year_scroll.pack(side="right", fill="y")
        self.year_list.pack(side="left", fill="both", expand=True)
        self.year_list.bind("<<ListboxSelect>>", self.jump_to_year)
        body.add(years, width=110, minsize=70)

        view = tk.Frame(body)
        self.canvas = tk.Canvas(view, bg="white", highlightthickness=0)
        scrollbar = tk.Scrollbar(view, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=lambda *a: (scrollbar.set(*a), self.schedule_render()),
                              yscrollincrement=10)
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        body.add(view, minsize=300)

        self.canvas.bind("<Configure>", lambda e: self.schedule_render(relayout=True))
        self.canvas.bind("<Double-Button-1>", self.open_viewer)
        self.canvas.bind("<Button-3>", self.show_menu)
        self.canvas.bind("<Motion>", self.show_path)
        self.menu = tk.Menu(self, tearoff=0)

        # The sorter binds these application-wide to act on its own grid; handle
        # them here first and stop them reaching it.
        self.bind("<MouseWheel>", lambda e: self.scroll(int(-e.delta / 120) * self.wheel_step(e)))
        self.bind("<Button-4>", lambda e: self.scroll(-self.wheel_step(e)))
        self.bind("<Button-5>", lambda e: self.scroll(self.wheel_step(e)))
        self.bind("<Prior>", lambda e: self.page(-1))
        self.bind("<Next>", lambda e: self.page(1))
        self.bind("<Home>", lambda e: self.moveto(e, 0.0))
        self.bind("<End>", lambda e: self.moveto(e, 1.0))
        for key in ("<Delete>", "<KeyPress-r>", "<KeyPress-R>", "<Control-x>", "<Control-X>",
                    "<Control-v>", "<Control-V>"):
            self.bind(key, lambda e: "break")
        self.protocol("WM_DELETE_WINDOW", self.close)

        threading.Thread(target=self.thumbnail_worker, daemon=True).start()
        self.load()

    # --- scanning -------------------------------------------------------------

    def browse(self):
        initial = self.folder_entry.get().strip()
        kwargs = {"initialdir": initial} if initial and os.path.isdir(initial) else {}
        folder = filedialog.askdirectory(parent=self, title="Timeline folder", **kwargs)
        if folder:
            self.folder_entry.delete(0, tk.END)
            self.folder_entry.insert(0, folder)
            self.load()

    def load(self):
        folder = self.folder_entry.get().strip()
        if not os.path.isdir(folder):
            self.status.config(text=f"⚠️ Not a folder: {folder}")
            return
        self.generation += 1
        self.title(f"Timeline — {folder}")
        self.items, self.rows, self.row_tops, self.headers = [], [], [], []
        self.clear_drawing()
        self.year_list.delete(0, tk.END)
        self.canvas.configure(scrollregion=(0, 0, 1, 1))
        self.status.config(text="Scanning...")
        threading.Thread(target=self.scan, args=(self.generation, folder), daemon=True).start()

    def scan(self, generation, folder):
        paths = []
        for directory, dirs, files in os.walk(folder):
            if generation != self.generation:
                return
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            paths += [os.path.join(directory, f) for f in sorted(files)
                      if not f.startswith(".") and f.lower().endswith(TIMELINE_EXTENSIONS)]
        items = []
        for n, path in enumerate(paths):
            if generation != self.generation:
                return
            items.append({"path": path, "date": timeline_date(path)})
            if n % 500 == 0:
                self.after(0, self.scan_progress, generation, n, len(paths))
        # Dated photos oldest first, then any with no date at all.
        items.sort(key=lambda d: (d["date"] is None, d["date"] or datetime.date.min, d["path"]))
        self.after(0, self.scan_done, generation, items)

    def scan_progress(self, generation, done, total):
        if generation == self.generation:
            self.status.config(text=f"Reading dates: {done} of {total}...")

    def scan_done(self, generation, items):
        if generation != self.generation:
            return
        self.items = items
        self.columns = 0  # forces a layout
        self.render()
        undated = sum(1 for d in items if d["date"] is None)
        self.status.config(text=f"{len(items)} photo(s)" + (f", {undated} with no date" if undated else "")
                           + ". Double-click to view; right-click to open the folder.")

    # --- layout and drawing ---------------------------------------------------

    def layout(self):
        """Lay the photos out in rows under a heading per month."""
        self.columns = max(1, (self.canvas.winfo_width() - 10) // self.cell)
        self.rows, self.headers = [], []
        y, start = 0, 0
        while start < len(self.items):
            date = self.items[start]["date"]
            month = (date.year, date.month) if date else None
            end = start
            while end < len(self.items) and self.month_of(end) == month:
                end += 1
            label = (datetime.date(*month, 1).strftime("%B %Y") if month else "No date") + f"   ({end - start})"
            self.headers.append((y, label, month[0] if month else None))
            y += self.HEADER_HEIGHT
            for first in range(start, end, self.columns):
                self.rows.append((y, first, min(first + self.columns, end)))
                y += self.cell
            start = end
        self.row_tops = [r[0] for r in self.rows]
        self.row_firsts = [r[1] for r in self.rows]
        self.canvas.configure(scrollregion=(0, 0, self.columns * self.cell + 10, max(y, 1)))

        self.year_list.delete(0, tk.END)
        self.year_tops = []
        for top, _, year in self.headers:
            label = str(year) if year else "No date"
            if not self.year_tops or self.year_tops[-1][0] != label:
                self.year_tops.append((label, top))
                self.year_list.insert(tk.END, label)

    def month_of(self, i):
        date = self.items[i]["date"]
        return (date.year, date.month) if date else None

    def clear_drawing(self):
        self.canvas.delete("all")
        self.drawn.clear()
        self.cell_items.clear()
        self.photos.clear()
        with self.wanted_lock:
            self.wanted.clear()

    def schedule_render(self, relayout=False):
        if relayout:
            self.columns = -1
        if not self._render_after:
            self._render_after = self.after(15, self.render)

    def render(self):
        """Draw the rows in and near the view, drop those far from it, and queue
        thumbnails for the photos that are showing."""
        self._render_after = None
        if not self.items:
            return
        width_columns = max(1, (self.canvas.winfo_width() - 10) // self.cell)
        if self.columns != width_columns:
            # Keep the same photo at the top across a change in columns.
            anchor = self.item_at_y(self.canvas.canvasy(0))
            self.clear_drawing()
            self.layout()
            for y, label, _ in self.headers:
                self.canvas.create_text(8, y + self.HEADER_HEIGHT - 8, text=label, anchor="sw",
                                        font=("TkDefaultFont", 12, "bold"), fill="#234")
                self.canvas.create_line(4, y + self.HEADER_HEIGHT - 4, self.columns * self.cell,
                                        y + self.HEADER_HEIGHT - 4, fill="#bcd")
            if anchor is not None:
                self.show_item(anchor)
        top, bottom = self.canvas.canvasy(0), self.canvas.canvasy(self.canvas.winfo_height())
        first = max(0, bisect.bisect_right(self.row_tops, top) - 1 - self.MARGIN_ROWS)
        last = min(len(self.rows), bisect.bisect_right(self.row_tops, bottom) + self.MARGIN_ROWS)
        for r in [r for r in self.drawn if not first <= r < last]:
            for item_id in self.drawn.pop(r):
                self.canvas.delete(item_id)
            for i in range(self.rows[r][1], self.rows[r][2]):
                self.cell_items.pop(i, None)
                self.photos.pop(i, None)
        visible = []
        for r in range(first, last):
            y, start, end = self.rows[r]
            if r not in self.drawn:
                ids = []
                for i in range(start, end):
                    cx = 5 + (i - start) * self.cell + self.cell // 2
                    cy = y + self.cell // 2
                    ids.append(self.canvas.create_rectangle(cx - 60, cy - 60, cx + 60, cy + 60,
                                                            outline="#ddd", fill="#f3f3f3"))
                    img = self.canvas.create_image(cx, cy)
                    ids.append(img)
                    self.cell_items[i] = img
                self.drawn[r] = ids
            visible += [i for i in range(start, end) if i not in self.photos]
        with self.wanted_lock:
            # Nearest the view are fetched first (popped from the end).
            self.wanted = [i for i in self.wanted if i in self.cell_items and i not in visible]
            mid = (top + bottom) / 2
            visible.sort(key=lambda i: -abs(self.item_y(i) - mid))
            self.wanted += visible
            self.wanted_lock.notify()

    def row_of(self, i):
        return bisect.bisect_right(self.row_firsts, i) - 1

    def item_y(self, i):
        return self.rows[self.row_of(i)][0] + self.cell / 2

    def total_height(self):
        return float(self.canvas.cget("scrollregion").split()[3])

    def item_at_y(self, y):
        if not self.rows:
            return None
        r = max(0, bisect.bisect_right(self.row_tops, y) - 1)
        return self.rows[r][1]

    def show_item(self, i):
        y = self.rows[self.row_of(i)][0]
        self.canvas.yview_moveto(max(0, y - self.HEADER_HEIGHT) / self.total_height())

    def thumbnail_worker(self):
        while True:
            with self.wanted_lock:
                while not self.wanted and not self.closed:
                    self.wanted_lock.wait()
                if self.closed:
                    return
                i = self.wanted.pop()
                generation = self.generation
                try:
                    path = self.items[i]["path"]
                except IndexError:
                    continue
            try:
                img = self.sorter.load_thumbnail(path)
            except Exception as e:
                print(f"⚠️ Timeline could not load {path}: {e}")
                continue
            try:
                self.after(0, self.place_thumbnail, generation, i, img)
            except Exception:
                return  # window closed

    def place_thumbnail(self, generation, i, img):
        if generation != self.generation or i not in self.cell_items:
            return
        photo = ImageTk.PhotoImage(img)
        self.photos[i] = photo
        self.canvas.itemconfig(self.cell_items[i], image=photo)

    # --- interaction ----------------------------------------------------------

    def item_at(self, event):
        x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        r = bisect.bisect_right(self.row_tops, y) - 1
        if r < 0 or y >= self.rows[r][0] + self.cell:
            return None
        i = self.rows[r][1] + int((x - 5) // self.cell)
        return i if x >= 5 and i < self.rows[r][2] else None

    def show_path(self, event):
        i = self.item_at(event)
        if i is not None:
            d = self.items[i]
            self.status.config(text=f"{d['date'] or 'No date'}   {d['path']}")

    def open_viewer(self, event):
        i = self.item_at(event)
        if i is not None:
            self.view(i)

    def show_menu(self, event):
        i = self.item_at(event)
        if i is None:
            return
        path = self.items[i]["path"]
        self.menu.delete(0, tk.END)
        self.menu.add_command(label="View", command=lambda: self.view(i))
        self.menu.add_command(label="Open Folder in Sorter",
                              command=lambda: self.open_in_sorter(os.path.dirname(path)))
        self.menu.tk_popup(event.x_root, event.y_root)

    def view(self, i):
        ImageViewer(self, self.items[i]["path"], [d["path"] for d in self.items])

    def photo_changed(self, path):
        """Redraw a photo's thumbnail after the viewer changes it."""
        for i, d in enumerate(self.items):
            if d["path"] == path and i in self.cell_items:
                self.photos.pop(i, None)
                with self.wanted_lock:
                    self.wanted.append(i)
                    self.wanted_lock.notify()
        changed = getattr(self.sorter, "photo_changed", None)
        if changed:
            changed(path)

    def open_in_sorter(self, folder):
        self.sorter.folder_entry.delete(0, tk.END)
        self.sorter.folder_entry.insert(0, folder)
        self.sorter.load_from_entry()
        self.sorter.lift()

    def jump_to_year(self, event):
        sel = self.year_list.curselection()
        if not sel or not self.year_tops:
            return
        self.canvas.yview_moveto(self.year_tops[sel[0]][1] / self.total_height())

    def wheel_step(self, event):
        return self.sorter.wheel_step(event)

    def scroll(self, units):
        self.canvas.yview_scroll(units, "units")
        return "break"

    def page(self, direction):
        self.canvas.yview_scroll(direction, "pages")
        return "break"

    def moveto(self, event, fraction):
        if not isinstance(event.widget, tk.Entry):
            self.canvas.yview_moveto(fraction)
        return "break"

    def close(self):
        self.generation += 1  # stops a scan in progress
        with self.wanted_lock:
            self.closed = True
            self.wanted.clear()
            self.wanted_lock.notify()
        self.destroy()


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
                self.tree_open = config.get("tree_open", [])
        else:
            self.image_directory = ""
            self.move_destination = ""
            self.subject_keywords = []
            self.saved_prefix = ""
            self.saved_selections = {}
            self.keyword_highlights = []
            self.photo_root = ""
            self.keywords_from_path = False
            self.tree_open = []
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
                "tree_open": self.open_tree_folders(),
            }, f)

    def __init__(self):
        super().__init__()
        self.title("Photo DnD Grid Sorter")
        self.geometry("960x720")
        self.protocol("WM_DELETE_WINDOW", self.on_app_close)
        self.thumb_size = (120, 120)
        self.cell_size = 140  # thumb + selection border + padding
        self.image_directory = ""
        self.max_columns = 6  # follows the grid's width; see relayout_columns
        self.image_data = []
        self.image_files = []
        self.folder_data = []
        self.folder_names = []
        self.grid_row_offset = 0
        self.load_generation = 0
        self.metadata_cache = {}  # path -> (mtime, (date_taken, keywords, (w, h)))
        self.tooltip = None
        self.tooltip_after = None
        self.keywords_window = None
        self.timeline_window = None
        self.grid_directory = None  # folder whose thumbnails are in the grid
        self.selected_widgets = []
        self.last_clicked_label = None
        self.dragged_widget = None
        self._is_dragging = False
        self._folder_drag = None
        self.cut_files = None  # (folder, filenames) waiting for Ctrl-V
        self.saved_view = {}  # scroll positions read for the folder being loaded
        self._tree_click = False  # the folder being opened was clicked in the tree
        self.history = History()
        self.color_busy = False

        self.load_config()
        self.hide_hidden_files_in_dialogs()

        self.create_ui()
        self.folder_entry.insert(0, self.image_directory)
        self.prefix_entry.insert(0, self.saved_prefix)
        #self.start_thumbnail_loading()

    def create_ui(self):
        menubar = tk.Menu(self)
        self.edit_menu = tk.Menu(menubar, tearoff=0, postcommand=self.update_edit_menu)
        self.edit_menu.add_command(label="Undo", accelerator="Ctrl+Z", command=self.undo)
        self.edit_menu.add_command(label="Redo", accelerator="Ctrl+Y", command=self.redo)
        menubar.add_cascade(label="Edit", menu=self.edit_menu)
        menubar.add_command(label="EXIF", command=self.open_keywords_window)
        menubar.add_command(label="Timeline", command=self.open_timeline)
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
        self.canvas.bind("<Configure>", self.on_canvas_resize)

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
        self.bind_all("<Control-z>", self.undo)
        self.bind_all("<Control-Z>", self.redo)  # Ctrl+Shift+Z
        self.bind_all("<Control-y>", self.redo)
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

    def view_file(self, folder):
        return os.path.join(folder, VIEW_FILE)

    def save_view_state(self):
        """Remember where the grid and tree are scrolled, in a hidden file in the
        folder on screen. Skipped mid-load, when the grid isn't all there yet."""
        folder = self.grid_directory
        if not folder or not hasattr(self, "on_disk_order") or not os.path.isdir(folder):
            return
        state = {"grid": round(self.canvas.yview()[0], 5), "tree": round(self.tree.yview()[0], 5)}
        if self.read_view_state(folder) == state:
            return  # unchanged: leave the file (and the folder's mtime) alone
        try:
            with open(self.view_file(folder), "w") as f:
                json.dump(state, f)
        except OSError as e:
            print(f"⚠️ Could not save scroll position: {e}")

    def read_view_state(self, folder):
        try:
            with open(self.view_file(folder)) as f:
                state = json.load(f)
            return state if isinstance(state, dict) else {}
        except (OSError, ValueError):
            return {}

    def clear_grid(self):
        self.save_view_state()
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
        self.save_view_state()
        try:
            self.save_config()
        except Exception as e:
            print(f"⚠️ Could not save settings: {e}")
        self.history.close()
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
        self.tree.bind("<Button-3>", self.show_tree_menu)
        self._tree_drop_item = None
        self._status_before_drop = None
        self._tree_edge_since = None
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

    def open_tree_folders(self):
        """Folders expanded in the tree; saved in the config so they reopen next time."""
        if not hasattr(self, "tree") or not self.tree.get_children(""):
            return self.tree_open  # tree not built yet: keep what was saved
        return sorted(i for i in self.tree_items() if self.tree.item(i, "open"))

    def refresh_tree(self):
        """Rebuild the tree from disk, keeping open the rows that were open (on the
        first build, the ones saved in the config)."""
        opened = set(self.open_tree_folders())
        self.tree.delete(*self.tree.get_children(""))
        self._tree_drop_item = None
        root = self.tree_root()
        if not os.path.isdir(root):
            return
        # The root starts open, and stays closed only if it was closed by hand.
        self.tree.insert("", "end", iid=root, text=f"📁 {os.path.basename(root) or root}",
                         open=root in opened or not opened)
        self.fill_tree_node(root)
        for path in sorted(opened, key=len):  # parents before their children
            if path != root and self.tree.exists(path):
                self.tree.item(path, open=True)
                self.fill_tree_node(path)
        self.reveal_in_tree()

    def restore_tree_view(self, top):
        """Scroll the tree to top (a yview fraction), then make sure the folder on
        screen is in view: rows opened or closed since a position was saved can
        leave it pointing somewhere else."""
        if top is not None:
            self.tree.yview_moveto(top)
        selected = self.tree.selection()
        if selected and not self.tree.bbox(selected[0]):
            self.tree.see(selected[0])

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
            # Name the target in the status line, so it's clear where a drop will land.
            if path:
                if self._status_before_drop is None:
                    self._status_before_drop = self.loading_label.cget("text")
                shown = os.path.relpath(path, self.tree_root())
                shown = os.path.basename(path) if shown.startswith("..") else shown
                self.loading_label.config(text=f"Drop into: {shown}")
            elif self._status_before_drop is not None:
                self.loading_label.config(text=self._status_before_drop)
                self._status_before_drop = None

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
            self.grid_row_offset = self.folder_rows()
            self.add_folder_widgets()
        # Bump the generation so a loader still running for a previous folder stops,
        # and any thumbnails it already queued via after() are discarded.
        self.load_generation += 1
        self.grid_directory = self.image_directory
        # A folder clicked in the tree keeps the tree where it is; otherwise go back
        # to where the tree was when this folder was last open.
        tree_top = self.tree.yview()[0] if self._tree_click else None
        self._tree_click = False
        self.refresh_tree()
        self.saved_view = self.read_view_state(self.image_directory)
        if tree_top is None:
            tree_top = self.saved_view.get("tree")
        self.after_idle(self.restore_tree_view, tree_top)
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

    def folder_rows(self):
        return (len(self.folder_names) + self.max_columns - 1) // self.max_columns

    def on_canvas_resize(self, event):
        # Resizing sends a burst of these; lay out once it settles.
        if getattr(self, "_relayout_after", None):
            self.after_cancel(self._relayout_after)
        self._relayout_after = self.after(80, self.relayout_columns)

    def relayout_columns(self):
        """Fit as many columns as the grid's width holds, keeping the photo at the
        top of the view in view."""
        self._relayout_after = None
        columns = max(1, self.canvas.winfo_width() // self.cell_size)
        if columns == self.max_columns:
            return
        top_row = int(self.canvas.canvasy(0) // self.cell_size) - self.grid_row_offset
        first_visible = max(0, top_row) * self.max_columns
        self.max_columns = columns
        if self.folder_names:
            self.grid_row_offset = self.folder_rows()
        for idx, data in enumerate(self.folder_data):
            self.canvas.coords(data["item"], *self.cell_center(idx // columns, idx % columns))
        self.redraw_grid()
        if top_row > 0 and self.image_data:
            row = self.grid_row_offset + min(first_visible, len(self.image_data) - 1) // columns
            height = max(1, (self.grid_row_offset + (len(self.image_data) + columns - 1) // columns) * self.cell_size)
            self.canvas.yview_moveto(row * self.cell_size / height)

    def refresh_folders(self):
        """Rebuild the folder tiles after one is added, shifting the photos to fit."""
        for data in self.folder_data:
            self.canvas.delete(data["item"])
            data["label"].destroy()
        entries = sorted(e for e in os.listdir(self.image_directory) if not e.startswith("."))
        self.folder_names = self.list_folders(entries)
        self.grid_row_offset = self.folder_rows()
        self.add_folder_widgets()
        self.redraw_grid()
        # Rebuilding the tree scrolls it (to the top, then to the folder on screen);
        # put it back where it was, both now and once the rows are laid out.
        tree_top = self.tree.yview()[0]
        self.refresh_tree()
        self.tree.yview_moveto(tree_top)
        self.after_idle(self.tree.yview_moveto, tree_top)

    def show_grid_menu(self, event):
        if not self.grid_directory:
            return
        self.grid_menu.delete(0, "end")
        photo = next((d for d in self.image_data if d["label"] is event.widget), None)
        if photo:
            path = os.path.join(self.image_directory, photo["filename"])
            self.grid_menu.add_command(label="Show EXIF Data...", command=lambda: MetadataWindow(self, path))
            self.grid_menu.add_separator()
        self.grid_menu.add_command(label="New Folder...", command=self.create_folder)
        folder = self.folder_at(event.x_root, event.y_root)
        if self.selected_widgets:
            self.grid_menu.add_command(label=f"Cut {len(self.selected_widgets)} Photo(s)", accelerator="Ctrl+X",
                                       command=self.cut_selection)
            self.grid_menu.add_command(label=f"Color Balance {len(self.selected_widgets)} Photo(s)",
                                       command=self.color_balance_selection,
                                       state="disabled" if self.color_busy else "normal")
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
            self.add_folder_commands(os.path.join(self.image_directory, folder["name"]))
        self.grid_menu.tk_popup(event.x_root, event.y_root)

    def add_folder_commands(self, path):
        name = os.path.basename(path)
        self.grid_menu.add_command(label=f"Rename Folder '{name}'...", command=lambda: self.rename_folder(path))
        self.grid_menu.add_command(label=f"Delete Folder '{name}'...", command=lambda: self.delete_folder(path))

    def show_tree_menu(self, event):
        """The grid's right-click commands, acting on the tree row clicked."""
        path = self.tree.identify_row(event.y)
        if not path or os.path.basename(path) == TREE_PLACEHOLDER:
            return
        name = os.path.basename(path) or path
        here = path == os.path.normpath(self.image_directory)
        self.grid_menu.delete(0, "end")
        self.grid_menu.add_command(label=f"New Folder in '{name}'...", command=lambda: self.create_folder(path))
        if self.selected_widgets:
            self.grid_menu.add_command(label=f"Cut {len(self.selected_widgets)} Photo(s)", accelerator="Ctrl+X",
                                       command=self.cut_selection)
        if self.cut_files:
            count = len(self.cut_files[1])
            self.grid_menu.add_command(label=f"Paste {count} Photo(s) " + ("Here" if here else f"into '{name}'"),
                                       command=lambda: self.paste_cut(folder_name=path))
        if path != self.tree_root():
            self.add_folder_commands(path)
        self.grid_menu.tk_popup(event.x_root, event.y_root)

    def delete_folder(self, path):
        """Move a folder into .trash in the folder holding it, asking first if it or
        any subfolder holds files other than thumbnail caches, rename/move logs and
        photos already in a .trash of its own."""
        path = os.path.normpath(path)
        parent, name = os.path.dirname(path), os.path.basename(path)
        current = os.path.normpath(self.image_directory)
        showing_it = current == path or current.startswith(path + os.sep)
        if showing_it and not self.check_unsaved_changes():
            return
        photos = folders = others = 0
        for dirpath, dirnames, filenames in os.walk(path):
            # Thumbnail caches and photos already trashed (which go along) aren't contents.
            dirnames[:] = [d for d in dirnames if d not in (THUMB_DIR, TRASH_DIR)]
            folders += len(dirnames)
            for f in filenames:
                if LOG_FILE_RE.match(f) or f == VIEW_FILE:
                    continue  # logs left by older versions of this app
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
            final = move_to_trash(parent, name)
        except Exception as e:
            messagebox.showerror("Delete Folder", f"Could not move '{name}' to {TRASH_DIR}:\n{e}")
            return
        trash = os.path.join(parent, TRASH_DIR)
        self.cut_folder_moved(path, os.path.join(trash, final))
        self.history.record(f"Delete Folder '{name}'", [MoveStep(path, os.path.join(trash, final))])
        if showing_it:  # the folder on screen went with it: show the one that held it
            self.image_directory = parent
            self.folder_entry.delete(0, tk.END)
            self.folder_entry.insert(0, parent)
            self.save_config()
            self.clear_grid()
            self.start_thumbnail_loading()
        else:
            self.refresh_folders()
        self.loading_label.config(text=f"Moved folder '{name}' to {TRASH_DIR}.")

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

    def rename_folder(self, old_path):
        old_path = os.path.normpath(old_path)
        parent, name = os.path.dirname(old_path), os.path.basename(old_path)
        new_name = self.ask_folder_name("Rename Folder", name)
        if not new_name or new_name == name:
            return
        new_path = os.path.join(parent, new_name)
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
        self.history.record(f"Rename Folder '{name}'", [MoveStep(old_path, new_path)])
        self.loading_label.config(text=f"Renamed folder '{name}' to '{new_name}'.")
        self.refresh_folders()

    def create_folder(self, parent=None):
        name = self.ask_folder_name("New Folder")
        if not name:
            return
        parent = parent or self.image_directory
        try:
            os.mkdir(os.path.join(parent, name))
            self.history.record(f"New Folder '{name}'", [MkdirStep(os.path.join(parent, name))])
        except FileExistsError:
            messagebox.showerror("New Folder", f"'{name}' already exists.")
            return
        except OSError as e:
            messagebox.showerror("New Folder", f"Could not create '{name}':\n{e}")
            return
        if self.tree.exists(parent) and not self.tree.item(parent, "open"):
            self.tree.item(parent, open=True)  # show the new folder
            self.fill_tree_node(parent)
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
        """Move photos dragged onto a folder tile or tree row, keeping their names."""
        moved = set(self.move_files(self.image_directory, [d["filename"] for d in group], dest_dir))
        self.remove_moved_from_grid([d for d in group if d["filename"] in moved])
        self.loading_label.config(text=f"Moved {len(moved)} file(s) to {os.path.basename(dest_dir)}.")

    def move_files(self, src_dir, names, dest_dir, label="Move"):
        """Move files between folders keeping their names. Returns the names that moved."""
        moved, steps = [], []
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
            steps.append(MoveStep(old_path, new_path))
            moved.append(name)
        self.history.record(f"{label} {len(moved)} Photo(s) to '{os.path.basename(dest_dir)}'", steps)
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
        """Move the cut photos into the folder on screen, or into folder_name (below it,
        or a full path from the tree)."""
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
        moved = self.move_files(src_dir, present, dest_dir, label="Paste")
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
            shown = "the parent folder" if folder_name == ".." else os.path.basename(dest_dir)
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
        # The grid is only its full height once loaded; don't undo a scroll made meanwhile.
        if "grid" in self.saved_view and self.canvas.yview()[0] == 0:
            self.update_idletasks()
            self.canvas.yview_moveto(self.saved_view["grid"])
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
        lbl.bind("<Double-Button-1>", lambda e, l=lbl: self.open_image_viewer(l))
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

    def open_timeline(self):
        if self.timeline_window and self.timeline_window.winfo_exists():
            self.timeline_window.deiconify()
            self.timeline_window.lift()
            return
        # Start where Move & Rename would: its last destination.
        folder = self.move_destination if self.move_destination and os.path.isdir(self.move_destination) \
            else (self.folder_entry.get().strip() or self.image_directory)
        self.timeline_window = TimelineWindow(self, folder)

    def photo_changed(self, path):
        """Redraw a photo's thumbnail after the viewer changes it."""
        data = next((d for d in self.image_data
                     if os.path.join(self.image_directory, d["filename"]) == path), None)
        if data is None:
            return
        try:
            img = self.load_thumbnail(path)
        except Exception as e:
            print(f"⚠️ Could not reload thumbnail for {path}: {e}")
            return
        photo = ImageTk.PhotoImage(img)
        data["label"].config(image=photo)
        data["label"].image = photo
        data["photo"] = photo

    def update_edit_menu(self):
        for index, label, action in ((0, self.history.undo_label(), "Undo"),
                                     (1, self.history.redo_label(), "Redo")):
            self.edit_menu.entryconfig(index, label=f"{action} {label}" if label else action,
                                       state="normal" if label and not self.color_busy else "disabled")

    def undo(self, event=None):
        return self.run_history(event, undo=True)

    def redo(self, event=None):
        return self.run_history(event, undo=False)

    def run_history(self, event, undo):
        if event is not None and isinstance(event.widget, (tk.Entry, ttk.Entry, tk.Text)):
            return  # Ctrl-Z in a text field keeps its usual meaning
        action = "Undo" if undo else "Redo"
        label = self.history.undo_label() if undo else self.history.redo_label()
        if not label:
            self.loading_label.config(text=f"Nothing to {action.lower()}.")
            return "break"
        if self.color_busy:
            self.loading_label.config(text=f"Wait for the color balance to finish before {action.lower()}ing.")
            return "break"
        # Changing files reloads the grid, which drops a reorder that isn't saved yet.
        if self.is_loading() or not self.check_unsaved_changes():
            return "break"
        loaded = self.grid_directory is not None
        label, steps, touched, problems = self.history.undo() if undo else self.history.redo()

        # Follow folders that moved back (or forward), including the one on screen.
        for step in reversed(steps) if undo else steps:
            if isinstance(step, MoveStep):
                old, new = (step.dst, step.src) if undo else (step.src, step.dst)
                if os.path.isdir(new):
                    self.folder_path_changed(old, new)
        folder = self.image_directory
        while folder and not os.path.isdir(folder) and os.path.dirname(folder) != folder:
            folder = os.path.dirname(folder)
        if folder != self.image_directory:
            self.image_directory = folder
            self.folder_entry.delete(0, tk.END)
            self.folder_entry.insert(0, folder)
        if loaded:
            self.clear_grid()
            self.start_thumbnail_loading()
        else:
            self.refresh_tree()

        timeline = self.timeline_window if self.timeline_window and self.timeline_window.winfo_exists() else None
        for path in set(touched):
            if timeline:
                timeline.photo_changed(path)
            for viewer in list(ImageViewer.open_viewers):
                viewer.file_changed(path)

        done = "Undid" if undo else "Redid"
        self.loading_label.config(text=f"{'↩️' if undo else '↪️'} {done} {label}."
                                  + (f" {len(problems)} step(s) couldn't be done." if problems else ""))
        print(f"{done} {label}")
        if problems:
            for p in problems:
                print(f"⚠️ {p}")
            messagebox.showwarning(action, f"{done} {label}, but {len(problems)} step(s) couldn't be done:\n\n"
                                   + "\n".join(problems[:8]) + ("\n..." if len(problems) > 8 else ""))
        return "break"

    def color_balance_selection(self):
        """Auto colour balance the selected photos in place, one pass each, the
        originals going to .trash. Runs in the background; Undo puts them back."""
        if self.color_busy:
            return
        paths = [os.path.join(self.image_directory, d["filename"])
                 for d in self.image_data if d["label"] in self.selected_widgets]
        if not paths:
            return
        self.color_busy = True
        threading.Thread(target=self.color_balance_thread, args=(paths,), daemon=True).start()

    def color_balance_thread(self, paths):
        done, failed = [], 0
        for n, path in enumerate(paths, 1):
            try:
                trashed = save_color_balanced(path)
                done.append((path, trashed))
                print(f"🎨 Color balanced {path} (original in {TRASH_DIR}/{trashed})")
                self.after(0, self.photo_changed, path)
            except Exception as e:
                failed += 1
                print(f"⚠️ Could not color balance {path}: {e}")
            self.after(0, lambda n=n: self.loading_label.config(
                text=f"Color balancing {n} of {len(paths)}..."))
        self.after(0, self.color_balance_done, done, failed)

    def color_balance_done(self, done, failed):
        self.color_busy = False
        self.history.record(f"Color Balance {len(done)} Photo(s)",
                            [step for path, trashed in done for step in color_balance_steps(path, trashed)])
        text = f"🎨 Color balanced {len(done)} photo(s)."
        if failed:
            text += f" {failed} failed (see terminal)."
        if done:
            text += " Ctrl+Z undoes it."
        self.loading_label.config(text=text)

    def keyword_targets(self):
        """Photos the EXIF window acts on: the selection (in grid order), else all.
        Returns (photos, whether they're a selection)."""
        selected = [d for d in self.image_data if d["label"] in self.selected_widgets]
        return (selected, True) if selected else (list(self.image_data), False)

    def open_image_viewer(self, label):
        # Look up the filename now rather than at load time: renames change it.
        data = next((d for d in self.image_data if d["label"] is label), None)
        if data:
            paths = [os.path.join(self.image_directory, d["filename"]) for d in self.image_data]
            ImageViewer(self, os.path.join(self.image_directory, data["filename"]), paths)

    def delete_selected_thumbnails(self, event=None):
        if not self.selected_widgets:
            messagebox.showwarning(
                title="⚠️ Delete",
                message="Nothing selected!")

            return

        to_delete = [d for d in self.image_data if d["label"] in self.selected_widgets]
        trash = os.path.join(self.image_directory, TRASH_DIR)
        trashed, steps = [], []
        for data in to_delete:
            path = os.path.join(self.image_directory, data["filename"])
            try:
                final = move_to_trash(self.image_directory, data["filename"])
                move_cached_thumb(path, os.path.join(trash, final))
                print(f"🗑️ Moved {path} to {TRASH_DIR}")
            except Exception as e:
                print(f"⚠️ Could not move {path} to {TRASH_DIR}: {e}")
                continue
            steps.append(MoveStep(path, os.path.join(trash, final)))
            trashed.append(data)
        self.remove_moved_from_grid(trashed)
        if trashed:
            self.loading_label.config(text=f"Moved {len(trashed)} photo(s) to {TRASH_DIR}.")
        self.history.record(f"Delete {len(trashed)} Photo(s)", steps)

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
        size_line = ""
        try:
            st = os.stat(path)
            mtime = st.st_mtime
            cached = self.metadata_cache.get(path)
            if cached and cached[0] == mtime:
                date_taken, keywords, dims = cached[1]
            else:
                date_taken, keywords = read_photo_metadata(path)
                dims = image_dimensions(path)
                self.metadata_cache[path] = (mtime, (date_taken, keywords, dims))
            size_line = (f"{dims[0]}×{dims[1]} · " if dims else "") + format_file_size(st.st_size) + "\n"
        except Exception as e:
            date_taken, keywords = None, []
            print(f"⚠️ Could not read metadata for {data['filename']}: {e}")

        text = (f"{data['filename']}\n"
                f"{size_line}"
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
            self.history.record(f"Rotate {data['filename']}", [RotateStep(path, clockwise)])
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
            self._tree_click = not drag["label"]
            self.navigate_to_folder(drag["path"])
            self._tree_click = False  # in case the folder wasn't opened

    def move_folder(self, src, dest_dir):
        """Move a folder into another one."""
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
        self.history.record(f"Move Folder '{name}'", [MoveStep(src, os.path.join(dest_dir, name))])
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
        log_entries, temp_names, steps = [], [], []

        for idx, data in enumerate(self.image_data, start=1):
            old_path = os.path.join(self.image_directory, data["filename"])
            ext = os.path.splitext(data["filename"])[1].lower()
            temp_name = f"_temp_{idx:03d}{ext}"
            temp_path = os.path.join(self.image_directory, temp_name)
            try:
                os.rename(old_path, temp_path)
                move_cached_thumb(old_path, temp_path)
                steps.append(MoveStep(old_path, temp_path))
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
                steps.append(MoveStep(temp_path, final_path))
            except Exception as e:
                print(f"⚠️ Final rename failed: {e}")
                continue
            self.image_data[idx - 1]["filename"] = final_name
            log_entries[idx - 1]["final"] = final_name

            if prefix_date and ext in JPEG_EXTENSIONS:
                try:
                    before = read_jpeg_dates(final_path)
                    set_jpeg_date_taken(final_path, prefix_date)
                    steps.append(ExifDateStep(final_path, before, read_jpeg_dates(final_path)))
                except Exception as e:
                    print(f"⚠️ Could not set EXIF date for {final_name}: {e}")

        self.set_on_disk_order()
        self.cut_files_renamed(self.image_directory,
                               {e["original"]: e["final"] for e in log_entries if "final" in e})
        self.loading_label.config(text=f"File renames complete.")

        self.history.record(f"Rename {len(temp_names)} Photo(s)", steps)

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
        moved, steps = [], []

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
                steps.append(MoveStep(old_path, final_path))
            except Exception as e:
                print(f"⚠️ Move failed for {data['filename']}: {e}")
                continue

            if prefix_date and ext in JPEG_EXTENSIONS:
                try:
                    before = read_jpeg_dates(final_path)
                    set_jpeg_date_taken(final_path, prefix_date)
                    steps.append(ExifDateStep(final_path, before, read_jpeg_dates(final_path)))
                except Exception as e:
                    print(f"⚠️ Could not set EXIF date for {final_name}: {e}")

            moved.append(data)

        self.remove_moved_from_grid(moved)
        self.loading_label.config(text=f"Moved {len(moved)} file(s) to {dest_dir}.")
        self.history.record(f"Move & Rename {len(moved)} Photo(s)", steps)

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

    def auto_scroll(self):
        """Scroll while a drag is held near (or past) the top or bottom edge of the
        grid, repeating on a timer so the pointer needn't keep moving."""
        self._autoscroll_after = None
        if not (getattr(self, "_drag_pressed", False) or getattr(self, "_folder_drag", None)):
            self._tree_edge_since = None
            return
        px, py = self._drag_pointer[0] - self.tree.winfo_rootx(), self._drag_pointer[1] - self.tree.winfo_rooty()
        if 0 <= px < self.tree.winfo_width():  # over the folder tree: scroll it instead
            # Only a thin strip at each end scrolls, and only after the pointer has
            # rested there a moment, slowly: a quick scroll under a pointer aimed at
            # the last visible row would carry the drop into some other folder.
            edge = 10
            step = -1 if py < edge else 1 if py > self.tree.winfo_height() - edge else 0
            if not step:
                self._tree_edge_since = None
                return
            now = time.monotonic()
            if self._tree_edge_since is None:
                self._tree_edge_since = now
            if now - self._tree_edge_since >= 0.6:
                self.tree.yview_scroll(step, "units")
                self.update_drop_highlight()
            self._autoscroll_after = self.after(150, self.auto_scroll)
            return
        self._tree_edge_since = None
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
