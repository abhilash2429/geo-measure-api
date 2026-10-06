import pytest
from factories import (
    kml_document,
    kml_geometry,
    line_wgs84,
    multi_shapefile_zip,
    sample_parcels_gdf,
    sample_survey_zip,
    shapefile_zip,
    square_wgs84,
    zip_bytes,
)
from fastapi.testclient import TestClient
from httpx import Response
from shapely.geometry import Polygon, shape

from geomeasure.config import Settings
from geomeasure.errors import ProcessingError

FILE_DETAIL_KEYS = {
    "id",
    "filename",
    "format",
    "status",
    "error",
    "crs",
    "crs_assumed",
    "feature_count",
    "layer_count",
    "size_bytes",
    "layers",
    "geometry_types",
    "created_at",
    "completed_at",
    "processing_ms",
}


def upload(client: TestClient, filename: str, content: bytes) -> Response:
    return client.post("/api/files/", files={"file": (filename, content)})


def upload_survey(client: TestClient) -> dict:
    """Upload the sample survey: index 0 is a canal, 1-3 are parcels, 4 is a well."""
    response = upload(client, "survey.zip", sample_survey_zip())
    assert response.status_code == 201, response.text
    return response.json()


def measurements(client: TestClient, file_id: str, **params) -> dict:
    response = client.get(f"/api/files/{file_id}/measurements/", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def assert_close(actual: float, expected: float) -> None:
    assert actual == pytest.approx(expected, rel=0.001)


def test_upload_shapefile_returns_file_detail(client: TestClient) -> None:
    body = upload_survey(client)

    assert set(body) == FILE_DETAIL_KEYS
    assert body["filename"] == "survey.zip"
    assert body["format"] == "SHAPEFILE"
    assert body["status"] == "COMPLETED"
    assert body["error"] is None
    assert body["crs"] == "EPSG:4326"
    assert body["crs_assumed"] is False
    assert body["feature_count"] == 5
    assert body["layer_count"] == 3
    assert body["layers"] == [
        {"name": "infra/canals", "feature_count": 1, "crs": "EPSG:4326", "crs_assumed": False},
        {"name": "parcels", "feature_count": 3, "crs": "EPSG:4326", "crs_assumed": False},
        {"name": "wells", "feature_count": 1, "crs": "EPSG:4326", "crs_assumed": False},
    ]
    assert body["geometry_types"] == {"Polygon": 3, "LineString": 1, "Point": 1}
    assert body["processing_ms"] >= 0

    detail = client.get(f"/api/files/{body['id']}/")
    assert detail.status_code == 200
    assert detail.json() == body


def test_measurements_match_known_shapes(client: TestClient) -> None:
    file_id = upload_survey(client)["id"]

    page = measurements(client, file_id)
    results = {item["properties"]["name"]: item for item in page["results"]}

    assert page["file_id"] == file_id
    assert page["count"] == 5
    assert [item["index"] for item in page["results"]] == [0, 1, 2, 3, 4]

    field_a = results["Field A"]
    assert field_a["geometry_type"] == "Polygon"
    assert field_a["source_crs"] == "EPSG:4326"
    assert field_a["layer"] == "parcels"
    assert field_a["geometry"]["type"] == "Polygon"
    assert field_a["measurement"]["status"] == "MEASURED"
    assert_close(field_a["measurement"]["area_m2"], 10_000)
    assert_close(field_a["measurement"]["perimeter_m"], 400)
    assert field_a["measurement"]["length_m"] is None
    assert field_a["measurement"]["projected_crs"]

    assert_close(results["Field B"]["measurement"]["area_m2"], 2_500)
    assert_close(results["Field C"]["measurement"]["area_m2"], 400)

    canal = results["Canal"]["measurement"]
    assert canal["status"] == "MEASURED"
    assert_close(canal["length_m"], 250)
    assert canal["area_m2"] is None

    well = results["Well"]["measurement"]
    assert well["status"] == "NOT_APPLICABLE"
    assert well["area_m2"] is None
    assert well["length_m"] is None

    summary = page["summary"]
    assert_close(summary["total_area_m2"], 12_900)
    assert_close(summary["total_length_m"], 250)
    assert summary["measured"] == 4
    assert summary["skipped"] == {"NOT_APPLICABLE": 1}


def test_projected_shapefile_is_returned_in_wgs84(client: TestClient) -> None:
    gdf = sample_parcels_gdf().to_crs("EPSG:32643")
    response = upload(client, "utm.zip", shapefile_zip(gdf))
    assert response.status_code == 201, response.text
    assert response.json()["crs"] == "EPSG:32643"

    field_a = measurements(client, response.json()["id"])["results"][0]
    assert field_a["source_crs"] == "EPSG:32643"
    centroid = shape(field_a["geometry"]).centroid
    assert centroid.x == pytest.approx(77.5946, abs=1e-4)
    assert centroid.y == pytest.approx(12.9716, abs=1e-4)
    assert_close(field_a["measurement"]["area_m2"], 10_000)


def test_coordinates_outside_the_declared_crs_do_not_break_the_file(client: TestClient) -> None:
    gdf = sample_parcels_gdf().to_crs("EPSG:32644")
    gdf.loc[0, "geometry"] = Polygon([(1e12, 1e12), (1e12 + 10, 1e12), (1e12 + 10, 1e12 + 10)])
    response = upload(client, "bad_coords.zip", shapefile_zip(gdf))
    assert response.status_code == 201, response.text

    results = measurements(client, response.json()["id"])["results"]
    assert results[0]["geometry"] is None
    assert results[0]["measurement"]["status"] == "INVALID_GEOMETRY"
    assert results[1]["measurement"]["status"] == "MEASURED"


def test_layers_in_different_crs_report_mixed(client: TestClient) -> None:
    parcels = sample_parcels_gdf()
    content = multi_shapefile_zip({"wgs84": parcels, "utm": parcels.to_crs("EPSG:32643")})

    response = upload(client, "mixed.zip", content)

    assert response.status_code == 201, response.text
    assert response.json()["crs"] == "MIXED"
    assert {layer["crs"] for layer in response.json()["layers"]} == {"EPSG:4326", "EPSG:32643"}


def test_upload_kml(client: TestClient) -> None:
    lon, lat = 77.5946, 12.9716
    content = kml_document(
        [
            ("Plot", kml_geometry(square_wgs84(lon, lat, 200))),
            ("Road", kml_geometry(line_wgs84(lon, lat + 0.01, 500))),
        ]
    )
    response = upload(client, "survey.kml", content)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["format"] == "KML"
    assert body["crs"] == "EPSG:4326"
    assert body["feature_count"] == 2

    results = measurements(client, body["id"])["results"]
    assert_close(results[0]["measurement"]["area_m2"], 40_000)
    assert_close(results[1]["measurement"]["length_m"], 500)


def test_unsupported_extension_is_rejected_and_not_stored(client: TestClient) -> None:
    response = upload(client, "notes.txt", b"hello")

    assert response.status_code == 400
    assert "Unsupported file type" in response.json()["detail"]
    assert client.get("/api/files/").json()["count"] == 0


def test_corrupt_zip_is_rejected_and_not_stored(client: TestClient, settings: Settings) -> None:
    response = upload(client, "parcels.zip", b"this is not a zip archive")

    assert response.status_code == 400
    assert response.json()["detail"]
    assert client.get("/api/files/").json()["count"] == 0
    assert list(settings.upload_dir.iterdir()) == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/files/nope/"),
        ("GET", "/api/files/nope/measurements/"),
        ("GET", "/api/files/nope/features.geojson"),
        ("DELETE", "/api/files/nope/"),
    ],
)
def test_unknown_file_returns_404(client: TestClient, method: str, path: str) -> None:
    response = client.request(method, path)
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_unreadable_file_is_kept_as_failed(client: TestClient, monkeypatch) -> None:
    def unreadable(paths):
        raise ProcessingError("Could not read parcels.shp: corrupt header")

    monkeypatch.setattr("geomeasure.pipeline.read_layers", unreadable)
    response = upload(client, "parcels.zip", shapefile_zip(sample_parcels_gdf()))

    assert response.status_code == 422
    body = response.json()
    assert body["status"] == "FAILED"
    assert body["error"] == "Could not read parcels.shp: corrupt header"
    assert body["feature_count"] == 0

    detail = client.get(f"/api/files/{body['id']}/")
    assert detail.status_code == 200
    assert detail.json()["status"] == "FAILED"

    for path in ("measurements/", "features.geojson"):
        conflict = client.get(f"/api/files/{body['id']}/{path}")
        assert conflict.status_code == 409
        assert "FAILED" in conflict.json()["detail"]


