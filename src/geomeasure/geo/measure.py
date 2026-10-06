"""Per-feature area and length, measured in metres in a projected CRS, never in degrees.

Strategy:
- Each feature is reprojected to WGS84 and measured in the UTM zone of its centroid (UPS
  near the poles). UTM is conformal and well known, and over most of a zone its area error
  stays under 0.1%.
- We check that instead of assuming it: the projection's areal scale factor is evaluated at
  every vertex, and if it strays more than 0.1% from 1 (features near a zone edge or wider
  than a zone, or close to the pole where UPS is 1.2% off in area), we switch to a Lambert
  Azimuthal Equal Area projection centred on the feature. LAEA is equal-area by construction,
  so area stays exact at any size. Its lengths drift slowly away from the centre (about 0.1%
  for a line 20 degrees long), which the geodesic cross-check below makes visible.
- Edges are treated as straight lines in the CRS the file was authored in, so long edges are
  densified in that CRS before reprojection. Otherwise a 20 degree polygon edge would bend
  differently in every projection and the numbers would disagree by more than a percent.
- Every result also carries the same quantity on the WGS84 ellipsoid (pyproj.Geod) and the
  percentage deviation, so the projected number can be audited feature by feature.
- Geometries crossing the antimeridian (longitude span over 180 degrees) are assumed to go
  the short way round: negative longitudes are shifted by +360 before measuring. A feature
  that still spans more than half the globe is ambiguous and is rejected as unsupported.
"""

from collections.abc import Iterator
from enum import Enum, auto
from functools import lru_cache

import numpy as np
import shapely
from pyproj import CRS, Geod, Proj
from pyproj.exceptions import ProjError
from shapely.geometry import LineString, MultiLineString, MultiPolygon
from shapely.geometry.base import BaseGeometry
from shapely.validation import explain_validity

from geomeasure.domain import Measurement, MeasurementStatus
from geomeasure.geo.crs import (
    WGS84,
    CoordinatesOutOfRange,
    crs_label,
    equal_area_crs,
    local_projected_crs,
    to_wgs84,
    transform_geometry,
)

MAX_AREAL_DISTORTION = 0.001
MAX_SEGMENT_DEGREES = 0.01
MAX_SEGMENT_PROJECTED_UNITS = 1000.0
DEVIATION_DECIMALS = 4

_GEOD = Geod(ellps="WGS84")


class _Kind(Enum):
    POINT = auto()
    LINEAR = auto()
    AREAL = auto()


def measure_feature(geom: BaseGeometry | None, crs: CRS | None) -> Measurement:
    if geom is None or geom.is_empty:
        return _rejected(MeasurementStatus.EMPTY_GEOMETRY, "The feature has no geometry.")
    if crs is None:
        return _rejected(
            MeasurementStatus.UNKNOWN_CRS,
            "The layer has no coordinate reference system, so coordinates cannot be "
            "converted to metres.",
        )

    geom = shapely.force_2d(geom)
    kind, geom = _classify(geom)

    if kind is None:
        return _rejected(
            MeasurementStatus.UNSUPPORTED_GEOMETRY,
            f"{geom.geom_type} mixes points, lines and polygons, which have no single measure.",
        )
    if kind is _Kind.POINT:
        return _rejected(
            MeasurementStatus.NOT_APPLICABLE, "Points have no area or length to measure."
        )
    if kind is _Kind.AREAL and not geom.is_valid:
        return _rejected(MeasurementStatus.INVALID_GEOMETRY, explain_validity(geom) + ".")

    if not crs.is_geographic:
        geom = shapely.segmentize(geom, MAX_SEGMENT_PROJECTED_UNITS)
    try:
        lonlat = to_wgs84(geom, crs)
    except CoordinatesOutOfRange as exc:
        return _rejected(MeasurementStatus.INVALID_GEOMETRY, str(exc))
    except ProjError as exc:
        return _rejected(
            MeasurementStatus.INVALID_GEOMETRY, f"Could not transform coordinates to WGS84: {exc}"
        )

    lonlat = _unwrap_antimeridian(lonlat)
    if _longitude_span(lonlat) > 180.0:
        return _rejected(
            MeasurementStatus.UNSUPPORTED_GEOMETRY,
            "The feature spans more than half the globe in longitude, so its extent is ambiguous.",
        )
    if crs.is_geographic:
        # Only after unwrapping: an edge from 179.99 to -179.99 would otherwise be
        # densified all the way round the globe.
        lonlat = shapely.segmentize(lonlat, MAX_SEGMENT_DEGREES)

    target = _measurement_crs(lonlat)
    projected = transform_geometry(lonlat, WGS84, target)

    if kind is _Kind.AREAL:
        return _measure_areal(lonlat, projected, target)
    return _measure_linear(lonlat, projected, target)


