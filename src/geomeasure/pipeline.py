"""The upload flow: validate, store, read, measure, persist.

An upload ends in one of three ways:
- InvalidUpload: the request was bad. Nothing is kept and the caller gets a 400.
- FAILED: the upload looked fine but could not be read. The record is kept with the error.
- COMPLETED: every feature is stored with its measurement.
"""

import logging
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pyproj import CRS
from pyproj.exceptions import ProjError
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry
from sqlalchemy import insert
from sqlalchemy.orm import Session

from geomeasure.config import Settings
from geomeasure.db import FeatureRecord, FileRecord, FileStatus, new_file_id
from geomeasure.domain import SourceFeature, SourceLayer
from geomeasure.errors import InvalidUpload, ProcessingError
from geomeasure.geo.crs import CoordinatesOutOfRange, crs_label, to_wgs84
from geomeasure.geo.measure import measure_feature
from geomeasure.ingest.reader import read_layers
from geomeasure.ingest.upload import detect_format, prepare_upload

logger = logging.getLogger(__name__)

MIXED_CRS = "MIXED"
UNEXPECTED_ERROR = "Unexpected error while processing the file. See server logs for details."


def process_upload(
    session: Session, filename: str, content: bytes, settings: Settings
) -> FileRecord:
    started = time.perf_counter()
    file_format = detect_format(filename)

    # Validate and unpack before creating the record, so a rejected upload never shows up.
    file_id = new_file_id()
    upload_dir = stored_upload_dir(settings, file_id)
    try:
        paths = prepare_upload(filename, content, upload_dir, settings)
    except InvalidUpload:
        shutil.rmtree(upload_dir, ignore_errors=True)
        raise

    record = FileRecord(
        id=file_id,
        filename=filename,
        format=file_format,
        status=FileStatus.PROCESSING,
        size_bytes=len(content),
    )
    session.add(record)
    session.commit()

    try:
        layers = read_layers(paths)
        _save_results(session, record, layers)
    except ProcessingError as exc:
        _mark_failed(session, record, str(exc))
    except Exception:
        logger.exception("Processing upload %s (%s) failed", record.id, filename)
        _mark_failed(session, record, UNEXPECTED_ERROR)

    record.completed_at = datetime.now(UTC)
    record.processing_ms = round((time.perf_counter() - started) * 1000)
    session.commit()
    return record


def stored_upload_dir(settings: Settings, file_id: str) -> Path:
    return settings.upload_dir / file_id


def _save_results(session: Session, record: FileRecord, layers: list[SourceLayer]) -> None:
    rows = [
        _feature_row(record.id, layer, feature) for layer in layers for feature in layer.features
    ]
    if rows:
        session.execute(insert(FeatureRecord), rows)

    record.status = FileStatus.COMPLETED
    record.crs = _file_crs(layers)
    record.crs_assumed = any(layer.crs_assumed for layer in layers)
    record.feature_count = len(rows)
    record.layer_count = len(layers)
    record.layers = [
        {
            "name": layer.name,
            "feature_count": len(layer.features),
            "crs": crs_label(layer.crs),
            "crs_assumed": layer.crs_assumed,
        }
        for layer in layers
    ]


def _feature_row(file_id: str, layer: SourceLayer, feature: SourceFeature) -> dict[str, Any]:
    geom = feature.geometry
    measurement = measure_feature(geom, layer.crs)

    return {
        "file_id": file_id,
        "index": feature.index,
        "layer": layer.name,
        "geometry_type": geom.geom_type if geom is not None else None,
        "geometry": _wgs84_geojson(geom, layer.crs),
        "source_crs": crs_label(layer.crs),
        "properties": feature.properties,
        "status": measurement.status,
        "reason": measurement.reason,
        "area_m2": measurement.area_m2,
        "perimeter_m": measurement.perimeter_m,
        "length_m": measurement.length_m,
        "projected_crs": measurement.projected_crs,
        "geodesic_area_m2": measurement.geodesic_area_m2,
        "geodesic_length_m": measurement.geodesic_length_m,
        "deviation_pct": measurement.deviation_pct,
    }


def _wgs84_geojson(geom: BaseGeometry | None, crs: CRS | None) -> dict[str, Any] | None:
    """GeoJSON in WGS84, or None when the shape cannot be placed on the globe.

    Returning nothing is better than passing off source coordinates as lon/lat.
    """
    if geom is None or crs is None:
        return None
    try:
        return mapping(to_wgs84(geom, crs))
    except (CoordinatesOutOfRange, ProjError):
        return None


def _file_crs(layers: list[SourceLayer]) -> str | None:
    labels = {crs_label(layer.crs) for layer in layers}
    if len(labels) > 1:
        return MIXED_CRS
    return labels.pop() if labels else None


def _mark_failed(session: Session, record: FileRecord, error: str) -> None:
    session.rollback()
    record.status = FileStatus.FAILED
    record.error = error
