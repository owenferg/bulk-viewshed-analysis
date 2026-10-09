import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
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
    PolygonSettings,
    adopt_legacy_outputs,
    affine_pixel,
    auto_utm_crs,
    build_source,
    discover_dems,
    load_observers,
    observer_stems,
    parse_args,
    payload_hash,
    plan_fingerprint,
    process_plan_with_cleanup,
    safe_stem,
    sql_literal,
    visible_summary,
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
        self.assertEqual(safe_stem("CON"), "_CON")
        self.assertEqual(safe_stem("Crête / Site: 1"), "Crete_Site_1")

    def test_stems_ignore_row_order_and_separate_colliding_ids(self) -> None:
        first = [Observer(2, "ridge", 1, 2, 3, 4, 5), Observer(3, "a b", 1, 2, 3, 4, 5)]
        self.assertEqual(observer_stems(first), {"ridge": "ridge", "a b": "a_b"})
        moved = [Observer(2, "new", 1, 2, 3, 4, 5), Observer(9, "ridge", 1, 2, 3, 4, 5)]
        self.assertEqual(observer_stems(moved)["ridge"], "ridge")
        clash = observer_stems([*first, Observer(4, "a/b", 1, 2, 3, 4, 5)])
        self.assertNotEqual(clash["a b"], clash["a/b"])
        self.assertTrue(clash["a b"].startswith("a_b_"))


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
    def __init__(self, visible_cells: int = 40, polygon_features: int = 1) -> None:
        self.calls = []
        self.visible_cells = visible_cells
        self.polygon_features = polygon_features

    def names(self):
        return [name for name, _ in self.calls]

    def run(self, name, arguments):
        arguments = list(arguments)
        self.calls.append((name, arguments))
        if name in {"gdalwarp", "gdal_viewshed"}:
            Path(arguments[-1]).write_bytes(b"fake raster")
            return CommandResult("", "", 0)
        if name == "gdal_polygonize":
            Path(arguments[arguments.index("GPKG") + 2]).write_bytes(b"fake parts")
            return CommandResult("", "", 0)
        if name == "ogr2ogr":
            driver = "ESRI Shapefile" if "ESRI Shapefile" in arguments else "GPKG"
            Path(arguments[arguments.index(driver) + 1]).write_bytes(b"fake polygon")
            return CommandResult("", "", 0)
        if name == "ogrinfo":
            return CommandResult(f"  n (Integer) = {self.polygon_features}\n", "", 0)
        if name == "gdalinfo":
            band = {
                "type": "Byte",
                "minimum": 0,
                "maximum": 1,
                "checksum": 123,
                "metadata": {"": {"STATISTICS_VALID_PERCENT": "100"}},
            }
            if "-hist" in arguments:
                buckets = [0] * 256
                buckets[0] = 100 - self.visible_cells
                buckets[1] = self.visible_cells
                band["histogram"] = {"count": 256, "min": -0.5, "max": 255.5, "buckets": buckets}
            payload = {
                "size": [10, 10],
                "coordinateSystem": {"wkt": 'PROJCRS["test"]'},
                "bands": [band],
            }
            return CommandResult(__import__("json").dumps(payload), "", 0)
        raise AssertionError(f"unexpected fake command: {name}")


def processing_fixture(root: Path, **overrides):
    source = root / "source.vrt"
    source.write_text("fake", encoding="utf-8")
    plan = Plan(
        Observer(2, "o'brien", 1, 2, 3, 4, 100),
        "EPSG:32610",
        500000,
        5000000,
        "000001_test",
        root / "rasters/000001_test.tif",
        root / "state/000001_test.json",
        root / "polygons/000001_test.gpkg",
        root / "shapefiles/000001_test.shp",
    )
    settings = dict(
        output_dir=root,
        resume=True,
        keep_work=False,
        cell_size=10,
        resampling="bilinear",
        warp_nodata=-999999,
        warp_threads=2,
        source_nodata=None,
        allow_dem_gaps=False,
        curvature_coefficient=0.85714,
        output_mode="NORMAL",
        out_of_range_value=254,
        visible_value=1,
        invisible_value=0,
        output_nodata=255,
        creation_option=["TILED=YES"],
    )
    settings.update(overrides)
    return source, plan, argparse.Namespace(**settings)


def astuple_head(plan):
    return plan.observer, plan.analysis_crs, plan.observer_x, plan.observer_y


def astuple_tail(plan):
    return (
        plan.analysis_crs, plan.observer_x, plan.observer_y, plan.stem,
        plan.output, plan.state, plan.polygon, plan.shapefile,
    )


def run_plan(plan, source, args, toolchain, polygons=None):
    with redirect_stdout(io.StringIO()):
        return process_plan_with_cleanup(plan, source, args, toolchain, "config", polygons)


