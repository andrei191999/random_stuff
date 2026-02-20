"""
SFTP Batch Uploader — GUI
Requires: paramiko  (pip install paramiko)
Built-in: tkinter, threading, queue
"""
import json
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

import paramiko



# ── upload worker (runs in a thread) ─────────────────────────────────────────

class UploadWorker:
    """Runs the SFTP upload sequence in a background thread."""

    STOP  = "STOP"
    DONE  = "DONE"

    def __init__(self, cfg: dict, files: list, log_q: queue.Queue,
                 confirm_q: queue.Queue, reply_q: queue.Queue):
        self.cfg       = cfg
        self.files     = files
        self.log_q     = log_q
        self.confirm_q = confirm_q   # worker → GUI: request for confirmation
        self.reply_q   = reply_q     # GUI → worker: True=continue False=stop
        self._stop     = threading.Event()

    def stop(self):
        self._stop.set()

    def _log(self, msg):
        self.log_q.put(("log", msg))

    def _connect(self) -> tuple:
        """Open transport + SFTP. Returns (transport, sftp)."""
        cfg = self.cfg
        transport = paramiko.Transport((cfg["host"], int(cfg["port"])))
        if cfg["auth"] == "key" and cfg["key_path"]:
            pkey = paramiko.PKey.from_private_key_file(cfg["key_path"])
            transport.connect(username=cfg["username"], pkey=pkey)
        else:
            transport.connect(username=cfg["username"],
                              password=cfg["password"])
        sftp = paramiko.SFTPClient.from_transport(transport)
        if sftp is None:
            transport.close()
            raise RuntimeError("Failed to open SFTP channel")
        return transport, sftp

    def _timer(self, text: str):
        self.log_q.put(("timer", text))

    def _sleep(self, seconds: int, timer_prefix: str = "", total: int = 0) -> bool:
        """Sleep second-by-second; returns False if stopped early.
        Sends timer events each second if timer_prefix is set."""
        for elapsed in range(seconds):
            if self._stop.is_set():
                return False
            remaining = seconds - elapsed
            if timer_prefix:
                mins, secs = divmod(remaining, 60)
                suffix = f"  [{total}]" if total else ""
                self._timer(f"{timer_prefix}  {mins:02d}m {secs:02d}s{suffix}")
            threading.Event().wait(1)
        if timer_prefix:
            self._timer("")
        return True

    def run(self):
        import datetime
        cfg   = self.cfg
        files = self.files

        # ── initial delay ─────────────────────────────────────────────────
        start_delay = int(cfg.get("start_delay_min", 0)) * 60
        if start_delay > 0:
            eta = datetime.datetime.now() + datetime.timedelta(seconds=start_delay)
            self._log(f"⏳ Upload scheduled to start at {eta.strftime('%H:%M:%S')} "
                      f"({cfg['start_delay_min']} min). Keep this computer on and connected!")
            if not self._sleep(start_delay, "⏳ Starting in"):
                self._log("⛔ Stopped during initial delay.")
                self.log_q.put(("done", False))
                return
            self._timer("")

        # ── connect ───────────────────────────────────────────────────────
        try:
            self._log(f"Connecting to {cfg['host']}:{cfg['port']} …")
            transport, sftp = self._connect()
            home = sftp.normalize(".")
            self._log(f"Connected ✓  (remote home: {home})")
        except Exception as exc:
            self._log(f"❌ Connection failed: {exc}")
            self.log_q.put(("done", False))
            return

        remote_dir        = cfg["remote_dir"].rstrip("/")
        delay             = int(cfg["delay"])  if cfg["use_delay"] else 0
        test_count        = int(cfg["test_n"]) if cfg["use_test"]  else 0
        total             = len(files)
        RECONNECT_THRESH  = 55   # reconnect proactively if delay >= this many seconds

        try:
            for idx, fpath in enumerate(files, 1):
                if self._stop.is_set():
                    self._log("⛔ Stopped by user.")
                    break

                fname       = os.path.basename(fpath)
                remote_path = f"{remote_dir}/{fname}" if remote_dir else fname

                if not os.path.exists(fpath):
                    self._log(f"[{idx:02d}/{total}] ⚠ SKIP (not found): {fname}")
                else:
                    # ensure connection still alive before uploading
                    if not transport.is_active():
                        self._log("   🔄 Connection lost — reconnecting …")
                        try:
                            transport, sftp = self._connect()
                            self._log("   🔄 Reconnected ✓")
                        except Exception as exc:
                            self._log(f"   ❌ Reconnect failed: {exc}")
                            break

                    self._log(f"[{idx:02d}/{total}] Uploading {fname} → {remote_path} …")
                    try:
                        sftp.put(fpath, remote_path, confirm=False)
                        self._log(f"[{idx:02d}/{total}] ✓ done")
                    except Exception as exc:
                        self._log(f"[{idx:02d}/{total}] ❌ Error: {exc}")

                # ── test-batch pause ──────────────────────────────────────
                if test_count and idx == test_count:
                    self._log(f"\n── Test batch done ({test_count} files) ──")
                    self.confirm_q.put("confirm")
                    answer = self.reply_q.get()
                    if not answer:
                        self._log("Stopped after test batch.")
                        break
                    self._log("Continuing …\n")

                # ── inter-file delay ───────────────────────────────────────
                if delay and idx < total and not self._stop.is_set():
                    if not self._sleep(delay, "⏱  Next upload in", idx + 1):
                        break
                    self._timer("")
                    # proactively reconnect if delay was long enough to drop the session
                    if delay >= RECONNECT_THRESH and not transport.is_active():
                        self._log("   🔄 Reconnecting after long delay …")
                        try:
                            transport, sftp = self._connect()
                            self._log("   🔄 Reconnected ✓")
                        except Exception as exc:
                            self._log(f"   ❌ Reconnect failed: {exc}")
                            break

        finally:
            try: sftp.close()
            except Exception: pass
            try: transport.close()
            except Exception: pass
            self.log_q.put(("done", True))


