#!/usr/bin/env python3
"""Tkinter GUI for rom_filter_copy.py. config.toml is the source of truth."""

import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

import tomllib

SCRIPT_DIR      = Path(__file__).parent
DEFAULT_CONFIG  = SCRIPT_DIR / "config.toml"
LOCAL_CONFIG    = SCRIPT_DIR / "config.local.toml"
SCRIPT_FILE = SCRIPT_DIR / "rom_filter_copy.py"


# ─────────────────────────────────────────────────────────── config I/O

def load_config() -> dict:
    if not LOCAL_CONFIG.exists() and DEFAULT_CONFIG.exists():
        shutil.copy(DEFAULT_CONFIG, LOCAL_CONFIG)
    if LOCAL_CONFIG.exists():
        with open(LOCAL_CONFIG, "rb") as f:
            return tomllib.load(f)
    return {}


def save_config(cfg: dict) -> None:
    copy_all_lines = "".join(f'    "{s}",\n' for s in sorted(cfg.get("copy_all_systems", [])))
    skip_sys_inline = ", ".join('"' + s + '"' for s in sorted(cfg.get("skip_systems", [])))
    rating = cfg.get("rating", 7.0)
    rating_str = f"{rating:g}"
    if "." not in rating_str:
        rating_str += ".0"
    sys_rating_lines = "".join(
        f"{s} = {r:g}\n" for s, r in sorted(cfg.get("system_ratings", {}).items())
    )
    content = (
        "# ROM Filter Copy — managed by gui.py\n"
        "# Edit here or via the GUI; GUI saves always overwrite.\n"
        "\n"
        f'roms_dir = "{cfg.get("roms_dir", "")}"\n'
        f'esde_data_dir = "{cfg.get("esde_data_dir", "")}"\n'
        f'target_roms_dir = "{cfg.get("target_roms_dir", "")}"\n'
        f'target_esde_data_dir = "{cfg.get("target_esde_data_dir", "")}"\n'
        "\n"
        f"rating = {rating_str}\n"
        f'overwrite = {"true" if cfg.get("overwrite") else "false"}\n'
        f'include_unrated = {"true" if cfg.get("include_unrated") else "false"}\n'
        f'verbose = {"true" if cfg.get("verbose") else "false"}\n'
        f"skip_systems = [{skip_sys_inline}]\n"
        "\n"
        "copy_all_systems = [\n"
        f"{copy_all_lines}"
        "]\n"
        "\n"
        "[system_ratings]\n"
        f"{sys_rating_lines}"
    )
    LOCAL_CONFIG.write_text(content, encoding="utf-8")


# ─────────────────────────────────────────────────────────── widgets

def _browse(var: tk.StringVar, parent: tk.Widget) -> None:
    initial = var.get().strip() or "/"
    path = filedialog.askdirectory(parent=parent, initialdir=initial)
    if path:
        var.set(path)


