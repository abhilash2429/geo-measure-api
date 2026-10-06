"""Read Shapefiles and KML into `SourceLayer`s with GDAL (through pyogrio).

Geometries come out untouched, in the layer's own CRS. Validating and measuring them is
the measurement layer's job.
"""

import base64
import datetime as dt
import itertools
import math
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
from pyogrio.errors import DataLayerError, DataSourceError
from pyproj import CRS

from geomeasure.domain import SourceFeature, SourceLayer
from geomeasure.errors import ProcessingError
from geomeasure.ingest.upload import EXTRACT_DIRNAME

WGS84 = CRS.from_epsg(4326)

_INTEGER_FIELDS = {"OFTInteger", "OFTInteger64"}

# Columns the LIBKML driver adds to every layer. Each maps to the value LIBKML reports
# when the KML did not set it; a column is dropped when it never holds anything else.
_LIBKML_DEFAULTS: dict[str, Any] = {
    "id": None,
    "description": None,
    "timestamp": None,
    "begin": None,
    "end": None,
    "altitudeMode": None,
    "tessellate": -1,
    "extrude": 0,
    "visibility": -1,
    "drawOrder": None,
    "icon": None,
    "snippet": None,
}


def read_layers(paths: list[Path]) -> list[SourceLayer]:
    """Read every dataset in order. Feature indexes run across all layers, starting at 0."""
    counter = itertools.count()
    layers: list[SourceLayer] = []
    for path in paths:
        if path.suffix.lower() == ".kml":
            layers.extend(_read_kml(path, counter))
        else:
            layers.append(_read_shapefile(path, counter))
    return layers


def _read_shapefile(path: Path, counter: Iterator[int]) -> SourceLayer:
    info = _gdal(path, pyogrio.read_info, path)
    frame = _gdal(path, pyogrio.read_dataframe, path)

    crs_assumed = False
    crs = CRS.from_user_input(info["crs"]) if info["crs"] else None
    if crs is None and _looks_like_lon_lat(frame):
        crs, crs_assumed = WGS84, True

    return SourceLayer(
        name=_shapefile_layer_name(path),
        crs=crs,
        crs_assumed=crs_assumed,
        features=_features(frame, _field_types(info), counter),
    )


def _read_kml(path: Path, counter: Iterator[int]) -> list[SourceLayer]:
    layers = []
    for layer_name, _geometry_type in _gdal(path, pyogrio.list_layers, path):
        frame = _gdal(path, pyogrio.read_dataframe, path, layer=layer_name)
        if frame.empty:
            continue
        info = _gdal(path, pyogrio.read_info, path, layer=layer_name)
        frame = _drop_libkml_noise(frame)
        layers.append(
            SourceLayer(
                name=str(layer_name),
                crs=WGS84,
                features=_features(frame, _field_types(info), counter),
            )
        )
    return layers


def _gdal[T](path: Path, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    try:
        return func(*args, **kwargs)
    except (DataSourceError, DataLayerError) as exc:
        raise ProcessingError(
            f"Could not read {_upload_relative(path)}: {_hide_server_paths(str(exc), path)}"
        ) from exc


def _hide_server_paths(message: str, path: Path) -> str:
    """GDAL quotes absolute paths. Clients should only see paths inside their own upload."""
    root = _upload_root(path)
    for prefix in {str(root), root.as_posix()}:
        message = message.replace(prefix + "\\", "").replace(prefix + "/", "")
    return message


def _upload_root(path: Path) -> Path:
    for ancestor in path.parents:
        if ancestor.name == EXTRACT_DIRNAME:
            return ancestor
    return path.parent


def _upload_relative(path: Path) -> str:
    return path.relative_to(_upload_root(path)).as_posix()


def _shapefile_layer_name(path: Path) -> str:
    """Path inside the zip without the extension, e.g. 'nested/parcels'."""
    return _upload_relative(path.with_suffix(""))


def _looks_like_lon_lat(frame: gpd.GeoDataFrame) -> bool:
    geometries = frame.geometry
    if not geometries.notna().any() or geometries.is_empty.all():
        return False
    min_x, min_y, max_x, max_y = geometries.total_bounds
    return -180 <= min_x <= max_x <= 180 and -90 <= min_y <= max_y <= 90


def _drop_libkml_noise(frame: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    noise = [
        column
        for column, default in _LIBKML_DEFAULTS.items()
        if column in frame.columns
        and all(_is_missing(value) or value == default for value in frame[column])
    ]
    return frame.drop(columns=noise)


def _field_types(info: dict) -> dict[str, str]:
    return dict(zip(info["fields"], info["ogr_types"], strict=True))


def _features(
    frame: gpd.GeoDataFrame, field_types: dict[str, str], counter: Iterator[int]
) -> list[SourceFeature]:
    attributes = frame.drop(columns=frame.geometry.name)
    features = []
    for geometry, record in zip(frame.geometry, attributes.to_dict("records"), strict=True):
        properties = {
            str(key): _json_value(value, field_types.get(key)) for key, value in record.items()
        }
        features.append(
            SourceFeature(index=next(counter), geometry=geometry, properties=properties)
        )
    return features


def _json_value(value: Any, ogr_type: str | None = None) -> Any:
    """Convert one attribute value to a plain JSON-serializable Python value."""
    if isinstance(value, (list, tuple, np.ndarray)):
        return [_json_value(item) for item in value]
    if _is_missing(value):
        return None
    if isinstance(value, np.generic):
        value = value.item()

    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        if ogr_type in _INTEGER_FIELDS and value.is_integer():
            return int(value)
        return value
    if isinstance(value, Decimal):
        return _json_value(float(value))
    if isinstance(value, (pd.Timestamp, dt.datetime)):
        return value.date().isoformat() if ogr_type == "OFTDate" else value.isoformat()
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return base64.b64encode(value).decode("ascii")
    return value


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False
