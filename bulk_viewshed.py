#!/usr/bin/env python3
"""run repeatable gdal viewsheds for observers listed in a csv file"""

from __future__ import annotations

import argparse
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
from typing import Any, Iterable, Sequence

from gdal_runtime import discover_gdal_runtime


TOOL_VERSION = "1.2.0"
# only changes when raster results change so viewsheds finished by an earlier
# release with the same raster pipeline can still be resumed
RASTER_PIPELINE_VERSION = "1.1.0"
POLYGON_PIPELINE_VERSION = 1
MINIMUM_GDAL_VERSION = (3, 4, 2)
# each observer needs its own terrain crop in memory so half the cores is a
# safer starting point than all of them
DEFAULT_JOBS = max(1, min(4, (os.cpu_count() or 2) // 2))
RASTER_TOOLS = (
    "gdal_viewshed",
    "gdalbuildvrt",
    "gdalinfo",
    "gdalsrsinfo",
    "gdaltransform",
    "gdalwarp",
)
POLYGON_TOOLS = ("gdal_polygonize", "ogr2ogr", "ogrinfo")
SHAPEFILE_PARTS = (".shp", ".shx", ".dbf", ".prj", ".cpg")
# shapefile attribute names are limited to 10 characters
SHAPEFILE_FIELD_NAMES = {
    "observer_height": "obs_height",
    "target_height": "tgt_height",
    "max_distance": "max_dist",
    "analysis_crs": "crs",
    "visible_area": "vis_area",
}
# the values of an observer that change its viewshed; its name and its row in
# the csv do not, so renumbering or reordering rows keeps finished work
OBSERVER_KEY_FIELDS = ("x", "y", "observer_height", "target_height", "max_distance")
OBSERVER_LAYER = "viewsheds"
COVERAGE_LAYER = "coverage"
# share of one observer's work that is finished when each stage starts
STAGE_STARTS = {
    False: {"preparing": 0.0, "viewshed": 0.5, "complete": 1.0},
    True: {"preparing": 0.0, "viewshed": 0.2, "polygon": 0.4, "complete": 1.0},
}
DISCOVERED_RASTER_EXTENSIONS = {".tif", ".tiff", ".img"}
CSV_COLUMNS = {
    "id",
    "x",
    "y",
    "observer_height",
    "target_height",
    "max_distance",
}
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}

PRINT_LOCK = threading.Lock()
CANCEL_REQUESTED = threading.Event()
SIGNAL_CANCELLED = threading.Event()
ACTIVE_PROCESSES: set[subprocess.Popen[str]] = set()
ACTIVE_LOCK = threading.Lock()
JSON_PROGRESS = False


class ToolError(RuntimeError):
    """raised when a gdal command cannot finish successfully"""


class CancelledError(RuntimeError):
    """raised when the current run is cancelled"""


@dataclass(frozen=True)
class Observer:
    """one observer row after csv values and defaults have been resolved"""

    row_number: int
    id: str
    x: float
    y: float
    observer_height: float
    target_height: float
    max_distance: float


@dataclass(frozen=True)
class Plan:
    """the projected location and output paths for one observer"""

    observer: Observer
    analysis_crs: str
    observer_x: float
    observer_y: float
    stem: str
    output: Path
    state: Path
    polygon: Path
    shapefile: Path


@dataclass(frozen=True)
class PolygonSettings:
    """polygon choices shared by every observer in a run"""

    crs: str
    simplify: float
    clip: Path | None  # already projected to the polygon crs
    config_hash: str
    shapefiles: bool = False


@dataclass(frozen=True)
class CommandResult:
    """captured output and timing from one external command"""

    stdout: str
    stderr: str
    elapsed_seconds: float


def utc_now() -> str:
    """return a compact utc timestamp for state and manifest files"""

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def format_duration(seconds: float) -> str:
    """shorten a run time for log lines"""

    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)}m {remainder:.0f}s"
    hours, minutes = divmod(minutes, 60)
    return f"{int(hours)}h {int(minutes)}m"


def log(message: str) -> None:
    """print one complete line when several workers are active"""

    with PRINT_LOCK:
        print(message, flush=True)


def progress(event: str, **values: Any) -> None:
    """send a machine readable event when a gui is listening"""

    if JSON_PROGRESS:
        payload = {"event": event, **values}
        log("@@PROGRESS@@" + json.dumps(payload, separators=(",", ":")))


def format_command(command: Sequence[str]) -> str:
    """format a command so a failed call can be copied into a terminal"""

    if os.name == "nt":
        return subprocess.list2cmdline(list(command))
    import shlex

    return shlex.join(command)


def terminate_process_tree(process: subprocess.Popen[str], force: bool = False) -> None:
    """stop a gdal process and any children started by a script wrapper"""

    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            arguments = ["taskkill", "/PID", str(process.pid), "/T"]
            if force:
                arguments.append("/F")
            subprocess.run(arguments, capture_output=True, check=False, timeout=10)
        else:
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
    except (OSError, ProcessLookupError, subprocess.SubprocessError):
        try:
            process.kill() if force else process.terminate()
        except (OSError, ProcessLookupError):
            pass
    if process.poll() is None:
        try:
            process.kill() if force else process.terminate()
        except (OSError, ProcessLookupError):
            pass


def request_cancel(_signum: int, _frame: Any) -> None:
    """record a signal from the shell or gui and stop active gdal work"""

    SIGNAL_CANCELLED.set()
    cancel_active_processes()


def cancel_active_processes() -> None:
    """ask every running command to stop as one cancellation operation"""

    CANCEL_REQUESTED.set()
    with ACTIVE_LOCK:
        processes = list(ACTIVE_PROCESSES)
    for process in processes:
        terminate_process_tree(process)


# observer input and portable names


def finite_number(value: str, label: str, row_number: int) -> float:
    """read one finite csv number and include its row in any error"""

    try:
        number = float(value)
    except ValueError as error:
        raise ValueError(f"csv row {row_number}: {label} must be a number") from error
    if not math.isfinite(number):
        raise ValueError(f"csv row {row_number}: {label} must be finite")
    return number


def optional_number(
    row: dict[str, str | None],
    column: str,
    fallback: float | None,
    row_number: int,
) -> float:
    """use a row value when present and otherwise use the run default"""

    raw = (row.get(column) or "").strip()
    if raw:
        return finite_number(raw, column, row_number)
    if fallback is None:
        raise ValueError(
            f"csv row {row_number}: {column} is blank and no command line default was supplied"
        )
    return fallback


def load_observers(
    path: Path,
    default_observer_height: float,
    default_target_height: float,
    default_max_distance: float | None,
    allow_extra_columns: bool = False,
) -> list[Observer]:
    """load and validate observer rows before any gdal work starts"""

    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        headers = reader.fieldnames
        if not headers:
            raise ValueError(f"observer csv has no header: {path}")
        normalized = [header.strip() for header in headers]
        if len(normalized) != len(set(normalized)):
            raise ValueError("observer csv contains duplicate column names")
        required = {"id", "x", "y"}
        missing = sorted(required - set(normalized))
        if missing:
            raise ValueError(f"observer csv is missing required columns: {', '.join(missing)}")
        unknown = sorted(set(normalized) - CSV_COLUMNS)
        if unknown and not allow_extra_columns:
            raise ValueError(
                "observer csv contains unknown columns: "
                + ", ".join(unknown)
                + " (use --allow-extra-columns to ignore them)"
            )
        reader.fieldnames = normalized

        observers: list[Observer] = []
        seen_ids: set[str] = set()
        for row_number, row in enumerate(reader, start=2):
            if row.get(None):
                raise ValueError(f"csv row {row_number}: contains more values than the header")
            identifier = (row.get("id") or "").strip()
            if not identifier:
                raise ValueError(f"csv row {row_number}: id cannot be blank")
            folded = identifier.casefold()
            if folded in seen_ids:
                raise ValueError(f"csv row {row_number}: duplicate id {identifier!r}")
            seen_ids.add(folded)
            x = finite_number((row.get("x") or "").strip(), "x", row_number)
            y = finite_number((row.get("y") or "").strip(), "y", row_number)
            observer_height = optional_number(
                row, "observer_height", default_observer_height, row_number
            )
            target_height = optional_number(row, "target_height", default_target_height, row_number)
            max_distance = optional_number(row, "max_distance", default_max_distance, row_number)
            if observer_height < 0:
                raise ValueError(f"csv row {row_number}: observer_height cannot be negative")
            if target_height < 0:
                raise ValueError(f"csv row {row_number}: target_height cannot be negative")
            if max_distance <= 0:
                raise ValueError(f"csv row {row_number}: max_distance must be positive")
            observers.append(
                Observer(
                    row_number,
                    identifier,
                    x,
                    y,
                    observer_height,
                    target_height,
                    max_distance,
                )
            )
    if not observers:
        raise ValueError(f"observer csv contains no data rows: {path}")
    return observers


