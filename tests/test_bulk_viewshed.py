import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bulk_viewshed import (  # noqa: E402
    CommandResult,
    Observer,
    OutputLock,
    Plan,
    affine_pixel,
    auto_utm_crs,
    build_source,
    discover_dems,
    load_observers,
    parse_args,
    process_plan_with_cleanup,
    safe_stem,
)


class ObserverCsvTests(unittest.TestCase):
    def write_csv(self, directory: str, text: str) -> Path:
        path = Path(directory) / "observers.csv"
        path.write_text(text, encoding="utf-8")
        return path

    def test_blanks_use_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_csv(directory, "id,x,y,observer_height,target_height,max_distance\na,1,2,,,\n")
            observer = load_observers(path, 3, 1, 500)[0]
            self.assertEqual(observer.observer_height, 3)
            self.assertEqual(observer.target_height, 1)
            self.assertEqual(observer.max_distance, 500)

    def test_row_values_override_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_csv(directory, "id,x,y,observer_height,target_height,max_distance\na,1,2,10,4,900\n")
            observer = load_observers(path, 3, 1, 500)[0]
            self.assertEqual((observer.observer_height, observer.target_height), (10, 4))
            self.assertEqual(observer.max_distance, 900)

    def test_unknown_columns_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_csv(directory, "id,x,y,azmiuth\na,1,2,90\n")
            with self.assertRaisesRegex(ValueError, "unknown columns: azmiuth"):
                load_observers(path, 2, 0, 500)

    def test_duplicate_ids_are_case_insensitive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_csv(directory, "id,x,y\nAlpha,1,2\nalpha,2,3\n")
            with self.assertRaisesRegex(ValueError, "duplicate id"):
                load_observers(path, 2, 0, 500)

    def test_non_finite_numbers_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_csv(directory, "id,x,y\na,nan,2\n")
            with self.assertRaisesRegex(ValueError, "must be finite"):
                load_observers(path, 2, 0, 500)


class GeometryTests(unittest.TestCase):
    def test_auto_utm_works_in_both_hemispheres(self) -> None:
        self.assertEqual(auto_utm_crs(-123, 45), "EPSG:32610")
        self.assertEqual(auto_utm_crs(151, -33), "EPSG:32756")

    def test_auto_utm_rejects_polar_coordinates(self) -> None:
        with self.assertRaisesRegex(ValueError, "polar"):
            auto_utm_crs(10, 85)

    def test_affine_inverse_supports_rotation(self) -> None:
        geotransform = (100, 10, 2, 200, 1, -10)
        x = 100 + 3 * 10 + 4 * 2
        y = 200 + 3 * 1 + 4 * -10
        column, row = affine_pixel(geotransform, x, y)
        self.assertAlmostEqual(column, 3)
        self.assertAlmostEqual(row, 4)

    def test_safe_stem_is_portable(self) -> None:
        self.assertEqual(safe_stem("CON", 2), "000001__CON")
        self.assertEqual(safe_stem("Crête / Site: 1", 3), "000002_Crete_Site_1")


class DemDiscoveryTests(unittest.TestCase):
    def test_directories_are_recursive_and_case_insensitive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "nested"
            nested.mkdir()
            expected = nested / "terrain.TIFF"
            expected.write_bytes(b"not needed for discovery")
            (nested / "notes.txt").write_text("ignore", encoding="utf-8")
            self.assertEqual(discover_dems([root]), [expected.resolve()])

    def test_vrt_is_only_accepted_when_passed_directly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vrt = root / "mosaic.vrt"
            vrt.write_text("<VRTDataset />", encoding="utf-8")
            tif = root / "terrain.tif"
            tif.write_bytes(b"placeholder")
            self.assertEqual(discover_dems([root]), [tif.resolve()])
            self.assertEqual(discover_dems([vrt]), [vrt.resolve()])


class FakeToolchain:
    def __init__(self) -> None:
        self.calls = []

    def run(self, name, arguments):
        self.calls.append((name, list(arguments)))
        Path(arguments[-1]).write_text("<VRTDataset />", encoding="utf-8")
        return CommandResult("", "", 0)


