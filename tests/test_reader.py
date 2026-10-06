import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import pytest
from factories import (
    kml_document,
    kml_geometry,
    line_wgs84,
    sample_parcels_gdf,
    sample_survey_zip,
    shapefile_zip,
    square_wgs84,
    zip_bytes,
)
from pyproj import CRS
from shapely.geometry import LineString, Point, Polygon

from geomeasure.config import Settings
from geomeasure.domain import SourceLayer
from geomeasure.errors import ProcessingError
from geomeasure.ingest.reader import _json_value, read_layers
from geomeasure.ingest.upload import prepare_upload

LON, LAT = 77.5946, 12.9716


def _read(content: bytes, filename: str, tmp_path: Path) -> list[SourceLayer]:
    paths = prepare_upload(filename, content, tmp_path / "upload", Settings(data_dir=tmp_path))
    return read_layers(paths)


def _read_shapefile(gdf: gpd.GeoDataFrame, tmp_path: Path, **zip_options) -> SourceLayer:
    [layer] = _read(shapefile_zip(gdf, **zip_options), "parcels.zip", tmp_path)
    return layer


def test_reads_shapefile_features(tmp_path: Path) -> None:
    layer = _read_shapefile(sample_parcels_gdf(), tmp_path)

    assert layer.name == "parcels"
    assert layer.crs == CRS.from_epsg(4326)
    assert layer.crs_assumed is False
    assert [f.index for f in layer.features] == [0, 1, 2]
    assert all(isinstance(f.geometry, Polygon) for f in layer.features)
    # Shapefiles store exterior rings clockwise, so compare normalized geometries.
    expected = sample_parcels_gdf().geometry[0].normalize()
    assert layer.features[0].geometry.normalize().equals_exact(expected, 1e-9)


def test_normalizes_attribute_types(tmp_path: Path) -> None:
    layer = _read_shapefile(sample_parcels_gdf(), tmp_path)

    first, _, last = (f.properties for f in layer.features)
    assert first == {"name": "Field A", "plot_id": 101, "yield_t": 4.25, "surveyed": "2024-03-01"}
    assert type(first["plot_id"]) is int
    assert type(first["yield_t"]) is float
    assert last["yield_t"] is None
    assert last["surveyed"] is None
    json.dumps([f.properties for f in layer.features], allow_nan=False)


def test_nested_shapefile_layer_name_keeps_folder(tmp_path: Path) -> None:
    layer = _read_shapefile(sample_parcels_gdf(), tmp_path, folder="survey/2024")
    assert layer.name == "survey/2024/parcels"


def test_index_runs_across_layers(tmp_path: Path) -> None:
    layers = _read(sample_survey_zip(), "survey.zip", tmp_path)

    assert [layer.name for layer in layers] == ["infra/canals", "parcels", "wells"]
    indexes = [f.index for layer in layers for f in layer.features]
    assert indexes == [0, 1, 2, 3, 4]
    assert [f.geometry.geom_type for layer in layers for f in layer.features] == [
        "LineString",
        "Polygon",
        "Polygon",
        "Polygon",
        "Point",
    ]


def test_keeps_declared_projected_crs(tmp_path: Path) -> None:
    gdf = sample_parcels_gdf().to_crs(32644)

    layer = _read_shapefile(gdf, tmp_path)

    assert layer.crs.to_epsg() == 32644
    assert layer.crs_assumed is False
    assert layer.features[0].geometry.bounds[0] > 100_000


def test_missing_prj_with_lon_lat_coordinates_assumes_wgs84(tmp_path: Path) -> None:
    layer = _read_shapefile(sample_parcels_gdf(), tmp_path, include_prj=False)

    assert layer.crs == CRS.from_epsg(4326)
    assert layer.crs_assumed is True


def test_missing_prj_with_projected_coordinates_leaves_crs_unknown(tmp_path: Path) -> None:
    gdf = sample_parcels_gdf().to_crs(32644)

    layer = _read_shapefile(gdf, tmp_path, include_prj=False)

    assert layer.crs is None
    assert layer.crs_assumed is False
    assert len(layer.features) == 3


def test_null_geometry_passes_through(tmp_path: Path) -> None:
    gdf = gpd.GeoDataFrame(
        {"name": ["kept", "no shape"]},
        geometry=[square_wgs84(LON, LAT, 10), None],
        crs="EPSG:4326",
    )

    layer = _read_shapefile(gdf, tmp_path)

    assert [f.index for f in layer.features] == [0, 1]
    assert layer.features[1].geometry is None
    assert layer.features[1].properties == {"name": "no shape"}


def test_unreadable_shapefile_raises_processing_error(tmp_path: Path) -> None:
    content = zip_bytes({"bad.shp": b"garbage" * 20, "bad.shx": b"x", "bad.dbf": b"x"})
    with pytest.raises(ProcessingError, match=r"Could not read bad\.shp"):
        _read(content, "bad.zip", tmp_path)