class ProcessingTests(unittest.TestCase):
    def test_height_mode_explicitly_sets_out_of_range_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, plan, args = processing_fixture(
                root, resume=False, output_mode="GROUND", out_of_range_value=-1, output_nodata=-1
            )
            toolchain = FakeProcessingToolchain()
            state = run_plan(plan, source, args, toolchain)
            self.assertEqual(state["status"], "complete")
            self.assertIsNone(state["visible"])
            warp = next(call for call in toolchain.calls if call[0] == "gdalwarp")
            self.assertIn("-srcalpha", warp[1])
            self.assertEqual(warp[1][warp[1].index("-wo") + 1], "NUM_THREADS=2")
            viewshed = next(call for call in toolchain.calls if call[0] == "gdal_viewshed")
            arguments = viewshed[1]
            self.assertEqual(arguments[arguments.index("-ov") + 1], "-1")
            self.assertFalse((root / ".work/000001_test").exists())


class PolygonTests(unittest.TestCase):
    def test_polygon_is_dissolved_from_visible_cells_with_observer_attributes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, plan, args = processing_fixture(root)
            clip = root / "clip.gpkg"
            toolchain = FakeProcessingToolchain()
            state = run_plan(
                plan, source, args, toolchain, PolygonSettings("EPSG:4326", 25, clip, "hash")
            )
            self.assertEqual(state["visible"], {"cells": 40, "area": 4000})
            self.assertEqual(state["polygon"]["output"], "polygons/000001_test.gpkg")
            self.assertTrue(plan.polygon.is_file())
            arguments = next(call for call in toolchain.calls if call[0] == "ogr2ogr")[1]
            sql = arguments[arguments.index("-sql") + 1]
            self.assertIn("ST_Union(geom)", sql)
            self.assertIn("WHERE value = 1", sql)
            self.assertIn("'o''brien' AS id", sql)
            self.assertIn("CAST(4000.0 AS REAL) AS visible_area", sql)
            self.assertEqual(arguments[arguments.index("-t_srs") + 1], "EPSG:4326")
            self.assertEqual(arguments[arguments.index("-simplify") + 1], "25")
            self.assertEqual(arguments[arguments.index("-clipdst") + 1], str(clip))
            self.assertIn("-makevalid", arguments)
            self.assertEqual(list(plan.polygon.parent.glob(".*")), [])

    def test_changed_polygon_settings_keep_the_finished_raster(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, plan, args = processing_fixture(root)
            run_plan(plan, source, args, FakeProcessingToolchain())

            toolchain = FakeProcessingToolchain()
            settings = PolygonSettings("EPSG:4326", 0, None, "first")
            state = run_plan(plan, source, args, toolchain, settings)
            self.assertEqual(state["run_status"], "updated")
            self.assertNotIn("gdal_viewshed", toolchain.names())
            self.assertIn("gdal_polygonize", toolchain.names())

            toolchain = FakeProcessingToolchain()
            state = run_plan(plan, source, args, toolchain, settings)
            self.assertEqual(state["run_status"], "resumed")
            self.assertNotIn("gdal_polygonize", toolchain.names())

            toolchain = FakeProcessingToolchain()
            changed = PolygonSettings("EPSG:4326", 30, None, "second")
            state = run_plan(plan, source, args, toolchain, changed)
            self.assertEqual(state["run_status"], "updated")
            self.assertNotIn("gdal_viewshed", toolchain.names())
            self.assertIn("gdal_polygonize", toolchain.names())

    def test_no_visible_cells_warns_and_skips_the_polygon(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, plan, args = processing_fixture(root)
            toolchain = FakeProcessingToolchain(visible_cells=0)
            settings = PolygonSettings("EPSG:4326", 0, None, "hash")
            state = run_plan(plan, source, args, toolchain, settings)
            self.assertEqual(state["status"], "complete")
            self.assertIn("no cells are visible", state["warnings"][0])
            self.assertIsNone(state["polygon"]["output"])
            self.assertNotIn("gdal_polygonize", toolchain.names())
            # an empty result is still a finished one when the run is resumed
            state = run_plan(plan, source, args, FakeProcessingToolchain(0), settings)
            self.assertEqual(state["run_status"], "resumed")

    def test_polygon_emptied_by_the_clip_is_not_written(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, plan, args = processing_fixture(root)
            toolchain = FakeProcessingToolchain(polygon_features=0)
            settings = PolygonSettings("EPSG:4326", 0, root / "clip.gpkg", "hash")
            state = run_plan(plan, source, args, toolchain, settings)
            self.assertIsNone(state["polygon"]["output"])
            self.assertIn("empty after clipping", state["polygon"]["warnings"][0])
            self.assertFalse(plan.polygon.exists())

    def test_visible_summary_needs_one_bucket_per_byte_value(self) -> None:
        band = {"type": "Byte", "histogram": {"min": -0.5, "max": 255.5, "buckets": [5] * 256}}
        self.assertEqual(visible_summary({"bands": [band]}, 1, 10), {"cells": 5, "area": 500})
        band["histogram"]["buckets"] = [5] * 10
        self.assertIsNone(visible_summary({"bands": [band]}, 1, 10))
        self.assertIsNone(visible_summary({"bands": [{"type": "Byte"}]}, 1, 10))

    def test_sql_literals_keep_numbers_real_and_escape_quotes(self) -> None:
        self.assertEqual(sql_literal(30), "30.0")
        self.assertEqual(sql_literal(None), "NULL")
        self.assertEqual(sql_literal("it's"), "'it''s'")

    def test_polygon_options_need_polygons_and_normal_mode(self) -> None:
        base = ArgumentTests().base_args()
        self.assertFalse(parse_args(base).polygons)
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parse_args([*base, "--polygon-simplify", "5"])
            with self.assertRaises(SystemExit):
                parse_args([*base, "--polygons", "--output-mode", "dem"])


class ResumeKeyTests(unittest.TestCase):
    def test_moving_or_renumbering_a_row_keeps_the_finished_viewshed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, plan, args = processing_fixture(root)
            run_plan(plan, source, args, FakeProcessingToolchain())

            moved = Plan(Observer(40, "o'brien", 1, 2, 3, 4, 100), *astuple_tail(plan))
            self.assertEqual(plan_fingerprint(moved, "config"), plan_fingerprint(plan, "config"))
            toolchain = FakeProcessingToolchain()
            state = run_plan(moved, source, args, toolchain)
            self.assertEqual(state["run_status"], "resumed")
            self.assertEqual(state["observer"]["row_number"], 40)
            self.assertNotIn("gdal_viewshed", toolchain.names())

            taller = Plan(Observer(40, "o'brien", 1, 2, 9, 4, 100), *astuple_tail(plan))
            self.assertNotEqual(plan_fingerprint(taller, "config"), plan_fingerprint(plan, "config"))

    def test_row_numbered_outputs_from_earlier_releases_are_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, plan, args = processing_fixture(root)
            plan = Plan(
                *astuple_head(plan),
                "o_brien",
                root / "rasters/o_brien.tif",
                root / "state/o_brien.json",
                root / "polygons/o_brien.gpkg",
                root / "shapefiles/o_brien.shp",
            )
            saved = {
                "row_number": 7, "id": "o'brien", "x": 1, "y": 2,
                "observer_height": 3, "target_height": 4, "max_distance": 100,
            }
            old = payload_hash(
                {"run_config_hash": "config", "observer": saved, "crs": "EPSG:32610"}
            )
            for folder in ("rasters", "state", "polygons"):
                (root / folder).mkdir()
            (root / "rasters/000006_o_brien.tif").write_bytes(b"fake raster")
            (root / "polygons/000006_o_brien.gpkg").write_bytes(b"fake polygon")
            (root / "state/000006_o_brien.json").write_text(
                json.dumps({
                    "status": "complete",
                    "fingerprint": old,
                    "observer": saved,
                    "analysis_crs": "EPSG:32610",
                    "output": "rasters/000006_o_brien.tif",
                    "polygon": {
                        "fingerprint": payload_hash({"raster": old, "polygons": "hash"}),
                        "output": "polygons/000006_o_brien.gpkg",
                    },
                }),
                encoding="utf-8",
            )

            self.assertEqual(adopt_legacy_outputs([plan], root, "config", "hash"), 1)

            state = json.loads(plan.state.read_text(encoding="utf-8"))
            self.assertEqual(state["fingerprint"], plan_fingerprint(plan, "config"))
            self.assertEqual(state["output"], "rasters/o_brien.tif")
            self.assertEqual(state["polygon"]["output"], "polygons/o_brien.gpkg")
            self.assertEqual(
                state["polygon"]["fingerprint"],
                payload_hash({"raster": state["fingerprint"], "polygons": "hash"}),
            )
            self.assertTrue(plan.output.is_file() and plan.polygon.is_file())
            self.assertEqual(list((root / "state").glob("0*")), [])
            self.assertEqual(adopt_legacy_outputs([plan], root, "config", "hash"), 0)


class ShapefileTests(unittest.TestCase):
    def test_shapefile_is_exported_with_short_field_names_and_resumed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, plan, args = processing_fixture(root)
            settings = PolygonSettings("EPSG:4326", 0, None, "hash")
            run_plan(plan, source, args, FakeProcessingToolchain(), settings)
            self.assertFalse(plan.shapefile.exists())

            # asking for shapefiles later only adds the export
            toolchain = FakeProcessingToolchain()
            wanted = PolygonSettings("EPSG:4326", 0, None, "hash", True)
            run_plan(plan, source, args, toolchain, wanted)
            self.assertEqual(toolchain.names(), ["gdalinfo", "ogr2ogr"])
            self.assertTrue(plan.shapefile.is_file())
            sql = toolchain.calls[-1][1][toolchain.calls[-1][1].index("-sql") + 1]
            self.assertIn("observer_height AS obs_height", sql)
            self.assertIn("visible_area AS vis_area", sql)

            toolchain = FakeProcessingToolchain()
            state = run_plan(plan, source, args, toolchain, wanted)
            self.assertEqual(state["run_status"], "resumed")
            self.assertNotIn("ogr2ogr", toolchain.names())

    def test_shapefiles_need_polygons(self) -> None:
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parse_args([*ArgumentTests().base_args(), "--shapefiles"])


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
