import os
from pathlib import Path

import pytest
from factories import (
    kml_document,
    kml_geometry,
    multi_shapefile_zip,
    sample_parcels_gdf,
    shapefile_zip,
    zip_bytes,
)

from geomeasure.config import Settings
from geomeasure.errors import InvalidUpload
from geomeasure.ingest.upload import detect_format, prepare_upload, sanitize_filename


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path)


def _prepare(content: bytes, tmp_path: Path, settings: Settings, filename: str) -> list[Path]:
    return prepare_upload(filename, content, tmp_path / "upload", settings)


@pytest.mark.parametrize(
    ("filename", "expected"),
    [("parcels.zip", "SHAPEFILE"), ("PARCELS.ZIP", "SHAPEFILE"), ("farm.Kml", "KML")],
)
def test_detect_format(filename: str, expected: str) -> None:
    assert detect_format(filename) == expected


@pytest.mark.parametrize("filename", ["parcels.geojson", "parcels", "parcels.shp", ""])
def test_detect_format_rejects_other_types(filename: str) -> None:
    with pytest.raises(InvalidUpload, match=r"\.zip .* or \.kml"):
        detect_format(filename)


def test_detect_format_explains_kmz() -> None:
    with pytest.raises(InvalidUpload, match="KMZ files are not supported yet"):
        detect_format("farm.kmz")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/pass wd.kml", "pass_wd.kml"),
        (r"C:\Users\me\Field Survey (1).zip", "Field_Survey_1_.zip"),
        ("....kml", "kml"),
        ("///", "upload"),
    ],
)
def test_sanitize_filename(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_stores_original_under_sanitized_name(tmp_path: Path, settings: Settings) -> None:
    paths = _prepare(shapefile_zip(sample_parcels_gdf()), tmp_path, settings, "../My Farm.zip")

    assert (tmp_path / "upload" / "My_Farm.zip").is_file()
    assert [p.relative_to(tmp_path / "upload").as_posix() for p in paths] == [
        "extracted/parcels.shp"
    ]


def test_kml_returns_stored_file(tmp_path: Path, settings: Settings) -> None:
    content = kml_document([("Field", kml_geometry(sample_parcels_gdf().geometry[0]))])

    paths = _prepare(content, tmp_path, settings, "farm.kml")

    assert paths == [tmp_path / "upload" / "farm.kml"]
    assert paths[0].read_bytes() == content


def test_rejects_empty_file(tmp_path: Path, settings: Settings) -> None:
    with pytest.raises(InvalidUpload, match="empty"):
        _prepare(b"", tmp_path, settings, "parcels.zip")


def test_rejects_upload_over_size_limit(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, max_upload_mb=1)
    with pytest.raises(InvalidUpload, match="exceeds the 1 MB limit"):
        _prepare(b"x" * (1024 * 1024 + 1), tmp_path, settings, "farm.kml")


def test_rejects_file_that_is_not_a_zip(tmp_path: Path, settings: Settings) -> None:
    with pytest.raises(InvalidUpload, match="not a valid zip"):
        _prepare(b"definitely not a zip", tmp_path, settings, "parcels.zip")


@pytest.mark.parametrize("member", ["../evil.shp", "a/../../evil.shp", "/abs/evil.shp", "C:/x.shp"])
def test_rejects_zip_slip(member: str, tmp_path: Path, settings: Settings) -> None:
    with pytest.raises(InvalidUpload, match="unsafe path"):
        _prepare(zip_bytes({member: b"x"}), tmp_path, settings, "parcels.zip")
    assert not (tmp_path / "evil.shp").exists()


def test_rejects_too_many_entries(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, max_zip_entries=3)
    content = zip_bytes({f"file{i}.txt": b"x" for i in range(4)})
    with pytest.raises(InvalidUpload, match="4 entries; at most 3"):
        _prepare(content, tmp_path, settings, "parcels.zip")


def test_rejects_oversized_extraction(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, max_extracted_mb=1)
    content = zip_bytes({"big.bin": os.urandom(2 * 1024 * 1024)})
    with pytest.raises(InvalidUpload, match="exceeds the 1 MB extraction limit"):
        _prepare(content, tmp_path, settings, "parcels.zip")


def test_rejects_suspicious_compression_ratio(tmp_path: Path, settings: Settings) -> None:
    content = zip_bytes({"bomb.dbf": b"\0" * (10 * 1024 * 1024)})
    with pytest.raises(InvalidUpload, match="zip bomb"):
        _prepare(content, tmp_path, settings, "parcels.zip")


def test_rejects_zip_without_shapefile(tmp_path: Path, settings: Settings) -> None:
    with pytest.raises(InvalidUpload, match=r"does not contain a Shapefile \(\.shp\)"):
        _prepare(zip_bytes({"readme.txt": b"hello"}), tmp_path, settings, "parcels.zip")


def test_names_missing_shapefile_parts(tmp_path: Path, settings: Settings) -> None:
    content = zip_bytes({"survey/parcels.shp": b"x", "survey/parcels.dbf": b"x"})
    with pytest.raises(InvalidUpload, match=r"'survey/parcels.shp' is incomplete: missing \.shx\."):
        _prepare(content, tmp_path, settings, "parcels.zip")


def test_names_all_missing_parts(tmp_path: Path, settings: Settings) -> None:
    with pytest.raises(InvalidUpload, match=r"missing \.shx and \.dbf"):
        _prepare(zip_bytes({"parcels.shp": b"x"}), tmp_path, settings, "parcels.zip")


def test_sibling_extensions_are_case_insensitive(tmp_path: Path, settings: Settings) -> None:
    content = zip_bytes({"PARCELS.SHP": b"x", "PARCELS.SHX": b"x", "PARCELS.DBF": b"x"})
    paths = _prepare(content, tmp_path, settings, "parcels.zip")
    assert [p.name for p in paths] == ["PARCELS.SHP"]


def test_finds_shapefile_in_nested_folder(tmp_path: Path, settings: Settings) -> None:
    content = shapefile_zip(sample_parcels_gdf(), folder="survey/2024")
    paths = _prepare(content, tmp_path, settings, "parcels.zip")
    assert [p.relative_to(tmp_path / "upload/extracted").as_posix() for p in paths] == [
        "survey/2024/parcels.shp"
    ]


def test_finds_every_shapefile(tmp_path: Path, settings: Settings) -> None:
    gdf = sample_parcels_gdf()
    content = multi_shapefile_zip({"parcels": gdf, "north/fields": gdf})

    paths = _prepare(content, tmp_path, settings, "parcels.zip")

    names = [p.relative_to(tmp_path / "upload/extracted").as_posix() for p in paths]
    assert names == ["north/fields.shp", "parcels.shp"]


def test_ignores_macos_junk(tmp_path: Path, settings: Settings) -> None:
    junk = {
        "__MACOSX/._parcels.shp": b"\0\5\26\7",
        "__MACOSX/survey/._other.shp": b"\0",
        ".hidden/ghost.shp": b"\0",
        "._parcels.shp": b"\0",
        ".DS_Store": b"\0",
    }
    content = shapefile_zip(sample_parcels_gdf(), extra_files=junk)

    paths = _prepare(content, tmp_path, settings, "parcels.zip")

    extracted = tmp_path / "upload/extracted"
    assert [p.name for p in paths] == ["parcels.shp"]
    assert not (extracted / "__MACOSX").exists()
    assert not (extracted / ".DS_Store").exists()


def test_rejects_kml_that_is_not_xml(tmp_path: Path, settings: Settings) -> None:
    with pytest.raises(InvalidUpload, match="not valid XML"):
        _prepare(b"<kml><Document><Placemark>", tmp_path, settings, "farm.kml")


def test_rejects_xml_that_is_not_kml(tmp_path: Path, settings: Settings) -> None:
    with pytest.raises(InvalidUpload, match="root element is <html>"):
        _prepare(b"<html><body/></html>", tmp_path, settings, "farm.kml")