def test_kml_point_line_polygon(tmp_path: Path) -> None:
    content = kml_document(
        [
            ("Well", kml_geometry(Point(LON, LAT))),
            ("Canal", kml_geometry(line_wgs84(LON, LAT, 250))),
            ("Field", kml_geometry(square_wgs84(LON, LAT, 100))),
        ]
    )

    [layer] = _read(content, "farm.kml", tmp_path)

    assert layer.crs == CRS.from_epsg(4326)
    assert layer.crs_assumed is False
    assert [type(f.geometry) for f in layer.features] == [Point, LineString, Polygon]
    assert [f.properties["Name"] for f in layer.features] == ["Well", "Canal", "Field"]


SCHEMA_FIELDS = (
    '<Schema name="crops" id="crops">'
    '<SimpleField name="crop" type="string"/><SimpleField name="yield" type="int"/>'
    "</Schema>"
)


def _multipolygon_kml() -> str:
    a = kml_geometry(square_wgs84(LON, LAT, 100))
    b = kml_geometry(square_wgs84(LON + 0.01, LAT, 100))
    return f"<MultiGeometry>{a}{b}</MultiGeometry>"


def _schema_data(crop: str, yield_t: int) -> str:
    return (
        '<ExtendedData><SchemaData schemaUrl="#crops">'
        f'<SimpleData name="crop">{crop}</SimpleData>'
        f'<SimpleData name="yield">{yield_t}</SimpleData>'
        "</SchemaData></ExtendedData>"
    )


def test_kml_folders_multigeometry_and_extended_data(tmp_path: Path) -> None:
    content = kml_document(
        folders={
            "North block": [
                (
                    "Paddy",
                    "<description>two plots</description>"
                    + _schema_data("rice", 5)
                    + _multipolygon_kml(),
                ),
                (
                    "Track",
                    '<ExtendedData><Data name="owner"><value>Ravi</value></Data></ExtendedData>'
                    + kml_geometry(line_wgs84(LON, LAT, 100)),
                ),
            ],
            "Empty": [],
            "South block": [("Well", kml_geometry(Point(LON, LAT - 0.01)))],
        }
    ).replace(b"<Document>", b"<Document>" + SCHEMA_FIELDS.encode())
    path = tmp_path / "farm.kml"
    path.write_bytes(content)
    assert pyogrio.read_info(path, layer="North block")["driver"] == "LIBKML"

    layers = _read(content, "farm.kml", tmp_path)

    assert [layer.name for layer in layers] == ["North block", "South block"]
    paddy, track = layers[0].features
    assert paddy.geometry.geom_type == "MultiPolygon"
    assert len(paddy.geometry.geoms) == 2
    assert paddy.properties == {
        "Name": "Paddy",
        "description": "two plots",
        "crop": "rice",
        "yield": 5,
        "owner": None,
    }
    assert track.properties["owner"] == "Ravi"
    assert track.properties["yield"] is None
    assert [f.index for layer in layers for f in layer.features] == [0, 1, 2]
    json.dumps([f.properties for layer in layers for f in layer.features], allow_nan=False)


def test_kml_keeps_libkml_columns_that_carry_data(tmp_path: Path) -> None:
    body = "<TimeStamp><when>2024-03-01T10:30:00Z</when></TimeStamp>" + kml_geometry(
        Point(LON, LAT)
    )

    [layer] = _read(kml_document([("Well", body)]), "farm.kml", tmp_path)

    properties = layer.features[0].properties
    assert properties["timestamp"].startswith("2024-03-01T10:30:00")
    assert "tessellate" not in properties
    assert "visibility" not in properties


def test_kml_without_placemarks_yields_no_layers(tmp_path: Path) -> None:
    assert _read(kml_document(folders={"Empty": []}), "farm.kml", tmp_path) == []


@pytest.mark.parametrize(
    ("value", "ogr_type", "expected"),
    [
        (np.int64(7), "OFTInteger64", 7),
        (np.float64(2.5), "OFTReal", 2.5),
        (5.0, "OFTInteger", 5),
        (float("nan"), "OFTReal", None),
        (float("inf"), "OFTReal", None),
        (pd.NaT, "OFTDateTime", None),
        (pd.Timestamp("2024-03-01"), "OFTDate", "2024-03-01"),
        (pd.Timestamp("2024-03-01T10:30:00"), "OFTDateTime", "2024-03-01T10:30:00"),
        (dt.date(2024, 3, 1), None, "2024-03-01"),
        (Decimal("1.25"), None, 1.25),
        (b"\x00\x01", "OFTBinary", "AAE="),
        (np.bool_(True), None, True),
        ([np.int32(1), None], "OFTIntegerList", [1, None]),
        ("text", "OFTString", "text"),
    ],
)
def test_json_value(value, ogr_type, expected) -> None:
    assert _json_value(value, ogr_type) == expected
