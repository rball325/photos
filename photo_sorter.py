import tkinter as tk
from tkinter import messagebox
from tkinter import filedialog
from PIL import Image, ImageTk, IptcImagePlugin
import os, json, threading, datetime, calendar, re, math, stat
import xml.etree.ElementTree as ET
from send2trash import send2trash
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


def set_jpeg_date_taken(path, date):
    """Write date into JPEG EXIF date tags by editing only the EXIF segment
    in place -- the compressed image data is never touched/re-encoded."""
    exif_date = date.strftime("%Y:%m:%d 00:00:00")
    try:
        exif_dict = piexif.load(path)
    except Exception:
        exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
    exif_dict["0th"][piexif.ImageIFD.DateTime] = exif_date
    exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = exif_date
    exif_dict["Exif"][piexif.ExifIFD.DateTimeDigitized] = exif_date
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
                    tk.Button(self.cal_frame, text=str(day), width=3,
                              command=lambda d=day: self._pick_day(d)).grid(row=row, column=col)

    def _pick_day(self, day):
        self.result = self._date.replace(day=day)
        self.destroy()


class ImageViewer(tk.Toplevel):
    def __init__(self, master, image_path):
        super().__init__(master)
        self.title(os.path.basename(image_path))
        self.geometry("800x600")
        self._image = Image.open(image_path)
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