def test_shapefile_gdal_cannot_read_is_kept_as_failed(
    client: TestClient, settings: Settings
) -> None:
    junk = {name: b"\x00" * 200 for name in ("survey/bad.shp", "survey/bad.shx", "survey/bad.dbf")}
    response = upload(client, "bad.zip", zip_bytes(junk))

    assert response.status_code == 422, response.text
    assert response.json()["status"] == "FAILED"
    error = response.json()["error"]
    assert error.startswith("Could not read survey/bad.shp")
    assert str(settings.data_dir.resolve().parent) not in error
    assert settings.data_dir.resolve().parent.as_posix() not in error


def test_unexpected_error_is_kept_as_failed_without_leaking_details(
    client: TestClient, monkeypatch
) -> None:
    def broken(paths):
        raise RuntimeError("secret internal state")

    monkeypatch.setattr("geomeasure.pipeline.read_layers", broken)
    response = upload(client, "parcels.zip", shapefile_zip(sample_parcels_gdf()))

    assert response.status_code == 422
    assert response.json()["status"] == "FAILED"
    assert "secret" not in response.json()["error"]


def test_measurements_pagination(client: TestClient) -> None:
    file_id = upload_survey(client)["id"]

    page = measurements(client, file_id, limit=2, offset=1)

    assert page["count"] == 5
    assert page["limit"] == 2
    assert page["offset"] == 1
    assert [item["index"] for item in page["results"]] == [1, 2]
    assert page["summary"]["measured"] == 4