# ── main GUI ──────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SFTP Batch Uploader")
        self.resizable(True, True)
        self.minsize(720, 600)

        self._worker   : UploadWorker | None = None
        self._thread   : threading.Thread | None = None
        self._log_q    = queue.Queue()
        self._confirm_q = queue.Queue()
        self._reply_q  = queue.Queue()

        self._build_ui()
        self._poll()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        self._tab_conn  = ttk.Frame(nb, padding=10)
        self._tab_files = ttk.Frame(nb, padding=10)
        self._tab_opts  = ttk.Frame(nb, padding=10)

        nb.add(self._tab_conn,  text="  🔑 Connection  ")
        nb.add(self._tab_files, text="  📂 Files  ")
        nb.add(self._tab_opts,  text="  ⚙ Options  ")

        self._build_connection_tab()
        self._build_files_tab()
        self._build_options_tab()
        self._build_log_panel()

    # ── Connection tab ────────────────────────────────────────────────────────

    def _build_connection_tab(self):
        f = self._tab_conn
        f.columnconfigure(1, weight=1)

        # ── preset selector (row 0) ───────────────────────────────────────
        ttk.Label(f, text="Preset:").grid(row=0, column=0, sticky="w", pady=4, padx=(0,8))
        pf = ttk.Frame(f)
        pf.grid(row=0, column=1, sticky="ew")
        self._preset_var   = tk.StringVar()
        self._preset_combo = ttk.Combobox(pf, textvariable=self._preset_var, width=22)
        self._preset_combo.pack(side="left")
        self._preset_combo.bind("<<ComboboxSelected>>", lambda _: self._load_preset_from_combo())
        ttk.Button(pf, text="Save",     command=self._save_preset).pack(side="left", padx=(4,0))
        ttk.Button(pf, text="Save as…", command=self._save_preset_as).pack(side="left", padx=(2,0))
        ttk.Button(pf, text="Delete",   command=self._delete_preset).pack(side="left", padx=(2,0))
        ttk.Button(pf, text="⭐ Default", command=self._set_default_preset).pack(side="left", padx=(2,0))
        ttk.Separator(f, orient="horizontal").grid(row=1, column=0, columnspan=2, sticky="ew", pady=6)

        def row(label, r):
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=4, padx=(0,8))

        row("Host:", 2);       self.v_host = tk.StringVar()
        row("Port:", 3);       self.v_port = tk.StringVar(value="22")
        row("Username:", 4);   self.v_user = tk.StringVar()
        row("Auth type:", 5);  self.v_auth = tk.StringVar(value="password")
        row("Password:", 6);   self.v_pw   = tk.StringVar()
        row("Key file:", 7);   self.v_key  = tk.StringVar()
        row("Remote dir:", 8); self.v_rdir = tk.StringVar()

        ttk.Entry(f, textvariable=self.v_host).grid(row=2, column=1, sticky="ew")
        ttk.Entry(f, textvariable=self.v_port, width=8).grid(row=3, column=1, sticky="w")
        ttk.Entry(f, textvariable=self.v_user).grid(row=4, column=1, sticky="ew")

        auth_f = ttk.Frame(f)
        auth_f.grid(row=5, column=1, sticky="w")
        ttk.Radiobutton(auth_f, text="Password", variable=self.v_auth,
                        value="password", command=self._toggle_auth).pack(side="left")
        ttk.Radiobutton(auth_f, text="SSH Key",  variable=self.v_auth,
                        value="key",      command=self._toggle_auth).pack(side="left", padx=8)

        self._pw_entry = ttk.Entry(f, textvariable=self.v_pw, show="•")
        self._pw_entry.grid(row=6, column=1, sticky="ew")

        key_f = ttk.Frame(f)
        key_f.grid(row=7, column=1, sticky="ew")
        key_f.columnconfigure(0, weight=1)
        self._key_entry = ttk.Entry(key_f, textvariable=self.v_key, state="disabled")
        self._key_entry.grid(row=0, column=0, sticky="ew")
        self._key_btn   = ttk.Button(key_f, text="Browse…", state="disabled",
                                     command=self._browse_key)
        self._key_btn.grid(row=0, column=1, padx=(4,0))

        ttk.Entry(f, textvariable=self.v_rdir).grid(row=8, column=1, sticky="ew")
        ttk.Button(f, text="Test Connection", command=self._test_conn)\
            .grid(row=9, column=0, columnspan=2, pady=(16,0))

        self.after(0, self._load_default_preset)

    # ── Presets ───────────────────────────────────────────────────────────────

    def _presets_path(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "sftp_presets.json")

    def _load_presets(self) -> dict:
        p = self._presets_path()
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    return json.load(fh)
            except Exception:
                pass
        return {"default": "", "presets": {}}

    def _save_presets_file(self, data: dict):
        with open(self._presets_path(), "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)

    def _refresh_preset_dropdown(self, data: dict):
        names = list(data.get("presets", {}).keys())
        self._preset_combo["values"] = names
        default = data.get("default", "")
        self._preset_combo.set(default if default in names else (names[0] if names else ""))

    def _load_default_preset(self):
        data = self._load_presets()
        self._refresh_preset_dropdown(data)
        default = data.get("default", "")
        p = data.get("presets", {}).get(default)
        if p:
            self._apply_preset(p)

    def _apply_preset(self, p: dict):
        self.v_host.set(p.get("host", ""))
        self.v_port.set(p.get("port", "22"))
        self.v_user.set(p.get("username", ""))
        self.v_auth.set(p.get("auth", "password"))
        self.v_pw.set(p.get("password", ""))
        self.v_key.set(p.get("key_path", ""))
        self.v_rdir.set(p.get("remote_dir", ""))
        self._toggle_auth()

    def _load_preset_from_combo(self):
        name = self._preset_var.get().strip()
        data = self._load_presets()
        p = data.get("presets", {}).get(name)
        if p:
            self._apply_preset(p)
        else:
            messagebox.showwarning("Not found", f"No preset named '{name}'.")

    def _save_preset(self):
        name = self._preset_var.get().strip()
        if not name:
            name = simpledialog.askstring("Save preset", "Enter a name for this preset:")
            if not name:
                return
            self._preset_combo.set(name)
        data = self._load_presets()
        data.setdefault("presets", {})[name] = {
            "host":       self.v_host.get().strip(),
            "port":       self.v_port.get().strip() or "22",
            "username":   self.v_user.get().strip(),
            "auth":       self.v_auth.get(),
            "password":   self.v_pw.get(),
            "key_path":   self.v_key.get().strip(),
            "remote_dir": self.v_rdir.get().strip(),
        }
        if not data.get("default"):
            data["default"] = name
        self._save_presets_file(data)
        self._refresh_preset_dropdown(data)
        self._log(f"💾 Preset '{name}' saved.")

    def _save_preset_as(self):
        """Always prompt for a new name — never overwrites existing."""
        name = simpledialog.askstring("Save as new preset", "Enter a name for the new preset:")
        if not name:
            return
        data = self._load_presets()
        if name in data.get("presets", {}):
            if not messagebox.askyesno("Overwrite?", f"Preset '{name}' already exists. Overwrite?"):
                return
        self._preset_combo.set(name)
        data.setdefault("presets", {})[name] = {
            "host":       self.v_host.get().strip(),
            "port":       self.v_port.get().strip() or "22",
            "username":   self.v_user.get().strip(),
            "auth":       self.v_auth.get(),
            "password":   self.v_pw.get(),
            "key_path":   self.v_key.get().strip(),
            "remote_dir": self.v_rdir.get().strip(),
        }
        if not data.get("default"):
            data["default"] = name
        self._save_presets_file(data)
        self._refresh_preset_dropdown(data)
        self._preset_combo.set(name)
        self._log(f"💾 Preset '{name}' saved as new.")

    def _delete_preset(self):
        name = self._preset_var.get().strip()
        if not name:
            return
        data = self._load_presets()
        if name not in data.get("presets", {}):
            messagebox.showwarning("Not found", f"No preset '{name}'.")
            return
        if not messagebox.askyesno("Delete", f"Delete preset '{name}'?"):
            return
        del data["presets"][name]
        if data.get("default") == name:
            data["default"] = next(iter(data["presets"]), "")
        self._save_presets_file(data)
        self._refresh_preset_dropdown(data)
        self._log(f"🗑 Preset '{name}' deleted.")

    def _set_default_preset(self):
        name = self._preset_var.get().strip()
        if not name:
            return
        data = self._load_presets()
        if name not in data.get("presets", {}):
            messagebox.showwarning("Not saved", f"Save preset '{name}' first.")
            return
        data["default"] = name
        self._save_presets_file(data)
        self._log(f"⭐ Default preset set to '{name}'.")

    def _toggle_auth(self):
        if self.v_auth.get() == "key":
            self._pw_entry.config(state="disabled")
            self._key_entry.config(state="normal")
            self._key_btn.config(state="normal")
        else:
            self._pw_entry.config(state="normal")
            self._key_entry.config(state="disabled")
            self._key_btn.config(state="disabled")

    def _browse_key(self):
        path = filedialog.askopenfilename(title="Select SSH private key",
                                          filetypes=[("Key files", "*.pem *.ppk *.key *"),
                                                     ("All", "*")])
        if path:
            self.v_key.set(path)

    def _test_conn(self):
        cfg = self._get_cfg()
        def _run():
            try:
                t = paramiko.Transport((cfg["host"], int(cfg["port"])))
                if cfg["auth"] == "key" and cfg["key_path"]:
                    pk = paramiko.PKey.from_private_key_file(cfg["key_path"])
                    t.connect(username=cfg["username"], pkey=pk)
                else:
                    t.connect(username=cfg["username"], password=cfg["password"])
                sftp = paramiko.SFTPClient.from_transport(t)
                if sftp is None:
                    raise RuntimeError("Failed to open SFTP channel")
                home = sftp.normalize(".")
                sftp.close()
                t.close()
                # schedule UI updates on main thread
                self.after(0, lambda: self._log(f"✅ Connection OK — remote home: {home}"))
                self.after(0, lambda: messagebox.showinfo("Connection OK",
                                                          f"Connected!\nRemote home: {home}"))
            except Exception as exc:
                msg = str(exc)
                self.after(0, lambda: self._log(f"❌ Connection failed: {msg}"))
                self.after(0, lambda: messagebox.showerror("Connection failed", msg))
        threading.Thread(target=_run, daemon=True).start()

    # ── Files tab ─────────────────────────────────────────────────────────────

    def _build_files_tab(self):
        f = self._tab_files
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)

        btn_f = ttk.Frame(f)
        btn_f.grid(row=0, column=0, sticky="ew", pady=(0,6))
        ttk.Button(btn_f, text="Add files…",   command=self._add_files).pack(side="left")
        ttk.Button(btn_f, text="Add folder…",  command=self._add_folder).pack(side="left", padx=4)
        ttk.Button(btn_f, text="Remove selected", command=self._remove_files).pack(side="left")
        ttk.Button(btn_f, text="Clear all",    command=self._clear_files).pack(side="left", padx=4)

        list_f = ttk.Frame(f)
        list_f.grid(row=1, column=0, sticky="nsew")
        list_f.columnconfigure(0, weight=1)
        list_f.rowconfigure(0, weight=1)

        self._file_list = tk.Listbox(list_f, selectmode="extended",
                                     activestyle="none", font=("Consolas", 9))
        sb = ttk.Scrollbar(list_f, orient="vertical", command=self._file_list.yview)
        self._file_list.config(yscrollcommand=sb.set)
        self._file_list.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")

        self._file_count_lbl = ttk.Label(f, text="0 files selected")
        self._file_count_lbl.grid(row=2, column=0, sticky="w", pady=(4,0))

    def _add_files(self):
        paths = filedialog.askopenfilenames(title="Select files to upload",
                                            filetypes=[("CSV", "*.csv"), ("All", "*")])
        for p in paths:
            if p not in self._file_list.get(0, "end"):
                self._file_list.insert("end", p)
        self._update_count()

    def _add_folder(self):
        folder = filedialog.askdirectory(title="Select folder")
        if not folder:
            return
        exts = (".csv",)
        added = 0
        for fn in sorted(os.listdir(folder)):
            if fn.lower().endswith(exts):
                full = os.path.join(folder, fn)
                if full not in self._file_list.get(0, "end"):
                    self._file_list.insert("end", full)
                    added += 1
        self._log(f"Added {added} files from {folder}")
        self._update_count()

    def _remove_files(self):
        for idx in reversed(self._file_list.curselection()):
            self._file_list.delete(idx)
        self._update_count()

    def _clear_files(self):
        self._file_list.delete(0, "end")
        self._update_count()

    def _update_count(self):
        n = self._file_list.size()
        self._file_count_lbl.config(text=f"{n} file{'s' if n!=1 else ''} selected")

    # ── Options tab ───────────────────────────────────────────────────────────

    def _build_options_tab(self):
        f = self._tab_opts
        f.columnconfigure(1, weight=1)

        # Delay
        self.v_use_delay = tk.BooleanVar(value=True)
        self.v_delay     = tk.StringVar(value="90")
        ttk.Checkbutton(f, text="Delay between uploads (seconds):",
                        variable=self.v_use_delay,
                        command=self._toggle_delay).grid(row=0, column=0, sticky="w", pady=6)
        self._delay_spin = ttk.Spinbox(f, from_=0, to=3600, textvariable=self.v_delay,
                                       width=8)
        self._delay_spin.grid(row=0, column=1, sticky="w", padx=8)

        # Test batch
        self.v_use_test = tk.BooleanVar(value=True)
        self.v_test_n   = tk.StringVar(value="3")
        ttk.Checkbutton(f, text="Test batch — pause after N files:",
                        variable=self.v_use_test,
                        command=self._toggle_test).grid(row=1, column=0, sticky="w", pady=6)
        self._test_spin = ttk.Spinbox(f, from_=1, to=999, textvariable=self.v_test_n,
                                      width=8)
        self._test_spin.grid(row=1, column=1, sticky="w", padx=8)

        # Start delay
        ttk.Separator(f, orient="horizontal").grid(row=2, column=0, columnspan=2,
                                                   sticky="ew", pady=10)
        self.v_use_start_delay = tk.BooleanVar(value=False)
        self.v_start_delay     = tk.StringVar(value="0")
        ttk.Checkbutton(f, text="Start delay (minutes before upload begins):",
                        variable=self.v_use_start_delay,
                        command=self._toggle_start_delay).grid(row=3, column=0, sticky="w", pady=6)
        sd_f = ttk.Frame(f)
        sd_f.grid(row=3, column=1, sticky="w", padx=8)
        self._start_delay_spin = ttk.Spinbox(sd_f, from_=0, to=1440,
                                             textvariable=self.v_start_delay,
                                             width=8, state="disabled",
                                             command=self._update_start_delay_lbl)
        self._start_delay_spin.pack(side="left")
        self._start_delay_lbl = ttk.Label(sd_f, text="", foreground="gray")
        self._start_delay_lbl.pack(side="left", padx=(8, 0))
        self.v_start_delay.trace_add("write", lambda *_: self._update_start_delay_lbl())

    def _toggle_delay(self):
        s = "normal" if self.v_use_delay.get() else "disabled"
        self._delay_spin.config(state=s)

    def _toggle_test(self):
        s = "normal" if self.v_use_test.get() else "disabled"
        self._test_spin.config(state=s)

    def _toggle_start_delay(self):
        s = "normal" if self.v_use_start_delay.get() else "disabled"
        self._start_delay_spin.config(state=s)
        self._update_start_delay_lbl()

    def _update_start_delay_lbl(self):
        import datetime
        if not self.v_use_start_delay.get():
            self._start_delay_lbl.config(text="")
            return
        try:
            mins = int(self.v_start_delay.get())
        except ValueError:
            return
        if mins <= 0:
            self._start_delay_lbl.config(text="(immediate)", foreground="gray")
            return
        eta = datetime.datetime.now() + datetime.timedelta(minutes=mins)
        color = "darkorange" if mins > 30 else "blue"
        self._start_delay_lbl.config(
            text=f"→ starts ~{eta.strftime('%H:%M')}  {'⚠ keep laptop on!' if mins > 30 else ''}",
            foreground=color)

    # ── Log panel + controls ──────────────────────────────────────────────────

    def _build_log_panel(self):
        # ── timer status bar ──────────────────────────────────────────────
        timer_bar = tk.Frame(self, bg="#1e1e2e", height=28)
        timer_bar.pack(fill="x", padx=8, pady=(0, 2))
        timer_bar.pack_propagate(False)
        self._timer_lbl = tk.Label(timer_bar, text="", bg="#1e1e2e", fg="#cdd6f4",
                                   font=("Consolas", 10, "bold"), anchor="w", padx=8)
        self._timer_lbl.pack(fill="both", expand=True)

        frame = ttk.LabelFrame(self, text=" Log ", padding=6)
        frame.pack(fill="both", expand=True, padx=8, pady=(0,8))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        self._log_txt = scrolledtext.ScrolledText(
            frame, height=12, font=("Consolas", 9),
            state="disabled", wrap="none")
        self._log_txt.grid(row=0, column=0, columnspan=3, sticky="nsew")

        btn_f = ttk.Frame(frame)
        btn_f.grid(row=1, column=0, sticky="w", pady=(6,0))

        self._start_btn = ttk.Button(btn_f, text="▶  Start Upload",
                                     command=self._start, style="Accent.TButton")
        self._start_btn.pack(side="left")

        self._stop_btn = ttk.Button(btn_f, text="⏹  Stop",
                                    command=self._stop, state="disabled")
        self._stop_btn.pack(side="left", padx=8)

        ttk.Button(btn_f, text="Clear log",
                   command=self._clear_log).pack(side="left")

    # ── upload control ────────────────────────────────────────────────────────

    def _get_cfg(self) -> dict:
        return {
            "host":            self.v_host.get().strip(),
            "port":            self.v_port.get().strip() or "22",
            "username":        self.v_user.get().strip(),
            "auth":            self.v_auth.get(),
            "password":        self.v_pw.get(),
            "key_path":        self.v_key.get().strip(),
            "remote_dir":      self.v_rdir.get().strip(),
            "use_delay":       self.v_use_delay.get(),
            "delay":           self.v_delay.get() or "0",
            "use_test":        self.v_use_test.get(),
            "test_n":          self.v_test_n.get() or "1",
            "start_delay_min": self.v_start_delay.get() if self.v_use_start_delay.get() else "0",
        }

    def _start(self):
        import datetime
        files = list(self._file_list.get(0, "end"))
        if not files:
            messagebox.showwarning("No files", "Please add files to upload first.")
            return

        cfg = self._get_cfg()
        if not cfg["host"] or not cfg["username"]:
            messagebox.showwarning("Missing info", "Host and username are required.")
            return

        # Warn if initial delay is long
        start_delay_min = int(cfg.get("start_delay_min", 0))
        if start_delay_min > 30:
            eta = datetime.datetime.now() + datetime.timedelta(minutes=start_delay_min)
            ok = messagebox.askokcancel(
                "⚠ Long delay — keep your laptop on!",
                f"Upload will start in {start_delay_min} minutes "
                f"(around {eta.strftime('%H:%M')}).\n\n"
                "⚠  Make sure this computer stays on and connected\n"
                "    to the internet for the entire duration!\n\n"
                "Click OK to confirm and start the countdown.")
            if not ok:
                return

        # clear queues
        for q in (self._log_q, self._confirm_q, self._reply_q):
            while not q.empty():
                try: q.get_nowait()
                except: pass

        self._log_q     = queue.Queue()
        self._confirm_q = queue.Queue()
        self._reply_q   = queue.Queue()

        self._worker = UploadWorker(cfg, files, self._log_q,
                                    self._confirm_q, self._reply_q)
        self._thread = threading.Thread(target=self._worker.run, daemon=True)
        self._thread.start()

        self._start_btn.config(state="disabled")
        self._stop_btn.config(state="normal")

    def _stop(self):
        if self._worker:
            self._worker.stop()
            # unblock any pending confirmation wait
            try: self._reply_q.put_nowait(False)
            except: pass

    # ── polling ───────────────────────────────────────────────────────────────

    def _poll(self):
        # drain log queue — keep only last timer event, show all log/done
        timer_text = None
        try:
            while True:
                kind, data = self._log_q.get_nowait()
                if kind == "log":
                    self._log(data)
                elif kind == "timer":
                    timer_text = data          # only last one matters
                elif kind == "done":
                    self._timer_lbl.config(text="")
                    self._start_btn.config(state="normal")
                    self._stop_btn.config(state="disabled")
                    self._log("\n─── Upload session ended ───\n")
        except queue.Empty:
            pass
        if timer_text is not None:
            self._timer_lbl.config(text=timer_text)

        # check for confirmation request from worker
        try:
            self._confirm_q.get_nowait()   # blocks? no — nowait
            self._ask_continue()
        except queue.Empty:
            pass

        self.after(150, self._poll)

    def _ask_continue(self):
        answer = messagebox.askyesno(
            "Test batch done",
            "Test batch completed.\n\nContinue uploading the remaining files?")
        self._reply_q.put(answer)

    # ── log helpers ───────────────────────────────────────────────────────────

    def _log(self, msg: str):
        self._log_txt.config(state="normal")
        self._log_txt.insert("end", msg + "\n")
        self._log_txt.see("end")
        self._log_txt.config(state="disabled")

    def _clear_log(self):
        self._log_txt.config(state="normal")
        self._log_txt.delete("1.0", "end")
        self._log_txt.config(state="disabled")


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    app.mainloop()
