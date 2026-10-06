"""Plain data types shared between the reader, the measurement code and the API layer.

Nothing in here touches the database or HTTP, so every stage of the pipeline can be
tested on its own.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pyproj import CRS
from shapely.geometry.base import BaseGeometry


@dataclass
class SourceFeature:
    """One feature exactly as it came out of the file, still in the layer's own CRS."""

    index: int
    geometry: BaseGeometry | None
    properties: dict[str, Any]


@dataclass
class SourceLayer:
    """A layer read from the upload. A zip can hold several shapefiles, a KML several folders."""

    name: str
    crs: CRS | None
    # True when the file did not declare a CRS and we inferred WGS84 from the coordinates.
    crs_assumed: bool = False
    features: list[SourceFeature] = field(default_factory=list)


class MeasurementStatus(StrEnum):
    MEASURED = "MEASURED"
    NOT_APPLICABLE = "NOT_APPLICABLE"  # points: nothing to measure
    UNSUPPORTED_GEOMETRY = "UNSUPPORTED_GEOMETRY"  # e.g. mixed GeometryCollection
    INVALID_GEOMETRY = "INVALID_GEOMETRY"  # e.g. self-intersecting polygon
    EMPTY_GEOMETRY = "EMPTY_GEOMETRY"
    UNKNOWN_CRS = "UNKNOWN_CRS"


@dataclass(frozen=True)
class Measurement:
    status: MeasurementStatus
    reason: str | None = None

    # Values from the projected calculation. That is the "official" answer.
    area_m2: float | None = None
    perimeter_m: float | None = None
    length_m: float | None = None
    projected_crs: str | None = None  # e.g. "EPSG:32643"

    # Same quantity computed on the WGS84 ellipsoid, used as an accuracy cross-check.
    geodesic_area_m2: float | None = None
    geodesic_length_m: float | None = None
    deviation_pct: float | None = None
