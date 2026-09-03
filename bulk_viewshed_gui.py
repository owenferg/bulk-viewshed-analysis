#!/usr/bin/env python3
"""desktop interface for the bulk gdal viewshed engine"""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
from typing import Any

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError as error:  # pragma: no cover - this depends on the system python build
    raise SystemExit(
        "the tk desktop toolkit is not installed. install python3-tk on linux or use a "
        "standard python installer from python.org"
    ) from error

from bulk_viewshed import discover_dems, load_observers


APP_NAME = "bulk viewshed analysis"
PROJECT_ROOT = Path(__file__).resolve().parent
ENGINE = PROJECT_ROOT / "bulk_viewshed.py"
PROGRESS_PREFIX = "@@PROGRESS@@"
SETTINGS_PATH = Path.home() / ".bulk-viewshed-gui.json"
EXISTING_CHOICES = {
    "resume finished viewsheds": "resume",
    "rebuild matching viewsheds": "overwrite",
    "stop if output files exist": "stop",
}


def open_folder(path: Path) -> None:
    """open a folder with the file manager provided by the operating system"""

    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def counted(number: int, noun: str) -> str:
    """format a count without interface text like observer(s)"""

    ending = "" if number == 1 else "s"
    return f"{number} {noun}{ending}"


class PathRow(ttk.Frame):
    """a text field paired with the right file or folder picker"""

    def __init__(
        self,
        parent: tk.Misc,
        variable: tk.StringVar,
        title: str,
        directory: bool,
        filetypes: tuple[tuple[str, str], ...] = (("all files", "*.*"),),
    ) -> None:
        """set up a reusable path input"""

        super().__init__(parent)
        self.variable = variable
        self.title = title
        self.directory = directory
        self.filetypes = filetypes
        ttk.Entry(self, textvariable=variable).pack(side="left", fill="x", expand=True)
        ttk.Button(self, text="browse…", command=self.choose).pack(side="left", padx=(6, 0))

    def choose(self) -> None:
        """open the picker in the folder nearest the current value"""

        current = self.variable.get().strip()
        if self.directory:
            selected = filedialog.askdirectory(title=self.title, initialdir=current or None)
        else:
            selected = filedialog.askopenfilename(
                title=self.title,
                initialdir=str(Path(current).parent) if current else None,
                filetypes=self.filetypes,
            )
        if selected:
            self.variable.set(selected)


