# bulk-viewshed-analysis

a tool that creates one terrain viewshed for each observer in a csv
it can also trace each visible area as a polygon and dissolve them all into one coverage layer
it runs on windows, macos, and linux using GDAL from QGIS or another GDAL installation

## Before you start

you will need...

- python 3.10 or newer
- QGIS or GDAL 3.4.2 or newer
- one or more elevation rasters
- a csv containing your observer locations

QGIS is the easiest way to install GDAL if you do not already have it

## Open the tool

- windows - open `run-bulk-viewshed.bat`
- macos - open `run-bulk-viewshed.command`
- linux - run `./run-bulk-viewshed.sh`
- any system - run `python bulk_viewshed_gui.py`

some linux systems may also need `python3-tk`

on macos the python that comes with the system uses an old tk toolkit that can draw an empty window. if that happens install python from [python.org](https://www.python.org/downloads/) and open the launcher again

## Set up a run

1. choose your **observer locations csv**
2. add your **terrain elevation data** as individual rasters or a folder
3. choose an **output folder**
4. set **location CRS** to the CRS used by the csv coordinates
5. leave **viewshed CRS** as `auto-utm` unless your project needs one shared projected CRS
6. set the cell size distance and heights for your analysis
7. tick **polygons** if you also want vector outlines of what each observer can see
8. choose **check inputs**
9. once the check passes choose **run viewsheds**

with `auto-utm` the cell size and maximum distance are in metres

smaller cells give more detail but take longer to process

## What is the observer CSV?
"observers" are each point on the map you would like to execute viewshed analysis for. you will need to provide a CSV file containing each observer you would like to tool to use. each observer will need an `id`, `x`, and `y` column. it is also recommended you include height for your observer (if the observer is positioned on a tower, or you would like to factor in human height, etc.), and a maximum distance (by default, the tool will not cap how far the viewshed will spread, which may not be accurate to what your observer could actually see). 

this is an example of what your CSV should look like:
```csv
id,x,y,observer_height,target_height,max_distance
ridge-tower,-121.7500,45.5000,15,2,20000
valley-site,-121.6250,45.4250,,,10000
```

blank values will use the defaults in the GUI.

the `id` must be unique (can be any name, number, etc.) and the `x` and `y` are your longitude and latitude values, and must be accurate to your coordinate reference system.

for your convenience, choose **"create template..."** in the GUI to create a template CSV file to input your observers into. 

## Settings

- **cell size** controls the output raster resolution
- **maximum distance** is the farthest distance tested from each observer
- **observer height** is the observer height above the ground
- **target height** is the height of the feature being viewed
- **observers at once** controls how many viewsheds run together. each one loads its own terrain so lower it if your computer runs short on memory
- **resume finished viewsheds** keeps valid work from an earlier run
- **polygons** also traces each visible area as a polygon and builds a combined coverage file

the advanced tab can usually be left at its defaults

**reset settings** returns every setting to its default and keeps your chosen files and folders

## Polygons

polygons need the default `normal` output mode and a GDAL install that includes `gdal_polygonize` `ogr2ogr` and `ogrinfo`. QGIS includes all three

these settings are in the advanced tab

- **polygon simplify** removes the stair-stepped cell edges. it uses the same units as the cell size and `0` keeps every edge. one or two cell sizes is a good starting point for web maps
- **polygon crs** is the single CRS every polygon is saved in so they can be combined. the default is `EPSG:4326`
- **polygon clip boundary** trims polygons to a vector file such as a land mask so viewsheds stop at a coastline
- **also save each polygon as a shapefile** adds a `shapefiles` folder for software such as ArcGIS. attribute names are shortened to fit the shapefile limit

changing only the polygon settings and running again with **resume finished viewsheds** keeps the finished rasters and only redraws the polygons

## Results

choose **open output folder** when the run finishes

```text
output folder/
├── rasters/        one GeoTIFF viewshed per observer
├── polygons/       one GeoPackage polygon per observer when polygons are on
├── shapefiles/     the same polygons as shapefiles when that option is on
├── viewsheds.gpkg  every polygon together plus one dissolved coverage layer
├── state/          information used to validate and resume each result
└── manifest.json   settings and results for the full run
```

files are named after each observer `id`, so adding removing or reordering rows in the csv and running again only calculates the observers that are new or changed. outputs from older versions that were named by row number are renamed the first time you run again

in the default output mode `1` is visible `0` is hidden `254` is outside the maximum distance and `255` is nodata

`viewsheds.gpkg` has two layers. `viewsheds` has one feature per observer with its csv values and visible area. `coverage` is everything any observer can see as a single feature

each state file and the manifest record how many cells are visible and the visible area in squared viewshed CRS units

## Command line

the same engine runs without the GUI

```sh
python bulk_viewshed.py \
  --observers examples/observers.csv \
  --dem path/to/dems \
  --output-dir output \
  --observer-crs EPSG:4326 \
  --cell-size 10 \
  --polygons --polygon-simplify 15 --shapefiles
```

run `python bulk_viewshed.py --help` for every option

## Issues you may come across

- if macos says the `.command` file could not be executed, run `chmod +x run-bulk-viewshed.command` in this folder once. if macos blocks it as an unidentified download, right click it and choose **open**

- if GDAL is not found, open the advanced tab and choose your QGIS or GDAL folder
- if an observer is outside the elevation data, check the location CRS and coordinate order
- if elevation coverage is incomplete, add the missing raster tiles before running again
- if a run is interrupted, leave existing outputs set to **resume finished viewsheds** and start it again
- if polygons fail with a missing tool, select your QGIS folder in the advanced tab so its GDAL is used
