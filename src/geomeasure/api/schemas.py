from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from geomeasure.db import FileStatus
from geomeasure.domain import MeasurementStatus


class ErrorResponse(BaseModel):
    detail: str


class LayerSummary(BaseModel):
    name: str
    feature_count: int
    crs: str | None
    crs_assumed: bool = False


class FileSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    filename: str
    format: str
    status: FileStatus
    error: str | None
    crs: str | None = Field(description='CRS label of the source data, or "MIXED" across layers.')
    crs_assumed: bool = Field(description="True when no CRS was declared and WGS84 was inferred.")
    feature_count: int
    layer_count: int
    size_bytes: int
    created_at: datetime
    completed_at: datetime | None
    processing_ms: int | None


class FileDetail(FileSummary):
    layers: list[LayerSummary]
    geometry_types: dict[str, int] = Field(description="Feature count per geometry type.")

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "id": "kX3v9QpL2aE",
                "filename": "survey.kml",
                "format": "KML",
                "status": "COMPLETED",
                "error": None,
                "crs": "EPSG:4326",
                "crs_assumed": False,
                "feature_count": 120,
                "layer_count": 1,
                "size_bytes": 48211,
                "created_at": "2026-10-06T09:12:44Z",
                "completed_at": "2026-10-06T09:12:45Z",
                "processing_ms": 412,
                "layers": [{"name": "Parcels", "feature_count": 120, "crs": "EPSG:4326"}],
                "geometry_types": {"Polygon": 118, "LineString": 2},
            }
        },
    )


class FileList(BaseModel):
    count: int
    limit: int
    offset: int
    results: list[FileSummary]


class MeasurementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    status: MeasurementStatus
    reason: str | None
    area_m2: float | None
    perimeter_m: float | None
    length_m: float | None
    projected_crs: str | None = Field(
        description="Projected CRS the official values were computed in."
    )
    geodesic_area_m2: float | None
    geodesic_length_m: float | None
    deviation_pct: float | None = Field(
        description="Difference between the projected and geodesic values, in percent."
    )


class FeatureMeasurement(BaseModel):
    index: int
    layer: str
    geometry_type: str | None
    source_crs: str | None
    properties: dict[str, Any]
    geometry: dict[str, Any] | None = Field(description="GeoJSON geometry in WGS84.")
    measurement: MeasurementOut


class MeasurementSummary(BaseModel):
    total_area_m2: float
    total_length_m: float
    measured: int
    skipped: dict[MeasurementStatus, int] = Field(
        description="Features that were not measured, by status."
    )


class MeasurementPage(BaseModel):
    file_id: str
    crs: str | None
    summary: MeasurementSummary = Field(description="Totals over the whole file, ignoring filters.")
    count: int = Field(description="Number of features matching the filters.")
    limit: int
    offset: int
    results: list[FeatureMeasurement]
