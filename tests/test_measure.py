import pytest
import shapely
from pyproj import CRS
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
)
from shapely.geometry.base import BaseGeometry

from geomeasure.domain import MeasurementStatus
from geomeasure.geo.crs import (
    WGS84,
    crs_label,
    equal_area_crs,
    local_projected_crs,
    to_wgs84,
    transform_geometry,
)
from geomeasure.geo.measure import measure_feature

ONE_KM2 = 1_000_000.0


def local_aeqd(lon: float, lat: float) -> CRS:
    return CRS.from_proj4(f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m")


def to_lonlat(geom: BaseGeometry, lon: float, lat: float) -> BaseGeometry:
    """Build a shape in metres around (lon, lat) and return it in WGS84 lon/lat."""
    return transform_geometry(geom, local_aeqd(lon, lat), WGS84)


def square(half_side: float) -> Polygon:
    h = half_side
    return Polygon([(-h, -h), (h, -h), (h, h), (-h, h)])


def km_square_at(lon: float, lat: float) -> BaseGeometry:
    return to_lonlat(square(500), lon, lat)


@pytest.mark.parametrize(
    ("lon", "lat"),
    [(9.5, 0.0), (10.0, 60.0), (151.2, -33.9)],
    ids=["equator", "60N", "sydney"],
)
def test_one_km_square_measures_one_km2(lon, lat):
    result = measure_feature(km_square_at(lon, lat), WGS84)

    assert result.status is MeasurementStatus.MEASURED
    assert result.area_m2 == pytest.approx(ONE_KM2, rel=1e-3)
    assert result.perimeter_m == pytest.approx(4000, rel=1e-3)
    assert result.geodesic_area_m2 == pytest.approx(ONE_KM2, rel=1e-4)
    assert abs(result.deviation_pct) < 0.1
    assert result.projected_crs == crs_label(local_projected_crs(lon, lat))
    assert result.length_m is None


def test_ten_km_line():
    line = to_lonlat(LineString([(-5000, 0), (5000, 0)]), 78.5, 17.4)

    result = measure_feature(line, WGS84)

    assert result.status is MeasurementStatus.MEASURED
    assert result.length_m == pytest.approx(10_000, rel=1e-3)
    assert result.geodesic_length_m == pytest.approx(10_000, abs=1)
    assert result.area_m2 is None


def test_projected_source_crs_gives_same_area_as_wgs84():
    utm43 = CRS.from_epsg(32643)
    polygon = Polygon(
        [(500_000, 2_000_000), (501_000, 2_000_000), (501_000, 2_001_000), (500_000, 2_001_000)]
    )

    from_projected = measure_feature(polygon, utm43)
    from_lonlat = measure_feature(to_wgs84(polygon, utm43), WGS84)

    assert from_projected.status is MeasurementStatus.MEASURED
    assert from_projected.area_m2 == pytest.approx(from_lonlat.area_m2, rel=1e-6)
    assert from_projected.projected_crs == "EPSG:32643"


def test_multipolygon_sums_its_parts():
    first = km_square_at(10.0, 45.0)
    second = km_square_at(10.1, 45.0)

    combined = measure_feature(MultiPolygon([first, second]), WGS84)
    parts = [measure_feature(p, WGS84) for p in (first, second)]

    assert combined.area_m2 == pytest.approx(sum(p.area_m2 for p in parts), rel=1e-9)
    assert combined.perimeter_m == pytest.approx(sum(p.perimeter_m for p in parts), rel=1e-9)
    assert combined.area_m2 == pytest.approx(2 * ONE_KM2, rel=1e-3)


def test_polygon_with_hole_excludes_the_hole():
    holed = Polygon(square(500).exterior.coords, [square(250).exterior.coords])

    result = measure_feature(to_lonlat(holed, 10.0, 45.0), WGS84)

    assert result.area_m2 == pytest.approx(750_000, rel=1e-3)
    assert result.geodesic_area_m2 == pytest.approx(750_000, rel=1e-4)
    assert result.perimeter_m == pytest.approx(6000, rel=1e-3)


def test_z_coordinates_are_ignored():
    flat = km_square_at(10.0, 45.0)
    raised = shapely.force_3d(flat, z=350.0)

    assert measure_feature(raised, WGS84) == measure_feature(flat, WGS84)


def test_geometry_collection_of_polygons_is_measured_as_one():
    first = km_square_at(10.0, 45.0)
    second = km_square_at(10.1, 45.0)

    result = measure_feature(GeometryCollection([first, second]), WGS84)

    assert result.status is MeasurementStatus.MEASURED
    assert result.area_m2 == pytest.approx(2 * ONE_KM2, rel=1e-3)


def test_geometry_collection_of_lines_is_measured_as_one():
    lines = [
        to_lonlat(LineString([(0, 0), (1000, 0)]), 10.0, 45.0),
        to_lonlat(LineString([(0, 500), (2000, 500)]), 10.0, 45.0),
    ]

    result = measure_feature(GeometryCollection(lines), WGS84)

    assert result.length_m == pytest.approx(3000, rel=1e-3)


@pytest.mark.parametrize("geom", [Point(78.5, 17.4), MultiPoint([(78.5, 17.4), (78.6, 17.5)])])
def test_points_are_not_measured(geom):
    result = measure_feature(geom, WGS84)

    assert result.status is MeasurementStatus.NOT_APPLICABLE
    assert result.reason


@pytest.mark.parametrize("geom", [None, Polygon(), GeometryCollection()])
def test_empty_geometry(geom):
    assert measure_feature(geom, WGS84).status is MeasurementStatus.EMPTY_GEOMETRY


def test_missing_crs():
    result = measure_feature(km_square_at(10.0, 45.0), None)

    assert result.status is MeasurementStatus.UNKNOWN_CRS
    assert result.area_m2 is None


def test_self_intersecting_polygon_is_invalid():
    bowtie = Polygon([(10.0, 45.0), (10.01, 45.01), (10.01, 45.0), (10.0, 45.01)])

    result = measure_feature(bowtie, WGS84)

    assert result.status is MeasurementStatus.INVALID_GEOMETRY
    assert "Self-intersection" in result.reason


def test_mixed_geometry_collection_is_unsupported():
    mixed = GeometryCollection([km_square_at(10.0, 45.0), Point(10.0, 45.0)])

    result = measure_feature(mixed, WGS84)

    assert result.status is MeasurementStatus.UNSUPPORTED_GEOMETRY
    assert result.reason


def test_coordinates_outside_lonlat_range_are_invalid():
    bad = Polygon([(10.0, 95.0), (11.0, 95.0), (11.0, 96.0)])

    assert measure_feature(bad, WGS84).status is MeasurementStatus.INVALID_GEOMETRY


@pytest.mark.parametrize(
    "epsg",
    [
        32644,  # UTM returns inf for coordinates this far out
        3857,  # Web Mercator silently clamps them to latitude 90
    ],
)
def test_coordinates_outside_projected_crs_domain_are_invalid(epsg):
    far_away = Polygon([(1e12, 1e12), (1e12 + 10, 1e12), (1e12 + 10, 1e12 + 10)])

    result = measure_feature(far_away, CRS.from_epsg(epsg))

    assert result.status is MeasurementStatus.INVALID_GEOMETRY
    assert result.area_m2 is None


@pytest.mark.parametrize(
    ("lon", "lat", "epsg"),
    [
        (78.5, 17.4, 32644),  # Hyderabad
        (151.2, -33.87, 32756),  # Sydney
        (-74.0, 40.7, 32618),  # New York
        (5.3, 60.4, 32632),  # Bergen, Norway exception
        (15.6, 78.2, 32633),  # Svalbard exception
        (179.9, 0.0, 32660),
        (180.0, 10.0, 32601),
        (0.0, 89.0, 5041),
        (0.0, -85.0, 5042),
    ],
)
def test_local_projected_crs(lon, lat, epsg):
    assert local_projected_crs(lon, lat).to_epsg() == epsg


def test_crs_label():
    assert crs_label(WGS84) == "EPSG:4326"
    assert crs_label(None) is None
    assert crs_label(equal_area_crs(80.0, 20.0)) == "WGS 84 / LAEA centred on 20.0000N 80.0000E"


def test_to_wgs84_leaves_lonlat_alone():
    polygon = km_square_at(10.0, 45.0)

    assert to_wgs84(polygon, WGS84) is polygon


def test_wide_polygon_falls_back_to_equal_area():
    wide = Polygon([(70, 10), (90, 10), (90, 30), (70, 30)])

    result = measure_feature(wide, WGS84)

    assert result.projected_crs.startswith("WGS 84 / LAEA")
    assert abs(result.deviation_pct) < 0.01


def test_wide_line_falls_back_and_stays_close_to_geodesic():
    result = measure_feature(LineString([(70, 10), (90, 10)]), WGS84)

    assert result.projected_crs.startswith("WGS 84 / LAEA")
    assert abs(result.deviation_pct) < 0.5


def test_polar_feature_avoids_ups_distortion():
    result = measure_feature(km_square_at(0.0, 89.5), WGS84)

    assert result.projected_crs.startswith("WGS 84 / LAEA")
    assert result.area_m2 == pytest.approx(ONE_KM2, rel=1e-3)


def test_feature_crossing_antimeridian_is_measured_the_short_way():
    crossing = km_square_at(180.0, -16.5)
    longitudes = shapely.get_coordinates(crossing)[:, 0]
    assert longitudes.min() < -179 and longitudes.max() > 179

    result = measure_feature(crossing, WGS84)

    assert result.status is MeasurementStatus.MEASURED
    assert result.area_m2 == pytest.approx(ONE_KM2, rel=1e-3)
    assert abs(result.deviation_pct) < 0.1


def test_feature_spanning_most_of_the_globe_is_unsupported():
    line = LineString([(-170, 0), (0, 0), (170, 0)])

    result = measure_feature(line, WGS84)

    assert result.status is MeasurementStatus.UNSUPPORTED_GEOMETRY
    assert "half the globe" in result.reason
