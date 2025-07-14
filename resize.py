import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext
import os
import json
import time
import threading
from PIL import Image

CONFIG_FILE = "photo_resizer_settings.json"
PHOTO_TYPES = ('.jpg', '.jpeg', '.tiff', '.png')

class PhotoResizerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Photo Resizer")

        # UI state
        self.src_path = tk.StringVar()
        self.dst_path = tk.StringVar()
        self.test_mode = tk.BooleanVar()
        self.going = False
        self.source_list = []

        self.total_input_size = 0
        self.total_output_size = 0
        self.processed = 0

        self.setup_ui()
        self.load_config()

    def setup_ui(self):
        frame = ttk.Frame(self.root)
        frame.pack(padx=10, pady=10)

        ttk.Label(frame, text="Source Directory:").grid(row=0, column=0, sticky='w')
        ttk.Entry(frame, textvariable=self.src_path, width=50).grid(row=0, column=1)
        ttk.Button(frame, text="Browse", command=self.select_src).grid(row=0, column=2)

        ttk.Label(frame, text="Destination Directory:").grid(row=1, column=0, sticky='w')
        ttk.Entry(frame, textvariable=self.dst_path, width=50).grid(row=1, column=1)
        ttk.Button(frame, text="Browse", command=self.select_dst).grid(row=1, column=2)

        ttk.Checkbutton(frame, text="Test Mode", variable=self.test_mode).grid(row=2, column=1, sticky='w')

        self.scan_btn = ttk.Button(frame, text="Scan", command=self.scan)
        self.scan_btn.grid(row=3, column=0, pady=5)

        self.go_btn = ttk.Button(frame, text="Go", command=self.toggle_processing)
        self.go_btn.grid(row=3, column=2, pady=5)

        self.log = scrolledtext.ScrolledText(self.root, width=80, height=20, state='disabled')
        self.log.pack(padx=10, pady=10)

    def select_src(self):
        path = filedialog.askdirectory()
        if path:
            self.src_path.set(path)
            self.save_config()

    def select_dst(self):
        path = filedialog.askdirectory()
        if path:
            self.dst_path.set(path)
            self.save_config()

    def log_message(self, message):
        self.log.config(state='normal')
        self.log.insert(tk.END, message + '\n')
        self.log.see(tk.END)
        self.log.config(state='disabled')
        self.root.update_idletasks()

    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                cfg = json.load(f)
                self.src_path.set(cfg.get("source", ""))
                self.dst_path.set(cfg.get("destination", ""))

    def save_config(self):
        with open(CONFIG_FILE, 'w') as f:
            json.dump({
                "source": self.src_path.get(),
                "destination": self.dst_path.get()
            }, f)

    def scan(self):
        source_dir = self.src_path.get()
        if not os.path.isdir(source_dir):
            self.log_message("Invalid source directory.")
            return

        self.log_message("Scanning for image files...")
        self.source_list.clear()
        self.total_input_size = 0

        for root_dir, _, files in os.walk(source_dir):
            for file in files:
                if file.lower().endswith(PHOTO_TYPES):
                    full_path = os.path.join(root_dir, file)
                    rel_path = os.path.relpath(full_path, source_dir)
                    self.source_list.append(rel_path)
                    self.total_input_size += os.path.getsize(full_path)

        mb = self.total_input_size / (1024 * 1024)
        self.log_message(f"Found {len(self.source_list)} image(s). Total size: {mb:.2f} MB")

    def toggle_processing(self):
        if not self.going:
            self.going = True
            self.go_btn.config(text="Stop")
            threading.Thread(target=self.process_images, daemon=True).start()
        else:
            self.going = False
            self.log_message("Stop requested...")

    def process_images(self):
        if not self.source_list:
            self.log_message("No files to process. Please scan first.")
            self.going = False
            self.go_btn.config(text="Go")
            return

        self.total_output_size = 0
        self.processed = 0
        test = self.test_mode.get()
        src_root = self.src_path.get()
        dst_root = self.dst_path.get()
        last_log = time.time()

        for rel_path in self.source_list:
            if not self.going:
                break

            try:
                src_file = os.path.join(src_root, rel_path)
                with Image.open(src_file) as img:
                    w, h = img.size
                    if w > h:
                        new_h = 1080
                        new_w = int(w * (1080 / h))
                    else:
                        new_w = 1080
                        new_h = int(h * (1080 / w))

                    self.log_message(f"{rel_path}: {w}x{h} -> {new_w}x{new_h}")

                    if not test:
                        resized = img.resize((new_w, new_h), Image.LANCZOS)
                        dest_file = os.path.join(dst_root, rel_path)
                        os.makedirs(os.path.dirname(dest_file), exist_ok=True)
                        resized.save(dest_file, "JPEG", quality=95)
                        self.total_output_size += os.path.getsize(dest_file)

                    self.processed += 1

                    if time.time() - last_log > 1:
                        self.log_message(f"Processed {self.processed} file(s)...")
                        last_log = time.time()

            except Exception as e:
                self.log_message(f"Error: {rel_path} — {e}")

        self.log_message("Processing complete." if self.going else "Processing stopped.")
        self.log_message(f"Files processed: {self.processed}")
        self.log_message(f"Total input: {self.total_input_size / (1024*1024):.2f} MB")
        if not test:
            self.log_message(f"Total output: {self.total_output_size / (1024*1024):.2f} MB")

        self.going = False
        self.go_btn.config(text="Go")
        
    def auto_scroll(self, event):
        SCROLL_MARGIN = 30
        SCROLL_SPEED = 3
        canvas_y = event.y_root - self.canvas.winfo_rooty()
        canvas_height = self.canvas.winfo_height()
        if canvas_y < SCROLL_MARGIN:
            self.canvas.yview_scroll(-SCROLL_SPEED, "units")
        elif canvas_y > canvas_height - SCROLL_MARGIN:
            self.canvas.yview_scroll(SCROLL_SPEED, "units")
        
if __name__ == "__main__":
    root = tk.Tk()
    app = PhotoResizerApp(root)
    root.mainloop()