def safe_stem(identifier: str) -> str:
    """make an output name from an id that works on every supported system"""

    normalized = unicodedata.normalize("NFKD", identifier)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", ascii_text).strip(" ._-")
    cleaned = re.sub(r"_+", "_", cleaned) or "observer"
    if cleaned.upper() in WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned[:80].rstrip(" .") or "observer"


def observer_stems(observers: Sequence[Observer]) -> dict[str, str]:
    """name each observer's outputs after its id alone

    Names used to start with the csv row number, so adding a row above an
    observer renamed its files and its viewshed was calculated again.
    """

    cleaned = {observer.id: safe_stem(observer.id) for observer in observers}
    counts: dict[str, int] = {}
    for stem in cleaned.values():
        counts[stem.casefold()] = counts.get(stem.casefold(), 0) + 1
    return {
        # different ids can clean to the same name and then need telling apart
        identifier: stem
        if counts[stem.casefold()] == 1
        else f"{stem}_{hashlib.sha256(identifier.encode('utf-8')).hexdigest()[:8]}"
        for identifier, stem in cleaned.items()
    }


def discover_dems(paths: Iterable[Path]) -> list[Path]:
    """collect raster files from individual paths and nested folders"""

    discovered: set[Path] = set()
    for path in paths:
        expanded = path.expanduser().resolve()
        if expanded.is_file():
            discovered.add(expanded)
        elif expanded.is_dir():
            discovered.update(
                candidate.resolve()
                for candidate in expanded.rglob("*")
                if candidate.is_file()
                and candidate.suffix.casefold() in DISCOVERED_RASTER_EXTENSIONS
            )
        else:
            raise ValueError(f"dem input does not exist: {path}")
    if not discovered:
        raise ValueError("no dem rasters were found")
    return sorted(discovered, key=lambda item: str(item).casefold())


# gdal command lookup and process handling


class Toolchain:
    """find and run the small set of gdal commands used by the analysis"""

    def __init__(
        self,
        gdal_bin: Path | None = None,
        timeout: float | None = None,
        gdal_python: Path | None = None,
        tool_directories: Sequence[Path] | None = None,
        environment: dict[str, str] | None = None,
    ) -> None:
        """keep command lookup and subprocess settings together for the run"""

        self.gdal_bin = gdal_bin.expanduser().resolve() if gdal_bin else None
        self.timeout = timeout
        self.gdal_python = str(gdal_python.expanduser().resolve()) if gdal_python else None
        self.tool_directories = tuple(tool_directories or ())
        self.environment = environment
        self._commands: dict[str, list[str]] = {}

    def command(self, name: str) -> list[str]:
        """resolve a gdal command once and reuse that path for later calls"""

        if name in self._commands:
            return list(self._commands[name])
        candidates: list[Path] = []
        suffixes = (".exe", "", ".py", ".bat", ".cmd") if os.name == "nt" else ("", ".py")
        if self.gdal_bin:
            candidates.extend(self.gdal_bin / f"{name}{suffix}" for suffix in suffixes)
        elif self.tool_directories:
            for directory in self.tool_directories:
                candidates.extend(directory / f"{name}{suffix}" for suffix in suffixes)
        else:
            for candidate_name in (name, f"{name}.py"):
                resolved = shutil.which(candidate_name)
                if resolved:
                    candidates.append(Path(resolved))
        executable = next((candidate for candidate in candidates if candidate.is_file()), None)
        if executable is None:
            location = f" under {self.gdal_bin}" if self.gdal_bin else " on PATH"
            raise RuntimeError(f"required gdal command {name!r} was not found{location}")
        suffix = executable.suffix.casefold()
        if suffix == ".py":
            if os.name != "nt" and os.access(executable, os.X_OK) and not self.gdal_python:
                command = [str(executable)]
            else:
                command = [self.gdal_python or sys.executable, str(executable)]
        elif suffix in {".bat", ".cmd"}:
            command = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(executable)]
        else:
            command = [str(executable)]
        self._commands[name] = command
        return list(command)

    def run(
        self,
        name: str,
        arguments: Sequence[str],
        *,
        input_text: str | None = None,
        check: bool = True,
    ) -> CommandResult:
        """run one command with captured output cancellation and timeout handling"""

        if CANCEL_REQUESTED.is_set():
            raise CancelledError("run cancelled")
        command = [*self.command(name), *map(str, arguments)]
        process_options: dict[str, Any] = {}
        if os.name == "nt":
            process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            process_options["start_new_session"] = True
        started = time.monotonic()
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self.environment,
            **process_options,
        )
        with ACTIVE_LOCK:
            ACTIVE_PROCESSES.add(process)
        if CANCEL_REQUESTED.is_set():
            terminate_process_tree(process)
        try:
            try:
                stdout, stderr = process.communicate(input=input_text, timeout=self.timeout)
            except subprocess.TimeoutExpired as error:
                terminate_process_tree(process, force=True)
                stdout, stderr = process.communicate()
                raise ToolError(
                    f"command timed out after {self.timeout:g} seconds: {format_command(command)}"
                ) from error
        finally:
            with ACTIVE_LOCK:
                ACTIVE_PROCESSES.discard(process)
        if CANCEL_REQUESTED.is_set():
            raise CancelledError("run cancelled")
        if check and process.returncode:
            details = "\n".join((stderr or stdout).strip().splitlines()[-20:])
            raise ToolError(
                f"command failed with exit code {process.returncode}: {format_command(command)}"
                + (f"\n{details}" if details else "")
            )
        return CommandResult(stdout, stderr, time.monotonic() - started)

    def gdal_version(self) -> tuple[str, tuple[int, ...]]:
        """read the installed version and reject older viewshed implementations"""

        result = self.run("gdal_viewshed", ["--version"], check=False)
        text = (result.stdout + "\n" + result.stderr).strip()
        match = re.search(r"GDAL\s+(\d+)\.(\d+)(?:\.(\d+))?", text, re.IGNORECASE)
        if not match:
            raise RuntimeError(f"could not determine gdal version from: {text!r}")
        version = tuple(int(part or 0) for part in match.groups())
        if version < MINIMUM_GDAL_VERSION:
            required = ".".join(map(str, MINIMUM_GDAL_VERSION))
            raise RuntimeError(f"gdal {required} or newer is required")
        return match.group(0), version


# coordinates and raster metadata


