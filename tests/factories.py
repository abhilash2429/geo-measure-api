"""Builders for test uploads: shapefile zips, KML documents and shapes of known size."""

import datetime as dt
import io
import math
import tempfile
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import geopandas as gpd
from pyproj import Transformer
from shapely.geometry import LineString, Point, Polygon


def shapefile_zip(
    gdf: gpd.GeoDataFrame,
    *,
    name: str = "parcels",
    include_prj: bool = True,
    folder: str | None = None,
    extra_files: dict[str, bytes] | None = None,
) -> bytes:
    """Write `gdf` as `<name>.shp` (+ .shx/.dbf/.prj/.cpg) and return the zip bytes.

    `folder` puts the shapefile under that path inside the zip (e.g. "survey/2024").
    `include_prj=False` leaves out the .prj, as if the CRS were never declared.
    `extra_files` maps archive paths to raw bytes and is added verbatim, which is how
    tests add junk like "__MACOSX/._parcels.shp" or a second shapefile's parts.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        arcpath = f"{folder.strip('/')}/{name}" if folder else name
        _add_shapefile(archive, gdf, arcpath, include_prj=include_prj)
        for arcname, data in (extra_files or {}).items():
            archive.writestr(arcname, data)
    return buffer.getvalue()


def multi_shapefile_zip(layers: dict[str, gpd.GeoDataFrame]) -> bytes:
    """Zip several shapefiles. Keys are archive paths without extension, e.g. "roads/main"."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for arcpath, gdf in layers.items():
            _add_shapefile(archive, gdf, arcpath)
    return buffer.getvalue()


def _add_shapefile(
    archive: zipfile.ZipFile, gdf: gpd.GeoDataFrame, arcpath: str, *, include_prj: bool = True
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        gdf.to_file(Path(tmp) / "layer.shp", engine="pyogrio")
        for part in sorted(Path(tmp).iterdir()):
            if part.suffix == ".prj" and not include_prj:
                continue
            archive.write(part, arcpath + part.suffix)


def zip_bytes(files: dict[str, bytes]) -> bytes:
    """Zip arbitrary `{archive path: content}` pairs, for building malformed archives."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for arcname, data in files.items():
            archive.writestr(arcname, data)
    return buffer.getvalue()


def kml_document(
    placemarks: list[tuple[str, str]] | None = None,
    *,
    folders: dict[str, list[tuple[str, str]]] | None = None,
) -> bytes:
    """Build a KML 2.2 document.

    Each placemark is `(name, body)` where `body` is inserted into the <Placemark>
    unchanged: normally a geometry such as "<Polygon>...</Polygon>", optionally preceded
    by <description> or <ExtendedData>. Top-level placemarks come first, then one
    <Folder> per entry in `folders`.
    """
    parts = [_placemark(name, body) for name, body in placemarks or []]
    for folder_name, folder_placemarks in (folders or {}).items():
        inner = "".join(_placemark(name, body) for name, body in folder_placemarks)
        parts.append(f"<Folder><name>{escape(folder_name)}</name>{inner}</Folder>")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        f"{''.join(parts)}"
        "</Document></kml>"
    ).encode()


def _placemark(name: str, body: str) -> str:
    return f"<Placemark><name>{escape(name)}</name>{body}</Placemark>"


def kml_geometry(geometry: Point | LineString | Polygon) -> str:
    """KML fragment for a simple shapely geometry in lon/lat."""

    def coordinates(coords) -> str:
        return " ".join(f"{x},{y}" for x, y, *_ in coords)

    if isinstance(geometry, Point):
        return f"<Point><coordinates>{geometry.x},{geometry.y}</coordinates></Point>"
    if isinstance(geometry, LineString):
        return f"<LineString><coordinates>{coordinates(geometry.coords)}</coordinates></LineString>"
    if isinstance(geometry, Polygon):
        ring = coordinates(geometry.exterior.coords)
        return (
            "<Polygon><outerBoundaryIs><LinearRing>"
            f"<coordinates>{ring}</coordinates>"
            "</LinearRing></outerBoundaryIs></Polygon>"
        )
    raise TypeError(f"No KML fragment for {geometry.geom_type}")


def square_wgs84(lon: float, lat: float, side_m: float) -> Polygon:
    """A square of `side_m` metres centred on (lon, lat), returned in lon/lat.

    The square is exact in a local azimuthal equidistant projection, so its true area
    is `side_m ** 2` to well within a part per million for field-sized squares.
    """
    to_wgs84 = _local_to_wgs84(lon, lat)
    half = side_m / 2
    corners = [(-half, -half), (half, -half), (half, half), (-half, half), (-half, -half)]
    return Polygon([to_wgs84.transform(x, y) for x, y in corners])


def line_wgs84(lon: float, lat: float, length_m: float, bearing_deg: float = 90) -> LineString:
    """A straight line of `length_m` metres starting at (lon, lat), heading `bearing_deg`.

    Distances from the projection centre are exact in azimuthal equidistant, so the
    geodesic length of the result is `length_m`.
    """
    to_wgs84 = _local_to_wgs84(lon, lat)
    bearing = math.radians(bearing_deg)
    end = (length_m * math.sin(bearing), length_m * math.cos(bearing))
    return LineString([to_wgs84.transform(0, 0), to_wgs84.transform(*end)])


def _local_to_wgs84(lon: float, lat: float) -> Transformer:
    local = f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m"
    return Transformer.from_crs(local, "EPSG:4326", always_xy=True)


def sample_parcels_gdf() -> gpd.GeoDataFrame:
    """Three square parcels in EPSG:4326 near Bengaluru with str, int, float and date columns.

    Sides are 100 m, 50 m and 20 m. The last parcel has a missing yield and survey date.
    A shapefile holds one geometry type, so lines and points live in `sample_survey_zip`.
    """
    lon, lat = 77.5946, 12.9716
    return gpd.GeoDataFrame(
        {
            "name": ["Field A", "Field B", "Field C"],
            "plot_id": [101, 102, 103],
            "yield_t": [4.25, 3.5, float("nan")],
            "surveyed": [dt.date(2024, 3, 1), dt.date(2024, 3, 2), None],
        },
        geometry=[
            square_wgs84(lon, lat, 100),
            square_wgs84(lon + 0.01, lat, 50),
            square_wgs84(lon + 0.02, lat, 20),
        ],
        crs="EPSG:4326",
    )


def sample_survey_zip() -> bytes:
    """A zip with parcels (polygons), infra/canals (one 250 m line) and wells (one point).

    Read in archive order the layers are "infra/canals", "parcels", "wells".
    """
    lon, lat = 77.5946, 12.9716
    canals = gpd.GeoDataFrame(
        {"name": ["Canal"]}, geometry=[line_wgs84(lon, lat + 0.01, 250)], crs="EPSG:4326"
    )
    wells = gpd.GeoDataFrame({"name": ["Well"]}, geometry=[Point(lon + 0.03, lat)], crs="EPSG:4326")
    return multi_shapefile_zip(
        {"parcels": sample_parcels_gdf(), "infra/canals": canals, "wells": wells}
    )