def test_measurements_filters(client: TestClient) -> None:
    file_id = upload_survey(client)["id"]

    measured = measurements(client, file_id, status="MEASURED")
    assert measured["count"] == 4
    assert {item["measurement"]["status"] for item in measured["results"]} == {"MEASURED"}

    polygons = measurements(client, file_id, geometry_type="Polygon")
    assert [item["geometry_type"] for item in polygons["results"]] == ["Polygon"] * 3

    without_geometry = measurements(client, file_id, include_geometry=False)
    assert all(item["geometry"] is None for item in without_geometry["results"])


@pytest.mark.parametrize("params", [{"limit": 1001}, {"limit": 0}, {"offset": -1}, {"status": "X"}])
def test_measurements_rejects_bad_query_params(client: TestClient, params: dict) -> None:
    file_id = upload_survey(client)["id"]
    response = client.get(f"/api/files/{file_id}/measurements/", params=params)
    assert response.status_code == 422


def test_geojson_export(client: TestClient) -> None:
    file_id = upload_survey(client)["id"]

    response = client.get(f"/api/files/{file_id}/features.geojson")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/geo+json")
    collection = response.json()
    assert collection["type"] == "FeatureCollection"
    assert len(collection["features"]) == 5

    field_a = collection["features"][1]
    assert field_a["type"] == "Feature"
    assert field_a["id"] == 1
    assert field_a["geometry"]["type"] == "Polygon"
    assert field_a["properties"]["name"] == "Field A"
    assert field_a["properties"]["status"] == "MEASURED"
    assert field_a["properties"]["layer"] == "parcels"
    assert_close(field_a["properties"]["area_m2"], 10_000)


def test_list_files_newest_first(client: TestClient) -> None:
    first = upload_survey(client)["id"]
    second = upload_survey(client)["id"]

    listing = client.get("/api/files/").json()
    assert listing["count"] == 2
    assert [item["id"] for item in listing["results"]] == [second, first]

    paged = client.get("/api/files/", params={"limit": 1, "offset": 1}).json()
    assert [item["id"] for item in paged["results"]] == [first]


def test_delete_removes_record_and_stored_files(client: TestClient, settings: Settings) -> None:
    file_id = upload_survey(client)["id"]
    assert (settings.upload_dir / file_id).exists()

    response = client.delete(f"/api/files/{file_id}/")

    assert response.status_code == 204
    assert client.get(f"/api/files/{file_id}/").status_code == 404
    assert not (settings.upload_dir / file_id).exists()


def test_health_and_root_redirect(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}

    root = client.get("/", follow_redirects=False)
    assert root.status_code == 307
    assert root.headers["location"] == "/docs"