def _measure_areal(lonlat: BaseGeometry, projected: BaseGeometry, target: CRS) -> Measurement:
    area = projected.area
    geodesic_area, _ = _GEOD.geometry_area_perimeter(shapely.orient_polygons(lonlat))
    geodesic_area = abs(geodesic_area)
    return Measurement(
        status=MeasurementStatus.MEASURED,
        area_m2=area,
        perimeter_m=projected.length,
        projected_crs=crs_label(target),
        geodesic_area_m2=geodesic_area,
        deviation_pct=_deviation_pct(area, geodesic_area),
    )


def _measure_linear(lonlat: BaseGeometry, projected: BaseGeometry, target: CRS) -> Measurement:
    length = projected.length
    geodesic_length = _GEOD.geometry_length(lonlat)
    return Measurement(
        status=MeasurementStatus.MEASURED,
        length_m=length,
        projected_crs=crs_label(target),
        geodesic_length_m=geodesic_length,
        deviation_pct=_deviation_pct(length, geodesic_length),
    )


def _rejected(status: MeasurementStatus, reason: str) -> Measurement:
    return Measurement(status=status, reason=reason)


def _classify(geom: BaseGeometry) -> tuple[_Kind | None, BaseGeometry]:
    """Decide what kind of measure applies, merging single-dimension collections into a Multi*."""
    match geom.geom_type:
        case "Point" | "MultiPoint":
            return _Kind.POINT, geom
        case "LineString" | "MultiLineString" | "LinearRing":
            return _Kind.LINEAR, geom
        case "Polygon" | "MultiPolygon":
            return _Kind.AREAL, geom
        case "GeometryCollection":
            return _classify_collection(geom)
    return None, geom


def _classify_collection(collection: BaseGeometry) -> tuple[_Kind | None, BaseGeometry]:
    parts = [part for part in _leaves(collection) if not part.is_empty]
    dimensions = {part.geom_type for part in parts}
    if dimensions <= {"Polygon"}:
        return _Kind.AREAL, MultiPolygon(parts)
    if dimensions <= {"LineString", "LinearRing"}:
        return _Kind.LINEAR, MultiLineString([LineString(part.coords) for part in parts])
    if dimensions <= {"Point"}:
        return _Kind.POINT, collection
    return None, collection


def _leaves(geom: BaseGeometry) -> Iterator[BaseGeometry]:
    if hasattr(geom, "geoms"):
        for part in geom.geoms:
            yield from _leaves(part)
    else:
        yield geom


def _longitude_span(geom: BaseGeometry) -> float:
    min_lon, _, max_lon, _ = geom.bounds
    return max_lon - min_lon


def _unwrap_antimeridian(geom: BaseGeometry) -> BaseGeometry:
    if _longitude_span(geom) <= 180.0:
        return geom

    def shift(coords: np.ndarray) -> np.ndarray:
        lon = np.where(coords[:, 0] < 0.0, coords[:, 0] + 360.0, coords[:, 0])
        return np.column_stack([lon, coords[:, 1]])

    return shapely.transform(geom, shift)


def _measurement_crs(lonlat: BaseGeometry) -> CRS:
    centroid = lonlat.centroid
    local = local_projected_crs(centroid.x, centroid.y)
    if _max_areal_distortion(lonlat, local) <= MAX_AREAL_DISTORTION:
        return local
    return equal_area_crs(centroid.x, centroid.y)


def _max_areal_distortion(lonlat: BaseGeometry, crs: CRS) -> float:
    coords = shapely.get_coordinates(lonlat)
    factors = _proj(crs).get_factors(coords[:, 0], coords[:, 1])
    return float(np.max(np.abs(np.asarray(factors.areal_scale) - 1.0)))


@lru_cache(maxsize=128)
def _proj(crs: CRS) -> Proj:
    return Proj(crs)


def _deviation_pct(projected: float, geodesic: float) -> float | None:
    if geodesic == 0:
        return None
    deviation = round((projected - geodesic) / geodesic * 100.0, DEVIATION_DECIMALS)
    return deviation + 0.0  # turns a rounded -0.0 into 0.0
