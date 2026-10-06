# geo-measure-api

Upload a zipped Shapefile or a KML, and this API tells you the area of every polygon and the length of every line, in metres.

The tricky part of this assignment is the CRS. You can't measure in lat/lon degrees, so every feature gets projected first. I pick the projection per feature, based on where it sits on the globe, and every number comes back with an exact ellipsoid value next to it, so you can check how accurate it is instead of taking my word for it.

Built for the Aereo SDE intern take-home ("Geospatial File Measurement API").

## Contents

- [Setup](#setup)
- [Try it](#try-it)
- [API](#api)
- [Architecture](#architecture)
- [Design decisions](#design-decisions)
- [Learnings](#learnings)
- [Future scope](#future-scope)

## Setup

You need Python 3.12 and [uv](https://docs.astral.sh/uv/). GDAL comes bundled inside the pyogrio and pyproj wheels, so there's nothing else to install.

```bash
uv sync
uv run uvicorn geomeasure.main:app --reload
```

The API runs on http://127.0.0.1:8000, and the interactive docs are at `/docs`. On first start it creates a `./data/` folder with a SQLite database and the uploads.

Tests and lint:

```bash
uv run pytest            # 122 tests, about 20 s
uv run ruff check .
uv run ruff format --check .
```

CI runs these on Ubuntu and Windows, and also builds the Docker image and uploads a sample file to it.

Docker:

```bash
docker build -t geomeasure .
docker run -p 8000:8000 -v geomeasure-data:/data geomeasure
```

Config is all environment variables with a `GEOMEASURE_` prefix (a `.env` file works too):

| Variable | Default | What it does |
|---|---|---|
| `GEOMEASURE_DATA_DIR` | `data` | Where uploads are stored |
| `GEOMEASURE_DATABASE_URL` | `sqlite:///data/geomeasure.db` | Any SQLAlchemy URL |
| `GEOMEASURE_MAX_UPLOAD_MB` | `50` | Max upload size |
| `GEOMEASURE_MAX_EXTRACTED_MB` | `500` | Max size a zip can expand to |
| `GEOMEASURE_MAX_COMPRESSION_RATIO` | `100` | Max compression ratio, to stop zip bombs |
| `GEOMEASURE_MAX_ZIP_ENTRIES` | `1000` | Max files inside a zip |

## Try it

I put two sample files in `samples/`, modelled on what a drone survey team near Hyderabad might hand over:

- `hyderabad_survey.kml` has three folders. "Fields" has two parcels and one self-intersecting bowtie polygon (a typical digitising mistake). "Infrastructure" has a road and a survey pole. "Reference" has a 100 m check plot near Eluru.
- `kothur_parcels_utm.zip` has two shapefiles in UTM 44N (EPSG:32644): two square parcels and a 2 km canal.

You can regenerate both with `uv run python scripts/make_samples.py`.

Upload the KML:

```bash
curl -F file=@samples/hyderabad_survey.kml http://127.0.0.1:8000/api/files/
```

```json
{
  "id": "1WeRIS3mquQ",
  "filename": "hyderabad_survey.kml",
  "format": "KML",
  "status": "COMPLETED",
  "crs": "EPSG:4326",
  "feature_count": 6,
  "layer_count": 3,
  "processing_ms": 698,
  "layers": [
    {"name": "Fields", "feature_count": 3, "crs": "EPSG:4326", "crs_assumed": false},
    {"name": "Infrastructure", "feature_count": 2, "crs": "EPSG:4326", "crs_assumed": false},
    {"name": "Reference", "feature_count": 1, "crs": "EPSG:4326", "crs_assumed": false}
  ],
  "geometry_types": {"LineString": 1, "Point": 1, "Polygon": 4}
}
```

(I trimmed a few fields like timestamps. Everything else is exactly what the server returned.)

Then get the measurements:

```bash
curl "http://127.0.0.1:8000/api/files/1WeRIS3mquQ/measurements/?include_geometry=false"
```

Here's one feature from that response. Field A is a 200 m square, so the right answer is 40,000 m²:

```json
{
  "index": 0,
  "layer": "Fields",
  "geometry_type": "Polygon",
  "source_crs": "EPSG:4326",
  "properties": {"Name": "Field A", "crop": "paddy", "owner_id": "101"},
  "measurement": {
    "status": "MEASURED",
    "area_m2": 39999.99999855672,
    "perimeter_m": 799.9999999855671,
    "projected_crs": "WGS 84 / LAEA centred on 17.2400N 78.4300E",
    "geodesic_area_m2": 40000.00000334298,
    "deviation_pct": 0.0
  }
}
```

The other features in the file:

| Feature | Result |
|---|---|
| Field B | Measured, 118,345 m² |
| Bowtie polygon | `INVALID_GEOMETRY`, reason: `Self-intersection[78.443 17.241].` |
| Access road | Measured, 1183.64 m |
| Survey pole | `NOT_APPLICABLE` (it's a point) |
| Eluru check plot | Measured in `EPSG:32644`, 9992 m² vs 10,000 m² geodesic, `deviation_pct` -0.08 |

The response also has a `summary` with totals for the whole file: area, length, and how many features were measured or skipped and why.

Two things worth noticing:

- **Hyderabad gets measured in LAEA, not UTM.** Hyderabad sits near the western edge of UTM zone 44N, where UTM's area error is just over the 0.1% I allow. So those features fall back to an equal-area projection centred on each one. Explained in [CRS handling](#crs-handling).
- **The Eluru plot shows UTM's built-in error.** It sits right on the zone's central meridian (81°E), so it stays in UTM. UTM scales distances by 0.9996 at the centre, which makes areas 0.9992 of true. That's exactly the -0.08% you see.

The shapefile sample works the same way: both parcels and the canal come back at 250,000 m², 90,000 m² and 2,000 m, matching their geodesic values. The source file is in UTM 44N, but Kothur is also near the zone edge, so they get measured in LAEA too.

To open results in QGIS:

```bash
curl -o hyderabad.geojson http://127.0.0.1:8000/api/files/1WeRIS3mquQ/features.geojson
```

## API

| Method | Path | What it does | Status codes |
|---|---|---|---|
| `POST` | `/api/files/` | Upload a `.zip` (Shapefile) or `.kml` as form field `file`. Processes it and returns the file record | 201, 400, 422 |
| `GET` | `/api/files/` | List files, newest first (`limit`, `offset`) | 200 |
| `GET` | `/api/files/{id}/` | Status and metadata: layers, CRS, counts per geometry type | 200, 404 |
| `GET` | `/api/files/{id}/measurements/` | Measurements per feature, plus totals for the file | 200, 404, 409 |
| `GET` | `/api/files/{id}/features.geojson` | Everything as a GeoJSON FeatureCollection with measurements in the properties | 200, 404, 409 |
| `DELETE` | `/api/files/{id}/` | Delete the file, its features and the stored upload | 204, 404 |
| `GET` | `/health` | Health check, including a database ping | 200 |

`/measurements/` takes `limit` (default 100, max 1000), `offset`, a `status` filter (e.g. `INVALID_GEOMETRY`), a `geometry_type` filter (e.g. `Polygon`), and `include_geometry` (default `true`). The `summary` always covers the whole file, whatever filters you pass.

What the status codes mean:

| Code | When |
|---|---|
| 201 | Processed fine |
| 400 | The upload itself is bad: wrong extension, empty, too big, not really a zip, a zip bomb, a zip trying to write outside its folder, a `.shp` missing its `.shx` or `.dbf`, or a KML that isn't XML. Nothing gets stored |
| 422 | The upload looked fine but GDAL couldn't read it. The record is kept as `FAILED` with the error, so you can still look it up |
| 404 | No file with that id |
| 409 | You asked for measurements on a file that didn't complete |

A few real examples:

```
$ curl -F "file=@notes.txt;filename=bad.zip" http://127.0.0.1:8000/api/files/
400 {"detail":"The upload has a .zip extension but is not a valid zip archive."}

$ curl -F file=@broken.zip http://127.0.0.1:8000/api/files/   # .shp/.shx/.dbf full of zero bytes
422 {"id":"DJ2jqiU_ZFg", "status":"FAILED", "error":"Could not read survey/bad.shp: ... not recognized as being in a supported file format.", ...}

$ curl http://127.0.0.1:8000/api/files/DJ2jqiU_ZFg/measurements/
409 {"detail":"File 'DJ2jqiU_ZFg' is FAILED, measurements need it to be COMPLETED. ..."}
```

### When a feature can't be measured

One bad feature doesn't fail the whole file. The file still completes, and each feature gets its own status with a plain-English `reason`:

| Status | Meaning |
|---|---|
| `MEASURED` | Area and perimeter for polygons, length for lines |
| `NOT_APPLICABLE` | A point. Nothing to measure |
| `UNSUPPORTED_GEOMETRY` | A collection that mixes points, lines and polygons, or a feature wider than half the globe |
| `INVALID_GEOMETRY` | A self-intersecting polygon (the reason says where), or coordinates that don't make sense for the file's CRS |
| `EMPTY_GEOMETRY` | No geometry at all |
| `UNKNOWN_CRS` | The file has no CRS and the coordinates don't look like lat/lon, so there's no way to get metres out of them |

Collections that contain only polygons or only lines get measured as one shape. Z values are ignored, so everything is measured in 2D.

## Architecture

### Layout

```
src/geomeasure/
  main.py          app setup
  config.py        settings from env vars
  pipeline.py      the upload flow: validate, store, read, measure, save
  domain.py        plain data types shared by every stage
  errors.py        InvalidUpload (-> 400) and ProcessingError (-> FAILED)
  api/             routes and response schemas
  ingest/
    upload.py      extension check, size limits, safe zip extraction
    reader.py      reads layers and features through GDAL, cleans up attributes
  geo/
    crs.py         reprojection and picking a CRS for a location
    measure.py     the measurement logic
  db/              SQLAlchemy models and session
tests/             ingest, reader, measurement and API tests
samples/           demo files
```

The geo and ingest code knows nothing about HTTP or the database. `measure_feature(geometry, crs)` is a pure function, so most of the measurement tests don't need a server or a file at all.

### What happens on upload

1. Check the extension. Only `.zip` and `.kml` are accepted.
2. Validate and unpack into `data/uploads/<id>/`. For a zip, that means extracting it safely, finding every `.shp` (nested folders included), and checking each one has its `.shx` and `.dbf`. For a KML, it means checking it parses as XML.
3. Only after that does a database row get created. A rejected upload never shows up in the file list, not even for a moment.
4. Read every layer with GDAL. A zip can hold several shapefiles and a KML several folders, and each one becomes a layer.
5. Measure each feature, convert its geometry to WGS84 GeoJSON for the output, and save everything in one bulk insert.
6. Mark the file `COMPLETED`, or `FAILED` with the error if GDAL couldn't read it.

Error messages never leak server paths. GDAL's messages get cleaned down to the path inside the upload, like `survey/bad.shp`. Anything unexpected is logged on the server, and the client gets a generic message.

### How a feature gets measured

1. Skip it if it's empty or has no CRS. Drop Z values.
2. Work out whether it's a point, line or polygon. Points stop here.
3. Polygons have to be valid. If one isn't, report where it breaks.
4. Add extra points along long edges, then convert to lat/lon. Why that matters is under [CRS handling](#crs-handling).
5. Handle features that cross the 180° line by taking the short way round.
6. Pick a projection for the feature (below) and project it.
7. Measure in that projection. These are the official numbers.
8. Measure again on the WGS84 ellipsoid with `pyproj.Geod` and record the % difference.

### CRS handling

**Reading the source CRS.** Shapefiles use their `.prj`. If the `.prj` is missing and every coordinate fits in lat/lon ranges, I assume WGS84 and say so with `crs_assumed: true`. If it's missing and the coordinates clearly aren't lat/lon, I don't guess. Those features come back as `UNKNOWN_CRS`. KML is always WGS84.

I also check that coordinates actually fit the CRS the file claims. PROJ doesn't always complain about garbage: UTM silently returns infinity, and Web Mercator quietly pins huge values to latitude 90. So I convert each geometry to lat/lon and back, and if it doesn't come back the same, the feature is marked `INVALID_GEOMETRY`. The rest of the file still processes.

**Picking the projection to measure in.** I do this per feature, not per file. A file can cover one village or a whole state, and one projection for everything would distort the features at the edges.

1. Start with the UTM zone of the feature's centre. Survey teams already work in UTM, so seeing `EPSG:32644` in the output means something to them.
2. Check UTM's area distortion at every vertex of the feature. If it's more than 0.1% off anywhere, switch to Lambert Azimuthal Equal Area (LAEA) centred on the feature. LAEA preserves area by design, so it handles features near zone edges, features wider than a zone, and the poles.

That's why the Hyderabad sample comes back in LAEA. Hyderabad is about 2.6° west of zone 44N's centre, where UTM's area distortion is 0.105%, just over the line. The Eluru plot sits right on the centre and stays in UTM.

**Adding points along edges.** A polygon edge is a straight line in whatever CRS it was drawn in. If you only reproject its two endpoints, each projection bends that edge differently, and on a large polygon UTM, LAEA and geodesic start disagreeing by over 1%. Adding points along the edge in the original CRS keeps the shape the surveyor actually drew.

**The cross-check.** Every measured feature also carries the exact value computed on the ellipsoid, plus `deviation_pct`. For survey-sized features it rounds to 0.0. In the tests, a 20° by 20° polygon stays under 0.01% and a 20° line under 0.5%.

## Design decisions

**FastAPI over Django.** This is one resource and a handful of endpoints. FastAPI gives me typed request and response models and OpenAPI docs from the same code. Django's admin and project structure would be weight I'd carry without using. SQLAlchemy handles the database.

**Processing inside the request.** Survey files are usually tens to a few thousand features, and both samples finish in under a second. So the upload returns the full result in one call, with no polling. The endpoint runs on FastAPI's threadpool, so it doesn't block other requests. I still store a status (`PROCESSING`, `COMPLETED`, `FAILED`), so moving to a background worker later is easy: return `202 Accepted` with the id and let the client poll `GET /api/files/{id}/`. The pipeline itself wouldn't change.

**400 vs 422.** A 400 means the request was wrong, and there's nothing worth keeping. A 422 means the file looked real but couldn't be read. I keep that record so the user can see what they uploaded and why it failed.

**Projection per feature instead of per file.** One CRS per file breaks on files that span zones and loses accuracy near zone edges. Picking per feature costs a lookup and a distortion check each time, and I cache the projection objects so it stays cheap.

Other options I considered for the measuring projection:

- **Web Mercator (EPSG:3857).** It's what web maps use, and it's wrong for area. It inflates areas by 1/cos²(latitude), which is about 10% at Hyderabad and 4x at 60°N.
- **Always measuring on the ellipsoid.** That's exact, but the brief asks for a projected CRS, and survey teams think in projected coordinates. So I use the projection for the official number and the ellipsoid as the check.
- **One fixed national CRS.** It ties the service to one country and still distorts toward the edges of a big one.

**Invalid polygons get reported, not fixed.** Shapely's `make_valid` turns a bowtie into two triangles, and their combined area is a guess at what the surveyor meant. For a service whose whole job is accurate numbers, I'd rather say exactly where the polygon breaks so the source gets fixed.

**Upload safety.** I never extract a zip blindly. Paths that try to escape the upload folder (`..`, absolute paths) are rejected. Zip bombs are stopped by limits on file count, total size and compression ratio, and I also count bytes as they're actually written, because zip headers can lie. `__MACOSX` junk is skipped, and if a `.shp` is missing its `.shx` or `.dbf`, the error names exactly which file is missing.

**SQLite.** Zero setup for whoever reviews this, and the database URL is configurable if you want Postgres.

**Things I added beyond the brief:** the GeoJSON export for QGIS, list and delete endpoints, filters and paging on measurements, whole-file totals, per-layer info, the geodesic cross-check, Docker, CI on Linux and Windows, and the sample data.

## Learnings

**KML through GDAL adds a lot of noise.** GDAL's LIBKML driver adds about a dozen columns to every layer (`tessellate`, `extrude`, `visibility`, `altitudeMode` and more), whether the file uses them or not. Some are filled with defaults like -1 or 0 instead of nulls, so simply dropping empty columns didn't work. I ended up keeping a list of each column's default and dropping a column only when every value is missing or equal to that default. LIBKML also turns every `<Folder>` into its own layer, which is why layers show up in the response.

**KML attributes are all strings.** Values in `<ExtendedData><Data>` have no types, so `owner_id` comes back as `"101"`, not `101`. You only get real types if the KML declares a `<Schema>`. I chose not to guess types from strings.

**Ring direction flips signs.** The shapefile writer stores outer rings clockwise, and `pyproj.Geod` returns a negative area for clockwise rings. So I orient polygons before the geodesic check.

**UTM isn't exact even at the centre.** Its 0.9996 scale factor makes areas about 0.08% low on the central meridian, and at Hyderabad's latitude the error crosses +0.1% around 2.5° out. That's why I check distortion at every vertex instead of trusting the zone.

**PROJ fails quietly.** Coordinates a CRS can't represent don't raise errors, they just turn into infinity or get clamped. Converting to lat/lon and back was the simplest reliable way I found to catch that.

**Edge densification mattered as much as picking the projection.** Without it, big polygons gave different areas in every projection. The fix was deciding that "straight" means straight in the source CRS, and adding points there.

## Future scope

- Background processing for big files: a worker queue, `202 Accepted`, and polling on the status field that already exists.
- PostGIS, so geometries are queryable (find parcels in an area, find overlaps) instead of stored as JSON.
- Database migrations with Alembic. Right now tables are created on startup.
- More formats: KMZ (currently rejected with a message to unzip it), GeoJSON and GeoPackage. GDAL already reads all of them.
- Auth and per-user files.
- S3 or Azure Blob for uploads instead of local disk.
- An optional repair endpoint that runs `make_valid` and shows the repaired shape and area, so fixing a polygon is an explicit choice.
- Streaming reads for very large files.
