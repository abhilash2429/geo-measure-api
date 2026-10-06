import shutil
from http import HTTPStatus
from pathlib import PurePosixPath
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from geomeasure.api.deps import SessionDep, SettingsDep
from geomeasure.api.schemas import (
    ErrorResponse,
    FeatureMeasurement,
    FileDetail,
    FileList,
    FileSummary,
    LayerSummary,
    MeasurementOut,
    MeasurementPage,
    MeasurementSummary,
)
from geomeasure.db import FeatureRecord, FileRecord, FileStatus
from geomeasure.domain import MeasurementStatus
from geomeasure.pipeline import process_upload, stored_upload_dir

router = APIRouter(prefix="/api/files", tags=["files"])

NOT_FOUND = {404: {"model": ErrorResponse, "description": "No file with this id."}}
NOT_READY = {409: {"model": ErrorResponse, "description": "The file is not COMPLETED."}}
NO_GEOMETRY = "NoGeometry"


@router.post(
    "/",
    status_code=HTTPStatus.CREATED,
    response_model=FileDetail,
    summary="Upload and process a Shapefile (.zip) or KML file",
    responses={
        400: {"model": ErrorResponse, "description": "Bad extension, corrupt or oversized upload."},
        422: {
            "model": FileDetail,
            "description": "Upload accepted but could not be processed. The record is kept as "
            "FAILED and stays fetchable.",
        },
    },
)
def upload_file(file: UploadFile, session: SessionDep, settings: SettingsDep) -> Any:
    filename = _client_filename(file.filename)
    # Read one byte past the limit so prepare_upload can reject oversized files
    # without us holding an arbitrarily large body in memory.
    content = file.file.read(settings.max_upload_mb * 1024 * 1024 + 1)

    record = process_upload(session, filename, content, settings)
    detail = _file_detail(session, record)
    if record.status == FileStatus.FAILED:
        return JSONResponse(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY, content=jsonable_encoder(detail)
        )
    return detail


