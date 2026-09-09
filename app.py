import os
import re
import sys
import time
import io
import threading
import subprocess
import concurrent.futures
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import yt_dlp
import requests
from PIL import Image, ImageTk

# Metadata Tagging
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, APIC


def find_ffmpeg():
    """Locates ffmpeg.exe either in the bundled PyInstaller directory or current path."""
    if getattr(sys, 'frozen', False):
        base_dir = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))

    ffmpeg_path = os.path.join(base_dir, "ffmpeg.exe")
    if os.path.exists(ffmpeg_path):
        return ffmpeg_path
    
    return "ffmpeg"


def clean_filename(title):
    """Cleans YouTube titles by removing video clutter and illegal Windows characters."""
    junk_patterns = [
        r'[\(\[\{]\s*(official\s*(music\s*)?video|lyric\s*video|lyrics|audio|4k|hd|visualizer|remastered)\s*[\)\]\}]',
        r'-\s*Topic$',
        r'\|.*$',
    ]
    
    clean_title = title
    for pattern in junk_patterns:
        clean_title = re.sub(pattern, '', clean_title, flags=re.IGNORECASE)

    clean_title = re.sub(r'[\\/:*?"<>|]', '', clean_title)
    clean_title = re.sub(r'\s+', ' ', clean_title).strip()
    return clean_title if clean_title else "Audio_Track"


def format_bytes(bytes_num):
    """Formats bytes into human-readable strings (KB, MB, GB)."""
    if not bytes_num or bytes_num <= 0:
        return "0 MB"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_num < 1024.0:
            return f"{bytes_num:.2f} {unit}"
        bytes_num /= 1024.0
    return f"{bytes_num:.2f} TB"


class YtDlpLogger:
    """Redirects yt-dlp log messages to the terminal widget."""
    def __init__(self, terminal_log_func):
        self.terminal_log_func = terminal_log_func

    def debug(self, msg):
        self.terminal_log_func(msg)

    def info(self, msg):
        self.terminal_log_func(msg)

    def warning(self, msg):
        self.terminal_log_func(f"WARNING: {msg}")

    def error(self, msg):
        self.terminal_log_func(f"ERROR: {msg}")