class ScrollableCheckList(ttk.Frame):
    """Fixed-height scrollable list of checkboxes with a live filter field."""

    def __init__(self, parent: tk.Widget, height: int = 160, **kwargs) -> None:
        super().__init__(parent, **kwargs)
        self._items: list[tuple[str, tk.BooleanVar, ttk.Checkbutton]] = []

        self._filter_var = tk.StringVar()
        self._filter_var.trace_add("write", lambda *_: self._refresh())

        top_row = ttk.Frame(self)
        top_row.pack(fill="x", pady=(0, 2))
        ttk.Entry(top_row, textvariable=self._filter_var).pack(side="left", fill="x", expand=True, padx=(0, 4))
        ttk.Button(top_row, text="All",  command=self._check_all).pack(side="left", padx=(0, 2))
        ttk.Button(top_row, text="None", command=self._uncheck_all).pack(side="left")

        scroll_frame = ttk.Frame(self)
        scroll_frame.pack(fill="both", expand=True)

        self._vsb = ttk.Scrollbar(scroll_frame, orient="vertical")
        self._vsb.pack(side="right", fill="y")
        self._canvas = tk.Canvas(scroll_frame, yscrollcommand=self._vsb.set,
                                 highlightthickness=0, height=height)
        self._canvas.pack(side="left", fill="both", expand=True)
        self._vsb.configure(command=self._canvas.yview)
        self._inner = ttk.Frame(self._canvas)
        self._win_id = self._canvas.create_window((0, 0), window=self._inner, anchor="nw")
        self._inner.bind("<Configure>", lambda _e: self._canvas.configure(
            scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>", lambda e: self._canvas.itemconfig(
            self._win_id, width=e.width))

    def _refresh(self) -> None:
        self._items.sort(key=lambda t: (not t[1].get(), t[0]))
        q = self._filter_var.get().strip().lower()
        for _, _, cb in self._items:
            cb.pack_forget()
        for name, _, cb in self._items:
            if not q or q in name.lower():
                cb.pack(anchor="w", padx=8, pady=1)
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _check_all(self) -> None:
        for _, var, _ in list(self._items):
            var.set(True)

    def _uncheck_all(self) -> None:
        for _, var, _ in list(self._items):
            var.set(False)

    def scroll(self, event: tk.Event) -> None:
        if event.num == 4:
            self._canvas.yview_scroll(-1, "units")
        elif event.num == 5:
            self._canvas.yview_scroll(1, "units")
        else:
            self._canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def populate(self, systems: list[str], checked: set[str]) -> dict[str, tk.BooleanVar]:
        for w in self._inner.winfo_children():
            w.destroy()
        self._items = []
        self._filter_var.set("")
        vars_: dict[str, tk.BooleanVar] = {}
        for name in sorted(systems, key=lambda n: (n not in checked, n)):
            var = tk.BooleanVar(value=(name in checked))
            cb = ttk.Checkbutton(self._inner, text=name, variable=var)
            cb.pack(anchor="w", padx=8, pady=1)
            self._items.append((name, var, cb))
            var.trace_add("write", lambda *_: self._refresh())
            vars_[name] = var
        return vars_

    def set_message(self, text: str) -> None:
        for w in self._inner.winfo_children():
            w.destroy()
        self._items = []
        self._filter_var.set("")
        ttk.Label(self._inner, text=text, foreground="gray").pack(
            anchor="w", padx=8, pady=4
        )


# ─────────────────────────────────────────────────────────── main app

