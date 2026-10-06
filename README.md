# geo-measure-api

A FastAPI service that takes a zipped Shapefile or a KML file, reads every feature, and returns area and perimeter for polygons and length for lines, in metres. Each feature is measured in a projected CRS chosen for its location, and every number comes with a geodesic cross-check so you can see how accurate it is.

Built for the Aereo SDE intern take-home, "Geospatial File Measurement API".

## Contents

- [Setup](#setup)
- [Try it in 30 seconds](#try-it-in-30-seconds)
- [API](#api)
- [Architecture](#architecture)
- [Design decisions](#design-decisions)
- [Learnings](#learnings)
- [Future scope](#future-scope)

## Setup

You need Python 3.12 and [uv](https://docs.astral.sh/uv/). GDAL ships inside the pyogrio and pyproj wheels, so there's nothing to install system-wide.

```bash
uv sync
uv run uvicorn geomeasure.main:app --reload
```

The API runs on http://127.0.0.1:8000. Interactive docs are at `/docs` (the root URL redirects there). On first start it creates `./data/` with a SQLite database and an `uploads/` folder.

### Tests

```bash
uv run pytest            # 122 tests, about 20 s
uv run ruff check .
uv run ruff format --check .
```

CI runs the same three commands on Ubuntu and Windows (`.github/workflows/ci.yml`).

### Docker

```bash
docker build -t geomeasure .
docker run -p 8000:8000 -v geomeasure-data:/data geomeasure
```

The image keeps the database and uploads in `/data`, so mount a volume if you want them to survive restarts.

### Configuration

All settings are environment variables with the `GEOMEASURE_` prefix. A `.env` file in the working directory is also read.

| Variable | Default | Meaning |
|---|---|---|
| `GEOMEASURE_DATA_DIR` | `data` | Where uploads are stored (`<data_dir>/uploads/<file_id>/`) |
| `GEOMEASURE_DATABASE_URL` | `sqlite:///data/geomeasure.db` | Any SQLAlchemy URL. SQLite gets WAL mode and foreign keys turned on |
| `GEOMEASURE_MAX_UPLOAD_MB` | `50` | Maximum upload size |
| `GEOMEASURE_MAX_EXTRACTED_MB` | `500` | Maximum total size a zip may expand to |
| `GEOMEASURE_MAX_COMPRESSION_RATIO` | `100` | Maximum uncompressed/compressed ratio for a zip |
| `GEOMEASURE_MAX_ZIP_ENTRIES` | `1000` | Maximum number of entries in a zip |

### Sample data

`samples/` has two small files modelled on what a drone survey team near Hyderabad would hand over:

- `hyderabad_survey.kml`: three folders. "Fields" has two valid parcels and one self-intersecting "bowtie" polygon. "Infrastructure" has a road (line) and a survey pole (point). "Reference" has one 100 m check plot near Eluru at 81.0°E, right on the central meridian of UTM zone 44N, so the file exercises both CRS paths (see [CRS handling](#crs-handling)).
- `kothur_parcels_utm.zip`: two shapefiles in UTM 44N (EPSG:32644) inside a `kothur/` folder: two parcels (500 m and 300 m squares) and a 2 km canal.

`uv run python scripts/make_samples.py` regenerates them from the test factories.

## Try it in 30 seconds

Start the server, then from the repo root:

```bash
curl -F file=@samples/hyderabad_survey.kml http://127.0.0.1:8000/api/files/
```

Response (`201 Created`):

```json
{
  "id": "1WeRIS3mquQ",
  "filename": "hyderabad_survey.kml",
  "format": "KML",
  "status": "COMPLETED",
  "error": null,
  "crs": "EPSG:4326",
  "crs_assumed": false,
  "feature_count": 6,
  "layer_count": 3,
  "size_bytes": 2225,
  "created_at": "2026-10-06T15:24:39.049914Z",
  "completed_at": "2026-10-06T15:24:39.737907Z",
  "processing_ms": 698,
  "layers": [
    {"name": "Fields", "feature_count": 3, "crs": "EPSG:4326", "crs_assumed": false},
    {"name": "Infrastructure", "feature_count": 2, "crs": "EPSG:4326", "crs_assumed": false},
    {"name": "Reference", "feature_count": 1, "crs": "EPSG:4326", "crs_assumed": false}
  ],
  "geometry_types": {"LineString": 1, "Point": 1, "Polygon": 4}
}
```

Fetch the measurements with the returned id:

```bash
curl "http://127.0.0.1:8000/api/files/1WeRIS3mquQ/measurements/?include_geometry=false"
```

Response, cut down to four of the six features. The numbers are exactly what the server returned.

```json
{
  "file_id": "1WeRIS3mquQ",
  "crs": "EPSG:4326",
  "summary": {
    "total_area_m2": 168337.2377824038,
    "total_length_m": 1183.6413123823827,
    "measured": 4,
    "skipped": {"INVALID_GEOMETRY": 1, "NOT_APPLICABLE": 1}
  },
  "count": 6,
  "limit": 100,
  "offset": 0,
  "results": [
    {
      "index": 0,
      "layer": "Fields",
      "geometry_type": "Polygon",
      "source_crs": "EPSG:4326",
      "properties": {"Name": "Field A", "crop": "paddy", "owner_id": "101"},
      "geometry": null,
      "measurement": {
        "status": "MEASURED",
        "reason": null,
        "area_m2": 39999.99999855672,
        "perimeter_m": 799.9999999855671,
        "length_m": null,
        "projected_crs": "WGS 84 / LAEA centred on 17.2400N 78.4300E",
        "geodesic_area_m2": 40000.00000334298,
        "geodesic_length_m": null,
        "deviation_pct": 0.0
      }
    },
    {
      "index": 2,
      "layer": "Fields",
      "geometry_type": "Polygon",
      "source_crs": "EPSG:4326",
      "properties": {"Name": "Digitising error", "crop": "unknown", "owner_id": "0"},
      "geometry": null,
      "measurement": {
        "status": "INVALID_GEOMETRY",
        "reason": "Self-intersection[78.443 17.241].",
        "area_m2": null,
        "perimeter_m": null,
        "length_m": null,
        "projected_crs": null,
        "geodesic_area_m2": null,
        "geodesic_length_m": null,
        "deviation_pct": null
      }
    },
    {
      "index": 4,
      "layer": "Infrastructure",
      "geometry_type": "Point",
      "source_crs": "EPSG:4326",
      "properties": {"Name": "Survey pole", "surface": "n/a"},
      "geometry": null,
      "measurement": {
        "status": "NOT_APPLICABLE",
        "reason": "Points have no area or length to measure.",
        "area_m2": null,
        "perimeter_m": null,
        "length_m": null,
        "projected_crs": null,
        "geodesic_area_m2": null,
        "geodesic_length_m": null,
        "deviation_pct": null
      }
    },
    {
      "index": 5,
      "layer": "Reference",
      "geometry_type": "Polygon",
      "source_crs": "EPSG:4326",
      "properties": {"Name": "Eluru check plot", "crop": "none", "owner_id": "0"},
      "geometry": null,
      "measurement": {
        "status": "MEASURED",
        "reason": null,
        "area_m2": 9992.00160029511,
        "perimeter_m": 399.8400000059046,
        "length_m": null,
        "projected_crs": "EPSG:32644",
        "geodesic_area_m2": 10000.000000440465,
        "geodesic_length_m": null,
        "deviation_pct": -0.08
      }
    }
  ]
}
```

What to look at:

- Field A is a 200 m square, so 40,000 m² is the expected answer. Hyderabad sits near the western edge of UTM zone 44N, where UTM's area error is just over the 0.1% I allow, so it was measured in an equal-area projection centred on the field. Field B and the road (index 1 and 3, not shown) went the same way; the road is 1183.6413 m projected and 1183.6413 m geodesic.
- The Eluru plot is a 100 m square on 81°E, the zone's central meridian. UTM is within tolerance there, so it stays in `EPSG:32644`. Its `deviation_pct` of -0.08 is UTM's 0.9996 scale factor showing up in the output: 0.9996² ≈ 0.9992, and 10,000 m² × 0.9992 is the 9992 m² you see. Details in [CRS handling](#crs-handling).

Now the projected shapefile:

```bash
curl -F file=@samples/kothur_parcels_utm.zip http://127.0.0.1:8000/api/files/
```

Response, with `error`, `size_bytes` and the timestamps left out:

```json
{
  "id": "apWAjS6U9E8",
  "filename": "kothur_parcels_utm.zip",
  "format": "SHAPEFILE",
  "status": "COMPLETED",
  "crs": "EPSG:32644",
  "crs_assumed": false,
  "feature_count": 3,
  "layer_count": 2,
  "layers": [
    {"name": "kothur/canal", "feature_count": 1, "crs": "EPSG:32644", "crs_assumed": false},
    {"name": "kothur/parcels", "feature_count": 2, "crs": "EPSG:32644", "crs_assumed": false}
  ],
  "geometry_types": {"LineString": 1, "Polygon": 2}
}
```

Its measurements, rounded to 2 decimals in this table only (raw values are like `249999.99993577428`):

| Feature | Projected | Geodesic | `projected_crs` |
|---|---|---|---|
| Main canal | 2000.00 m | 2000.00 m | LAEA centred on 17.1200N 78.2894E |
| P-001 | 250000.00 m², perimeter 2000.00 m | 250000.00 m² | LAEA centred on 17.1300N 78.2900E |
| P-002 | 90000.00 m², perimeter 1200.00 m | 90000.00 m² | LAEA centred on 17.1300N 78.3000E |

`deviation_pct` is `0.0` for all three. The source file is in UTM 44N, but Kothur is at 78.3°E, also near the zone's western edge, so these get measured in LAEA too, whatever the source CRS says.

To open the results in QGIS:

```bash
curl -o hyderabad.geojson http://127.0.0.1:8000/api/files/1WeRIS3mquQ/features.geojson
```

## API

| Method | Path | What it does | Status codes |
|---|---|---|---|
| `POST` | `/api/files/` | Upload a `.zip` (Shapefile) or `.kml` as multipart field `file`. Processes it and returns the file record | 201, 400, 422 |
| `GET` | `/api/files/` | List files, newest first. `limit` (1 to 100, default 20), `offset` | 200 |
| `GET` | `/api/files/{id}/` | Status and metadata for one file: layers, CRS, counts per geometry type | 200, 404 |
| `GET` | `/api/files/{id}/measurements/` | Per-feature measurements plus whole-file totals | 200, 404, 409 |
| `GET` | `/api/files/{id}/features.geojson` | WGS84 FeatureCollection with measurements flattened into properties | 200, 404, 409 |
| `DELETE` | `/api/files/{id}/` | Delete the record, its features and the stored upload | 204, 404 |
| `GET` | `/health` | Liveness plus a `SELECT 1` against the database | 200 |

Query parameters on `/measurements/`:

- `limit` (1 to 1000, default 100) and `offset`
- `status`, e.g. `MEASURED` or `INVALID_GEOMETRY`
- `geometry_type`, e.g. `Polygon`
- `include_geometry` (default `true`): include each feature's WGS84 GeoJSON geometry

`summary` always covers the whole file. `count` is the number of features matching the filters.

In the GeoJSON export, source properties whose names clash with a measurement field (say a shapefile column called `status`) are kept under a `source_` prefix.

### HTTP status codes

| Code | When | Body |
|---|---|---|
| 201 | Processed, `status` is `COMPLETED` | File record |
| 400 | The request is wrong: unsupported extension, empty, too large, not a zip, unsafe zip path, zip bomb, `.shp` without `.shx`/`.dbf`, KML that isn't XML. Nothing is stored | `{"detail": "..."}` |
| 422 | The upload passed validation but GDAL couldn't read it. The record is kept with `status: FAILED` and an `error`, and stays fetchable | File record |
| 404 | Unknown id | `{"detail": "..."}` |
| 409 | Measurements or GeoJSON requested for a file that isn't `COMPLETED` | `{"detail": "..."}` |

Examples from the running server:

```
$ curl -F "file=@notes.txt;filename=parcels.shp" http://127.0.0.1:8000/api/files/
400 {"detail":"Unsupported file type (.shp). Upload a .zip (containing a Shapefile) or .kml."}

$ curl -F "file=@notes.txt;filename=bad.zip" http://127.0.0.1:8000/api/files/
400 {"detail":"The upload has a .zip extension but is not a valid zip archive."}

$ curl -F file=@broken.zip http://127.0.0.1:8000/api/files/   # survey/bad.shp, .shx, .dbf full of zero bytes
422 {"id":"DJ2jqiU_ZFg","filename":"broken.zip","format":"SHAPEFILE","status":"FAILED","error":"Could not read survey/bad.shp: 'survey\\bad.shp' not recognized as being in a supported file format.; It might help to specify the correct driver explicitly by prefixing the file path with '<DRIVER>:', e.g. 'CSV:path'.", ...}

$ curl http://127.0.0.1:8000/api/files/DJ2jqiU_ZFg/measurements/
409 {"detail":"File 'DJ2jqiU_ZFg' is FAILED, measurements need it to be COMPLETED. Error: Could not read survey/bad.shp: ..."}
```

### Per-feature measurement status

A file is `COMPLETED` even if some of its features couldn't be measured. Each feature carries its own status and a readable `reason`.

| Status | Meaning |
|---|---|
| `MEASURED` | Area and perimeter (polygons) or length (lines) were computed |
| `NOT_APPLICABLE` | Point or MultiPoint, nothing to measure |
| `UNSUPPORTED_GEOMETRY` | A GeometryCollection mixing points, lines and polygons, or a feature spanning more than 180° of longitude |
| `INVALID_GEOMETRY` | Self-intersecting or otherwise invalid polygon (the reason says where), coordinates outside the valid lon/lat range or outside what the source CRS can represent, or a failed transform |
| `EMPTY_GEOMETRY` | No geometry, or an empty one |
| `UNKNOWN_CRS` | The layer has no CRS and the coordinates don't look like lon/lat, so they can't be turned into metres |

GeometryCollections are handled by what they contain. All polygons are measured as one MultiPolygon, all lines as one MultiLineString, all points are `NOT_APPLICABLE`, and anything mixed is `UNSUPPORTED_GEOMETRY`. Z values are dropped and all measurements are 2D.

## Architecture

### Layout

```
src/geomeasure/
  main.py          app factory, lifespan (tables, data dir), InvalidUpload -> 400
  config.py        Settings (pydantic-settings, GEOMEASURE_ prefix)
  pipeline.py      upload flow: validate, store, read, measure, persist
  domain.py        plain dataclasses and enums shared by every stage
  errors.py        InvalidUpload (400) and ProcessingError (FAILED record)
  api/             routers, response schemas, dependencies
  ingest/
    upload.py      extension check, size limits, safe zip extraction, sidecar checks
    reader.py      GDAL (pyogrio) -> SourceLayer/SourceFeature, attribute cleanup
  geo/
    crs.py         CRS labels, reprojection, UTM/UPS zone and LAEA construction
    measure.py     classification, CRS choice, measurement, geodesic cross-check
  db/              SQLAlchemy models (files, features) and session setup
tests/             ingest, reader, measurement and API tests
samples/           demo KML and zipped shapefile
scripts/           make_samples.py
```

The ingest and geo modules don't import anything from the API or the database. `measure_feature(geometry, crs)` is a pure function, which is how most of the measurement tests run without a server or a file.

### File-processing flow

1. `POST /api/files/` reads at most `max_upload_mb + 1` bytes from the upload.
2. Detect the format from the extension (`.zip` or `.kml`; `.kmz` gets its own message).
3. Generate the file id.
4. `prepare_upload` validates and unpacks into `data/uploads/<id>/`: size check, original written to disk, then for KML an XML parse that checks the root is `<kml>`, and for a zip a safe extraction that finds every `.shp`, nested folders included, and checks its sidecars.
5. Only now is a `files` row inserted with status `PROCESSING` and committed.
6. `ingest/reader.py` opens each dataset with pyogrio. A zip can hold several shapefiles and a KML several folders, and each becomes a layer. Attributes are converted to plain JSON values (numpy scalars, dates, NaN, bytes).
7. Each feature is measured (next section), its geometry is reprojected to WGS84 GeoJSON for output, and all feature rows go into the `features` table in one bulk insert.
8. The file row becomes `COMPLETED` with the CRS, layer list and counts, or `FAILED` with an error.

If steps 2 to 4 raise `InvalidUpload`, the upload folder is removed and the client gets a 400. No database row ever existed, so a rejected upload never shows up in the file list, even briefly. If GDAL can't open the data (`ProcessingError`), the row is kept as `FAILED` with GDAL's message, scrubbed so the only paths in it are relative to the upload (`survey/bad.shp`). Anything unexpected is logged with a traceback and also ends as `FAILED`, with a generic message to the client.

### Measurement flow

For each feature (`geo/measure.py`):

1. Reject empty geometries and layers without a CRS. Drop Z.
2. Classify as point, linear or areal. Merge single-type GeometryCollections, reject mixed ones.
3. Polygons must be valid, otherwise `INVALID_GEOMETRY` with shapely's explanation.
4. Densify projected sources at 1000-unit segments, then transform to WGS84 lon/lat.
5. Check the lon/lat range. If the feature spans more than 180° of longitude, assume it crosses the antimeridian the short way and shift negative longitudes by +360. If it still spans more than 180°, reject it. Geographic sources are densified here, at 0.01° segments, after the unwrap (otherwise an edge from 179.99 to -179.99 would be densified all the way round the globe).
6. Pick the measurement CRS (below) and project.
7. Take area, perimeter and length from shapely in the projected CRS. These are the official values.
8. Compute the same quantity on the WGS84 ellipsoid with `pyproj.Geod` and store it with `deviation_pct`.

### CRS handling

I pick the measurement CRS per feature, so a file covering one village and a file covering a whole state get the same treatment: each parcel is measured in a projection that suits where it is.

Source CRS:

- Shapefiles use their `.prj`. If there's no `.prj` and every coordinate fits lon/lat ranges, the layer is assumed to be WGS84 and `crs_assumed: true` is set on the layer and the file. Otherwise the CRS stays unknown, features come back as `UNKNOWN_CRS`, and no geometry is stored, so projected coordinates never get passed off as lon/lat.
- KML is WGS84 by spec.
- Projected coordinates are round-tripped back to the source CRS after conversion to WGS84, and features whose coordinates the CRS can't represent are rejected. PROJ doesn't always fail loudly here: UTM returns `inf`, and Web Mercator silently clamps a huge northing to latitude 90. Those features get `INVALID_GEOMETRY` with `geometry: null`, and the rest of the file still completes.
- If layers in one zip use different CRSs, the file-level `crs` is `"MIXED"` and each layer keeps its own.

Measurement CRS:

1. Start with the UTM zone of the feature's centroid (UPS north of 84°N or south of 80°S), including the Norway and Svalbard zone exceptions. Survey teams already work in UTM in QGIS, so `EPSG:32644` in the output means something to them.
2. Evaluate UTM's areal scale factor at every vertex with `pyproj.Proj.get_factors`. If any vertex is more than 0.1% off, switch to Lambert Azimuthal Equal Area on the WGS84 ellipsoid, centred on the feature's centroid. LAEA is equal-area by construction. This kicks in near zone edges, for features wider than a zone, and near the poles (UPS is about 1.2% off in area there).

The KML sample shows both paths. Hyderabad is at about 78.4°E, in zone 44N, whose central meridian is 81°E. Roughly 2.6° west of the centre, UTM's areal scale is 1.00105, just over the limit, so the Hyderabad features report LAEA. The Eluru check plot sits on 81°E, where UTM's areal scale is 0.9992 (0.9996²), inside the tolerance, so it stays in `EPSG:32644`. Its area comes out at 9992 m² against 10,000 m² geodesic (`deviation_pct` -0.08), which is the zone-centre scale factor showing through. The tests cover UTM picks at the equator, 60°N and Sydney, and LAEA fallbacks for wide, polar and antimeridian-crossing features.

Densification: an edge is a straight line in the CRS the file was drawn in. If you reproject only its two endpoints, every projection bends it differently, and for a 20° polygon the UTM, LAEA and geodesic answers disagree by more than a percent. Densifying in the source CRS keeps the shape the author drew.

Geodesic cross-check: every measured feature carries `geodesic_area_m2` or `geodesic_length_m` computed on the ellipsoid, plus `deviation_pct`. For survey-sized features it rounds to 0.0. In the tests a 20° by 20° polygon stays under 0.01%, and a 20° line under 0.5% (LAEA lengths drift slowly away from the centre). Anyone reading the output can check each number without trusting my projection choice.

## Design decisions

FastAPI over Django. This is one resource and a handful of endpoints. FastAPI gives typed request and response schemas and OpenAPI docs from the same Pydantic models. Django's admin, ORM and project structure would be weight I'd carry without using. SQLAlchemy 2.0 handles persistence.

Synchronous processing in the request. The upload endpoint is a plain `def`, so FastAPI runs it on its threadpool and the event loop stays free. Survey-sized files (tens to a few thousand features) finish quickly. The two samples take about 0.7 s each, and the client gets the full result in one call with no polling. Status is still persisted (`PROCESSING`, then `COMPLETED` or `FAILED`), so the data model already fits an async design. For large files I'd move `process_upload` into a worker behind a queue, return `202 Accepted` with the file id, and have the client poll `GET /api/files/{id}/`. The pipeline itself would barely change.

400 vs 422. A 400 means the request itself was wrong, and nothing is stored because there's nothing useful to keep. A 422 means the upload looked plausible but couldn't be read. The `FAILED` record stays so the user can see what they uploaded and why it failed.

Per-feature CRS over one CRS per file. A single CRS per file breaks on files that span zones and loses accuracy on files near a zone edge. Choosing per feature costs a CRS lookup and a scale-factor check per feature, and the CRS, `Proj` and transformer objects are cached.

Alternatives I considered for the measurement CRS:

- Web Mercator (EPSG:3857). Web maps use it, and it's wrong for area: areas are inflated by 1/cos²(latitude), about 10% at Hyderabad's latitude and 4x at 60°.
- Always geodesic. `pyproj.Geod` on the ellipsoid is exact for this purpose. The brief asks for measurement in a suitable projection, and survey teams think in projected coordinates, so the projection gives the official value and geodesic is the check.
- A fixed national CRS. It ties the service to one country and still distorts toward the edges of a large one.

Invalid polygons get reported and left alone. `make_valid` on a bowtie gives two triangles, and their combined area is a guess at what the surveyor meant. For a service whose job is accurate numbers, I'd rather return `INVALID_GEOMETRY` with the location of the self-intersection so the source gets fixed. The per-feature status means one bad polygon doesn't fail the whole file.

Upload safety. The zip is never extracted blindly:

- Zip-slip: absolute paths, drive letters and `..` components are rejected, and every resolved target must stay inside the extract folder.
- Zip bombs: limits on entry count, declared total size and compression ratio. Actual bytes written are also counted while streaming, since zip headers can lie.
- `__MACOSX/` folders and dotfiles are skipped.
- Every `.shp` must have its `.shx` and `.dbf` (case-insensitive), and the error names exactly what's missing.
- Client filenames are reduced to a safe basename before anything touches disk.

Storage. SQLite with JSON columns for properties, GeoJSON geometry and the layer list, and an index on `(file_id, status)` for the filtered queries. Zero setup for a reviewer, and the database URL is configurable.

Extras I added beyond the brief: the GeoJSON export for QGIS, list and delete endpoints, filters and pagination on measurements, whole-file summary totals, per-layer metadata, the geodesic cross-check, Docker, CI on Linux and Windows, and the sample data.

## Learnings

GDAL's LIBKML driver adds about a dozen columns to every KML layer whether the file uses them or not: `tessellate`, `extrude`, `visibility`, `altitudeMode`, `drawOrder`, `description` and others. Several are filled with sentinel defaults instead of nulls (-1 for `tessellate` and `visibility`, 0 for `extrude`), so a plain "drop empty columns" pass doesn't remove them. I keep a table of each column's LIBKML default and drop a column only when every value in it is missing or equal to that default. A KML that really sets a timestamp keeps it. LIBKML also turns each `<Folder>` into its own layer and lists empty folders as layers with no features, which is a big part of why layers show up in the response at all.

KML `<ExtendedData><Data>` values are untyped strings. In the sample, `owner_id` comes back as `"101"`. Typed values need a `<Schema>` with `<SimpleField>`, and then GDAL returns real integers. I chose not to guess types from strings.

The shapefile writer stores exterior rings clockwise, and `pyproj.Geod.geometry_area_perimeter` returns a signed area that is negative for clockwise rings. So polygons are oriented before the geodesic check. The same thing showed up in testing: comparing a geometry with its shapefile round-trip needed `normalize()` on both sides before an exact-equality check would pass.

UTM isn't exact at the centre of a zone either. Its 0.9996 scale factor makes area at the central meridian about 0.08% low, which the Eluru plot shows, and at Hyderabad's latitude the error crosses +0.1% around 2.5° out. Hence the per-vertex scale-factor check.

PROJ doesn't always raise on coordinates a CRS can't represent. UTM hands back `inf` and Web Mercator clamps to latitude 90, both without an error. A round-trip back to the source CRS was the simplest reliable check I found.

Densification ended up mattering as much as the choice of projection. Without it, long edges of a large polygon give different areas in UTM, LAEA and geodesic, because each one bends the edges differently. The fix was to decide which CRS defines "straight" (the source) and densify there.

## Future scope

- Async processing for large files: worker queue, `202 Accepted`, polling on the existing status field.
- PostGIS for storage, so geometries are queryable (features in a bounding box, overlapping parcels) instead of being JSON.
- Alembic migrations. Tables are currently created with `create_all` on startup.
- More input formats: KMZ (currently rejected with a message telling the user to unzip it), GeoJSON and GeoPackage. GDAL already reads all of them.
- Authentication and per-user files.
- Object storage (S3 or Azure Blob) for uploads instead of local disk.
- An optional repair endpoint that runs `make_valid` and shows the repaired geometry and area, so fixing is an explicit choice.
- Streaming reads for very large files instead of loading each layer into a dataframe.