class SongDownloaderApp:
    def __init__(self, root):
        self.root = root
        self.root.title("SongDownloader Pro")
        # Make window resizable and start maximized
        self.root.resizable(True, True)
        self._maximize_window()

        self.ffmpeg_path = find_ffmpeg()
        self.is_downloading = False
        self.song_data = []
        self.is_collapsed = False

        # Live speed & download size tracking variables across threads
        self.active_speeds = {}
        self.total_downloaded_bytes = 0
        self.lock = threading.Lock()

        # Logger for yt-dlp raw output
        self.ytdlp_logger = YtDlpLogger(self._terminal_log)

        # UI State Variables
        self.input_source = tk.StringVar()
        self.search_query = tk.StringVar()
        self.search_limit = tk.StringVar(value="5")
        self.out_dir = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Downloads"))
        self.bitrate = tk.StringVar(value="320")
        self.max_threads = tk.StringVar(value="3")
        self.select_all_mp3_var = tk.BooleanVar(value=False)
        self.select_all_wav_var = tk.BooleanVar(value=False)
        
        # Summary bar readout variables
        self.est_size_str = tk.StringVar(value="Total Selected Est: 0 MB")
        self.live_stats_str = tk.StringVar(value="Downloaded: 0 MB | Speed: 0 KB/s")

        self._build_ui()

    def _maximize_window(self):
        """Maximize the window on startup (cross‑platform)."""
        if sys.platform == "win32":
            self.root.state('zoomed')
        elif sys.platform == "linux":
            self.root.attributes('-zoomed', True)
        else:  # macOS or fallback
            # Use screen dimensions to set geometry
            screen_width = self.root.winfo_screenwidth()
            screen_height = self.root.winfo_screenheight()
            self.root.geometry(f"{screen_width}x{screen_height}")

    def _build_ui(self):
        # 1. Direct YouTube Search Frame
        frame_search = tk.LabelFrame(self.root, text=" 1. Search YouTube Directly ", padx=10, pady=6)
        frame_search.pack(fill="x", padx=15, pady=4)

        tk.Entry(frame_search, textvariable=self.search_query, width=48).pack(side="left", padx=5)
        tk.Label(frame_search, text="Results:").pack(side="left", padx=(5, 2))
        ttk.Combobox(frame_search, textvariable=self.search_limit, values=["5", "10", "15"], width=4, state="readonly").pack(side="left", padx=(0, 5))
        tk.Button(frame_search, text="Search YT", bg="#007bff", fg="white", font=("Segoe UI", 9, "bold"), command=self.search_youtube).pack(side="left", padx=3)

        # 2. Playlist / Video / .txt File Frame
        frame_file = tk.LabelFrame(self.root, text=" 2. Or Fetch via URL or .txt File ", padx=10, pady=6)
        frame_file.pack(fill="x", padx=15, pady=4)

        tk.Entry(frame_file, textvariable=self.input_source, width=48).pack(side="left", padx=5)
        tk.Button(frame_file, text="Browse .txt", command=self._browse_file).pack(side="left", padx=2)
        tk.Button(frame_file, text="Fetch", bg="#17a2b8", fg="white", font=("Segoe UI", 9, "bold"), command=self.fetch_songs).pack(side="left", padx=3)
        tk.Button(frame_file, text="Clear List", bg="#dc3545", fg="white", font=("Segoe UI", 9, "bold"), command=self.clear_songs).pack(side="left", padx=3)

        # 3. Settings Frame
        frame_opts = tk.LabelFrame(self.root, text=" 3. Settings ", padx=10, pady=6)
        frame_opts.pack(fill="x", padx=15, pady=4)

        tk.Label(frame_opts, text="Save Folder:").grid(row=0, column=0, sticky="w", padx=5)
        tk.Entry(frame_opts, textvariable=self.out_dir, width=52).grid(row=0, column=1, padx=5, pady=2, columnspan=2)
        tk.Button(frame_opts, text="Browse", command=self._browse_folder).grid(row=0, column=3, padx=5)

        tk.Label(frame_opts, text="MP3 Bitrate:").grid(row=1, column=0, sticky="w", padx=5, pady=4)
        ttk.Combobox(frame_opts, textvariable=self.bitrate, values=["320", "192", "128"], width=8, state="readonly").grid(row=1, column=1, sticky="w", padx=5, pady=4)

        tk.Label(frame_opts, text="Parallel Downloads:").grid(row=1, column=2, sticky="e", padx=5, pady=4)
        ttk.Combobox(frame_opts, textvariable=self.max_threads, values=["1", "2", "3", "5"], width=4, state="readonly").grid(row=1, column=3, sticky="w", padx=5, pady=4)

        self.update_btn = tk.Button(frame_opts, text="Update Core Engine", command=self.update_ytdlp)
        self.update_btn.grid(row=2, column=3, sticky="e", padx=5, pady=4)

        # 4. Scrollable Song Selection List Container
        frame_list_container = tk.LabelFrame(self.root, text=" 4. Select Songs & Formats ", padx=5, pady=5)
        frame_list_container.pack(fill="both", expand=True, padx=15, pady=4)

        # Master Controls Row
        frame_master = tk.Frame(frame_list_container)
        frame_master.pack(fill="x", padx=5, pady=2)

        self.btn_collapse = tk.Button(frame_master, text="Collapse Unselected", bg="#6c757d", fg="white", font=("Segoe UI", 8, "bold"), command=self.toggle_collapse)
        self.btn_collapse.pack(side="left", padx=5)

        tk.Checkbutton(frame_master, text="Select All WAV", variable=self.select_all_wav_var, command=self._toggle_all_wav, font=("Segoe UI", 8, "bold")).pack(side="right", padx=10)
        tk.Checkbutton(frame_master, text="Select All MP3", variable=self.select_all_mp3_var, command=self._toggle_all_mp3, font=("Segoe UI", 8, "bold")).pack(side="right", padx=10)

        # Scrollable Canvas
        self.canvas = tk.Canvas(frame_list_container, bg="#ffffff", highlightthickness=1, highlightbackground="#ccc")
        scrollbar = ttk.Scrollbar(frame_list_container, orient="vertical", command=self.canvas.yview)
        
        self.scrollable_frame = tk.Frame(self.canvas, bg="#ffffff")
        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )

        self.canvas_window = self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)

        # Bind canvas resize to update inner frame width
        self.canvas.bind('<Configure>', lambda e: self.canvas.itemconfig(self.canvas_window, width=e.width))

        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # 5. Live Summary Status Bar
        frame_summary = tk.Frame(self.root, bg="#e9ecef", padx=10, pady=5)
        frame_summary.pack(fill="x", padx=15, pady=2)

        tk.Label(frame_summary, textvariable=self.est_size_str, bg="#e9ecef", fg="#333333", font=("Segoe UI", 9, "bold")).pack(side="left")
        tk.Label(frame_summary, textvariable=self.live_stats_str, bg="#e9ecef", fg="#007bff", font=("Segoe UI", 9, "bold")).pack(side="right")

        # 6. Action Button
        self.btn_download = tk.Button(
            self.root, 
            text="Start Download", 
            bg="#28a745", 
            fg="white", 
            font=("Segoe UI", 10, "bold"),
            command=self.start_download
        )
        self.btn_download.pack(fill="x", padx=15, pady=4)

        # 7. Log Box (friendly messages)
        frame_log = tk.LabelFrame(self.root, text=" Logs ", padx=5, pady=5)
        frame_log.pack(fill="x", padx=15, pady=4)

        self.log_text = tk.Text(frame_log, state="disabled", wrap="word", height=5, bg="#1e1e1e", fg="#00ff00", font=("Consolas", 8))
        self.log_text.pack(fill="both", expand=True)

        # 8. Process Terminal (raw background output)
        frame_terminal = tk.LabelFrame(self.root, text=" Process Terminal ", padx=5, pady=5)
        frame_terminal.pack(fill="both", expand=True, padx=15, pady=4)

        self.terminal_text = tk.Text(frame_terminal, state="disabled", wrap="word",
                                     bg="#1e1e1e", fg="#cccccc",
                                     font=("Consolas", 8))
        terminal_scrollbar = ttk.Scrollbar(frame_terminal, orient="vertical",
                                           command=self.terminal_text.yview)
        self.terminal_text.configure(yscrollcommand=terminal_scrollbar.set)

        self.terminal_text.pack(side="left", fill="both", expand=True)
        terminal_scrollbar.pack(side="right", fill="y")

    def _log(self, message):
        """Thread‑safe logging to the user‑friendly log box."""
        self.root.after(0, lambda: self._append_to_log(message))

    def _append_to_log(self, message):
        self.log_text.config(state="normal")
        self.log_text.insert("end", f"{message}\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _terminal_log(self, message):
        """Thread‑safe logging to the raw process terminal."""
        self.root.after(0, lambda: self._append_to_terminal(message))

    def _append_to_terminal(self, message):
        self.terminal_text.config(state="normal")
        self.terminal_text.insert("end", message + "\n")
        self.terminal_text.see("end")
        self.terminal_text.config(state="disabled")

    def _browse_file(self):
        filename = filedialog.askopenfilename(filetypes=[("Text Files", "*.txt")])
        if filename:
            self.input_source.set(filename)

    def _browse_folder(self):
        folder = filedialog.askdirectory()
        if folder:
            self.out_dir.set(folder)

    def _toggle_all_mp3(self):
        state = self.select_all_mp3_var.get()
        for item in self.song_data:
            item["mp3_var"].set(state)
        self._recalculate_total_est_size()

    def _toggle_all_wav(self):
        state = self.select_all_wav_var.get()
        for item in self.song_data:
            item["wav_var"].set(state)
        self._recalculate_total_est_size()

    def _recalculate_total_est_size(self):
        total_mb = 0.0
        for item in self.song_data:
            has_wav = item["wav_var"].get()
            has_mp3 = item["mp3_var"].get()

            if has_wav:
                total_mb += item["meta"]["est_wav"]
            elif has_mp3:
                total_mb += item["meta"]["est_mp3"]

        self.est_size_str.set(f"Total Selected Est: ~{round(total_mb, 1)} MB")

    def clear_songs(self):
        for widget in self.scrollable_frame.winfo_children():
            widget.destroy()
        self.song_data.clear()
        self.select_all_mp3_var.set(False)
        self.select_all_wav_var.set(False)
        self.is_collapsed = False
        self.btn_collapse.config(text="Collapse Unselected", bg="#6c757d")
        self._recalculate_total_est_size()
        self._log("Song list cleared.")

    def _remove_single_song(self, item_dict):
        item_dict["row_frame"].destroy()
        if item_dict in self.song_data:
            self.song_data.remove(item_dict)
        self._recalculate_total_est_size()
        self._log(f"Removed '{item_dict['meta']['title']}' from list.")

    def toggle_collapse(self):
        if not self.song_data:
            return

        self.is_collapsed = not self.is_collapsed

        if self.is_collapsed:
            self.btn_collapse.config(text="Show All Tracks", bg="#007bff")
            hidden_count = 0
            for item in self.song_data:
                is_selected = item["mp3_var"].get() or item["wav_var"].get()
                if not is_selected:
                    item["row_frame"].pack_forget()
                    hidden_count += 1
            self._log(f"Collapsed view: Hiding {hidden_count} unselected tracks.")
        else:
            self.btn_collapse.config(text="Collapse Unselected", bg="#6c757d")
            for item in self.song_data:
                item["row_frame"].pack(fill="x", expand=True)
            self._log("Expanded view: Showing all tracks.")

    # --- Search & Fetch ---
    def search_youtube(self):
        query = self.search_query.get().strip()
        if not query:
            messagebox.showerror("Error", "Please enter a search query.")
            return

        limit = self.search_limit.get()
        self._log(f"\n--- Searching YouTube for: '{query}' (Top {limit} results) ---")
        self._terminal_log(f"--- Searching YouTube for: '{query}' (Top {limit} results) ---")
        self.root.config(cursor="watch")

        def worker():
            search_url = f"ytsearch{limit}:{query}"
            ydl_opts = {"quiet": True, "skip_download": True, "logger": self.ytdlp_logger}
            extracted_items = []
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(search_url, download=False)
                    if "entries" in info:
                        for entry in info["entries"]:
                            extracted_items.append(self._parse_track_info(entry, entry.get('url')))
            except Exception as e:
                self._log(f"Error executing search: {str(e)}")

            self.root.after(0, lambda: self._append_to_list(extracted_items))

        threading.Thread(target=worker, daemon=True).start()

    def fetch_songs(self):
        source = self.input_source.get().strip()
        if not source:
            messagebox.showerror("Error", "Please enter a Playlist/Video URL or select a .txt file.")
            return

        self._log("\n--- Fetching track details... Please wait ---")
        self._terminal_log(f"--- Fetching metadata from: {source} ---")
        self.root.config(cursor="watch")

        def worker():
            urls = []
            if os.path.exists(source) and source.endswith(".txt"):
                with open(source, "r", encoding="utf-8") as f:
                    urls = [line.strip() for line in f if line.strip() and not line.startswith("#")]
            else:
                urls = [source]

            ydl_opts = {"quiet": True, "skip_download": True, "logger": self.ytdlp_logger}
            extracted_items = []
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                for target in urls:
                    try:
                        info = ydl.extract_info(target, download=False)
                        if "entries" in info:
                            for entry in info["entries"]:
                                extracted_items.append(self._parse_track_info(entry, entry.get('url')))
                        else:
                            extracted_items.append(self._parse_track_info(info, target))
                    except Exception as e:
                        self._log(f"Error fetching metadata for '{target}': {str(e)}")

            self.root.after(0, lambda: self._append_to_list(extracted_items))

        threading.Thread(target=worker, daemon=True).start()

    def _parse_track_info(self, info, default_url):
        title = clean_filename(info.get("title", "Unknown Track"))
        duration = info.get("duration", 0)
        
        minutes = duration / 60
        est_mp3_mb = round(minutes * 2.4, 1) if duration else 0
        est_wav_mb = round(minutes * 10.0, 1) if duration else 0

        thumbnails = info.get("thumbnails", [])
        thumb_url = thumbnails[-1]["url"] if thumbnails else info.get("thumbnail", "")
        video_id = info.get("id", "00000000")

        return {
            "id": video_id,
            "title": title,
            "artist": info.get("artist") or info.get("uploader", "Unknown Artist"),
            "album": info.get("album", "YouTube Single"),
            "url": info.get("webpage_url") or default_url or f"https://www.youtube.com/watch?v={video_id}",
            "duration_str": time.strftime('%M:%S', time.gmtime(duration)) if duration else "N/A",
            "est_mp3": est_mp3_mb,
            "est_wav": est_wav_mb,
            "thumb_url": thumb_url
        }

    def _append_to_list(self, items):
        self.root.config(cursor="")
        if not items:
            self._log("No tracks found.")
            return

        for item in items:
            idx = len(self.song_data)
            row_frame = tk.Frame(self.scrollable_frame, bg="#f9f9f9" if idx % 2 == 0 else "#ffffff", padx=5, pady=4)
            row_frame.pack(fill="x", expand=True)

            thumb_label = tk.Label(row_frame, bg="#dddddd", width=60, height=45)
            thumb_label.pack(side="left", padx=(0, 8))
            self._load_thumbnail_async(item["thumb_url"], thumb_label)

            info_frame = tk.Frame(row_frame, bg=row_frame["bg"])
            info_frame.pack(side="left", fill="x", expand=True)

            title_label = tk.Label(info_frame, text=item["title"], anchor="w", bg=row_frame["bg"], font=("Segoe UI", 9, "bold"))
            title_label.pack(fill="x")

            meta_text = f"⏱ {item['duration_str']}  |  Est: ~{item['est_mp3']}MB (MP3) / ~{item['est_wav']}MB (WAV)"
            meta_label = tk.Label(info_frame, text=meta_text, anchor="w", fg="#666666", bg=row_frame["bg"], font=("Segoe UI", 8))
            meta_label.pack(fill="x")

            status_label = tk.Label(row_frame, text="", bg=row_frame["bg"], font=("Segoe UI", 8, "bold"))
            status_label.pack(side="left", padx=5)

            mp3_var = tk.BooleanVar(value=False)
            wav_var = tk.BooleanVar(value=False)

            cb_mp3 = tk.Checkbutton(row_frame, text="MP3", variable=mp3_var, bg=row_frame["bg"], command=self._recalculate_total_est_size)
            cb_mp3.pack(side="right", padx=3)

            cb_wav = tk.Checkbutton(row_frame, text="WAV", variable=wav_var, bg=row_frame["bg"], command=self._recalculate_total_est_size)
            cb_wav.pack(side="right", padx=3)

            item_record = {
                "meta": item,
                "mp3_var": mp3_var,
                "wav_var": wav_var,
                "status_label": status_label,
                "row_frame": row_frame
            }

            btn_del = tk.Button(row_frame, text="❌", bg=row_frame["bg"], fg="#dc3545", bd=0, font=("Segoe UI", 8, "bold"),
                                command=lambda rec=item_record: self._remove_single_song(rec))
            btn_del.pack(side="right", padx=(5, 0))

            self.song_data.append(item_record)

        self._recalculate_total_est_size()
        self._log(f"Added {len(items)} tracks to list. Total: {len(self.song_data)}.")

    def _load_thumbnail_async(self, url, label_widget):
        if not url:
            return

        def fetch():
            try:
                resp = requests.get(url, timeout=5)
                img_data = Image.open(io.BytesIO(resp.content))
                img_data = img_data.resize((60, 45), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(img_data)

                def update_ui():
                    label_widget.config(image=photo, width=60, height=45)
                    label_widget.image = photo

                self.root.after(0, update_ui)
            except Exception:
                pass

        threading.Thread(target=fetch, daemon=True).start()

    # --- Engine Updater ---
    def update_ytdlp(self):
        self.update_btn.config(state="disabled")
        self.root.config(cursor="watch")
        self._log("\n--- Checking for yt-dlp engine updates ---")
        self._terminal_log("--- Starting yt-dlp update via pip ---")

        def worker():
            try:
                process = subprocess.Popen(
                    [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    universal_newlines=True
                )
                for line in iter(process.stdout.readline, ''):
                    self._terminal_log(line.rstrip())
                process.stdout.close()
                return_code = process.wait()
                if return_code == 0:
                    self._log("SUCCESS: Engine update complete!")
                    self._terminal_log("--- Update finished successfully ---")
                else:
                    self._log("WARNING: Engine update had errors – check terminal.")
                    self._terminal_log(f"--- Update failed with code {return_code} ---")
            except Exception as e:
                self._log(f"ERROR: Failed to run update: {str(e)}")
                self._terminal_log(f"ERROR: {str(e)}")
            finally:
                self.root.after(0, lambda: self.update_btn.config(state="normal"))
                self.root.after(0, lambda: self.root.config(cursor=""))

        threading.Thread(target=worker, daemon=True).start()

    # --- Live Hook & Download Manager ---
    def _create_progress_hook(self, thread_id, status_label):
        last_downloaded = 0

        def hook(d):
            nonlocal last_downloaded
            if d['status'] == 'downloading':
                downloaded = d.get('downloaded_bytes', 0)
                speed = d.get('speed', 0) or 0
                
                delta = downloaded - last_downloaded
                if delta > 0:
                    with self.lock:
                        self.total_downloaded_bytes += delta
                        self.active_speeds[thread_id] = speed
                    last_downloaded = downloaded

                speed_str = format_bytes(speed) + "/s"
                
                self.root.after(0, lambda: status_label.config(text=f"⏳ {speed_str}", fg="#ffc107"))
                self.root.after(0, self._update_live_stats_ui)

            elif d['status'] == 'finished':
                with self.lock:
                    self.active_speeds[thread_id] = 0
                self.root.after(0, self._update_live_stats_ui)

        return hook

    def _update_live_stats_ui(self):
        with self.lock:
            total_speed = sum(self.active_speeds.values())
            curr_downloaded = self.total_downloaded_bytes

        speed_fmt = format_bytes(total_speed) + "/s"
        downloaded_fmt = format_bytes(curr_downloaded)
        self.live_stats_str.set(f"Downloaded: {downloaded_fmt} | Speed: {speed_fmt}")

    def start_download(self):
        if self.is_downloading:
            messagebox.showwarning("In Progress", "A download process is already running.")
            return

        out_folder = self.out_dir.get().strip()
        if not os.path.exists(out_folder):
            messagebox.showerror("Error", "Selected output folder does not exist.")
            return

        to_download = []
        seen_urls = set()

        for item in self.song_data:
            has_mp3 = item["mp3_var"].get()
            has_wav = item["wav_var"].get()

            if has_mp3 and has_wav:
                chosen_fmt = "wav"
            elif has_wav:
                chosen_fmt = "wav"
            elif has_mp3:
                chosen_fmt = "mp3"
            else:
                continue

            url = item["meta"]["url"]
            if url in seen_urls:
                item["status_label"].config(text="⚠️ Duplicate", fg="#fd7e14")
                continue
            seen_urls.add(url)

            to_download.append({
                "meta": item["meta"],
                "fmt": chosen_fmt,
                "status_label": item["status_label"]
            })

        if not to_download:
            messagebox.showwarning("Selection Empty", "Please check at least one MP3 or WAV box.")
            return

        self.active_speeds.clear()
        self.total_downloaded_bytes = 0
        self.live_stats_str.set("Downloaded: 0 MB | Speed: 0 KB/s")

        self.is_downloading = True
        self.btn_download.config(state="disabled", text="Downloading...")
        threading.Thread(target=self._process_queue, args=(to_download, out_folder), daemon=True).start()

    def _process_queue(self, queue, out_folder):
        num_threads = int(self.max_threads.get())
        self._log(f"\n--- Starting concurrent downloads ({len(queue)} items, {num_threads} threads) ---")
        self._terminal_log(f"--- Download session started: {len(queue)} items, {num_threads} threads ---")

        start_time = time.time()
        success_count = 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [
                executor.submit(self._download_single_track, item, out_folder, idx) 
                for idx, item in enumerate(queue)
            ]
            for future in concurrent.futures.as_completed(futures):
                if future.result():
                    success_count += 1

        elapsed = round(time.time() - start_time, 1)
        self._log(f"\nFinished: Downloaded {success_count}/{len(queue)} tracks in {elapsed}s.")
        self._terminal_log(f"--- Download session finished: {success_count}/{len(queue)} successful in {elapsed}s ---")
        self._finish_download()

    def _download_single_track(self, item, out_folder, thread_id):
        meta = item["meta"]
        fmt = item["fmt"]
        title = meta["title"]
        video_id = meta["id"]
        url = meta["url"]
        status_label = item["status_label"]
        selected_bitrate = self.bitrate.get()

        self._log(f"[*] Queueing [{fmt.upper()}]: {title}")
        self.root.after(0, lambda: status_label.config(text="⏳ Connecting...", fg="#ffc107"))

        # Explicitly thread-isolate temporary output files and parts
        temp_title_pattern = f"{title}_t{thread_id}_{video_id}"
        temp_outtmpl = os.path.join(out_folder, f"{temp_title_pattern}.%(ext)s")
        final_file_path = os.path.join(out_folder, f"{title}.{fmt}")
        temp_file_path = os.path.join(out_folder, f"{temp_title_pattern}.{fmt}")

        ydl_opts = {
            "format": "bestaudio/best",
            "ffmpeg_location": self.ffmpeg_path,
            "outtmpl": temp_outtmpl,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": fmt,
                "preferredquality": selected_bitrate if fmt == "mp3" else "0",
            }],
            "progress_hooks": [self._create_progress_hook(thread_id, status_label)],
            "logger": self.ytdlp_logger,   # <-- Add logger
            "quiet": False,                # <-- Enable verbose output
            "no_warnings": False,          # <-- Show warnings
            "overwrites": True,
        }

        # Attempt download with up to 3 retries in case of Windows file handle delays
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])

                # Retry safe file rename loop
                time.sleep(0.5)  # Short pause to let OS release file lock
                if os.path.exists(temp_file_path):
                    with self.lock:
                        if os.path.exists(final_file_path):
                            try:
                                os.remove(final_file_path)
                            except OSError:
                                pass
                        os.rename(temp_file_path, final_file_path)

                self._embed_metadata(final_file_path, fmt, meta)

                self._log(f"    --> SUCCESS: {title}.{fmt}")
                self.root.after(0, lambda: status_label.config(text="✔ Completed", fg="#28a745"))
                return True

            except Exception as e:
                self._log(f"    --> Attempt {attempt}/{max_retries} failed for '{title}': {str(e)}")
                if attempt < max_retries:
                    time.sleep(1.0 * attempt)  # Exponential backoff retry
                else:
                    self.root.after(0, lambda: status_label.config(text="❌ Failed", fg="#dc3545"))
                    return False

    def _embed_metadata(self, file_path, fmt, meta):
        if not os.path.exists(file_path):
            return

        try:
            img_bytes = None
            if meta.get("thumb_url"):
                r = requests.get(meta["thumb_url"], timeout=5)
                if r.status_code == 200:
                    img_bytes = r.content

            if fmt == "mp3":
                try:
                    audio = EasyID3(file_path)
                except Exception:
                    audio = EasyID3()
                
                audio["title"] = meta["title"]
                audio["artist"] = meta["artist"]
                audio["album"] = meta["album"]
                audio.save(file_path)

                if img_bytes:
                    tags = ID3(file_path)
                    tags.add(APIC(
                        encoding=3,
                        mime='image/jpeg',
                        type=3,
                        desc='Cover',
                        data=img_bytes
                    ))
                    tags.save(file_path)

        except Exception as e:
            self._log(f"    [!] Notice: Could not attach metadata tags: {str(e)}")

    def _finish_download(self):
        self.is_downloading = False
        with self.lock:
            self.active_speeds.clear()
        self.root.after(0, self._update_live_stats_ui)
        self.btn_download.config(state="normal", text="Start Download")


if __name__ == "__main__":
    root = tk.Tk()
    app = SongDownloaderApp(root)
    root.mainloop()