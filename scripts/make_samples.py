"""Regenerate the files in samples/ so reviewers can try the API without hunting for data.

Run with: uv run python scripts/make_samples.py
"""

import sys
from pathlib import Path

import geopandas as gpd
from shapely.geometry import LineString, Point, Polygon

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from factories import (  # noqa: E402
    kml_document,
    kml_geometry,
    line_wgs84,
    multi_shapefile_zip,
    square_wgs84,
)

SAMPLES = ROOT / "samples"


def survey_kml() -> bytes:
    """A small drone-survey style KML around Shamshabad, Hyderabad, plus one check plot at Eluru."""
    field_a = square_wgs84(78.43, 17.24, 200)
    field_b = Polygon(
        [(78.4360, 17.2400), (78.4395, 17.2402), (78.4398, 17.2431), (78.4362, 17.2428)]
    )
    road = LineString([(78.4300, 17.2380), (78.4350, 17.2390), (78.4410, 17.2385)])
    pole = Point(78.4330, 17.2415)
    # Near the UTM 44N central meridian (81E), so this one is measured in UTM, not the fallback.
    reference_plot = square_wgs84(81.0, 16.71, 100)
    bowtie = Polygon(
        [(78.4420, 17.2400), (78.4440, 17.2420), (78.4440, 17.2400), (78.4420, 17.2420)]
    )

    def placemark(name: str, geom, **data) -> tuple[str, str]:
        extended = "".join(f'<Data name="{k}"><value>{v}</value></Data>' for k, v in data.items())
        return name, f"<ExtendedData>{extended}</ExtendedData>{kml_geometry(geom)}"

    return kml_document(
        folders={
            "Fields": [
                placemark("Field A", field_a, crop="paddy", owner_id=101),
                placemark("Field B", field_b, crop="cotton", owner_id=102),
                placemark("Digitising error", bowtie, crop="unknown", owner_id=0),
            ],
            "Infrastructure": [
                placemark("Access road", road, surface="gravel"),
                placemark("Survey pole", pole, surface="n/a"),
            ],
            "Reference": [
                placemark("Eluru check plot", reference_plot, crop="none", owner_id=0),
            ],
        }
    )


def utm_shapefile_zip() -> bytes:
    """Parcels and a canal in UTM 44N (EPSG:32644), the way survey software usually exports."""
    parcels = gpd.GeoDataFrame(
        {"parcel_id": ["P-001", "P-002"], "village": ["Kothur", "Kothur"]},
        geometry=[square_wgs84(78.29, 17.13, 500), square_wgs84(78.30, 17.13, 300)],
        crs="EPSG:4326",
    ).to_crs("EPSG:32644")
    canal = gpd.GeoDataFrame(
        {"name": ["Main canal"]}, geometry=[line_wgs84(78.28, 17.12, 2000)], crs="EPSG:4326"
    ).to_crs("EPSG:32644")
    return multi_shapefile_zip({"kothur/parcels": parcels, "kothur/canal": canal})


def main() -> None:
    SAMPLES.mkdir(exist_ok=True)
    (SAMPLES / "hyderabad_survey.kml").write_bytes(survey_kml())
    (SAMPLES / "kothur_parcels_utm.zip").write_bytes(utm_shapefile_zip())
    print(f"Wrote samples to {SAMPLES}")


if __name__ == "__main__":
    main()
