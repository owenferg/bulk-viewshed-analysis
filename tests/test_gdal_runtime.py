import os
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gdal_runtime import CORE_TOOLS, runtime_from_location  # noqa: E402


class GdalRuntimeTests(unittest.TestCase):
    def test_qgis_root_finds_tools_and_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "QGIS 4.0"
            tools = root / "bin"
            tools.mkdir(parents=True)
            suffix = ".exe" if os.name == "nt" else ""
            for name in CORE_TOOLS:
                (tools / f"{name}{suffix}").write_bytes(b"")
            proj = root / "share/proj"
            gdal = root / "share/gdal"
            proj.mkdir(parents=True)
            gdal.mkdir(parents=True)

            runtime = runtime_from_location(root)

            self.assertIsNotNone(runtime)
            assert runtime is not None
            self.assertIn(tools, runtime.tool_directories)
            self.assertEqual(runtime.proj_data, proj)
            self.assertEqual(runtime.gdal_data, gdal)
            environment = runtime.environment({"PATH": "original"})
            self.assertTrue(environment["PATH"].startswith(str(tools)))
            self.assertEqual(environment["PROJ_DATA"], str(proj))

    def test_incomplete_location_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "gdalinfo").write_bytes(b"")
            self.assertIsNone(runtime_from_location(root))


if __name__ == "__main__":
    unittest.main()
