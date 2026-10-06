import secrets
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from geomeasure.domain import MeasurementStatus


class FileStatus(StrEnum):
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


def new_file_id() -> str:
    return secrets.token_urlsafe(8)


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Stores aware UTC datetimes. SQLite drops the tzinfo, so it is put back on the way out."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    pass


class FileRecord(Base):
    __tablename__ = "files"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=new_file_id)
    filename: Mapped[str] = mapped_column(String(255))
    format: Mapped[str] = mapped_column(String(16))
    status: Mapped[FileStatus] = mapped_column(Enum(FileStatus, native_enum=False, length=16))
    error: Mapped[str | None] = mapped_column(Text)

    # One label for the whole file, "MIXED" when layers disagree, None when unknown.
    crs: Mapped[str | None] = mapped_column(String(64))
    crs_assumed: Mapped[bool] = mapped_column(default=False)
    feature_count: Mapped[int] = mapped_column(default=0)
    layer_count: Mapped[int] = mapped_column(default=0)
    # [{"name", "feature_count", "crs", "crs_assumed"}, ...]
    layers: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    size_bytes: Mapped[int]

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    processing_ms: Mapped[int | None]

    features: Mapped[list["FeatureRecord"]] = relationship(
        back_populates="file",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="FeatureRecord.index",
    )


class FeatureRecord(Base):
    __tablename__ = "features"
    __table_args__ = (
        UniqueConstraint("file_id", "index"),
        Index("ix_features_file_status", "file_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"))
    index: Mapped[int]
    layer: Mapped[str] = mapped_column(String(255))
    geometry_type: Mapped[str | None] = mapped_column(String(32))
    # GeoJSON in WGS84. None when the feature has no geometry or its CRS is unknown.
    geometry: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    source_crs: Mapped[str | None] = mapped_column(String(64))
    properties: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    status: Mapped[MeasurementStatus] = mapped_column(
        Enum(MeasurementStatus, native_enum=False, length=32)
    )
    reason: Mapped[str | None] = mapped_column(Text)
    area_m2: Mapped[float | None]
    perimeter_m: Mapped[float | None]
    length_m: Mapped[float | None]
    projected_crs: Mapped[str | None] = mapped_column(String(64))
    geodesic_area_m2: Mapped[float | None]
    geodesic_length_m: Mapped[float | None]
    deviation_pct: Mapped[float | None]

    file: Mapped[FileRecord] = relationship(back_populates="features")