@router.get("/", response_model=FileList, summary="List uploaded files, newest first")
def list_files(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> FileList:
    total = session.scalar(select(func.count()).select_from(FileRecord)) or 0
    records = session.scalars(
        select(FileRecord)
        .order_by(FileRecord.created_at.desc(), FileRecord.id)
        .limit(limit)
        .offset(offset)
    )
    return FileList(
        count=total,
        limit=limit,
        offset=offset,
        results=[FileSummary.model_validate(record) for record in records],
    )


@router.get(
    "/{file_id}/",
    response_model=FileDetail,
    summary="Get a file's processing status and metadata",
    responses=NOT_FOUND,
)
def get_file(file_id: str, session: SessionDep) -> FileDetail:
    return _file_detail(session, _get_file_or_404(session, file_id))


@router.get(
    "/{file_id}/measurements/",
    response_model=MeasurementPage,
    summary="Per-feature measurements",
    responses=NOT_FOUND | NOT_READY,
)
def get_measurements(
    file_id: str,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    status: Annotated[MeasurementStatus | None, Query(description="Only this status.")] = None,
    geometry_type: Annotated[str | None, Query(description='e.g. "Polygon".')] = None,
    include_geometry: Annotated[bool, Query(description="Include WGS84 GeoJSON.")] = True,
) -> MeasurementPage:
    record = _get_completed_file_or_error(session, file_id)

    query = select(FeatureRecord).where(FeatureRecord.file_id == file_id)
    if status is not None:
        query = query.where(FeatureRecord.status == status)
    if geometry_type is not None:
        query = query.where(FeatureRecord.geometry_type == geometry_type)

    features = session.scalars(query.order_by(FeatureRecord.index).limit(limit).offset(offset))
    return MeasurementPage(
        file_id=record.id,
        crs=record.crs,
        summary=_measurement_summary(session, file_id),
        count=_count(session, query),
        limit=limit,
        offset=offset,
        results=[_feature_measurement(feature, include_geometry) for feature in features],
    )


@router.get(
    "/{file_id}/features.geojson",
    summary="Download features with measurements as GeoJSON",
    description="A WGS84 FeatureCollection with the measurement fields flattened into each "
    "feature's properties, ready to open in QGIS. Source properties whose names clash with a "
    'measurement field are kept under a "source_" prefix.',
    response_class=JSONResponse,
    responses={
        200: {"content": {"application/geo+json": {}}},
        **NOT_FOUND,
        **NOT_READY,
    },
)
def export_geojson(file_id: str, session: SessionDep) -> JSONResponse:
    _get_completed_file_or_error(session, file_id)
    features = session.scalars(
        select(FeatureRecord).where(FeatureRecord.file_id == file_id).order_by(FeatureRecord.index)
    )
    collection = {
        "type": "FeatureCollection",
        "features": [_geojson_feature(feature) for feature in features],
    }
    return JSONResponse(content=collection, media_type="application/geo+json")


@router.delete(
    "/{file_id}/",
    status_code=HTTPStatus.NO_CONTENT,
    summary="Delete a file, its features and the stored upload",
    responses=NOT_FOUND,
)
def delete_file(file_id: str, session: SessionDep, settings: SettingsDep) -> None:
    record = _get_file_or_404(session, file_id)
    session.delete(record)
    session.commit()
    shutil.rmtree(stored_upload_dir(settings, file_id), ignore_errors=True)


def _client_filename(raw: str | None) -> str:
    """Keep only the base name; browsers and curl may send a full client-side path."""
    name = PurePosixPath((raw or "").replace("\\", "/")).name
    if not name:
        raise HTTPException(HTTPStatus.BAD_REQUEST, "The upload has no filename.")
    return name


def _get_file_or_404(session: Session, file_id: str) -> FileRecord:
    record = session.get(FileRecord, file_id)
    if record is None:
        raise HTTPException(HTTPStatus.NOT_FOUND, f"File {file_id!r} not found.")
    return record


def _get_completed_file_or_error(session: Session, file_id: str) -> FileRecord:
    record = _get_file_or_404(session, file_id)
    if record.status != FileStatus.COMPLETED:
        message = f"File {file_id!r} is {record.status}, measurements need it to be COMPLETED."
        if record.error:
            message = f"{message} Error: {record.error}"
        raise HTTPException(HTTPStatus.CONFLICT, message)
    return record


def _file_detail(session: Session, record: FileRecord) -> FileDetail:
    counts = session.execute(
        select(FeatureRecord.geometry_type, func.count())
        .where(FeatureRecord.file_id == record.id)
        .group_by(FeatureRecord.geometry_type)
    )
    return FileDetail(
        **FileSummary.model_validate(record).model_dump(),
        layers=[LayerSummary(**layer) for layer in record.layers or []],
        geometry_types={geometry_type or NO_GEOMETRY: count for geometry_type, count in counts},
    )


def _measurement_summary(session: Session, file_id: str) -> MeasurementSummary:
    rows = session.execute(
        select(
            FeatureRecord.status,
            func.count(),
            func.coalesce(func.sum(FeatureRecord.area_m2), 0.0),
            func.coalesce(func.sum(FeatureRecord.length_m), 0.0),
        )
        .where(FeatureRecord.file_id == file_id)
        .group_by(FeatureRecord.status)
    ).all()

    measured, total_area, total_length = 0, 0.0, 0.0
    skipped: dict[MeasurementStatus, int] = {}
    for status, count, area, length in rows:
        if status == MeasurementStatus.MEASURED:
            measured, total_area, total_length = count, area, length
        else:
            skipped[status] = count

    return MeasurementSummary(
        total_area_m2=total_area,
        total_length_m=total_length,
        measured=measured,
        skipped=skipped,
    )


def _count(session: Session, query: Select[tuple[FeatureRecord]]) -> int:
    return session.scalar(select(func.count()).select_from(query.subquery())) or 0


def _feature_measurement(feature: FeatureRecord, include_geometry: bool) -> FeatureMeasurement:
    return FeatureMeasurement(
        index=feature.index,
        layer=feature.layer,
        geometry_type=feature.geometry_type,
        source_crs=feature.source_crs,
        properties=feature.properties,
        geometry=feature.geometry if include_geometry else None,
        measurement=MeasurementOut.model_validate(feature),
    )


def _geojson_feature(feature: FeatureRecord) -> dict[str, Any]:
    measured = {
        "feature_index": feature.index,
        "layer": feature.layer,
        "source_crs": feature.source_crs,
        **MeasurementOut.model_validate(feature).model_dump(mode="json"),
    }
    source = {
        (f"source_{key}" if key in measured else key): value
        for key, value in feature.properties.items()
    }
    return {
        "type": "Feature",
        "id": feature.index,
        "geometry": feature.geometry,
        "properties": {**source, **measured},
    }
