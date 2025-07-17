import tkinter as tk
from tkinter import messagebox
from tkinter import filedialog
from PIL import Image, ImageTk
import os, json, threading, datetime
from send2trash import send2trash

CONFIG_PATH = "config.json"

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
        self.image_directory = ""
        self.max_columns = 6
        self.image_data = []
        self.image_files = []
        self.selected_widgets = []
        self.last_clicked_index = None
        self.dragged_widget = None

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
        prefix_frame.pack(pady=5)

        tk.Button(self, text="Preview Rename", command=self.preview_order).pack(pady=2)
        tk.Button(self, text="Rename & Save Order", command=self.save_order).pack(pady=2)
        tk.Button(self, text="Restore From Log", command=self.restore_from_log).pack(pady=2)

        self.loading_label = tk.Label(self, text="Click 'Load' to load thumbnails")
        self.loading_label.pack(pady=5)

        self.canvas = tk.Canvas(self)
        self.frame = tk.Frame(self.canvas)
        self.scrollbar = tk.Scrollbar(self, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.create_window((0, 0), window=self.frame, anchor='nw')
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.configure(yscrollincrement=10)
        self.frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))

        self.canvas.bind_all("<MouseWheel>", self.on_mousewheel)       # Windows & macOS
        self.canvas.bind_all("<Button-4>", self.on_mousewheel_linux)   # Linux scroll up
        self.canvas.bind_all("<Button-5>", self.on_mousewheel_linux)   # Linux scroll down
        self.canvas.bind("<Button-1>", self.clear_selection_on_background)
        self.frame.bind("<Button-1>", self.clear_selection_on_background)  # 👈 Add this
        self.bind_all("<ButtonRelease-1>", self.destroy_drag_cursor)
        self.bind_all("<Delete>", self.delete_selected_thumbnails)        

    def choose_directory(self):
        selected_dir = filedialog.askdirectory()
        if selected_dir:
            self.image_directory = selected_dir
            self.folder_entry.delete(0, tk.END)
            self.folder_entry.insert(0, selected_dir)
            self.save_config()

    def load_from_entry(self):
        path = self.folder_entry.get()
        if os.path.exists(path):
            self.image_directory = path
            self.save_config()
            self.start_thumbnail_loading()

    def set_on_disk_order(self):
        self.on_disk_order = [d["filename"] for d in self.image_data]
        #print(f"Set ODO to: {self.on_disk_order}")

    def on_app_close(self):
        # check for unsaved reordering, if loading is complete
        if hasattr(self, 'on_disk_order'):
            current_order = [f["filename"] for f in self.image_data]
            #print(f"Current: {current_order}")
            #print(f"On-Disk: {self.on_disk_order}")
            if self.on_disk_order != current_order:
                confirm = messagebox.askyesno(
                    "Pending Changes",
                    "You’ve reordered files but haven’t renamed them.\nExit without saving?"
                )
                if not confirm:
                    return

        self.destroy()

    def on_mousewheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def on_mousewheel_linux(self, event):
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
        self.image_files = [f for f in sorted(os.listdir(self.image_directory))
                            if f.lower().endswith((".jpg", ".jpeg", ".png"))]
        self.total_files = len(self.image_files)
        threading.Thread(target=self.load_thumbnails_thread, daemon=True).start()

    def load_thumbnails_thread(self):
        for idx, file in enumerate(self.image_files):
            img_path = os.path.join(self.image_directory, file)
            try:
                img = Image.open(img_path)
                img.thumbnail(self.thumb_size, Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(img)
            except Exception:
                print(f"Failed to open file {file}")
                continue
            try:
                self.after(0, self.add_thumbnail_to_grid, file, photo, idx)
            except Exception:
                pass
        self.after(0, lambda: self.set_on_disk_order())
        self.after(0, lambda: self.loading_label.config(text=f"✅ {self.total_files} thumbnails loaded."))

    def add_thumbnail_to_grid(self, file, photo, idx):
        row = idx // self.max_columns
        column = idx % self.max_columns
        lbl = tk.Label(self.frame, image=photo, bd=2, relief="raised")
        lbl.image = photo
        lbl.grid(row=row, column=column, padx=5, pady=5)

        lbl.bind("<Button-1>", lambda e, l=lbl, i=idx: self.handle_click_and_drag(e, l, i))
        lbl.bind("<B1-Motion>", lambda e, l=lbl: self.handle_drag_motion(e, l))
        lbl.bind("<ButtonRelease-1>", self.finish_drag)

        self.image_data.append({
            "filename": file,
            "photo": photo,
            "label": lbl,
            "row": row,
            "column": column
        })
        self.loading_label.config(text=f"Loading {len(self.image_data)} of {self.total_files}...")

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

    def handle_click_and_drag(self, event, label, index):
        if not hasattr(self, 'on_disk_order'):
            return
        self.dragged_widget = label
        self.handle_click(event, label, index)

    def handle_drag_motion(self, event, label):
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
        x = event.x_root - self.frame.winfo_rootx()
        y = event.y_root - self.frame.winfo_rooty()
        target = None
        for data in self.image_data:
            lbl = data["label"]
            if lbl == self.dragged_widget:
                continue
            if lbl.winfo_x() <= x <= lbl.winfo_x() + lbl.winfo_width() and \
               lbl.winfo_y() <= y <= lbl.winfo_y() + lbl.winfo_height():
                target = lbl
                break
        if target and self.selected_widgets:
            group = [d for d in self.image_data if d["label"] in self.selected_widgets]
            remaining = [d for d in self.image_data if d["label"] not in self.selected_widgets]
            target_idx = next((i for i, d in enumerate(remaining) if d["label"] == target), len(remaining))
            self.image_data = remaining[:target_idx] + group + remaining[target_idx:]
            self.redraw_grid()

    def set_dragged_widget(self, label):
        self.dragged_widget = label
        pass

    def redraw_grid(self):
        row = column = 0
        for data in self.image_data:
            lbl = data["label"]
            lbl.grid(row=row, column=column, padx=5, pady=5)
            data["row"], data["column"] = row, column
            column += 1
            if column >= self.max_columns:
                column = 0
                row += 1

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
        prefix = self.prefix_entry.get().strip()
        if prefix and not prefix.endswith("_"):
            prefix += "_"
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
        self.image_data.clear()
        for widget in self.frame.winfo_children():
            widget.destroy()
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

# Launch the app
app = DragDropSorter()
app.mainloop()