class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("ROM Filter Copy")
        self.minsize(500, 400)
        self.resizable(True, True)
        self.geometry("575x780+257+186")
        self.withdraw()
        self.after(100, self._show)

        self._cfg = load_config()
        self._system_vars: dict[str, tk.BooleanVar] = {}
        self._filter_system_vars: dict[str, tk.BooleanVar] = {}
        self._system_ratings: dict[str, float] = dict(self._cfg.get("system_ratings", {}))
        self._process: subprocess.Popen | None = None
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._save_after_id: str | None = None
        self._refresh_after_id: str | None = None

        self._build_ui()
        self._refresh_systems()

    def _schedule_save(self, *_) -> None:
        if self._save_after_id:
            self.after_cancel(self._save_after_id)
        self._save_after_id = self.after(400, self._autosave)

    def _schedule_refresh(self, *_) -> None:
        if self._refresh_after_id:
            self.after_cancel(self._refresh_after_id)
        self._refresh_after_id = self.after(600, self._refresh_systems)

    def _autosave(self) -> None:
        self._save_after_id = None
        cfg = self._collect_cfg()
        save_config(cfg)
        self._cfg = cfg

    def _show(self) -> None:
        self.update_idletasks()
        self.deiconify()
        self.lift()

    # ── layout ────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        outer = ttk.Frame(self, padding=10)
        outer.grid(row=0, column=0, sticky="nsew")
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)  # notebook row stretches

        def _wheel_scroll(event: tk.Event) -> None:
            w: tk.Widget | None = self.winfo_containing(event.x_root, event.y_root)
            while w is not None:
                if w in (self._filter_grid._canvas, self._filter_grid._inner):
                    self._filter_grid.scroll(event)
                    return
                if w in (self._check_grid._canvas, self._check_grid._inner):
                    self._check_grid.scroll(event)
                    return
                w = getattr(w, "master", None)

        self.bind_all("<Button-4>", _wheel_scroll)
        self.bind_all("<Button-5>", _wheel_scroll)
        self.bind_all("<MouseWheel>", _wheel_scroll)

        # ── Paths ──────────────────────────────────────────────────────
        pf = ttk.LabelFrame(outer, text="Paths", padding=8)
        pf.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        pf.columnconfigure(1, weight=1)

        self._roms_dir_var    = tk.StringVar(value=self._cfg.get("roms_dir", ""))
        self._esde_dir_var    = tk.StringVar(value=self._cfg.get("esde_data_dir", ""))
        self._target_roms_var = tk.StringVar(value=self._cfg.get("target_roms_dir", ""))
        self._target_esde_var = tk.StringVar(value=self._cfg.get("target_esde_data_dir", ""))
        for _v in (self._roms_dir_var, self._esde_dir_var,
                   self._target_roms_var, self._target_esde_var):
            _v.trace_add("write", self._schedule_save)
        self._esde_dir_var.trace_add("write", self._schedule_refresh)

        for row, (label, var) in enumerate([
            ("ROMs dir",         self._roms_dir_var),
            ("ES-DE data dir",   self._esde_dir_var),
            ("Target ROMs dir",  self._target_roms_var),
            ("Target ES-DE dir", self._target_esde_var),
        ]):
            ttk.Label(pf, text=label + ":").grid(row=row, column=0, sticky="w", padx=(0, 6), pady=2)
            ttk.Entry(pf, textvariable=var).grid(row=row, column=1, sticky="ew", pady=2)
            ttk.Button(pf, text="Browse…", width=8,
                       command=lambda v=var: _browse(v, self)).grid(row=row, column=2, padx=(4, 0), pady=2)

        # ── Tabbed middle section ─────────────────────────────────────
        nb = ttk.Notebook(outer)
        nb.grid(row=1, column=0, sticky="nsew", pady=(0, 6))

        # Tab: Settings
        sf = ttk.Frame(nb, padding=8)
        nb.add(sf, text="Settings")
        sf.columnconfigure(1, weight=1)

        self._rating_var          = tk.DoubleVar(value=self._cfg.get("rating", 7.0))
        self._overwrite_var       = tk.BooleanVar(value=self._cfg.get("overwrite", False))
        self._include_unrated_var = tk.BooleanVar(value=self._cfg.get("include_unrated", False))
        self._verbose_var         = tk.BooleanVar(value=self._cfg.get("verbose", False))
        self._rating_var.trace_add("write", self._schedule_save)
        self._overwrite_var.trace_add("write", self._schedule_save)
        self._include_unrated_var.trace_add("write", self._schedule_save)
        self._verbose_var.trace_add("write", self._schedule_save)
        self._rating_var.trace_add("write", self._update_summary)
        self._overwrite_var.trace_add("write", self._update_summary)
        self._include_unrated_var.trace_add("write", self._update_summary)
        self._verbose_var.trace_add("write", self._update_summary)

        ttk.Label(sf, text="Min rating (0–10):").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Spinbox(sf, textvariable=self._rating_var,
                    from_=0.0, to=10.0, increment=0.5, width=6).grid(row=0, column=1, sticky="w")
        ttk.Checkbutton(sf, text="Overwrite (re-copy files already on target)",
                        variable=self._overwrite_var).grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Checkbutton(sf, text="Include unrated games",
                        variable=self._include_unrated_var).grid(row=2, column=0, columnspan=3, sticky="w")
        ttk.Checkbutton(sf, text="Verbose output (list game titles during preview)",
                        variable=self._verbose_var).grid(row=3, column=0, columnspan=3, sticky="w")

        # Tab: Systems filter
        ff = ttk.Frame(nb, padding=8)
        nb.add(ff, text="Systems filter")
        ff.columnconfigure(0, weight=1)
        ff.rowconfigure(1, weight=1)

        self._systems_include_mode = tk.BooleanVar(value=False)
        self._systems_include_mode.trace_add("write", self._update_summary)
        mode_frame = ttk.Frame(ff)
        mode_frame.grid(row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Radiobutton(mode_frame, text="Exclude checked", variable=self._systems_include_mode, value=False).pack(side="left")
        ttk.Radiobutton(mode_frame, text="Limit to checked", variable=self._systems_include_mode, value=True).pack(side="left", padx=(12, 0))

        self._filter_grid = ScrollableCheckList(ff)
        self._filter_grid.grid(row=1, column=0, sticky="nsew")
        self._filter_grid.set_message("Loading… (needs ES-DE data dir)")

        # Tab: Rating overrides
        rf = ttk.Frame(nb, padding=8)
        nb.add(rf, text="Rating overrides")
        rf.columnconfigure(0, weight=1)
        rf.rowconfigure(0, weight=1)

        self._sr_tree = ttk.Treeview(rf, columns=("system", "rating"),
                                     show="headings", selectmode="browse")
        self._sr_tree.heading("system", text="System")
        self._sr_tree.heading("rating", text="Min Rating")
        self._sr_tree.column("system", width=140, anchor="w")
        self._sr_tree.column("rating", width=80,  anchor="center")
        self._sr_tree.grid(row=0, column=0, sticky="nsew", pady=(0, 4))
        self._sr_tree.bind("<<TreeviewSelect>>", self._on_sr_select)
        self._sr_populate()

        add_row = ttk.Frame(rf)
        add_row.grid(row=1, column=0, sticky="w")

        self._sr_system_var = tk.StringVar()
        self._sr_combo = ttk.Combobox(add_row, textvariable=self._sr_system_var,
                                      state="readonly", width=16)
        self._sr_combo.pack(side="left", padx=(0, 4))

        self._sr_rating_var = tk.DoubleVar(value=7.0)
        ttk.Spinbox(add_row, textvariable=self._sr_rating_var,
                    from_=0.0, to=10.0, increment=0.5, width=6).pack(side="left", padx=(0, 4))

        ttk.Button(add_row, text="Add / Update",
                   command=self._on_sr_add).pack(side="left", padx=(0, 4))
        self._sr_remove_btn = ttk.Button(add_row, text="Remove",
                                         command=self._on_sr_remove, state="disabled")
        self._sr_remove_btn.pack(side="left", padx=(0, 4))
        ttk.Button(add_row, text="Clear all",
                   command=self._on_sr_clear).pack(side="left")

        # Tab: Bypass ratings filter
        csf = ttk.Frame(nb, padding=8)
        nb.add(csf, text="Bypass ratings filter")
        csf.columnconfigure(0, weight=1)
        csf.rowconfigure(0, weight=1)

        self._check_grid = ScrollableCheckList(csf)
        self._check_grid.grid(row=0, column=0, sticky="nsew")
        self._check_grid.set_message("Loading… (needs ES-DE data dir)")

        # ── Summary ────────────────────────────────────────────────────
        self._summary_lbl = ttk.Label(outer, text="", foreground="gray",
                                      font=("TkDefaultFont", 9), wraplength=520, justify="left")
        self._summary_lbl.grid(row=2, column=0, sticky="w", pady=(0, 4))
        self._update_summary()

        # ── Buttons ────────────────────────────────────────────────────
        bf = ttk.Frame(outer)
        bf.grid(row=3, column=0, sticky="ew", pady=(0, 6))

        self._dry_run_btn = ttk.Button(bf, text="Dry Run",    command=self._on_dry_run)
        self._run_btn     = ttk.Button(bf, text="Run Script", command=self._on_run)
        self._status_lbl  = ttk.Label(bf, text="", foreground="gray")

        self._dry_run_btn.pack(side="left", padx=(0, 4))
        self._run_btn.pack(side="left")
        self._status_lbl.pack(side="left", padx=8)

        # ── Output ─────────────────────────────────────────────────────
        of = ttk.LabelFrame(outer, text="Output", padding=4)
        of.grid(row=4, column=0, sticky="ew")
        of.columnconfigure(0, weight=1)

        self._output = scrolledtext.ScrolledText(
            of, height=14, state="disabled", font=("Courier", 10), wrap="word"
        )
        self._output.grid(sticky="ew")

    # ── systems ───────────────────────────────────────────────────────

    def _refresh_systems(self) -> None:
        esde_dir = self._esde_dir_var.get().strip()
        if not esde_dir:
            self._filter_grid.set_message("Set ES-DE data dir to load systems.")
            self._check_grid.set_message("Set ES-DE data dir to load systems.")
            return
        self._filter_grid.set_message("Loading…")
        self._check_grid.set_message("Loading…")
        threading.Thread(target=self._fetch_systems, args=(esde_dir,), daemon=True).start()

    def _fetch_systems(self, esde_dir: str) -> None:
        gamelists = Path(esde_dir) / "gamelists"
        try:
            systems = sorted(p.name for p in gamelists.iterdir() if p.is_dir())
        except Exception:
            systems = []
        self.after(0, self._apply_systems, systems)

    def _apply_systems(self, systems: list[str]) -> None:
        no_systems_msg = "No systems found — check ES-DE data dir."
        if not systems:
            self._filter_grid.set_message(no_systems_msg)
            self._check_grid.set_message(no_systems_msg)
            self._filter_system_vars = {}
            self._system_vars = {}
            return
        filter_checked = set(self._cfg.get("skip_systems", []))
        self._filter_system_vars = self._filter_grid.populate(systems, filter_checked)
        for var in self._filter_system_vars.values():
            var.trace_add("write", self._schedule_save)
            var.trace_add("write", self._update_summary)
        checked = set(self._cfg.get("copy_all_systems", []))
        self._system_vars = self._check_grid.populate(systems, checked)
        for var in self._system_vars.values():
            var.trace_add("write", self._schedule_save)
            var.trace_add("write", self._update_summary)
        self._sr_combo["values"] = systems
        self._update_summary()

    def _update_summary(self, *_) -> None:
        parts = []
        try:
            r = self._rating_var.get()
            parts.append(f"Rating ≥ {r:g}")
        except tk.TclError:
            pass
        if self._include_unrated_var.get():
            parts.append("incl. unrated")
        if self._overwrite_var.get():
            parts.append("overwrite on")
        if self._verbose_var.get():
            parts.append("verbose")
        if self._filter_system_vars:
            n = sum(1 for v in self._filter_system_vars.values() if v.get())
            if self._systems_include_mode.get():
                parts.append(f"limited to {n} system{'s' if n != 1 else ''}" if n else "no systems")
            elif n:
                parts.append(f"{n} system{'s' if n != 1 else ''} excluded")
        if self._system_ratings:
            n = len(self._system_ratings)
            parts.append(f"{n} rating override{'s' if n != 1 else ''}")
        if self._system_vars:
            n = sum(1 for v in self._system_vars.values() if v.get())
            if n:
                parts.append(f"{n} bypass rating")
        self._summary_lbl.configure(text="  ·  ".join(parts))

    # ── per-system rating helpers ─────────────────────────────────────

    def _sr_populate(self) -> None:
        self._sr_tree.delete(*self._sr_tree.get_children())
        for sys_name, rating in sorted(self._system_ratings.items()):
            self._sr_tree.insert("", "end", values=(sys_name, f"{rating:g}"))

    def _on_sr_select(self, _event=None) -> None:
        sel = self._sr_tree.selection()
        self._sr_remove_btn.configure(state="normal" if sel else "disabled")
        if sel:
            sys_name, rating_str = self._sr_tree.item(sel[0], "values")
            self._sr_system_var.set(sys_name)
            try:
                self._sr_rating_var.set(float(rating_str))
            except ValueError:
                pass

    def _on_sr_add(self) -> None:
        sys_name = self._sr_system_var.get().strip()
        if not sys_name:
            return
        try:
            rating = float(self._sr_rating_var.get())
        except (ValueError, tk.TclError):
            return
        self._system_ratings[sys_name] = rating
        self._sr_populate()
        self._schedule_save()
        self._update_summary()

    def _on_sr_clear(self) -> None:
        self._system_ratings.clear()
        self._sr_populate()
        self._sr_remove_btn.configure(state="disabled")
        self._schedule_save()
        self._update_summary()

    def _on_sr_remove(self) -> None:
        sel = self._sr_tree.selection()
        if not sel:
            return
        sys_name = self._sr_tree.item(sel[0], "values")[0]
        self._system_ratings.pop(sys_name, None)
        self._sr_populate()
        self._sr_remove_btn.configure(state="disabled")
        self._schedule_save()
        self._update_summary()

    # ── launch ────────────────────────────────────────────────────────

    def _collect_cfg(self) -> dict:
        if self._system_vars:
            copy_all = sorted(n for n, v in self._system_vars.items() if v.get())
        else:
            copy_all = self._cfg.get("copy_all_systems", [])
        if self._filter_system_vars:
            filter_systems = sorted(n for n, v in self._filter_system_vars.items() if v.get())
        else:
            filter_systems = self._cfg.get("skip_systems", [])
        return {
            "roms_dir":             self._roms_dir_var.get().strip(),
            "esde_data_dir":        self._esde_dir_var.get().strip(),
            "target_roms_dir":      self._target_roms_var.get().strip(),
            "target_esde_data_dir": self._target_esde_var.get().strip(),
            "rating":               self._rating_var.get(),
            "overwrite":            self._overwrite_var.get(),
            "include_unrated":      self._include_unrated_var.get(),
            "verbose":              self._verbose_var.get(),
            "skip_systems":         [] if self._systems_include_mode.get() else filter_systems,
            "copy_all_systems":     copy_all,
            "system_ratings":       dict(self._system_ratings),
        }

    def _validate(self, cfg: dict, require_targets: bool) -> str | None:
        if not cfg["roms_dir"]:
            return "ROMs dir is required."
        if not cfg["esde_data_dir"]:
            return "ES-DE data dir is required."
        if require_targets and not cfg["target_roms_dir"]:
            return "Target ROMs dir is required."
        if require_targets and not cfg["target_esde_data_dir"]:
            return "Target ES-DE dir is required."
        return None

    def _on_dry_run(self) -> None:
        self._launch(dry_run=True)

    def _on_run(self) -> None:
        self._launch(dry_run=False)

    def _launch(self, dry_run: bool) -> None:
        cfg = self._collect_cfg()
        err = self._validate(cfg, require_targets=True)
        if err:
            messagebox.showerror("Missing input", err, parent=self)
            return

        save_config(cfg)
        self._cfg = cfg

        cmd = [sys.executable, str(SCRIPT_FILE)]
        if dry_run:
            cmd.append("--dry-run")
        else:
            cmd.append("--yes")
        if self._include_unrated_var.get():
            cmd.append("--include-unrated")
        if self._verbose_var.get():
            cmd.append("--verbose")
        filter_systems = sorted(n for n, v in self._filter_system_vars.items() if v.get())
        if self._systems_include_mode.get() and filter_systems:
            cmd.append("--systems")
            cmd.extend(filter_systems)

        self._set_running(True)
        self._clear_output()
        self._append("$ " + " ".join(cmd) + "\n\n")

        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self._reader, daemon=True).start()
        self.after(50, self._drain)

    def _reader(self) -> None:
        assert self._process and self._process.stdout
        for line in self._process.stdout:
            self._queue.put(line)
        self._process.wait()
        self._queue.put(None)

    def _drain(self) -> None:
        try:
            while True:
                item = self._queue.get_nowait()
                if item is None:
                    rc = self._process.returncode if self._process else -1
                    self._append(f"\n[exited with code {rc}]")
                    self._set_running(False)
                    return
                self._append(item)
        except queue.Empty:
            pass
        self.after(50, self._drain)

    # ── helpers ───────────────────────────────────────────────────────

    def _set_running(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        self._run_btn.configure(state=state)
        self._dry_run_btn.configure(state=state)
        self._status_lbl.configure(text="Running…" if running else "")

    def _clear_output(self) -> None:
        self._output.configure(state="normal")
        self._output.delete("1.0", "end")
        self._output.configure(state="disabled")

    def _append(self, text: str) -> None:
        self._output.configure(state="normal")
        self._output.insert("end", text)
        self._output.see("end")
        self._output.configure(state="disabled")


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