class ViewshedWindow(tk.Tk):
    """collect settings and keep long running gdal work outside the interface"""

    def __init__(self) -> None:
        """restore the last session and build the main window"""

        super().__init__()
        self.title(APP_NAME)
        self.geometry("920x780")
        self.minsize(760, 650)
        self.protocol("WM_DELETE_WINDOW", self.close_window)

        self.process: subprocess.Popen[str] | None = None
        self.messages: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.completed_ids: set[str] = set()
        self.failed_ids: set[str] = set()
        self.total_observers = 0
        self.run_kind = "run"
        self.cancel_requested = False
        self.dem_paths: list[str] = []

        self._create_variables()
        self._load_settings()
        self._build_interface()
        self.after(100, self._drain_messages)

    def _create_variables(self) -> None:
        """keep widget values in one place for saving and command building"""

        self.observers = tk.StringVar()
        self.output = tk.StringVar()
        self.observer_crs = tk.StringVar(value="EPSG:4326")
        self.analysis_crs = tk.StringVar(value="auto-utm")
        self.cell_size = tk.StringVar(value="10")
        self.max_distance = tk.StringVar(value="20000")
        self.observer_height = tk.StringVar(value="2")
        self.target_height = tk.StringVar(value="0")
        self.jobs = tk.IntVar(value=min(4, os.cpu_count() or 1))
        self.existing = tk.StringVar(value="resume finished viewsheds")
        self.gdal_location = tk.StringVar()
        self.band = tk.IntVar(value=1)
        self.resampling = tk.StringVar(value="bilinear")
        self.curvature = tk.StringVar(value="0.85714")
        self.source_nodata = tk.StringVar()
        self.output_mode = tk.StringVar(value="normal")
        self.allow_gaps = tk.BooleanVar(value=False)
        self.allow_outside = tk.BooleanVar(value=False)
        self.allow_extra = tk.BooleanVar(value=True)
        self.keep_work = tk.BooleanVar(value=False)
        self.hash_sources = tk.BooleanVar(value=False)
        self.fail_fast = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="ready, choose observer locations and elevation data")

    def _build_interface(self) -> None:
        """arrange the everyday workflow above progress and logs"""

        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)

        heading = ttk.Label(root, text=APP_NAME, font=("TkDefaultFont", 16, "bold"))
        heading.pack(anchor="w")
        ttk.Label(
            root,
            text="find the ground each observer can see using your elevation data",
            wraplength=860,
        ).pack(anchor="w", pady=(2, 10))

        notebook = ttk.Notebook(root)
        notebook.pack(fill="x")
        basic = ttk.Frame(notebook, padding=10)
        advanced = ttk.Frame(notebook, padding=10)
        notebook.add(basic, text="inputs and settings")
        notebook.add(advanced, text="advanced")
        self._build_basic_tab(basic)
        self._build_advanced_tab(advanced)

        progress_frame = ttk.LabelFrame(root, text="progress", padding=8)
        progress_frame.pack(fill="x", pady=(10, 6))
        ttk.Label(progress_frame, textvariable=self.status, wraplength=850).pack(anchor="w")
        self.progress_bar = ttk.Progressbar(progress_frame, mode="determinate", maximum=1)
        self.progress_bar.pack(fill="x", pady=(6, 0))

        log_frame = ttk.Frame(root)
        log_frame.pack(fill="both", expand=True)
        self.log = tk.Text(log_frame, height=12, wrap="word", state="disabled")
        self.log.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(log_frame, command=self.log.yview)
        scrollbar.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=scrollbar.set)

        buttons = ttk.Frame(root)
        buttons.pack(fill="x", pady=(8, 0))
        self.check_button = ttk.Button(buttons, text="check inputs", command=self.check_inputs)
        self.check_button.pack(side="left")
        self.run_button = ttk.Button(buttons, text="run viewsheds", command=self.start_run)
        self.run_button.pack(side="left", padx=6)
        self.cancel_button = ttk.Button(buttons, text="cancel", command=self.cancel_run)
        self.cancel_button.pack(side="left")
        self.open_button = ttk.Button(buttons, text="open output folder", command=self.open_output)
        self.open_button.pack(side="right")
        self._set_running(False)

    def _build_basic_tab(self, parent: ttk.Frame) -> None:
        """build the inputs most runs need and explain each setting in place"""

        parent.columnconfigure(1, weight=1)
        row = 0
        ttk.Label(parent, text="observer locations csv").grid(
            row=row, column=0, sticky="w", padx=(0, 8)
        )
        PathRow(
            parent,
            self.observers,
            "choose observer locations csv",
            False,
            (("csv files", "*.csv"), ("all files", "*.*")),
        ).grid(row=row, column=1, sticky="ew")
        ttk.Button(parent, text="create template…", command=self.create_template).grid(
            row=row, column=2, padx=(6, 0)
        )

        row += 1
        ttk.Label(parent, text="terrain elevation data").grid(
            row=row, column=0, sticky="nw", pady=(8, 0), padx=(0, 8)
        )
        dem_frame = ttk.Frame(parent)
        dem_frame.grid(row=row, column=1, columnspan=2, sticky="ew", pady=(8, 0))
        dem_frame.columnconfigure(0, weight=1)
        self.dem_list = tk.Listbox(dem_frame, height=4)
        self.dem_list.grid(row=0, column=0, rowspan=3, sticky="ew")
        ttk.Button(dem_frame, text="add files…", command=self.add_dem_files).grid(
            row=0, column=1, padx=(6, 0), sticky="ew"
        )
        ttk.Button(dem_frame, text="add folder…", command=self.add_dem_folder).grid(
            row=1, column=1, padx=(6, 0), sticky="ew"
        )
        ttk.Button(dem_frame, text="remove", command=self.remove_dems).grid(
            row=2, column=1, padx=(6, 0), sticky="ew"
        )
        self._refresh_dem_list()

        row += 1
        ttk.Label(parent, text="output folder").grid(
            row=row, column=0, sticky="w", pady=(8, 0), padx=(0, 8)
        )
        PathRow(parent, self.output, "choose output folder", True).grid(
            row=row, column=1, columnspan=2, sticky="ew", pady=(8, 0)
        )

        separator = ttk.Separator(parent)
        row += 1
        separator.grid(row=row, column=0, columnspan=3, sticky="ew", pady=10)

        fields = (
            ("location crs", self.observer_crs, "the crs used by the x and y columns"),
            ("viewshed crs", self.analysis_crs, "auto-utm works well for most projects"),
            ("cell size", self.cell_size, "smaller cells show more detail but take longer"),
            ("maximum distance", self.max_distance, "the farthest distance to test"),
            ("observer height", self.observer_height, "height above the ground at each location"),
            ("target height", self.target_height, "height of the feature being viewed"),
        )
        for label, variable, help_text in fields:
            row += 1
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
            ttk.Entry(parent, textvariable=variable, width=24).grid(
                row=row,
                column=1,
                sticky="ew",
                pady=2,
            )
            ttk.Label(parent, text=help_text, foreground="#555555").grid(
                row=row, column=2, sticky="w", padx=(8, 0), pady=2
            )

        row += 1
        ttk.Label(parent, text="existing outputs").grid(row=row, column=0, sticky="w", pady=2)
        existing = ttk.Combobox(parent, textvariable=self.existing, state="readonly")
        existing["values"] = tuple(EXISTING_CHOICES)
        existing.grid(row=row, column=1, sticky="ew", pady=2)
        ttk.Label(parent, text="resume keeps finished work", foreground="#555555").grid(
            row=row, column=2, sticky="w", padx=(8, 0)
        )

        row += 1
        ttk.Label(parent, text="observers at once").grid(row=row, column=0, sticky="w", pady=2)
        ttk.Spinbox(parent, from_=1, to=max(1, os.cpu_count() or 1), textvariable=self.jobs).grid(
            row=row, column=1, sticky="ew", pady=2
        )
        ttk.Label(parent, text="2 to 4 works well on most computers", foreground="#555555").grid(
            row=row, column=2, sticky="w", padx=(8, 0)
        )

    def _build_advanced_tab(self, parent: ttk.Frame) -> None:
        """keep less common gdal controls away from the main workflow"""

        parent.columnconfigure(1, weight=1)
        fields: list[tuple[str, tk.Variable, str]] = [
            ("elevation band", self.band, "usually band 1"),
            ("terrain resampling", self.resampling, "bilinear works well for elevation"),
            ("earth curvature", self.curvature, "0.85714 includes standard refraction"),
            ("source nodata", self.source_nodata, "leave blank to use the raster value"),
            ("output mode", self.output_mode, "normal maps visible and hidden cells"),
        ]
        for row, (label, variable, help_text) in enumerate(fields):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
            if variable is self.resampling:
                field = ttk.Combobox(parent, textvariable=variable, state="readonly")
                field["values"] = ("near", "bilinear", "cubic", "cubicspline", "lanczos")
            elif variable is self.output_mode:
                field = ttk.Combobox(parent, textvariable=variable, state="readonly")
                field["values"] = ("normal", "dem", "ground")
            else:
                field = ttk.Entry(parent, textvariable=variable)
            field.grid(row=row, column=1, sticky="ew", pady=3)
            ttk.Label(parent, text=help_text, foreground="#555555").grid(
                row=row, column=2, sticky="w", padx=(8, 0)
            )

        row = len(fields)
        ttk.Separator(parent).grid(row=row, column=0, columnspan=3, sticky="ew", pady=10)
        checks = (
            (self.allow_extra, "ignore extra csv columns from gis exports"),
            (self.allow_gaps, "allow missing elevation cells inside a viewshed"),
            (self.allow_outside, "allow locations outside the elevation data"),
            (self.keep_work, "keep projected terrain files"),
            (self.hash_sources, "slower but safer resume checks"),
            (self.fail_fast, "stop after the first failed observer"),
        )
        for variable, text in checks:
            row += 1
            ttk.Checkbutton(parent, variable=variable, text=text).grid(
                row=row, column=0, columnspan=3, sticky="w", pady=2
            )

        row += 1
        ttk.Separator(parent).grid(row=row, column=0, columnspan=3, sticky="ew", pady=10)
        row += 1
        ttk.Label(parent, text="gdal or qgis location").grid(
            row=row, column=0, sticky="w", padx=(0, 8)
        )
        PathRow(parent, self.gdal_location, "choose a gdal or qgis folder", True).grid(
            row=row, column=1, sticky="ew"
        )
        ttk.Label(parent, text="leave blank to find it automatically", foreground="#555555").grid(
            row=row, column=2, sticky="w", padx=(8, 0)
        )

    def _load_settings(self) -> None:
        """restore useful values from the previous session when possible"""

        try:
            values = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        variable_names = (
            "observers",
            "output",
            "observer_crs",
            "analysis_crs",
            "cell_size",
            "max_distance",
            "observer_height",
            "target_height",
            "jobs",
            "existing",
            "gdal_location",
            "band",
            "resampling",
            "curvature",
            "source_nodata",
            "output_mode",
            "allow_gaps",
            "allow_outside",
            "allow_extra",
            "keep_work",
            "hash_sources",
            "fail_fast",
        )
        for name in variable_names:
            if name in values:
                try:
                    getattr(self, name).set(values[name])
                except tk.TclError:
                    pass
        old_existing_values = {value: label for label, value in EXISTING_CHOICES.items()}
        if self.existing.get() in old_existing_values:
            self.existing.set(old_existing_values[self.existing.get()])
        self.output_mode.set(self.output_mode.get().casefold())
        self.dem_paths = [str(value) for value in values.get("dem_paths", [])]

    def _save_settings(self) -> None:
        """remember form values without making a project file necessary"""

        names = (
            "observers",
            "output",
            "observer_crs",
            "analysis_crs",
            "cell_size",
            "max_distance",
            "observer_height",
            "target_height",
            "jobs",
            "existing",
            "gdal_location",
            "band",
            "resampling",
            "curvature",
            "source_nodata",
            "output_mode",
            "allow_gaps",
            "allow_outside",
            "allow_extra",
            "keep_work",
            "hash_sources",
            "fail_fast",
        )
        document = {name: getattr(self, name).get() for name in names}
        document["dem_paths"] = self.dem_paths
        try:
            SETTINGS_PATH.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass

    def _refresh_dem_list(self) -> None:
        """redraw the elevation list after an add or remove action"""

        if not hasattr(self, "dem_list"):
            return
        self.dem_list.delete(0, "end")
        for path in self.dem_paths:
            self.dem_list.insert("end", path)

    def add_dem_files(self) -> None:
        """add one or more elevation rasters while avoiding duplicates"""

        selected = filedialog.askopenfilenames(
            title="choose elevation rasters",
            filetypes=(("elevation rasters", "*.tif *.tiff *.img *.vrt"), ("all files", "*.*")),
        )
        for path in selected:
            if path not in self.dem_paths:
                self.dem_paths.append(path)
        self._refresh_dem_list()

    def add_dem_folder(self) -> None:
        """add a folder that the engine will search recursively"""

        selected = filedialog.askdirectory(title="choose a folder with elevation rasters")
        if selected and selected not in self.dem_paths:
            self.dem_paths.append(selected)
        self._refresh_dem_list()

    def remove_dems(self) -> None:
        """remove the selected sources from the form but never from disk"""

        selected = set(self.dem_list.curselection())
        self.dem_paths = [
            path for index, path in enumerate(self.dem_paths) if index not in selected
        ]
        self._refresh_dem_list()

    def create_template(self) -> None:
        """write a small valid csv that is ready to edit or open in a gis"""

        selected = filedialog.asksaveasfilename(
            title="save an observer locations template",
            defaultextension=".csv",
            initialfile="observers.csv",
            filetypes=(("csv files", "*.csv"),),
        )
        if not selected:
            return
        Path(selected).write_text(
            "id,x,y,observer_height,target_height,max_distance\n"
            "example_site,-123.0000,45.0000,,,\n",
            encoding="utf-8",
        )
        self.observers.set(selected)
        self.status.set("template created, replace the example row with your observer locations")

    def _numeric(self, variable: tk.Variable, label: str, *, positive: bool = False) -> float:
        """read one number from the form and give it a useful error label"""

        try:
            value = float(variable.get())
        except (TypeError, ValueError, tk.TclError) as error:
            raise ValueError(f"{label} must be a number") from error
        if positive and value <= 0:
            raise ValueError(f"{label} must be greater than zero")
        return value

    def validate_form(self) -> tuple[Path, list[Path], Path]:
        """catch simple path csv and number problems before starting a process"""

        observer_path = Path(self.observers.get().strip()).expanduser()
        output_path = Path(self.output.get().strip()).expanduser()
        if not self.observers.get().strip() or not observer_path.is_file():
            raise ValueError(f"observer locations csv not found: {observer_path}")
        if not self.dem_paths:
            raise ValueError("add at least one elevation raster or folder")
        dem_inputs = [Path(value).expanduser() for value in self.dem_paths]
        dem_files = discover_dems(dem_inputs)
        if not self.output.get().strip():
            raise ValueError("choose an output folder")
        cell_size = self._numeric(self.cell_size, "cell size", positive=True)
        max_distance = self._numeric(self.max_distance, "maximum distance", positive=True)
        observer_height = self._numeric(self.observer_height, "observer height")
        target_height = self._numeric(self.target_height, "target height")
        self._numeric(self.curvature, "curvature coefficient")
        if observer_height < 0 or target_height < 0:
            raise ValueError("observer and target heights cannot be negative")
        if self.source_nodata.get().strip():
            self._numeric(self.source_nodata, "source nodata")
        if not self.observer_crs.get().strip() or not self.analysis_crs.get().strip():
            raise ValueError("both crs fields are required")
        observers = load_observers(
            observer_path,
            observer_height,
            target_height,
            max_distance,
            self.allow_extra.get(),
        )
        self.status.set(
            f"inputs look good: {counted(len(observers), 'observer')} and "
            f"{counted(len(dem_files), 'elevation raster')} at {cell_size:g} unit cells"
        )
        return observer_path, dem_inputs, output_path

    def command_arguments(self, dry_run: bool) -> list[str]:
        """translate the form into the same arguments used by the public cli"""

        observer_path, dem_inputs, output_path = self.validate_form()
        arguments = [
            str(ENGINE),
            "--observers", str(observer_path),
            "--output-dir", str(output_path),
            "--observer-crs", self.observer_crs.get().strip(),
            "--analysis-crs", self.analysis_crs.get().strip(),
            "--cell-size", self.cell_size.get().strip(),
            "--max-distance", self.max_distance.get().strip(),
            "--observer-height", self.observer_height.get().strip(),
            "--target-height", self.target_height.get().strip(),
            "--jobs", str(self.jobs.get()),
            "--band", str(self.band.get()),
            "--resampling", self.resampling.get(),
            "--curvature-coefficient", self.curvature.get().strip(),
            "--output-mode", self.output_mode.get().upper(),
            "--json-progress",
        ]
        for dem in dem_inputs:
            arguments.extend(("--dem", str(dem)))
        existing_policy = EXISTING_CHOICES.get(self.existing.get(), "resume")
        if existing_policy == "resume":
            arguments.append("--resume")
        elif existing_policy == "overwrite":
            arguments.append("--overwrite")
        if self.gdal_location.get().strip():
            arguments.extend(("--gdal-bin", self.gdal_location.get().strip()))
        if self.source_nodata.get().strip():
            arguments.extend(("--source-nodata", self.source_nodata.get().strip()))
        flags = (
            (self.allow_gaps, "--allow-dem-gaps"),
            (self.allow_outside, "--allow-outside"),
            (self.allow_extra, "--allow-extra-columns"),
            (self.keep_work, "--keep-work"),
            (self.hash_sources, "--hash-sources"),
            (self.fail_fast, "--fail-fast"),
        )
        arguments.extend(flag for variable, flag in flags if variable.get())
        if dry_run:
            arguments.append("--dry-run")
        return arguments

    def check_inputs(self) -> None:
        """run the complete preflight without writing viewshed outputs"""

        self._launch(dry_run=True)

    def start_run(self) -> None:
        """confirm a rebuild when needed and start the real analysis"""

        if EXISTING_CHOICES.get(self.existing.get()) == "overwrite":
            confirmed = messagebox.askyesno(
                "rebuild finished viewsheds?",
                "viewsheds that match these settings will be rebuilt. "
                "other files will stay in place. keep going?",
            )
            if not confirmed:
                return
        self._launch(dry_run=False)

    def _launch(self, dry_run: bool) -> None:
        """reset run state and hand the command to a background reader thread"""

        if self.process and self.process.poll() is None:
            return
        try:
            arguments = self.command_arguments(dry_run)
        except (OSError, ValueError, tk.TclError) as error:
            messagebox.showerror("cannot start", str(error))
            return
        self._save_settings()
        self.run_kind = "check" if dry_run else "run"
        self.completed_ids.clear()
        self.failed_ids.clear()
        self.total_observers = 0
        self.cancel_requested = False
        self.progress_bar.configure(maximum=1, value=0)
        self._clear_log()
        self._append_log(
            "checking the csv elevation data and gdal..." if dry_run else "starting viewsheds..."
        )
        self.status.set("checking inputs and gdal…" if dry_run else "starting viewsheds…")
        self._set_running(True)
        threading.Thread(target=self._run_process, args=(arguments,), daemon=True).start()

    def _run_process(self, arguments: list[str]) -> None:
        """run the engine and pass each output line back to the main thread"""

        options: dict[str, Any] = {
            "cwd": str(PROJECT_ROOT),
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
        }
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        try:
            self.process = subprocess.Popen([sys.executable, *arguments], **options)
            assert self.process.stdout is not None
            for line in self.process.stdout:
                self.messages.put(("line", line.rstrip()))
            exit_code = self.process.wait()
            self.messages.put(("exit", exit_code))
        except Exception as error:
            self.messages.put(("error", str(error)))

    def _drain_messages(self) -> None:
        """handle process messages on the tkinter thread where widgets are safe"""

        try:
            while True:
                kind, value = self.messages.get_nowait()
                if kind == "line":
                    self._handle_line(value)
                elif kind == "exit":
                    self._handle_exit(int(value))
                elif kind == "error":
                    self._append_log(f"error: {value}")
                    self.status.set("could not start the viewshed process")
                    self._set_running(False)
        except queue.Empty:
            pass
        self.after(100, self._drain_messages)

    def _handle_line(self, line: str) -> None:
        """separate progress events from lines meant for the visible log"""

        if line.startswith(PROGRESS_PREFIX):
            try:
                event = json.loads(line[len(PROGRESS_PREFIX):])
            except json.JSONDecodeError:
                self._append_log(line)
                return
            self._handle_progress(event)
        elif line:
            self._append_log(line)

    def _handle_progress(self, event: dict[str, Any]) -> None:
        """turn one engine event into a short status and progress value"""

        event_type = event.get("event")
        if event_type == "plan":
            self.total_observers = int(event.get("observers", 0))
            self.progress_bar.configure(maximum=max(1, self.total_observers), value=0)
            self.status.set(
                f"ready for {counted(self.total_observers, 'observer')} using "
                f"{counted(int(event.get('dems', 0)), 'elevation raster')} with "
                f"{event.get('gdal_version', 'gdal')}"
            )
        elif event_type == "observer":
            identifier = str(event.get("id", "observer"))
            stage = event.get("stage")
            if stage == "complete":
                self.completed_ids.add(identifier)
                self.status.set(
                    f"completed {len(self.completed_ids)} of {self.total_observers}: {identifier}"
                )
            elif stage == "failed":
                self.failed_ids.add(identifier)
                self.status.set(f"failed: {identifier}, continuing with the other observers")
            else:
                label = "preparing terrain" if stage == "preparing" else "calculating viewshed"
                self.status.set(f"{identifier}: {label}")
            self.progress_bar["value"] = len(self.completed_ids | self.failed_ids)
        elif event_type == "finished":
            if event.get("status") == "validated":
                self.status.set("inputs passed the check, no viewsheds were created")
            else:
                self.status.set(
                    f"finished: {event.get('complete', 0)} complete and "
                    f"{event.get('failed', 0)} failed"
                )

    def _handle_exit(self, exit_code: int) -> None:
        """return the interface to idle and explain how the process ended"""

        self._set_running(False)
        if self.cancel_requested or exit_code == 130:
            self.status.set("run cancelled, finished viewsheds can be resumed")
        elif exit_code == 0:
            if self.run_kind == "check":
                self.status.set("inputs passed the check, run viewsheds when you are ready")
            else:
                self.progress_bar["value"] = max(1, self.total_observers)
                self.status.set(f"viewsheds complete, outputs are in {self.output.get()}")
        else:
            self.status.set(f"stopped with exit code {exit_code}, check the log for details")
            messagebox.showerror(
                "viewsheds stopped",
                "the run did not complete. check the log for details",
            )
        self.process = None

    def cancel_run(self) -> None:
        """send a normal interrupt before falling back to a forced stop"""

        process = self.process
        if not process or process.poll() is not None:
            return
        self.cancel_requested = True
        self.status.set("stopping active gdal work…")
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(process.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            process.terminate()
        self.after(5000, self._force_cancel)

    def _force_cancel(self) -> None:
        """stop the full process group when a normal interrupt is not enough"""

        process = self.process
        if not process or process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                )
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            process.kill()

    def _set_running(self, running: bool) -> None:
        """enable only the buttons that make sense for the current state"""

        state = "disabled" if running else "normal"
        self.check_button.configure(state=state)
        self.run_button.configure(state=state)
        self.cancel_button.configure(state="normal" if running else "disabled")
        self.open_button.configure(state="disabled" if running else "normal")

    def _append_log(self, text: str) -> None:
        """append one line while keeping the read only log at its end"""

        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self) -> None:
        """clear messages left by the previous check or run"""

        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def open_output(self) -> None:
        """create and open the chosen output folder"""

        if not self.output.get().strip():
            messagebox.showinfo("no output folder", "choose an output folder first")
            return
        try:
            open_folder(Path(self.output.get()).expanduser().resolve())
        except OSError as error:
            messagebox.showerror("could not open folder", str(error))

    def close_window(self) -> None:
        """save settings and protect an active run from an accidental close"""

        if self.process and self.process.poll() is None:
            if not messagebox.askyesno(
                "cancel the active run?",
                "closing the window will stop active gdal work. "
                "finished viewsheds can still be resumed",
            ):
                return
            self.cancel_run()
        self._save_settings()
        self.destroy()


def main() -> int:
    """start the desktop event loop"""

    window = ViewshedWindow()
    window.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