def transform_point(
    toolchain: Toolchain,
    x: float,
    y: float,
    source_crs: str,
    target_crs: str,
) -> tuple[float, float]:
    """transform one xy pair with the same gdal runtime as the analysis"""

    if source_crs.strip().casefold() == target_crs.strip().casefold():
        return x, y
    result = toolchain.run(
        "gdaltransform",
        ["-s_srs", source_crs, "-t_srs", target_crs],
        input_text=f"{x:.17g} {y:.17g}\n",
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise ToolError("gdaltransform returned no coordinate")
    parts = lines[-1].split()
    try:
        transformed = float(parts[0]), float(parts[1])
    except (IndexError, ValueError) as error:
        raise ToolError(f"could not parse gdaltransform output: {lines[-1]!r}") from error
    if not all(math.isfinite(value) for value in transformed):
        raise ToolError("gdaltransform returned non-finite coordinates")
    return transformed


def auto_utm_crs(longitude: float, latitude: float) -> str:
    """choose the local wgs 84 utm zone for a longitude and latitude"""

    if not -180 <= longitude <= 180 or not -80 <= latitude <= 84:
        raise ValueError(
            f"auto-utm requires longitude in [-180, 180] and latitude in [-80, 84]; "
            f"got {longitude}, {latitude}. Supply --analysis-crs for polar or wrapped data."
        )
    zone = min(60, max(1, int((longitude + 180) // 6) + 1))
    # these are the standard utm exceptions around norway and svalbard
    if 56 <= latitude < 64 and 3 <= longitude < 12:
        zone = 32
    elif 72 <= latitude <= 84:
        if 0 <= longitude < 9:
            zone = 31
        elif 9 <= longitude < 21:
            zone = 33
        elif 21 <= longitude < 33:
            zone = 35
        elif 33 <= longitude < 42:
            zone = 37
    return f"EPSG:{32600 + zone if latitude >= 0 else 32700 + zone}"


def affine_pixel(geotransform: Sequence[float], x: float, y: float) -> tuple[float, float]:
    """convert map coordinates to a raster column and row"""

    if len(geotransform) != 6:
        raise ValueError("source raster has an invalid geotransform")
    origin_x, pixel_x, rotation_x, origin_y, rotation_y, pixel_y = geotransform
    determinant = pixel_x * pixel_y - rotation_x * rotation_y
    if determinant == 0:
        raise ValueError("source raster geotransform is not invertible")
    delta_x = x - origin_x
    delta_y = y - origin_y
    column = (delta_x * pixel_y - delta_y * rotation_x) / determinant
    row = (delta_y * pixel_x - delta_x * rotation_y) / determinant
    return column, row


def inspect_raster(
    toolchain: Toolchain,
    raster: Path,
    stats: bool = False,
    checksum: bool = False,
    histogram: bool = False,
) -> dict[str, Any]:
    """read raster metadata as json with optional validation statistics"""

    arguments = ["-json"]
    if stats:
        arguments.append("-stats")
    if checksum:
        arguments.append("-checksum")
    if histogram:
        arguments.append("-hist")
    arguments.append(str(raster))
    result = toolchain.run("gdalinfo", arguments)
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ToolError(f"gdalinfo did not return valid json for {raster}") from error
    size = info.get("size", [])
    if len(size) != 2 or any(int(value) <= 0 for value in size):
        raise ToolError(f"raster has invalid dimensions: {raster}")
    return info


def source_crs_wkt(info: dict[str, Any]) -> str:
    """return the raster crs or explain why it cannot be used"""

    wkt = (info.get("coordinateSystem") or {}).get("wkt")
    if not wkt:
        raise ValueError("source dem has no coordinate reference system")
    return str(wkt)


def is_projected(info: dict[str, Any]) -> bool:
    """check whether raster metadata describes a projected crs"""

    wkt = source_crs_wkt(info).lstrip().upper()
    return (
        wkt.startswith("PROJCRS[")
        or wkt.startswith("PROJCS[")
        or (wkt.startswith("COMPOUNDCRS[") and "PROJCRS[" in wkt)
    )


def crs_is_projected(toolchain: Toolchain, crs: str) -> bool:
    """ask gdal whether a user supplied crs is projected"""

    result = toolchain.run("gdalsrsinfo", ["-o", "PROJJSON", crs])
    try:
        definition = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ToolError(f"gdalsrsinfo returned invalid projjson for {crs!r}") from error
    if definition.get("type") == "ProjectedCRS":
        return True
    if definition.get("type") == "CompoundCRS":
        return any(
            component.get("type") == "ProjectedCRS"
            for component in definition.get("components", [])
            if isinstance(component, dict)
        )
    return False


def band_valid_percent(info: dict[str, Any], band_number: int) -> float | None:
    """read the valid pixel percentage added by gdal statistics"""

    band = info.get("bands", [])[band_number - 1]
    domains = band.get("metadata") or {}
    metadata = domains.get("") or {}
    raw = metadata.get("STATISTICS_VALID_PERCENT")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def visible_summary(
    info: dict[str, Any],
    visible_value: int,
    cell_size: float,
) -> dict[str, Any] | None:
    """count visible cells from the one bucket per value histogram of a byte raster"""

    band = (info.get("bands") or [{}])[0]
    histogram = band.get("histogram") or {}
    buckets = histogram.get("buckets") or []
    if (
        band.get("type") != "Byte"
        or len(buckets) != 256
        or histogram.get("min") != -0.5
        or histogram.get("max") != 255.5
    ):
        return None
    cells = int(buckets[visible_value])
    return {"cells": cells, "area": cells * cell_size**2}


def file_sha256(path: Path) -> str:
    """hash a file in blocks so large rasters do not fill memory"""

    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def creation_options(values: Sequence[str]) -> list[str]:
    """turn name value pairs into repeated gdal creation arguments"""

    arguments: list[str] = []
    for value in values:
        if "=" not in value or not value.split("=", 1)[0].strip():
            raise ValueError(f"invalid creation option {value!r}; expected NAME=VALUE")
        arguments.extend(("-co", value))
    return arguments


# shared terrain and observer planning


def build_source(
    toolchain: Toolchain,
    dem_files: Sequence[Path],
    work_directory: Path,
    source_nodata: float | None,
    band_number: int,
) -> Path:
    """build one virtual raster over every source dem"""

    work_directory.mkdir(parents=True, exist_ok=True)
    source_list = work_directory / "dem-files.txt"
    source_list.write_text("".join(f"{path}\n" for path in dem_files), encoding="utf-8")
    vrt = work_directory / "source-dems.vrt"
    temporary_vrt = work_directory / f"source-dems.{uuid.uuid4().hex}.tmp.vrt"
    try:
        # the alpha band keeps real coverage gaps visible even when a source
        # elevation band does not have its own nodata value
        arguments = ["-strict", "-b", str(band_number), "-addalpha"]
        if source_nodata is not None:
            value = f"{source_nodata:.17g}"
            arguments.extend(("-srcnodata", value, "-vrtnodata", value))
        arguments.extend(("-input_file_list", str(source_list), str(temporary_vrt)))
        result = toolchain.run("gdalbuildvrt", arguments)
        warnings = warning_lines(result.stdout, result.stderr)
        if warnings:
            raise ToolError(
                "gdalbuildvrt reported warnings in strict mode:\n" + "\n".join(warnings)
            )
        os.replace(temporary_vrt, vrt)
    finally:
        temporary_vrt.unlink(missing_ok=True)
    return vrt


def build_plans(
    observers: Sequence[Observer],
    observer_crs: str,
    analysis_crs_option: str,
    source_info: dict[str, Any],
    output_directory: Path,
    toolchain: Toolchain,
    allow_outside: bool,
) -> list[Plan]:
    """project observer locations and assign their output paths"""

    source_wkt = source_crs_wkt(source_info)
    geotransform = source_info.get("geoTransform")
    width, height = map(int, source_info["size"])
    if not geotransform:
        raise ValueError("source dem has no affine geotransform")

    stems = observer_stems(observers)
    plans: list[Plan] = []
    for observer in observers:
        source_x, source_y = transform_point(
            toolchain, observer.x, observer.y, observer_crs, source_wkt
        )
        column, row = affine_pixel(geotransform, source_x, source_y)
        if not allow_outside and not (0 <= column < width and 0 <= row < height):
            raise ValueError(
                f"observer {observer.id!r} is outside the source dem footprint "
                f"(pixel {column:.2f}, {row:.2f})"
            )

        if analysis_crs_option.casefold() == "auto-utm":
            longitude, latitude = transform_point(
                toolchain, observer.x, observer.y, observer_crs, "EPSG:4326"
            )
            analysis_crs = auto_utm_crs(longitude, latitude)
        else:
            analysis_crs = analysis_crs_option
        observer_x, observer_y = transform_point(
            toolchain, observer.x, observer.y, observer_crs, analysis_crs
        )
        stem = stems[observer.id]
        plans.append(
            Plan(
                observer,
                analysis_crs,
                observer_x,
                observer_y,
                stem,
                output_directory / "rasters" / f"{stem}.tif",
                output_directory / "state" / f"{stem}.json",
                output_directory / "polygons" / f"{stem}.gpkg",
                output_directory / "shapefiles" / f"{stem}.shp",
            )
        )
    return plans


def warning_lines(*streams: str) -> list[str]:
    """keep warning lines from otherwise successful gdal commands"""

    warnings: list[str] = []
    for line in "\n".join(streams).splitlines():
        if "warning" in line.casefold():
            warnings.append(line.strip())
    return warnings


def write_json(path: Path, document: dict[str, Any]) -> None:
    """replace a json file atomically so interrupted writes stay readable"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read_state(plan: Plan) -> dict[str, Any] | None:
    """load the saved state for one observer when it is readable"""

    try:
        state = json.loads(plan.state.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return state if isinstance(state, dict) else None


def state_reusable(
    plan: Plan,
    state: dict[str, Any] | None,
    fingerprint: str,
    toolchain: Toolchain,
) -> bool:
    """confirm that saved state still matches a healthy output raster"""

    if state is None or not plan.output.is_file():
        return False
    try:
        if state.get("status") != "complete" or state.get("fingerprint") != fingerprint:
            return False
        # the fingerprint checks inputs while the hash dimensions and gdal
        # checksum confirm that the raster itself has not changed
        expected = state.get("raster") or {}
        if not expected.get("sha256") or file_sha256(plan.output) != expected["sha256"]:
            return False
        info = inspect_raster(toolchain, plan.output, checksum=True)
        band = info.get("bands", [{}])[0]
        if list(info.get("size", [])) != [expected.get("width"), expected.get("height")]:
            return False
        if band.get("checksum") != expected.get("checksum"):
            return False
        if plan.output.stat().st_size != expected.get("bytes"):
            return False
    except (OSError, ValueError, ToolError):
        return False
    return True


def polygon_reusable(plan: Plan, state: dict[str, Any] | None, fingerprint: str) -> bool:
    """confirm that a saved polygon was built from this raster and these settings"""

    polygon = (state or {}).get("polygon") or {}
    if polygon.get("fingerprint") != fingerprint:
        return False
    if polygon.get("output") is None:
        # nothing was visible so there is no file to check
        return True
    try:
        return plan.polygon.stat().st_size == polygon.get("bytes")
    except OSError:
        return False


def observer_fingerprint(run_config_hash: str, observer: dict[str, Any], crs: str) -> str:
    """identify the observer values and settings behind one output"""

    return payload_hash(
        {
            "run_config_hash": run_config_hash,
            "observer": {name: observer.get(name) for name in OBSERVER_KEY_FIELDS},
            "crs": crs,
        }
    )


def plan_fingerprint(plan: Plan, run_config_hash: str) -> str:
    """identify the exact observer and settings behind one output"""

    return observer_fingerprint(run_config_hash, asdict(plan.observer), plan.analysis_crs)


def adopt_legacy_outputs(
    plans: Sequence[Plan],
    output_directory: Path,
    run_config_hash: str,
    polygon_config_hash: str | None,
) -> int:
    """rename outputs saved under row numbered names and re-key their state

    Earlier releases named files after the csv row and hashed the row number
    and id into the resume fingerprint. Outputs made with the current settings
    keep their rasters and polygons.
    """

    legacy: dict[str, tuple[Path, dict[str, Any]]] = {}
    numbered = "[0-9][0-9][0-9][0-9][0-9][0-9]_*.json"
    for path in sorted((output_directory / "state").glob(numbered)):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            legacy.setdefault(str(state["observer"]["id"]), (path, state))
        except (OSError, ValueError, KeyError, TypeError):
            continue

    adopted = 0
    for plan in plans:
        if plan.state.exists() or plan.observer.id not in legacy:
            continue
        path, state = legacy[plan.observer.id]
        old_raster = output_directory / "rasters" / f"{path.stem}.tif"
        old_polygon = output_directory / "polygons" / f"{path.stem}.gpkg"
        if not old_raster.is_file() or plan.output.exists() or plan.polygon.exists():
            continue
        saved = state["observer"]
        crs = state.get("analysis_crs")
        old_fingerprint = payload_hash(
            {"run_config_hash": run_config_hash, "observer": saved, "crs": crs}
        )
        if state.get("fingerprint") == old_fingerprint:
            state["fingerprint"] = observer_fingerprint(run_config_hash, saved, crs)
            polygon = state.get("polygon") or {}
            if polygon_config_hash and polygon.get("fingerprint") == payload_hash(
                {"raster": old_fingerprint, "polygons": polygon_config_hash}
            ):
                polygon["fingerprint"] = payload_hash(
                    {"raster": state["fingerprint"], "polygons": polygon_config_hash}
                )
        os.replace(old_raster, plan.output)
        state["output"] = str(plan.output.relative_to(output_directory))
        if old_polygon.is_file():
            plan.polygon.parent.mkdir(parents=True, exist_ok=True)
            os.replace(old_polygon, plan.polygon)
            if (state.get("polygon") or {}).get("output"):
                state["polygon"]["output"] = str(plan.polygon.relative_to(output_directory))
        write_json(plan.state, state)
        path.unlink()
        adopted += 1
    return adopted


# one observer from terrain crop through validated output


def sql_literal(value: Any) -> str:
    """format a python value for a sqlite select list"""

    if value is None:
        return "NULL"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    # a decimal point keeps whole numbers from becoming an integer column
    return repr(float(value))


def valid_feature_count(toolchain: Toolchain, path: Path, layer: str) -> int:
    """count features that still have a geometry after clipping and repair"""

    result = toolchain.run(
        "ogrinfo",
        [
            "-ro",
            "-q",
            str(path),
            "-sql",
            f'SELECT COUNT(*) AS n FROM "{layer}" WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)',
        ],
    )
    match = re.search(r"\bn \(Integer(?:64)?\) = (\d+)", result.stdout)
    if not match:
        raise ToolError(f"could not count polygon features in {path}")
    return int(match.group(1))


def build_viewshed_raster(
    plan: Plan,
    source: Path,
    args: argparse.Namespace,
    toolchain: Toolchain,
    work: Path,
    warnings: list[str],
    viewshed_fraction: float,
) -> dict[str, Any]:
    """prepare terrain and create a validated viewshed raster for one observer"""

    projected_dem = work / "projected-dem.tif"
    temporary_output = plan.output.with_name(
        f".{plan.output.stem}.{uuid.uuid4().hex}.tmp{plan.output.suffix}"
    )
    observer = plan.observer
    margin = observer.max_distance + args.cell_size
    bounds = (
        plan.observer_x - margin,
        plan.observer_y - margin,
        plan.observer_x + margin,
        plan.observer_y + margin,
    )

    # each observer gets a small projected terrain crop rather than a costly
    # reprojection of the full source mosaic
    warp_arguments = [
        "-overwrite",
        "-t_srs",
        plan.analysis_crs,
        "-te",
        *(f"{value:.17g}" for value in bounds),
        "-tr",
        f"{args.cell_size:.17g}",
        f"{args.cell_size:.17g}",
        "-tap",
        "-r",
        args.resampling,
        "-srcalpha",
        "-ot",
        "Float32",
        "-dstnodata",
        f"{args.warp_nodata:.17g}",
        # cores are shared out between the observers running at the same time
        "-multi",
        "-wo",
        f"NUM_THREADS={args.warp_threads}",
        "-of",
        "GTiff",
        "-co",
        "TILED=YES",
        "-co",
        "COMPRESS=DEFLATE",
        "-co",
        "PREDICTOR=3",
    ]
    if args.source_nodata is not None:
        warp_arguments.extend(("-srcnodata", f"{args.source_nodata:.17g}"))
    warp_arguments.extend((str(source), str(projected_dem)))
    warp = toolchain.run("gdalwarp", warp_arguments)
    warnings.extend(warning_lines(warp.stdout, warp.stderr))
    projected_info = inspect_raster(toolchain, projected_dem, stats=True)
    if not is_projected(projected_info):
        raise ToolError(f"analysis crs is not projected: {plan.analysis_crs}")
    valid_percent = band_valid_percent(projected_info, 1)
    if not args.allow_dem_gaps and (valid_percent is None or valid_percent < 100.0):
        detail = "could not be measured" if valid_percent is None else f"is {valid_percent:g}%"
        raise ToolError(
            f"valid dem coverage {detail} in the analysis area for {observer.id!r}; "
            "add the missing terrain or use --allow-dem-gaps when gaps are expected"
        )

    log(f"[{observer.id}] calculating viewshed")
    progress("observer", id=observer.id, stage="viewshed", fraction=viewshed_fraction)

    # values are kept distinct so a gis can separate visibility from the
    # analysis boundary and true nodata without reading the state file
    viewshed_arguments = [
        "-b",
        "1",
        "-ox",
        f"{plan.observer_x:.17g}",
        "-oy",
        f"{plan.observer_y:.17g}",
        "-oz",
        f"{observer.observer_height:.17g}",
        "-tz",
        f"{observer.target_height:.17g}",
        "-md",
        f"{observer.max_distance:.17g}",
        "-cc",
        f"{args.curvature_coefficient:.17g}",
        "-om",
        args.output_mode,
        "-ov",
        str(args.out_of_range_value),
    ]
    if args.output_mode == "NORMAL":
        viewshed_arguments.extend(
            (
                "-vv",
                str(args.visible_value),
                "-iv",
                str(args.invisible_value),
            )
        )
    viewshed_arguments.extend(
        (
            "-a_nodata",
            str(args.output_nodata),
            "-of",
            "GTiff",
            *creation_options(args.creation_option),
            str(projected_dem),
            str(temporary_output),
        )
    )
    viewshed = toolchain.run("gdal_viewshed", viewshed_arguments)
    warnings.extend(warning_lines(viewshed.stdout, viewshed.stderr))
    normal = args.output_mode == "NORMAL"
    output_info = inspect_raster(
        toolchain, temporary_output, stats=True, checksum=True, histogram=normal
    )
    if not temporary_output.is_file() or temporary_output.stat().st_size == 0:
        raise ToolError("gdal_viewshed did not create a usable output raster")
    output_valid_percent = band_valid_percent(output_info, 1)
    if output_valid_percent is None or output_valid_percent <= 0:
        raise ToolError("viewshed output has no measurable valid pixels")
    visible = visible_summary(output_info, args.visible_value, args.cell_size) if normal else None
    if visible and visible["cells"] == 0:
        warnings.append("no cells are visible from this observer")
    os.replace(temporary_output, plan.output)

    band = output_info["bands"][0]
    return {
        "raster": {
            "width": int(output_info["size"][0]),
            "height": int(output_info["size"][1]),
            "data_type": band.get("type"),
            "minimum": band.get("minimum"),
            "maximum": band.get("maximum"),
            "checksum": band.get("checksum"),
            "sha256": file_sha256(plan.output),
            "bytes": plan.output.stat().st_size,
        },
        # area is in squared analysis crs units
        "visible": visible,
    }


def build_polygon(
    plan: Plan,
    visible: dict[str, Any] | None,
    args: argparse.Namespace,
    polygons: PolygonSettings,
    toolchain: Toolchain,
    work: Path,
    fingerprint: str,
    warnings: list[str],
) -> dict[str, Any]:
    """dissolve the visible cells of one viewshed into a single polygon feature"""

    empty = {"fingerprint": fingerprint, "output": None, "crs": polygons.crs, "features": 0}
    plan.polygon.unlink(missing_ok=True)
    if visible and visible["cells"] == 0:
        return empty

    observer = plan.observer
    parts = work / "visible-parts.gpkg"
    parts.unlink(missing_ok=True)
    plan.polygon.parent.mkdir(parents=True, exist_ok=True)
    temporary = plan.polygon.with_name(
        f".{plan.polygon.stem}.{uuid.uuid4().hex}.tmp{plan.polygon.suffix}"
    )
    # diagonal neighbours are joined so thin ridgelines stay one shape
    polygonize = toolchain.run(
        "gdal_polygonize",
        ["-8", "-q", "-b", "1", "-f", "GPKG", str(plan.output), str(parts), "parts", "value"],
    )
    warnings.extend(warning_lines(polygonize.stdout, polygonize.stderr))

    attributes = {
        "id": observer.id,
        "x": observer.x,
        "y": observer.y,
        "observer_height": observer.observer_height,
        "target_height": observer.target_height,
        "max_distance": observer.max_distance,
        "analysis_crs": plan.analysis_crs,
        "cell_size": args.cell_size,
        "visible_area": visible["area"] if visible else None,
    }
    # the cast keeps an unknown area from turning the column into text
    attributes = {
        name: f"CAST({sql_literal(value)} AS REAL)" if name == "visible_area" else sql_literal(value)
        for name, value in attributes.items()
    }
    columns = ", ".join(f"{value} AS {name}" for name, value in attributes.items())
    arguments = [
        "-f",
        "GPKG",
        str(temporary),
        str(parts),
        "-dialect",
        "SQLITE",
        "-sql",
        f"SELECT ST_Union(geom) AS geom, {columns} FROM parts WHERE value = {args.visible_value}",
        "-nln",
        OBSERVER_LAYER,
        "-nlt",
        "MULTIPOLYGON",
        "-t_srs",
        polygons.crs,
    ]
    if polygons.simplify:
        # ogr2ogr simplifies before it reprojects so the tolerance stays in
        # analysis crs units
        arguments.extend(("-simplify", f"{polygons.simplify:.17g}"))
    if polygons.clip:
        arguments.extend(("-clipdst", str(polygons.clip)))
    # reprojection and clipping can leave rings that touch or collapse
    arguments.append("-makevalid")
    dissolve = toolchain.run("ogr2ogr", arguments)
    warnings.extend(warning_lines(dissolve.stdout, dissolve.stderr))

    if valid_feature_count(toolchain, temporary, OBSERVER_LAYER) != 1:
        temporary.unlink(missing_ok=True)
        warnings.append("the visible area is empty after clipping so no polygon was written")
        return empty
    os.replace(temporary, plan.polygon)
    return {
        **empty,
        "output": str(plan.polygon.relative_to(args.output_dir)),
        "features": 1,
        "bytes": plan.polygon.stat().st_size,
    }


def remove_shapefile(plan: Plan) -> None:
    """delete every file that makes up one observer's shapefile"""

    for suffix in SHAPEFILE_PARTS:
        plan.shapefile.with_suffix(suffix).unlink(missing_ok=True)


def export_shapefile(
    plan: Plan,
    polygon: dict[str, Any],
    toolchain: Toolchain,
    work: Path,
) -> None:
    """copy one observer's polygon to a shapefile for software that cannot read geopackage"""

    remove_shapefile(plan)
    if not polygon.get("output"):
        return
    names = ("id", *OBSERVER_KEY_FIELDS, "analysis_crs", "cell_size", "visible_area")
    columns = ", ".join(
        f"{name} AS {SHAPEFILE_FIELD_NAMES[name]}" if name in SHAPEFILE_FIELD_NAMES else name
        for name in names
    )
    # written in the work folder first so a stopped run cannot leave half a shapefile
    staging = work / "shapefile"
    staging.mkdir(exist_ok=True)
    toolchain.run(
        "ogr2ogr",
        [
            "-f",
            "ESRI Shapefile",
            str(staging / plan.shapefile.name),
            str(plan.polygon),
            "-sql",
            f"SELECT geom, {columns} FROM {OBSERVER_LAYER}",
            "-nln",
            plan.shapefile.stem,
            "-lco",
            "ENCODING=UTF-8",
        ],
    )
    plan.shapefile.parent.mkdir(parents=True, exist_ok=True)
    for suffix in SHAPEFILE_PARTS:
        part = (staging / plan.shapefile.name).with_suffix(suffix)
        if part.is_file():
            os.replace(part, plan.shapefile.with_suffix(suffix))
    if not plan.shapefile.is_file():
        raise ToolError(f"ogr2ogr did not create {plan.shapefile.name}")


def process_plan(
    plan: Plan,
    source: Path,
    args: argparse.Namespace,
    toolchain: Toolchain,
    run_config_hash: str,
    polygons: PolygonSettings | None = None,
) -> dict[str, Any]:
    """run or resume the raster and polygon stages for one observer"""

    started = time.monotonic()
    observer = plan.observer
    stage_starts = STAGE_STARTS[polygons is not None]
    fingerprint = plan_fingerprint(plan, run_config_hash)
    polygon_fingerprint = (
        payload_hash({"raster": fingerprint, "polygons": polygons.config_hash}) if polygons else ""
    )

    # the raster and its polygon are checked separately so changing only the
    # polygon settings does not repeat the much slower viewshed
    previous = read_state(plan) if args.resume else None
    raster_ready = state_reusable(plan, previous, fingerprint, toolchain)
    polygon_ready = polygons is None or (
        raster_ready and polygon_reusable(plan, previous, polygon_fingerprint)
    )
    shapefile_ready = not (polygons and polygons.shapefiles) or (
        polygon_ready
        and (
            plan.shapefile.is_file()
            or not ((previous or {}).get("polygon") or {}).get("output")
        )
    )
    if previous is not None and raster_ready and polygon_ready and shapefile_ready:
        if previous.get("observer") != asdict(observer):
            # the row can move in the csv without touching the viewshed
            previous["observer"] = asdict(observer)
            write_json(plan.state, previous)
        previous["run_status"] = "resumed"
        log(f"[{observer.id}] reused validated output")
        progress("observer", id=observer.id, stage="complete", fraction=1.0, resumed=True)
        return previous

    work = args.output_dir / ".work" / plan.stem
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    plan.output.parent.mkdir(parents=True, exist_ok=True)
    progress("observer", id=observer.id, stage="preparing", fraction=stage_starts["preparing"])

    if previous is not None and raster_ready:
        document = {
            **previous,
            "run_status": "updated",
            "generated_utc": utc_now(),
            "observer": asdict(observer),
        }
        log(f"[{observer.id}] reused validated raster")
    else:
        log(f"[{observer.id}] preparing projected dem ({plan.analysis_crs})")
        # a polygon traced from the raster being replaced would be stale
        plan.polygon.unlink(missing_ok=True)
        remove_shapefile(plan)
        warnings: list[str] = []
        document = {
            "schema_version": 2,
            "status": "complete",
            "run_status": "created",
            "generated_utc": utc_now(),
            "fingerprint": fingerprint,
            "observer": asdict(observer),
            "analysis_crs": plan.analysis_crs,
            "analysis_x": plan.observer_x,
            "analysis_y": plan.observer_y,
            "output": str(plan.output.relative_to(args.output_dir)),
            **build_viewshed_raster(
                plan, source, args, toolchain, work, warnings, stage_starts["viewshed"]
            ),
            "polygon": None,
            "warnings": warnings,
        }

    if polygons is not None and not polygon_ready:
        # saved first so a polygon failure does not cost the finished raster
        write_json(plan.state, document)
        remove_shapefile(plan)
        log(f"[{observer.id}] tracing visible area polygon")
        progress("observer", id=observer.id, stage="polygon", fraction=stage_starts["polygon"])
        polygon_warnings: list[str] = []
        document["polygon"] = {
            **build_polygon(
                plan,
                document.get("visible"),
                args,
                polygons,
                toolchain,
                work,
                polygon_fingerprint,
                polygon_warnings,
            ),
            "warnings": polygon_warnings,
        }
    if polygons is not None and polygons.shapefiles:
        export_shapefile(plan, document["polygon"], toolchain, work)

    document["elapsed_seconds"] = round(time.monotonic() - started, 3)
    write_json(plan.state, document)
    if not args.keep_work:
        shutil.rmtree(work)
    log(
        f"[{observer.id}] complete in {format_duration(time.monotonic() - started)}"
        f" -> {plan.output}"
    )
    progress(
        "observer", id=observer.id, stage="complete", fraction=1.0, output=str(plan.output)
    )
    return document


def process_plan_with_cleanup(
    plan: Plan,
    source: Path,
    args: argparse.Namespace,
    toolchain: Toolchain,
    run_config_hash: str,
    polygons: PolygonSettings | None = None,
) -> dict[str, Any]:
    """run one plan and remove partial files whether it passes or fails"""

    try:
        return process_plan(plan, source, args, toolchain, run_config_hash, polygons)
    finally:
        for target in (plan.output, plan.polygon):
            for temporary in target.parent.glob(f".{target.stem}.*"):
                if temporary.is_file():
                    temporary.unlink(missing_ok=True)
        work = args.output_dir / ".work" / plan.stem
        if work.exists() and not args.keep_work:
            shutil.rmtree(work, ignore_errors=True)


def payload_hash(payload: dict[str, Any]) -> str:
    """hash a json document the same way on every run"""

    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# products that describe the whole run


def prepare_clip(
    toolchain: Toolchain,
    boundary: Path,
    crs: str,
    work_directory: Path,
) -> Path:
    """project the clip boundary once so every observer can share it"""

    clip = work_directory / "polygon-clip.gpkg"
    clip.unlink(missing_ok=True)
    toolchain.run(
        "ogr2ogr",
        ["-f", "GPKG", str(clip), str(boundary), "-t_srs", crs, "-nln", "clip", "-makevalid"],
    )
    return clip


def build_combined(
    states: Sequence[dict[str, Any]],
    args: argparse.Namespace,
    toolchain: Toolchain,
    previous: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """gather every observer polygon into one file with a dissolved coverage layer"""

    polygons = [state["polygon"] for state in states if (state.get("polygon") or {}).get("output")]
    combined = args.output_dir / "viewsheds.gpkg"
    if not polygons:
        combined.unlink(missing_ok=True)
        return None
    fingerprint = payload_hash({"polygons": [polygon["fingerprint"] for polygon in polygons]})
    if (
        args.resume
        and previous
        and previous.get("fingerprint") == fingerprint
        and combined.is_file()
        and combined.stat().st_size == previous.get("bytes")
    ):
        log(f"reused combined polygons: {combined}")
        return previous

    log(f"combining {len(polygons)} polygons and dissolving coverage")
    progress("products", stage="combining", polygons=len(polygons))
    work = args.output_dir / ".work"
    temporary = work / f"viewsheds.{uuid.uuid4().hex}.gpkg"
    coverage = work / f"coverage.{uuid.uuid4().hex}.gpkg"
    try:
        for index, polygon in enumerate(polygons):
            toolchain.run(
                "ogr2ogr",
                [
                    "-f",
                    "GPKG",
                    *(("-update", "-append") if index else ()),
                    str(temporary),
                    str(args.output_dir / polygon["output"]),
                    "-nln",
                    OBSERVER_LAYER,
                    "-nlt",
                    "MULTIPOLYGON",
                ],
            )
        toolchain.run(
            "ogr2ogr",
            [
                "-f",
                "GPKG",
                str(coverage),
                str(temporary),
                "-dialect",
                "SQLITE",
                "-sql",
                f"SELECT ST_Union(geom) AS geom, COUNT(*) AS observers FROM {OBSERVER_LAYER}",
                "-nln",
                COVERAGE_LAYER,
                "-nlt",
                "MULTIPOLYGON",
                "-makevalid",
            ],
        )
        # the second layer lets one file answer both which observer sees a
        # place and whether any observer does
        toolchain.run(
            "ogr2ogr",
            ["-f", "GPKG", "-update", str(temporary), str(coverage), "-nln", COVERAGE_LAYER],
        )
        if valid_feature_count(toolchain, temporary, COVERAGE_LAYER) != 1:
            raise ToolError("dissolved coverage is empty")
        os.replace(temporary, combined)
    finally:
        for leftover in work.glob(f"{temporary.stem}*"):
            leftover.unlink(missing_ok=True)
        for leftover in work.glob(f"{coverage.stem}*"):
            leftover.unlink(missing_ok=True)
    return {
        "output": combined.name,
        "fingerprint": fingerprint,
        "observer_layer": OBSERVER_LAYER,
        "coverage_layer": COVERAGE_LAYER,
        "polygons": len(polygons),
        "bytes": combined.stat().st_size,
    }


def config_document(
    args: argparse.Namespace,
    dem_files: Sequence[Path],
    gdal_version: str,
) -> dict[str, Any]:
    """record every input and setting that can change an output"""

    return {
        "tool_version": RASTER_PIPELINE_VERSION,
        "gdal_version": gdal_version,
        "dem_inventory": [
            {
                "path": str(path),
                "size": path.stat().st_size,
                "mtime_ns": path.stat().st_mtime_ns,
                **({"sha256": file_sha256(path)} if args.hash_sources else {}),
            }
            for path in dem_files
        ],
        "observer_crs": args.observer_crs,
        "analysis_crs": args.analysis_crs,
        "cell_size": args.cell_size,
        "band": args.band,
        "resampling": args.resampling,
        "source_nodata": args.source_nodata,
        "warp_nodata": args.warp_nodata,
        "curvature_coefficient": args.curvature_coefficient,
        "output_mode": args.output_mode,
        "visible_value": args.visible_value,
        "invisible_value": args.invisible_value,
        "out_of_range_value": args.out_of_range_value,
        "output_nodata": args.output_nodata,
        "creation_options": args.creation_option,
    }


# command line and full run coordination


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """read command line settings and resolve mode dependent defaults"""

    parser = argparse.ArgumentParser(
        description=(
            "create one gdal terrain viewshed per observer. distances use the analysis crs "
            "units and heights use the dem elevation units"
        )
    )
    parser.add_argument("--observers", type=Path, required=True, help="observer csv")
    parser.add_argument(
        "--dem",
        type=Path,
        action="append",
        required=True,
        help="dem raster or folder; repeat this option for more inputs",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--observer-crs",
        required=True,
        help="crs used by csv x and y values such as EPSG:4326",
    )
    parser.add_argument(
        "--analysis-crs",
        default="auto-utm",
        help="projected output crs or auto-utm for a local zone per observer",
    )
    parser.add_argument("--cell-size", type=float, required=True, help="output pixel size")
    parser.add_argument(
        "--max-distance",
        type=float,
        help="default radius when a csv row has no max_distance",
    )
    parser.add_argument(
        "--observer-height", type=float, default=2.0, help="default height above the ground"
    )
    parser.add_argument(
        "--target-height", type=float, default=0.0, help="default target height above the ground"
    )
    parser.add_argument("--band", type=int, default=1, help="elevation band in the source dem")
    parser.add_argument(
        "--resampling",
        choices=("near", "bilinear", "cubic", "cubicspline", "lanczos"),
        default="bilinear",
        help="resampling used while projecting elevation data",
    )
    parser.add_argument("--source-nodata", type=float)
    parser.add_argument("--warp-nodata", type=float, default=-999999.0)
    parser.add_argument(
        "--curvature-coefficient",
        type=float,
        default=0.85714,
        help="gdal earth curvature coefficient including refraction",
    )
    parser.add_argument(
        "--output-mode",
        type=str.casefold,
        choices=("normal", "dem", "ground"),
        default="normal",
        help="normal maps visible and hidden cells",
    )
    parser.add_argument("--visible-value", type=int, default=1)
    parser.add_argument("--invisible-value", type=int, default=0)
    parser.add_argument(
        "--out-of-range-value",
        type=float,
        help="value used beyond the maximum distance",
    )
    parser.add_argument("--output-nodata", type=float)
    parser.add_argument(
        "--creation-option",
        action="append",
        default=[],
        metavar="name=value",
        help="geotiff creation option as name=value; repeat when needed",
    )
    parser.add_argument(
        "--polygons",
        action="store_true",
        help="also trace each visible area as a polygon and build a combined coverage file",
    )
    parser.add_argument(
        "--polygon-crs",
        default="EPSG:4326",
        help="crs shared by every polygon output",
    )
    parser.add_argument(
        "--polygon-simplify",
        type=float,
        default=0.0,
        help="simplification tolerance in analysis crs units; 0 keeps every cell edge",
    )
    parser.add_argument(
        "--shapefiles",
        action="store_true",
        help="also save each polygon as a shapefile for software such as arcgis",
    )
    parser.add_argument(
        "--polygon-clip",
        type=Path,
        help="vector file such as a land mask; polygons are trimmed to its features",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=DEFAULT_JOBS,
        help=f"number of observers processed at the same time (default: {DEFAULT_JOBS})",
    )
    parser.add_argument("--timeout", type=float, help="time limit in seconds for each gdal command")
    parser.add_argument(
        "--gdal-bin",
        type=Path,
        help="gdal tools folder or qgis install root when automatic detection does not work",
    )
    parser.add_argument(
        "--gdal-python",
        type=Path,
        help="python interpreter used by gdal utility scripts",
    )
    parser.add_argument(
        "--allow-outside",
        action="store_true",
        help="allow observers outside the dem",
    )
    parser.add_argument(
        "--allow-dem-gaps",
        action="store_true",
        help="allow missing elevation cells inside an analysis area",
    )
    parser.add_argument("--allow-extra-columns", action="store_true")
    parser.add_argument(
        "--hash-sources",
        action="store_true",
        help="hash complete source files before deciding what can be resumed",
    )
    parser.add_argument("--keep-work", action="store_true", help="keep each projected terrain crop")
    parser.add_argument("--dry-run", action="store_true", help="validate and print the plan only")
    parser.add_argument(
        "--json-progress",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    existing = parser.add_mutually_exclusive_group()
    existing.add_argument("--overwrite", action="store_true")
    existing.add_argument("--resume", action="store_true")
    parser.add_argument("--fail-fast", action="store_true")
    args = parser.parse_args(argv)
    args.output_mode = args.output_mode.upper()

    for name in ("cell_size", "observer_height", "target_height", "curvature_coefficient"):
        value = getattr(args, name)
        if not math.isfinite(value) or value < 0 or (name == "cell_size" and value == 0):
            parser.error(f"--{name.replace('_', '-')} has an invalid value")
    if args.max_distance is not None and (
        not math.isfinite(args.max_distance) or args.max_distance <= 0
    ):
        parser.error("--max-distance must be positive and finite")
    if args.band < 1:
        parser.error("--band must be at least 1")
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    if not math.isfinite(args.polygon_simplify) or args.polygon_simplify < 0:
        parser.error("--polygon-simplify cannot be negative")
    if args.polygons and args.output_mode != "NORMAL":
        parser.error("--polygons needs the normal output mode")
    if not args.polygons and (args.polygon_simplify or args.polygon_clip or args.shapefiles):
        parser.error("--polygon-simplify, --polygon-clip, and --shapefiles need --polygons")
    if args.timeout is not None and args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.output_nodata is None:
        args.output_nodata = 255 if args.output_mode == "NORMAL" else -1
    if args.out_of_range_value is None:
        args.out_of_range_value = 254 if args.output_mode == "NORMAL" else args.output_nodata
    if args.output_mode == "NORMAL":
        for name in ("visible_value", "invisible_value", "out_of_range_value"):
            value = getattr(args, name)
            if not float(value).is_integer() or not 0 <= value <= 255:
                parser.error(f"--{name.replace('_', '-')} must be an integer from 0 to 255")
            setattr(args, name, int(value))
    numeric_options = (
        "source_nodata",
        "warp_nodata",
        "output_nodata",
        "out_of_range_value",
    )
    for name in numeric_options:
        value = getattr(args, name)
        if value is not None and not math.isfinite(value):
            parser.error(f"--{name.replace('_', '-')} must be finite")
    if args.output_mode == "NORMAL":
        if not float(args.output_nodata).is_integer() or not 0 <= args.output_nodata <= 255:
            parser.error("--output-nodata must be an integer from 0 to 255 in NORMAL mode")
        args.output_nodata = int(args.output_nodata)
        classes = {
            args.visible_value,
            args.invisible_value,
            args.out_of_range_value,
            args.output_nodata,
        }
        if len(classes) != 4:
            parser.error(
                "normal mode visible invisible out-of-range and nodata values must be distinct"
            )
    default_creation_options = ["TILED=YES", "COMPRESS=DEFLATE", "BIGTIFF=IF_SAFER"]
    supplied_names = {value.partition("=")[0].strip().upper() for value in args.creation_option}
    args.creation_option = [
        value
        for value in default_creation_options
        if value.partition("=")[0].upper() not in supplied_names
    ] + args.creation_option
    try:
        creation_options(args.creation_option)
    except ValueError as error:
        parser.error(str(error))
    return args


def output_conflicts(plans: Sequence[Plan]) -> list[Path]:
    """list output or state paths that are already present"""

    return [
        path
        for plan in plans
        for path in (plan.output, plan.state, plan.polygon, plan.shapefile)
        if path.exists()
    ]


class OutputLock:
    """keep two runs from writing to the same output folder"""

    def __init__(self, output_directory: Path) -> None:
        """place the lock beside the files it protects"""

        self.path = output_directory / ".bulk-viewshed.lock"
        self.acquired = False

    def acquire(self) -> None:
        """create the lock only when another run has not created it first"""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as error:
            raise RuntimeError(
                f"output directory is locked by another run: {self.path}. "
                "remove a stale lock only after confirming no run is active"
            ) from error
        with os.fdopen(descriptor, "w", encoding="utf-8") as lock_file:
            lock_file.write(f"pid={os.getpid()}\nstarted_utc={utc_now()}\n")
        self.acquired = True

    def release(self) -> None:
        """remove a lock owned by this run"""

        if self.acquired:
            self.path.unlink(missing_ok=True)
            self.acquired = False


def path_is_within(path: Path, directory: Path) -> bool:
    """check path containment without relying on string prefixes"""

    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def run(argv: Sequence[str] | None = None) -> int:
    """validate inputs build the shared source and process every observer"""

    global JSON_PROGRESS
    CANCEL_REQUESTED.clear()
    SIGNAL_CANCELLED.clear()
    args = parse_args(argv)
    JSON_PROGRESS = args.json_progress
    args.observers = args.observers.expanduser().resolve()
    args.output_dir = args.output_dir.expanduser().resolve()
    observers = load_observers(
        args.observers,
        args.observer_height,
        args.target_height,
        args.max_distance,
        args.allow_extra_columns,
    )
    for dem_input in args.dem:
        dem_path = dem_input.expanduser().resolve()
        if dem_path.is_dir() and path_is_within(args.output_dir, dem_path):
            raise ValueError(
                f"--output-dir cannot be inside a searched dem directory: {dem_path}"
            )
    if args.polygon_clip:
        args.polygon_clip = args.polygon_clip.expanduser().resolve()
        if not args.polygon_clip.is_file():
            raise ValueError(f"polygon clip boundary not found: {args.polygon_clip}")
    # a run that failed before its first viewshed leaves only scratch files
    if (
        args.output_dir.is_dir()
        and any(item.name != ".work" for item in args.output_dir.iterdir())
        and not (args.overwrite or args.resume or args.dry_run)
    ):
        raise ValueError(
            "output directory is not empty; use --overwrite, --resume, or a new directory"
        )
    dem_files = discover_dems(args.dem)
    required_tools = {*RASTER_TOOLS, *(POLYGON_TOOLS if args.polygons else ())}
    runtime = discover_gdal_runtime(args.gdal_bin, tuple(sorted(required_tools)))
    toolchain = Toolchain(
        timeout=args.timeout,
        # polygon tools are python scripts in some installs and need the
        # interpreter that ships with the gdal bindings
        gdal_python=args.gdal_python or runtime.python,
        tool_directories=runtime.tool_directories,
        environment=runtime.environment(),
    )
    args.warp_threads = max(1, (os.cpu_count() or 1) // min(args.jobs, len(observers)))
    for tool in sorted(required_tools):
        toolchain.command(tool)
    gdal_version, _ = toolchain.gdal_version()

    for dem_file in dem_files:
        dem_info = inspect_raster(toolchain, dem_file)
        if args.band > len(dem_info.get("bands", [])):
            raise ValueError(
                f"dem {dem_file} has {len(dem_info.get('bands', []))} band(s); "
                f"--band {args.band} is invalid"
            )
        source_crs_wkt(dem_info)

    lock: OutputLock | None = None
    if args.dry_run:
        temporary_context = tempfile.TemporaryDirectory(prefix="bulk-viewshed-")
        work_directory = Path(temporary_context.name)
    else:
        temporary_context = None
        lock = OutputLock(args.output_dir)
        lock.acquire()
        work_directory = args.output_dir / ".work"
    try:
        source = build_source(
            toolchain, dem_files, work_directory, args.source_nodata, args.band
        )
        source_info = inspect_raster(toolchain, source)
        plans = build_plans(
            observers,
            args.observer_crs,
            args.analysis_crs,
            source_info,
            args.output_dir,
            toolchain,
            args.allow_outside,
        )
        for analysis_crs in sorted({plan.analysis_crs for plan in plans}):
            if not crs_is_projected(toolchain, analysis_crs):
                raise ValueError(f"analysis crs is not projected: {analysis_crs}")
        polygons: PolygonSettings | None = None
        if args.polygons:
            clip = (
                prepare_clip(toolchain, args.polygon_clip, args.polygon_crs, work_directory)
                if args.polygon_clip
                else None
            )
            polygons = PolygonSettings(
                args.polygon_crs,
                args.polygon_simplify,
                clip,
                payload_hash(
                    {
                        "pipeline": POLYGON_PIPELINE_VERSION,
                        "crs": args.polygon_crs,
                        "simplify": args.polygon_simplify,
                        "clip_sha256": file_sha256(args.polygon_clip) if clip else None,
                    }
                ),
                args.shapefiles,
            )
        config = config_document(args, dem_files, gdal_version)
        run_config_hash = payload_hash(config)
        if not args.dry_run:
            adopted = adopt_legacy_outputs(
                plans,
                args.output_dir,
                run_config_hash,
                polygons.config_hash if polygons else None,
            )
            if adopted:
                log(f"renamed {adopted} viewsheds saved by an earlier version of this tool")
        conflicts = output_conflicts(plans)
        if conflicts and not (args.overwrite or args.resume or args.dry_run):
            preview = "\n".join(f"  {path}" for path in conflicts[:10])
            suffix = f"\n  ... and {len(conflicts) - 10} more" if len(conflicts) > 10 else ""
            raise ValueError(
                "outputs already exist; use --overwrite or --resume:\n" + preview + suffix
            )

        log(
            f"gdal: {gdal_version}; dems: {len(dem_files)}; observers: {len(plans)}; "
            f"jobs: {args.jobs}; polygons: {'yes' if polygons else 'no'}"
        )
        progress(
            "plan",
            observers=len(plans),
            dems=len(dem_files),
            gdal_version=gdal_version,
            dry_run=args.dry_run,
        )
        for plan in plans:
            log(
                f"plan {plan.observer.id}: ({plan.observer_x:.3f}, {plan.observer_y:.3f}) "
                f"{plan.analysis_crs}, radius={plan.observer.max_distance:g} -> {plan.output}"
            )
        if args.dry_run:
            log("input check complete and no viewsheds were created")
            progress("finished", status="validated", complete=0, failed=0)
            return 0

        args.output_dir.mkdir(parents=True, exist_ok=True)
        if args.overwrite:
            for plan in plans:
                plan.output.unlink(missing_ok=True)
                plan.state.unlink(missing_ok=True)
                plan.polygon.unlink(missing_ok=True)
                remove_shapefile(plan)
        manifest_path = args.output_dir / "manifest.json"
        try:
            previous_combined = json.loads(manifest_path.read_text(encoding="utf-8")).get(
                "combined"
            )
        except (OSError, json.JSONDecodeError, AttributeError):
            previous_combined = None
        run_started = time.monotonic()
        combined: dict[str, Any] | None = None

        def save_manifest() -> None:
            """record the settings and every result known so far"""

            write_json(
                manifest_path,
                {
                    "schema_version": 2,
                    "generated_utc": utc_now(),
                    "tool": "bulk-gdal-viewshed",
                    "tool_version": TOOL_VERSION,
                    "gdal_version": gdal_version,
                    "config_hash": run_config_hash,
                    "config": config,
                    "polygons": (
                        {
                            "crs": args.polygon_crs,
                            "simplify": args.polygon_simplify,
                            "clip": str(args.polygon_clip) if args.polygon_clip else None,
                            "shapefiles": args.shapefiles,
                        }
                        if polygons
                        else None
                    ),
                    "combined": combined,
                    "observers": [results[key] for key in sorted(results)],
                },
            )

        results: dict[int, dict[str, Any]] = {}
        failures: list[dict[str, Any]] = []
        futures: dict[Future[dict[str, Any]], Plan] = {}
        with ThreadPoolExecutor(max_workers=args.jobs, thread_name_prefix="viewshed") as executor:
            for plan in plans:
                futures[executor.submit(
                    process_plan_with_cleanup,
                    plan,
                    source,
                    args,
                    toolchain,
                    run_config_hash,
                    polygons,
                )] = plan
            for future in as_completed(futures):
                plan = futures[future]
                try:
                    results[plan.observer.row_number] = future.result()
                except (CancelledError, FutureCancelledError):
                    results[plan.observer.row_number] = {
                        "row_number": plan.observer.row_number,
                        "id": plan.observer.id,
                        "status": "cancelled",
                    }
                except Exception as error:
                    failure = {
                        "row_number": plan.observer.row_number,
                        "id": plan.observer.id,
                        "status": "failed",
                        "error": str(error),
                    }
                    results[plan.observer.row_number] = failure
                    failures.append(failure)
                    log(f"[{plan.observer.id}] failed: {error}")
                    progress("observer", id=plan.observer.id, stage="failed", error=str(error))
                    if args.fail_fast:
                        cancel_active_processes()
                        for pending in futures:
                            pending.cancel()

                # update the manifest after every observer so a stopped run
                # still leaves a useful record of completed and failed work
                save_manifest()

        if SIGNAL_CANCELLED.is_set():
            log("cancelled")
            return 130
        complete = [
            results[key] for key in sorted(results) if results[key].get("status") == "complete"
        ]
        combined_error: str | None = None
        if polygons and not CANCEL_REQUESTED.is_set():
            # built from whatever finished so one bad observer does not hold
            # back the coverage for the rest
            try:
                combined = build_combined(complete, args, toolchain, previous_combined)
            except ToolError as error:
                combined_error = str(error)
                log(f"combined polygons failed: {error}")
            save_manifest()
        elapsed = time.monotonic() - run_started
        log(
            f"finished in {format_duration(elapsed)} with {len(complete)} complete and "
            f"{len(failures)} failed. manifest saved to {manifest_path}"
        )
        failed = bool(failures or combined_error)
        progress(
            "finished",
            status="failed" if failed else "complete",
            complete=len(complete),
            failed=len(failures),
            manifest=str(manifest_path),
            elapsed_seconds=round(elapsed, 1),
        )
        return 1 if failed else 0
    finally:
        if lock is not None:
            lock.release()
        if temporary_context is not None:
            temporary_context.cleanup()


def main() -> None:
    """install signal handlers and turn uncaught errors into a clean exit"""

    signal.signal(signal.SIGINT, request_cancel)
    signal.signal(signal.SIGTERM, request_cancel)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, request_cancel)
    try:
        raise SystemExit(run())
    except CancelledError:
        raise SystemExit(130)
    except Exception as error:
        print(f"error: {error}", file=sys.stderr, flush=True)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