class DragDropSorter(tk.Tk):

    def load_config(self):
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
                self.image_directory = config.get("image_directory", "")
        else:
            self.image_directory = ""

    def save_config(self):
        with open(CONFIG_PATH, "w") as f:
            json.dump({"image_directory": self.image_directory}, f)

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
        self.selected_widgets = []
        self.last_clicked_index = None
        self.dragged_widget = None
        self._is_dragging = False

        self.load_config()

        self.create_ui()
        self.folder_entry.insert(0, self.image_directory)
        #self.start_thumbnail_loading()

    def create_ui(self):
        source_frame = tk.Frame(self)
        
        self.folder_label = tk.Label(source_frame, text="Image Folder:")
        self.folder_label.pack(side="left")

        self.folder_entry = tk.Entry(source_frame, width=50)
        self.folder_entry.pack(side="left", expand=True, fill="x")

        self.load_button = tk.Button(source_frame, text="Load", command=self.load_from_entry)
        self.load_button.pack(side="left")

        self.browse_button = tk.Button(source_frame, text="Browse...", command=self.choose_directory)
        self.browse_button.pack(side="left")

        source_frame.pack(pady=5)

        prefix_frame = tk.Frame(self)
        tk.Label(prefix_frame, text="Filename Prefix:").pack(side="left")
        self.prefix_entry = tk.Entry(prefix_frame)
        self.prefix_entry.pack(side="left", padx=5)
        tk.Button(prefix_frame, text="📅", command=self.choose_date_prefix).pack(side="left")
        tk.Button(prefix_frame, text="Auto", command=self.auto_set_prefix).pack(side="left", padx=(5, 0))
        prefix_frame.pack(pady=5)

        tk.Button(self, text="Preview Rename", command=self.preview_order).pack(pady=2)
        tk.Button(self, text="Rename & Save Order", command=self.save_order).pack(pady=2)
        tk.Button(self, text="Restore From Log", command=self.restore_from_log).pack(pady=2)

        self.loading_label = tk.Label(self, text="Click 'Load' to load thumbnails")
        self.loading_label.pack(pady=5)

        # Thumbnails are placed directly on the canvas as window items rather than
        # gridded into one inner Frame: X11 caps window size at 32767px, and a Frame
        # tall enough for ~1500 thumbnails made Tk abort with BadAlloc.
        self.canvas = tk.Canvas(self)
        self.scrollbar = tk.Scrollbar(self, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.configure(yscrollincrement=10)

        self.canvas.bind_all("<MouseWheel>", self.on_mousewheel)       # Windows & macOS
        self.canvas.bind_all("<Button-4>", self.on_mousewheel_linux)   # Linux scroll up
        self.canvas.bind_all("<Button-5>", self.on_mousewheel_linux)   # Linux scroll down
        self.canvas.bind("<Button-1>", self.clear_selection_on_background)
        self.bind_all("<ButtonRelease-1>", self.destroy_drag_cursor)
        self.bind_all("<Delete>", self.delete_selected_thumbnails)        

    def choose_date_prefix(self):
        dialog = DatePickerDialog(self)
        self.wait_window(dialog)
        if dialog.result:
            self.prefix_entry.delete(0, tk.END)
            self.prefix_entry.insert(0, dialog.result.strftime("%Y-%m-%d") + "_")

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
        self.last_clicked_index = None
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
        self.destroy()

    def on_mousewheel(self, event):
        self.hide_tooltip()  # the grid moves under the pointer without a <Leave>
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def on_mousewheel_linux(self, event):
        self.hide_tooltip()
        if event.num == 4:
            self.canvas.yview_scroll(-3, "units")
        elif event.num == 5:
            self.canvas.yview_scroll(3, "units")

    def clear_selection_on_background(self, event):
        widget = self.winfo_containing(event.x_root, event.y_root)
        # If not one of the image thumbnails, clear selection
        if widget not in [d["label"] for d in self.image_data]:
            for lbl in self.selected_widgets:
                lbl.config(relief="raised", bd=2)
            self.selected_widgets.clear()
            self.last_clicked_index = None

    def start_thumbnail_loading(self):
        entries = sorted(os.listdir(self.image_directory))
        self.folder_names = [d for d in entries
                              if os.path.isdir(os.path.join(self.image_directory, d))]
        parent = os.path.dirname(self.image_directory.rstrip(os.sep))
        if parent and parent != self.image_directory.rstrip(os.sep):
            self.folder_names = [".."] + self.folder_names
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
            self.canvas.create_window(*self.cell_center(row, column), window=lbl)
            lbl.bind("<Button-1>", lambda e, n=name: self.navigate_to_folder(n))
            self.folder_data.append({"name": name, "label": lbl})
        self.update_scrollregion()

    def load_thumbnails_thread(self, generation, directory, files):
        for idx, file in enumerate(files):
            if generation != self.load_generation:
                return
            img_path = os.path.join(directory, file)
            try:
                img = Image.open(img_path)
                img.thumbnail(self.thumb_size, Image.Resampling.LANCZOS)
                img.load()
            except Exception as e:
                print(f"Failed to open file {file}: {e}")
                continue
            try:
                self.after(0, self.add_thumbnail_to_grid, generation, file, img, idx)
            except Exception:
                pass
        self.after(0, self.finish_loading, generation)

    def finish_loading(self, generation):
        if generation != self.load_generation:
            return
        self.set_on_disk_order()
        self.loading_label.config(text=f"✅ {self.total_files} thumbnails loaded.")

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

        lbl.bind("<Button-1>", lambda e, l=lbl, i=idx: self.handle_click_and_drag(e, l, i))
        lbl.bind("<Double-Button-1>", lambda e, f=file: self.open_image_viewer(f))
        lbl.bind("<B1-Motion>", lambda e, l=lbl: self.handle_drag_motion(e, l))
        lbl.bind("<ButtonRelease-1>", self.finish_drag)
        lbl.bind("<Enter>", lambda e, l=lbl: self.schedule_tooltip(l))
        lbl.bind("<Leave>", self.hide_tooltip)

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
        for data in to_delete:
            path = os.path.join(self.image_directory, data["filename"])
            try:
                send2trash(path)
                print(f"🗑️ Deleted {path}")
            except Exception as e:
                print(f"⚠️ Could not delete {path}: {e}")
                continue
            self.canvas.delete(data["item"])
            data["label"].destroy()
            self.image_data.remove(data)

        self.selected_widgets.clear()
        self.last_clicked_index = None
        self.set_on_disk_order()
        self.redraw_grid()

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

    def handle_click_and_drag(self, event, label, index):
        self.hide_tooltip()
        if not hasattr(self, 'on_disk_order'):
            return
        self.dragged_widget = label
        self.handle_click(event, label, index)

    def handle_drag_motion(self, event, label):
        self._is_dragging = True
        self.dragged_widget = label  # Prep drag target
        self.create_drag_cursor(event)  # Show drag visual
        self.auto_scroll(event)        # Scroll if near edge
        self.move_drag_cursor(event)   # Move overlay

    def handle_click(self, event, label, index):
        ctrl_pressed = (event.state & 0x0004) != 0
        shift_pressed = (event.state & 0x0001) != 0

        if ctrl_pressed:
            if label in self.selected_widgets:
                self.selected_widgets.remove(label)
                label.config(relief="raised", bd=2)
            else:
                self.selected_widgets.append(label)
                label.config(relief="solid", bd=4, highlightbackground="blue")
            self.last_clicked_index = index

        elif shift_pressed:
            if self.last_clicked_index is not None:
                i1, i2 = sorted((self.last_clicked_index, index))
                for i in range(i1, i2 + 1):
                    lbl = self.image_data[i]["label"]
                    if lbl not in self.selected_widgets:
                        self.selected_widgets.append(lbl)
                        lbl.config(relief="solid", bd=4, highlightbackground="blue")
            else:
                self.selected_widgets = [label]
                label.config(relief="solid", bd=4, highlightbackground="blue")
                self.last_clicked_index = index

        else:
            # If already selected, don’t reset — just prep for drag
            if label in self.selected_widgets:
                self.last_clicked_index = index
            else:
                for lbl in self.selected_widgets:
                    lbl.config(relief="raised", bd=2)
                self.selected_widgets = [label]
                label.config(relief="solid", bd=4, highlightbackground="blue")
                self.last_clicked_index = index

    def finish_drag(self, event):
        self.canvas.unbind("<Motion>")
        self.destroy_drag_cursor(event)
        # Hit-test against the fixed cell layout in canvas coordinates; widget
        # winfo_x/y aren't kept current for items scrolled out of view.
        x = self.canvas.canvasx(event.x_root - self.canvas.winfo_rootx())
        y = self.canvas.canvasy(event.y_root - self.canvas.winfo_rooty())
        target = None
        column = int(x // self.cell_size)
        position = (int(y // self.cell_size) - self.grid_row_offset) * self.max_columns + column
        if 0 <= column < self.max_columns and 0 <= position < len(self.image_data):
            lbl = self.image_data[position]["label"]
            if lbl != self.dragged_widget:
                target = lbl
        if self.selected_widgets:
            group = [d for d in self.image_data if d["label"] in self.selected_widgets]
            remaining = [d for d in self.image_data if d["label"] not in self.selected_widgets]
            if target:
                target_idx = next((i for i, d in enumerate(remaining) if d["label"] == target), len(remaining))
            elif self._is_dragging:
                target_idx = len(remaining)  # drop past last item → append to end
            else:
                target_idx = None
            if target_idx is not None:
                self.image_data = remaining[:target_idx] + group + remaining[target_idx:]
                self.redraw_grid()
        self._is_dragging = False

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

    def restore_from_log(self):
        logs = [f for f in os.listdir(self.image_directory) if f.startswith("rename_log_") and f.endswith(".json")]
        if not logs:
            messagebox.showerror("Restore Error", "No rename logs found.")
            return
        logs.sort(reverse=True)
        log_path = os.path.join(self.image_directory, logs[0])
        try:
            with open(log_path, "r") as f:
                log = json.load(f)
        except Exception:
            messagebox.showerror("Error", f"Could not read {logs[0]}")
            return
        if not messagebox.askyesno("Confirm Restore", f"Restore original filenames from {logs[0]}?"):
            return

        restored = 0
        for entry in log.get("files", []):
            final = os.path.join(self.image_directory, entry.get("final", entry["temporary"]))
            original = os.path.join(self.image_directory, entry["original"])
            if os.path.exists(original) and final != original:
                overwrite = messagebox.askyesno("Conflict Warning",
                    f"File '{entry['original']}' already exists.\nOverwrite?")
                if not overwrite:
                    continue
            try:
                os.rename(final, original)
                restored += 1
            except Exception as e:
                print(f"⚠️ Could not restore {entry['final']}: {e}")
        messagebox.showinfo("Restore Complete", f"Restored {restored} files from {logs[0]}")
        self.clear_grid()
        self.start_thumbnail_loading()

    def auto_scroll(self, event):
        canvas_height = self.canvas.winfo_height()
        canvas_top = self.canvas.winfo_rooty()
        canvas_bottom = canvas_top + canvas_height
        mouse_y = event.y_root

        distance_from_top = mouse_y - canvas_top
        distance_from_bottom = canvas_bottom - mouse_y
        edge_threshold = canvas_height / 6

        speed = 0
        if distance_from_top < edge_threshold:
            speed = -int((edge_threshold - distance_from_top) / 60)
        elif distance_from_bottom < edge_threshold:
            speed = int((edge_threshold - distance_from_bottom) / 60)

        if speed != 0:
            self.canvas.yview_scroll(speed, "units")

if __name__ == "__main__":
    app = DragDropSorter()
    app.mainloop()