class GdalCommandTests(unittest.TestCase):
    def test_vrt_is_strict_and_selects_the_requested_band(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.tif"
            source.write_bytes(b"placeholder")
            toolchain = FakeToolchain()
            result = build_source(toolchain, [source], root / "work", -9999, 2)
            self.assertTrue(result.is_file())
            name, arguments = toolchain.calls[0]
            self.assertEqual(name, "gdalbuildvrt")
            self.assertIn("-strict", arguments)
            self.assertIn("-addalpha", arguments)
            self.assertEqual(arguments[arguments.index("-b") + 1], "2")
            self.assertEqual(arguments[arguments.index("-srcnodata") + 1], "-9999")


class ArgumentTests(unittest.TestCase):
    def base_args(self):
        return [
            "--observers", "observers.csv", "--dem", "terrain.tif",
            "--output-dir", "output", "--observer-crs", "EPSG:4326",
            "--cell-size", "10",
        ]

    def test_normal_class_defaults_are_distinct_bytes(self) -> None:
        args = parse_args(self.base_args())
        self.assertEqual(
            (args.visible_value, args.invisible_value, args.out_of_range_value, args.output_nodata),
            (1, 0, 254, 255),
        )

    def test_height_mode_uses_nodata_outside_radius(self) -> None:
        args = parse_args([*self.base_args(), "--output-mode", "GROUND"])
        self.assertEqual(args.out_of_range_value, args.output_nodata)

    def test_normal_nodata_must_be_a_distinct_byte(self) -> None:
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parse_args([*self.base_args(), "--output-nodata", "2.5"])
            with self.assertRaises(SystemExit):
                parse_args([*self.base_args(), "--output-nodata", "1"])


class FakeProcessingToolchain:
    def __init__(self) -> None:
        self.calls = []

    def run(self, name, arguments):
        arguments = list(arguments)
        self.calls.append((name, arguments))
        if name in {"gdalwarp", "gdal_viewshed"}:
            Path(arguments[-1]).write_bytes(b"fake raster")
            return CommandResult("", "", 0)
        if name == "gdalinfo":
            payload = {
                "size": [10, 10],
                "coordinateSystem": {"wkt": 'PROJCRS["test"]'},
                "bands": [{
                    "type": "Byte",
                    "minimum": 0,
                    "maximum": 1,
                    "checksum": 123,
                    "metadata": {"": {"STATISTICS_VALID_PERCENT": "100"}},
                }],
            }
            return CommandResult(__import__("json").dumps(payload), "", 0)
        raise AssertionError(f"unexpected fake command: {name}")


class ProcessingTests(unittest.TestCase):
    def test_height_mode_explicitly_sets_out_of_range_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.vrt"
            source.write_text("fake", encoding="utf-8")
            observer = Observer(2, "test", 1, 2, 3, 4, 100)
            plan = Plan(
                observer,
                "EPSG:32610",
                500000,
                5000000,
                "000001_test",
                root / "rasters/000001_test.tif",
                root / "state/000001_test.json",
            )
            args = argparse.Namespace(
                output_dir=root,
                resume=False,
                keep_work=False,
                cell_size=10,
                resampling="bilinear",
                warp_nodata=-999999,
                source_nodata=None,
                allow_dem_gaps=False,
                curvature_coefficient=0.85714,
                output_mode="GROUND",
                out_of_range_value=-1,
                visible_value=1,
                invisible_value=0,
                output_nodata=-1,
                creation_option=["TILED=YES"],
            )
            toolchain = FakeProcessingToolchain()
            with redirect_stdout(io.StringIO()):
                state = process_plan_with_cleanup(plan, source, args, toolchain, "config")
            self.assertEqual(state["status"], "complete")
            warp = next(call for call in toolchain.calls if call[0] == "gdalwarp")
            self.assertIn("-srcalpha", warp[1])
            viewshed = next(call for call in toolchain.calls if call[0] == "gdal_viewshed")
            arguments = viewshed[1]
            self.assertEqual(arguments[arguments.index("-ov") + 1], "-1")
            self.assertFalse((root / ".work/000001_test").exists())


class OutputLockTests(unittest.TestCase):
    def test_prevents_two_writers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = OutputLock(Path(directory))
            second = OutputLock(Path(directory))
            first.acquire()
            try:
                with self.assertRaisesRegex(RuntimeError, "locked by another run"):
                    second.acquire()
            finally:
                first.release()
            self.assertFalse(first.path.exists())


if __name__ == "__main__":
    unittest.main()
