#!/usr/bin/env python3
"""find a usable gdal command line runtime on windows macos and linux"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Iterable, Mapping, Sequence


GDAL_ROOT_ENV = "BULK_VIEWSHED_GDAL_ROOT"
CORE_TOOLS = (
    "gdal_viewshed",
    "gdalinfo",
    "gdalsrsinfo",
    "gdaltransform",
    "gdalwarp",
    "gdalbuildvrt",
)


def _version_key(path: Path) -> tuple[int, ...]:
    """turn version numbers in a folder name into a sortable tuple"""

    values = tuple(map(int, re.findall(r"\d+", path.name)))
    return values or (0,)


def _unique_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    """keep existing folders once while preserving discovery order"""

    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = os.path.normcase(str(path))
        if path.is_dir() and key not in seen:
            seen.add(key)
            result.append(path)
    return tuple(result)


def _tool_file(directories: Sequence[Path], name: str) -> Path | None:
    """find one command across native executables scripts and wrappers"""

    suffixes = (".exe", "", ".py", ".bat", ".cmd") if os.name == "nt" else ("", ".py")
    return next(
        (
            directory / f"{name}{suffix}"
            for directory in directories
            for suffix in suffixes
            if (directory / f"{name}{suffix}").is_file()
        ),
        None,
    )


def candidate_directories(location: Path) -> tuple[Path, ...]:
    """list likely tool folders below a gdal folder or qgis install root"""

    location = location.expanduser().resolve()
    # a user may select the qgis root or one of its nested bin folders
    # checking nearby parents lets both choices work the same way
    roots = (location, location.parent, location.parent.parent)
    candidates: list[Path] = [location]
    for root in roots:
        candidates.extend(
            (
                root / "bin",
                root / "apps/gdal/bin",
                root / "apps/qgis/bin",
                root / "apps/qgis-ltr/bin",
                root / "Contents/MacOS",
                root / "Contents/Resources/bin",
            )
        )
        candidates.extend(sorted(root.glob("apps/Python*/Scripts"), reverse=True))
    return _unique_paths(candidates)


@dataclass(frozen=True)
class GdalRuntime:
    """command folders and environment data for one gdal installation"""

    root: Path | None
    tool_directories: tuple[Path, ...]
    proj_data: Path | None = None
    gdal_data: Path | None = None

    def environment(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        """add the discovered tools and data folders to a child environment"""

        env = dict(os.environ if base is None else base)
        current_path = env.get("PATH", "")
        entries = [*(str(path) for path in self.tool_directories)]
        if current_path:
            entries.append(current_path)
        env["PATH"] = os.pathsep.join(entries)
        if self.proj_data:
            env["PROJ_DATA"] = str(self.proj_data)
        if self.gdal_data:
            env["GDAL_DATA"] = str(self.gdal_data)
        if self.root:
            env[GDAL_ROOT_ENV] = str(self.root)
        return env

    def missing_tools(self, required: Sequence[str] = CORE_TOOLS) -> list[str]:
        """list required commands that are not part of this runtime"""

        return [name for name in required if _tool_file(self.tool_directories, name) is None]


def runtime_from_location(
    location: Path,
    required: Sequence[str] = CORE_TOOLS,
) -> GdalRuntime | None:
    """build a complete runtime from a gdal folder or qgis installation"""

    location = location.expanduser().resolve()
    directories = tuple(
        directory
        for directory in candidate_directories(location)
        if any(_tool_file((directory,), name) for name in required)
    )
    if not directories:
        return None
    root_candidates = (location, location.parent, location.parent.parent)

    def first_directory(relative_paths: Sequence[str]) -> Path | None:
        """return the first matching data folder near the selected location"""

        return next(
            (
                root / relative
                for root in root_candidates
                for relative in relative_paths
                if (root / relative).is_dir()
            ),
            None,
        )

    runtime = GdalRuntime(
        location,
        directories,
        first_directory(("share/proj", "apps/qgis/share/proj", "Contents/Resources/proj")),
        first_directory(("share/gdal", "apps/qgis/share/gdal", "Contents/Resources/gdal")),
    )
    return None if runtime.missing_tools(required) else runtime


def standard_install_locations(
    platform_name: str = sys.platform,
    environ: Mapping[str, str] | None = None,
) -> list[Path]:
    """list overrides and common qgis or gdal installation roots"""

    env = os.environ if environ is None else environ
    locations: list[Path] = []
    for variable in (GDAL_ROOT_ENV, "QGIS_ROOT", "OSGEO4W_ROOT"):
        if env.get(variable):
            locations.append(Path(env[variable]))
    if platform_name == "win32":
        for variable in ("ProgramFiles", "ProgramW6432"):
            if env.get(variable):
                locations.extend(
                    sorted(Path(env[variable]).glob("QGIS*"), key=_version_key, reverse=True)
                )
        locations.extend((Path("C:/OSGeo4W"), Path("C:/OSGeo4W64")))
    elif platform_name == "darwin":
        locations.extend(
            sorted(Path("/Applications").glob("QGIS*.app"), key=_version_key, reverse=True)
        )
    else:
        locations.extend((Path("/usr"), Path("/usr/local"), Path("/opt/qgis")))
    return list(_unique_paths(locations))


def discover_gdal_runtime(
    explicit: Path | None = None,
    required: Sequence[str] = CORE_TOOLS,
) -> GdalRuntime:
    """find gdal on path or in a standard qgis installation"""

    if explicit:
        runtime = runtime_from_location(explicit, required)
        if runtime:
            return runtime
        raise RuntimeError(
            f"the selected gdal or qgis location is missing required tools: {explicit}"
        )

    # path is preferred because it normally reflects a runtime the user has
    # already configured and tested outside this application
    path_tools = [Path(found).resolve() for name in required if (found := shutil.which(name))]
    if len(path_tools) == len(required):
        return GdalRuntime(None, _unique_paths(path.parent for path in path_tools))

    for location in standard_install_locations():
        runtime = runtime_from_location(location, required)
        if runtime:
            return runtime

    missing = ", ".join(required)
    raise RuntimeError(
        "could not find a complete gdal installation. install qgis or gdal then add its tools "
        f"to path or select the install folder. required tools: {missing}"
    )
