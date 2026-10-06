"""Turn raw upload bytes into files on disk that GDAL can open.

Everything that can be rejected without GDAL is rejected here as `InvalidUpload`, so the
reader only ever sees inputs that are structurally sane.
"""

import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Literal
from xml.etree import ElementTree

from geomeasure.config import Settings
from geomeasure.errors import InvalidUpload

UploadFormat = Literal["SHAPEFILE", "KML"]

EXTRACT_DIRNAME = "extracted"
ACCEPTED_TYPES = ".zip (containing a Shapefile) or .kml"
SHAPEFILE_REQUIRED_SIBLINGS = (".shx", ".dbf")

_MB = 1024 * 1024
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_FILENAME_LENGTH = 120


def detect_format(filename: str) -> UploadFormat:
    suffix = Path(filename or "").suffix.lower()
    if suffix == ".zip":
        return "SHAPEFILE"
    if suffix == ".kml":
        return "KML"
    if suffix == ".kmz":
        raise InvalidUpload(
            f"KMZ files are not supported yet. Unzip it and upload the .kml inside, "
            f"or upload a {ACCEPTED_TYPES}."
        )
    shown = suffix or "no extension"
    raise InvalidUpload(f"Unsupported file type ({shown}). Upload a {ACCEPTED_TYPES}.")


def prepare_upload(filename: str, content: bytes, dest_dir: Path, settings: Settings) -> list[Path]:
    """Store the upload in `dest_dir` and return the dataset paths the reader should open.

    For a zip that is every `.shp` inside it (nested folders included); for a KML it is
    the stored file itself.
    """
    upload_format = detect_format(filename)
    _check_size(content, settings)

    dest_dir.mkdir(parents=True, exist_ok=True)
    stored = dest_dir / sanitize_filename(filename)
    stored.write_bytes(content)

    if upload_format == "KML":
        _check_kml(content)
        return [stored]

    extract_root = dest_dir / EXTRACT_DIRNAME
    _extract_zip(stored, extract_root, settings)
    return _find_shapefiles(extract_root)


def sanitize_filename(filename: str) -> str:
    """Reduce a client-supplied name to a safe basename, keeping its extension."""
    basename = re.split(r"[\\/]", filename or "")[-1]
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", basename).strip("._")
    if not cleaned:
        return "upload" + Path(basename).suffix.lower()
    if len(cleaned) > _MAX_FILENAME_LENGTH:
        suffix = Path(cleaned).suffix[:10]
        cleaned = cleaned[: _MAX_FILENAME_LENGTH - len(suffix)] + suffix
    return cleaned


def _check_size(content: bytes, settings: Settings) -> None:
    if not content:
        raise InvalidUpload("The uploaded file is empty.")
    if len(content) > settings.max_upload_mb * _MB:
        raise InvalidUpload(
            f"The upload is {len(content) / _MB:.1f} MB, which exceeds the "
            f"{settings.max_upload_mb} MB limit."
        )


def _check_kml(content: bytes) -> None:
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise InvalidUpload(f"The KML file is not valid XML: {exc}.") from None
    if _local_name(root.tag) != "kml":
        raise InvalidUpload(
            f"The file is XML but not KML: the root element is <{_local_name(root.tag)}>, "
            "expected <kml>."
        )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _extract_zip(archive_path: Path, extract_root: Path, settings: Settings) -> None:
    try:
        archive = zipfile.ZipFile(archive_path)
    except zipfile.BadZipFile:
        raise InvalidUpload(
            "The upload has a .zip extension but is not a valid zip archive."
        ) from None

    with archive:
        members = archive.infolist()
        _check_archive_limits(members, archive_path.stat().st_size, settings)

        extract_root.mkdir(parents=True, exist_ok=True)
        budget = settings.max_extracted_mb * _MB
        for member in members:
            target = _safe_target(extract_root, member.filename)
            if member.is_dir() or _is_junk(member.filename):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            budget -= _copy_member(archive, member, target, budget)


def _check_archive_limits(
    members: list[zipfile.ZipInfo], archive_size: int, settings: Settings
) -> None:
    if len(members) > settings.max_zip_entries:
        raise InvalidUpload(
            f"The zip contains {len(members)} entries; at most {settings.max_zip_entries} "
            "are allowed."
        )

    total_size = sum(member.file_size for member in members)
    if total_size > settings.max_extracted_mb * _MB:
        raise InvalidUpload(
            f"The zip expands to {total_size / _MB:.1f} MB, which exceeds the "
            f"{settings.max_extracted_mb} MB extraction limit."
        )

    ratio = total_size / max(archive_size, 1)
    if ratio > settings.max_compression_ratio:
        raise InvalidUpload(
            f"The zip has a compression ratio of {ratio:.0f}:1, above the allowed "
            f"{settings.max_compression_ratio}:1. It looks like a zip bomb."
        )


def _safe_target(extract_root: Path, member_name: str) -> Path:
    """Resolve where a zip member would land, refusing anything outside `extract_root`."""
    normalized = member_name.replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    is_absolute = normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized)
    if is_absolute or ".." in parts:
        raise InvalidUpload(f"The zip contains an unsafe path: {member_name!r}.")

    root = extract_root.resolve()
    target = root.joinpath(*parts).resolve()
    if not target.is_relative_to(root):
        raise InvalidUpload(f"The zip contains an unsafe path: {member_name!r}.")
    return target


def _is_junk(member_name: str) -> bool:
    parts = PurePosixPath(member_name.replace("\\", "/")).parts
    return any(part == "__MACOSX" or part.startswith(".") for part in parts)


def _copy_member(
    archive: zipfile.ZipFile, member: zipfile.ZipInfo, target: Path, budget: int
) -> int:
    """Stream one member to disk and return the bytes written.

    The sizes in the zip header are only claims, so the real output is counted against
    the remaining extraction budget as it is written.
    """
    written = 0
    with archive.open(member) as source, target.open("wb") as sink:
        while chunk := source.read(_MB):
            written += len(chunk)
            if written > budget:
                raise InvalidUpload(
                    "The zip expands beyond the extraction limit; its size headers are wrong."
                )
            sink.write(chunk)
    return written


def _find_shapefiles(extract_root: Path) -> list[Path]:
    shapefiles = sorted(
        path for path in extract_root.rglob("*") if path.is_file() and path.suffix.lower() == ".shp"
    )
    if not shapefiles:
        raise InvalidUpload("The zip does not contain a Shapefile (.shp).")

    for shp in shapefiles:
        missing = _missing_siblings(shp)
        if missing:
            shown = shp.relative_to(extract_root).as_posix()
            raise InvalidUpload(
                f"Shapefile {shown!r} is incomplete: missing {' and '.join(missing)}. "
                "A Shapefile needs its .shp, .shx and .dbf files together."
            )
    return shapefiles


def _missing_siblings(shp: Path) -> list[str]:
    stem = shp.stem.lower()
    present = {
        path.suffix.lower()
        for path in shp.parent.iterdir()
        if path.is_file() and path.stem.lower() == stem
    }
    return [ext for ext in SHAPEFILE_REQUIRED_SIBLINGS if ext not in present]
