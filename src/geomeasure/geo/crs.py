"""CRS helpers: labelling, reprojection to WGS84 and choosing a metric CRS for a location."""

from functools import lru_cache

import numpy as np
import shapely
from pyproj import CRS, Transformer
from pyproj.crs import GeographicCRS, ProjectedCRS
from pyproj.crs.coordinate_operation import LambertAzimuthalEqualAreaConversion
from shapely.geometry.base import BaseGeometry

WGS84 = CRS.from_epsg(4326)

UPS_NORTH_EPSG = 5041
UPS_SOUTH_EPSG = 5042
UTM_NORTH_LIMIT = 84.0
UTM_SOUTH_LIMIT = -80.0


def crs_label(crs: CRS | None) -> str | None:
    """Return "AUTHORITY:CODE" when the CRS maps to a known code, otherwise its name."""
    if crs is None:
        return None
    authority = crs.to_authority(min_confidence=70)
    if authority is not None:
        name, code = authority
        return f"{name}:{code}"
    return crs.name


def is_wgs84(crs: CRS) -> bool:
    return crs.equals(WGS84, ignore_axis_order=True)


@lru_cache(maxsize=128)
def _transformer(source: CRS, target: CRS) -> Transformer:
    return Transformer.from_crs(source, target, always_xy=True)


def transform_geometry(geom: BaseGeometry, source: CRS, target: CRS) -> BaseGeometry:
    """Reproject a geometry (x/y order, Z dropped) from one CRS to another."""
    transformer = _transformer(source, target)

    def project(coords: np.ndarray) -> np.ndarray:
        x, y = transformer.transform(coords[:, 0], coords[:, 1])
        return np.column_stack([x, y])

    return shapely.transform(geom, project)


class CoordinatesOutOfRange(ValueError):
    """The coordinates cannot be placed on the globe in the CRS the file declares."""


def to_wgs84(geom: BaseGeometry, crs: CRS) -> BaseGeometry:
    """Reproject to WGS84 lon/lat, refusing coordinates the source CRS cannot represent.

    PROJ does not always fail loudly on bad input: UTM returns inf, and Web Mercator clamps
    any huge northing to latitude 90. Projecting the result back and comparing catches both.
    """
    lonlat = geom if is_wgs84(crs) else transform_geometry(geom, crs, WGS84)

    coords = shapely.get_coordinates(lonlat)
    if not (np.isfinite(coords).all() and (np.abs(coords[:, 1]) <= 90.0).all()):
        raise CoordinatesOutOfRange("Coordinates fall outside the valid longitude/latitude range.")
    if not crs.is_geographic and not _round_trips(geom, lonlat, crs):
        raise CoordinatesOutOfRange(
            f"Coordinates are outside the area {crs_label(crs)} can represent."
        )
    return lonlat


def _round_trips(source: BaseGeometry, lonlat: BaseGeometry, crs: CRS) -> bool:
    back = shapely.get_coordinates(transform_geometry(lonlat, WGS84, crs))
    return bool(np.allclose(back, shapely.get_coordinates(source), rtol=1e-9, atol=1e-3))


def utm_zone(lon: float, lat: float) -> int:
    """UTM zone number for a point, including the Norway and Svalbard exceptions."""
    lon = (lon + 180.0) % 360.0 - 180.0
    if 56.0 <= lat < 64.0 and 3.0 <= lon < 12.0:
        return 32
    if 72.0 <= lat < 84.0 and 0.0 <= lon < 42.0:
        if lon < 9.0:
            return 31
        if lon < 21.0:
            return 33
        if lon < 33.0:
            return 35
        return 37
    return min(int((lon + 180.0) // 6.0) + 1, 60)


@lru_cache(maxsize=256)
def _crs_from_epsg(code: int) -> CRS:
    return CRS.from_epsg(code)


def local_projected_crs(lon: float, lat: float) -> CRS:
    """The UTM zone containing the point, or UPS beyond the UTM latitude limits."""
    if lat >= UTM_NORTH_LIMIT:
        return _crs_from_epsg(UPS_NORTH_EPSG)
    if lat < UTM_SOUTH_LIMIT:
        return _crs_from_epsg(UPS_SOUTH_EPSG)
    base = 32600 if lat >= 0 else 32700
    return _crs_from_epsg(base + utm_zone(lon, lat))


def equal_area_crs(lon: float, lat: float) -> CRS:
    """Lambert Azimuthal Equal Area on the WGS84 ellipsoid, centred on the given point."""
    lon = (lon + 180.0) % 360.0 - 180.0
    return _laea(round(lon, 6), round(lat, 6))


@lru_cache(maxsize=128)
def _laea(lon: float, lat: float) -> CRS:
    conversion = LambertAzimuthalEqualAreaConversion(
        latitude_natural_origin=lat, longitude_natural_origin=lon
    )
    name = f"WGS 84 / LAEA centred on {_format_lat(lat)} {_format_lon(lon)}"
    return ProjectedCRS(conversion=conversion, geodetic_crs=GeographicCRS(), name=name)


def _format_lat(lat: float) -> str:
    return f"{abs(lat):.4f}{'N' if lat >= 0 else 'S'}"


def _format_lon(lon: float) -> str:
    return f"{abs(lon):.4f}{'E' if lon >= 0 else 'W'}"
