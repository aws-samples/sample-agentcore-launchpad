"""Artifact discovery and immutable evidence snapshots for SkillEval."""
from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import stat
import struct
import subprocess
import zipfile
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath

_SKIP_ROOTS = {".agents", ".claude", ".codex", ".git"}
_SKIP_ROOT_FILES = {"task.md", "codex_last_message.txt"}
_SUPPORTED_KINDS = {"xlsx", "xls", "docx", "doc", "pdf", "image", "pptx", "ppt"}
_SUFFIX_KINDS = {
    ".xlsx": "xlsx",
    ".xls": "xls",
    ".docx": "docx",
    ".doc": "doc",
    ".pdf": "pdf",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
    ".tif": "image",
    ".tiff": "image",
    ".pptx": "pptx",
    ".ppt": "ppt",
}
_SPECIFIC_MIME_KINDS = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel": "xls",
    "application/msexcel": "xls",
    "application/x-msexcel": "xls",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.ms-powerpoint": "ppt",
    "application/mspowerpoint": "ppt",
    "application/x-mspowerpoint": "ppt",
    "application/pdf": "pdf",
    "application/x-pdf": "pdf",
    "image/png": "image",
    "image/jpeg": "image",
    "image/jpg": "image",
    "image/pjpeg": "image",
    "image/webp": "image",
    "image/tiff": "image",
    "image/x-tiff": "image",
}
_GENERIC_MIMES = {
    "",
    "application/octet-stream",
    "binary/octet-stream",
    "application/zip",
    "application/x-zip",
    "application/x-zip-compressed",
    "multipart/x-zip",
    "application/x-ole-storage",
    "application/vnd.ms-office",
    "application/cdfv2",
    "application/x-cdf",
    "application/x-empty",
    "inode/x-empty",
}
_ZIP_MIMES = {
    "application/zip",
    "application/x-zip",
    "application/x-zip-compressed",
    "multipart/x-zip",
}
_OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_OOXML_MARKERS = {
    "xlsx": b"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
    "docx": b"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    "pptx": b"application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
}
_OOXML_PREFIXES = {"xlsx": "xl/", "docx": "word/", "pptx": "ppt/"}
_HASH_CHUNK_SIZE = 1024 * 1024
_DEFAULT_MAX_BYTES = 100 * 1024 * 1024
_ZIP_EOCD_SIZE = 22
_ZIP_MAX_COMMENT_BYTES = 65535
_ZIP64_EOCD_LOCATOR_SIZE = 20
_ZIP64_EOCD_FIXED_SIZE = 56
_MAX_ZIP64_EOCD_BYTES = 1024 * 1024
_ZIP_LOCAL_FILE_HEADER = struct.Struct("<4s5H3L2H")
_ZIP_CENTRAL_DIRECTORY_HEADER = struct.Struct("<4s6H3L5H2L")
_ZIP64_EXTRA_ID = 0x0001
_ZIP32_MAX = 0xFFFFFFFF
_MAX_OOXML_ENTRIES = 10_000
_MAX_OOXML_CENTRAL_DIRECTORY_BYTES = 16 * 1024 * 1024
_HAS_REQUIRED_DIR_FD = all(
    function in os.supports_dir_fd
    for function in (os.open, os.stat, os.mkdir, os.unlink, os.rmdir)
)
_HAS_STAT_NOFOLLOW = os.stat in os.supports_follow_symlinks
_HAS_SCANDIR_FD = os.scandir in os.supports_fd


@dataclass(frozen=True)
class ManifestEntry:
    path: str
    size: int
    sha256: str
    mime: str
    kind: str | None


@dataclass(frozen=True)
class EvidenceSnapshot:
    evidence_dir: str
    scratch_dir: str
    tree_hash: str
    files: tuple[ManifestEntry, ...]


class ArtifactValidationError(ValueError):
    """Raised when target-produced filesystem state is invalid."""


class ArtifactCollectionError(RuntimeError):
    """Raised when evaluator infrastructure cannot securely collect artifacts."""


class ArtifactConfigurationError(ArtifactCollectionError, ValueError):
    """Raised when evaluator paths or configuration are unsafe."""


class EvidenceLimitError(ArtifactValidationError):
    """Raised when candidate evidence exceeds its configured byte limit."""


@dataclass
class _JudgeDirs:
    root_path: str
    evidence_path: str
    scratch_path: str
    root_name: str
    parent_descriptor: int
    root_descriptor: int
    evidence_descriptor: int
    scratch_descriptor: int

    def close_children(self) -> None:
        for name in ("scratch_descriptor", "evidence_descriptor", "root_descriptor"):
            descriptor = getattr(self, name)
            if descriptor >= 0:
                os.close(descriptor)
                setattr(self, name, -1)

    def close(self) -> None:
        self.close_children()
        if self.parent_descriptor >= 0:
            os.close(self.parent_descriptor)
            self.parent_descriptor = -1


def _normalized_mime(mime: str | None) -> str:
    return (mime or "").split(";", 1)[0].strip().lower()


def _require_secure_filesystem_capabilities() -> None:
    missing: list[str] = []
    for name in ("O_NOFOLLOW", "O_DIRECTORY"):
        if not getattr(os, name, 0):
            missing.append(name)
    if not _HAS_REQUIRED_DIR_FD:
        missing.append("dir_fd")
    if not _HAS_STAT_NOFOLLOW:
        missing.append("follow_symlinks=False")
    if not _HAS_SCANDIR_FD:
        missing.append("scandir(fd)")
    if missing:
        raise ArtifactCollectionError(
            "secure artifact collection requires POSIX capabilities: "
            + ", ".join(missing)
        )


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _file_flags() -> int:
    return os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)


def _raise_artifact_open_error(
    exc: OSError,
    *,
    validation_message: str,
    collection_message: str,
) -> None:
    if exc.errno in {errno.ELOOP, errno.ENOENT, errno.ENOTDIR}:
        raise ArtifactValidationError(validation_message) from exc
    raise ArtifactCollectionError(f"{collection_message}: {exc}") from exc


def _stat_identity(info: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_nlink,
    )


def _mime_from_descriptor(descriptor: int) -> str:
    position = os.lseek(descriptor, 0, os.SEEK_CUR)
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            proc = subprocess.run(
                ["file", "--brief", "--mime-type", "-"],
                stdin=descriptor,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return _normalized_mime(proc.stdout)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return "application/octet-stream"
    finally:
        os.lseek(descriptor, position, os.SEEK_SET)


def _safe_zip_member(name: str) -> bool:
    if not name or "\x00" in name or "\\" in name:
        return False
    path = PurePosixPath(name)
    return not path.is_absolute() and ".." not in path.parts


def _zip64_extra_payload(extra: bytes) -> tuple[bool, bytes | None]:
    offset = 0
    payload = None
    while offset < len(extra):
        if len(extra) - offset < 4:
            return False, None
        field_id, field_size = struct.unpack_from("<HH", extra, offset)
        offset += 4
        field_end = offset + field_size
        if field_end > len(extra):
            return False, None
        if field_id == _ZIP64_EXTRA_ID:
            if payload is not None:
                return False, None
            payload = extra[offset:field_end]
        offset = field_end
    return True, payload


def _resolve_central_zip64_fields(
    compressed_size: int,
    uncompressed_size: int,
    local_header_offset: int,
    disk_number: int,
    extra: bytes,
) -> tuple[int, int, int, int] | None:
    needs_zip64 = (
        compressed_size == _ZIP32_MAX
        or uncompressed_size == _ZIP32_MAX
        or local_header_offset == _ZIP32_MAX
        or disk_number == 0xFFFF
    )
    valid, payload = _zip64_extra_payload(extra)
    if not valid or (needs_zip64 and payload is None):
        return None
    if payload is None:
        return (
            compressed_size,
            uncompressed_size,
            local_header_offset,
            disk_number,
        )

    offset = 0

    def read_u64(value: int) -> int | None:
        nonlocal offset
        if value != _ZIP32_MAX:
            return value
        if len(payload) - offset < 8:
            return None
        resolved = struct.unpack_from("<Q", payload, offset)[0]
        offset += 8
        return resolved

    resolved_uncompressed = read_u64(uncompressed_size)
    resolved_compressed = read_u64(compressed_size)
    resolved_local_offset = read_u64(local_header_offset)
    if (
        resolved_uncompressed is None
        or resolved_compressed is None
        or resolved_local_offset is None
    ):
        return None
    if disk_number == 0xFFFF:
        if len(payload) - offset < 4:
            return None
        disk_number = struct.unpack_from("<L", payload, offset)[0]
    return (
        resolved_compressed,
        resolved_uncompressed,
        resolved_local_offset,
        disk_number,
    )


def _candidate_has_canonical_zip_layout(
    descriptor: int,
    physical_eocd_offset: int,
    archive_prefix: int,
    central_offset: int,
    central_size: int,
    total_entries: int,
) -> bool:
    central_start = archive_prefix + central_offset
    if (
        archive_prefix < 0
        or central_start < archive_prefix
        or central_start + central_size != physical_eocd_offset
        or total_entries > _MAX_OOXML_ENTRIES
        or central_size > _MAX_OOXML_CENTRAL_DIRECTORY_BYTES
    ):
        return False

    cursor = central_start
    local_regions: list[tuple[int, int]] = []
    local_offsets: set[int] = set()
    for _ in range(total_entries):
        if cursor + _ZIP_CENTRAL_DIRECTORY_HEADER.size > physical_eocd_offset:
            return False
        fixed = _read_descriptor(
            descriptor,
            _ZIP_CENTRAL_DIRECTORY_HEADER.size,
            cursor,
        )
        if len(fixed) != _ZIP_CENTRAL_DIRECTORY_HEADER.size:
            return False
        fields = _ZIP_CENTRAL_DIRECTORY_HEADER.unpack(fixed)
        if fields[0] != b"PK\x01\x02":
            return False
        (
            filename_size,
            extra_size,
            comment_size,
            central_disk,
        ) = fields[10:14]
        variable_size = filename_size + extra_size + comment_size
        entry_end = (
            cursor
            + _ZIP_CENTRAL_DIRECTORY_HEADER.size
            + variable_size
        )
        if entry_end > physical_eocd_offset:
            return False
        variable = _read_descriptor(
            descriptor,
            variable_size,
            cursor + _ZIP_CENTRAL_DIRECTORY_HEADER.size,
        )
        if len(variable) != variable_size:
            return False
        filename = variable[:filename_size]
        extra = variable[filename_size:filename_size + extra_size]
        resolved = _resolve_central_zip64_fields(
            fields[8],
            fields[9],
            fields[16],
            central_disk,
            extra,
        )
        if resolved is None:
            return False
        compressed_size, _uncompressed_size, local_offset, disk_number = resolved
        physical_local_offset = archive_prefix + local_offset
        if (
            disk_number != 0
            or physical_local_offset in local_offsets
            or physical_local_offset < archive_prefix
            or physical_local_offset + _ZIP_LOCAL_FILE_HEADER.size
            > central_start
        ):
            return False
        local_offsets.add(physical_local_offset)
        local_fixed = _read_descriptor(
            descriptor,
            _ZIP_LOCAL_FILE_HEADER.size,
            physical_local_offset,
        )
        if len(local_fixed) != _ZIP_LOCAL_FILE_HEADER.size:
            return False
        local_fields = _ZIP_LOCAL_FILE_HEADER.unpack(local_fixed)
        if (
            local_fields[0] != b"PK\x03\x04"
            or local_fields[2] != fields[3]
            or local_fields[3] != fields[4]
        ):
            return False
        local_filename_size, local_extra_size = local_fields[-2:]
        local_variable_size = local_filename_size + local_extra_size
        data_start = (
            physical_local_offset
            + _ZIP_LOCAL_FILE_HEADER.size
            + local_variable_size
        )
        if data_start > central_start:
            return False
        local_variable = _read_descriptor(
            descriptor,
            local_variable_size,
            physical_local_offset + _ZIP_LOCAL_FILE_HEADER.size,
        )
        if (
            len(local_variable) != local_variable_size
            or local_filename_size != filename_size
            or local_variable[:local_filename_size] != filename
        ):
            return False
        data_end = data_start + compressed_size
        if data_end > central_start:
            return False
        local_regions.append((physical_local_offset, data_end))
        cursor = entry_end

    if cursor != physical_eocd_offset:
        return False
    ordered_regions = sorted(local_regions)
    return all(
        data_end <= (
            ordered_regions[index + 1][0]
            if index + 1 < len(ordered_regions)
            else central_start
        )
        for index, (_header_start, data_end) in enumerate(ordered_regions)
    )


def _find_zip64_eocd_record(
    descriptor: int,
    locator_offset: int,
    zip64_offset: int,
    classic_fields: tuple[int, int, int, int, int, int],
) -> tuple[int, bytes, int] | None:
    window_start = max(0, locator_offset - _MAX_ZIP64_EOCD_BYTES)
    window = _read_descriptor(
        descriptor,
        locator_offset - window_start,
        window_start,
    )
    if len(window) != locator_offset - window_start:
        return None

    candidates: list[tuple[int, bytes, int]] = []
    search_from = 0
    while True:
        index = window.find(b"PK\x06\x06", search_from)
        if index < 0:
            break
        search_from = index + 1
        if len(window) - index < _ZIP64_EOCD_FIXED_SIZE:
            continue
        record_size = struct.unpack_from("<Q", window, index + 4)[0]
        total_size = 12 + record_size
        physical_offset = window_start + index
        if not (
            record_size >= 44
            and total_size <= _MAX_ZIP64_EOCD_BYTES
            and physical_offset + total_size == locator_offset
        ):
            continue
        fixed = window[index:index + _ZIP64_EOCD_FIXED_SIZE]
        try:
            (
                signature,
                parsed_record_size,
                version_made,
                version_needed,
                disk_number,
                central_disk,
                entries_on_disk,
                total_entries,
                central_size,
                central_offset,
            ) = struct.unpack("<4sQ2H2L4Q", fixed)
        except struct.error:
            continue
        archive_prefix = physical_offset - zip64_offset
        central_start = central_offset + archive_prefix
        classic_pairs = tuple(
            zip(
                classic_fields,
                (
                    0xFFFF,
                    0xFFFF,
                    0xFFFF,
                    0xFFFF,
                    0xFFFFFFFF,
                    0xFFFFFFFF,
                ),
                (
                    disk_number,
                    central_disk,
                    entries_on_disk,
                    total_entries,
                    central_size,
                    central_offset,
                ),
            )
        )
        if (
            signature != b"PK\x06\x06"
            or parsed_record_size != record_size
            or not 45 <= (version_made & 0xFF) <= 63
            or not 45 <= version_needed <= 63
            or disk_number != 0
            or central_disk != 0
            or entries_on_disk != total_entries
            or total_entries > _MAX_OOXML_ENTRIES
            or central_size > _MAX_OOXML_CENTRAL_DIRECTORY_BYTES
            or central_offset + central_size != zip64_offset
            or archive_prefix < 0
            or central_start < archive_prefix
            or central_start + central_size != physical_offset
            or any(
                classic not in {sentinel, resolved}
                for classic, sentinel, resolved in classic_pairs
            )
            or not _candidate_has_canonical_zip_layout(
                descriptor,
                physical_offset,
                archive_prefix,
                central_offset,
                central_size,
                total_entries,
            )
        ):
            continue
        candidates.append((physical_offset, fixed, total_size))
    return candidates[0] if len(candidates) == 1 else None


def _validate_zipfile_bounds(
    descriptor: int,
    central_start: int,
    total_entries: int,
    archive_prefix: int,
) -> bool:
    try:
        with os.fdopen(os.dup(descriptor), "rb") as source:
            with zipfile.ZipFile(source) as archive:
                members = archive.infolist()
                if (
                    len(members) != total_entries
                    or archive.start_dir != central_start
                ):
                    return False
                ordered = sorted(
                    members,
                    key=lambda member: member.header_offset,
                )
                for index, member in enumerate(ordered):
                    header_offset = member.header_offset
                    boundary = (
                        ordered[index + 1].header_offset
                        if index + 1 < len(ordered)
                        else central_start
                    )
                    if (
                        header_offset < archive_prefix
                        or header_offset + _ZIP_LOCAL_FILE_HEADER.size
                        > boundary
                        or boundary > central_start
                    ):
                        return False
                    fixed = _read_descriptor(
                        descriptor,
                        _ZIP_LOCAL_FILE_HEADER.size,
                        header_offset,
                    )
                    if len(fixed) != _ZIP_LOCAL_FILE_HEADER.size:
                        return False
                    fields = _ZIP_LOCAL_FILE_HEADER.unpack(fixed)
                    if fields[0] != b"PK\x03\x04":
                        return False
                    filename_size, extra_size = fields[-2:]
                    data_start = (
                        header_offset
                        + _ZIP_LOCAL_FILE_HEADER.size
                        + filename_size
                        + extra_size
                    )
                    if (
                        data_start > boundary
                        or data_start + member.compress_size > boundary
                    ):
                        return False
    except (
        OSError,
        RuntimeError,
        NotImplementedError,
        ValueError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ):
        return False
    return True


def _preflight_zip_descriptor(
    descriptor: int,
    *,
    validate_local_bounds: bool = True,
) -> bool:
    file_size = os.fstat(descriptor).st_size
    tail_size = min(file_size, _ZIP_EOCD_SIZE + _ZIP_MAX_COMMENT_BYTES)
    if tail_size < _ZIP_EOCD_SIZE:
        return False
    tail_offset = file_size - tail_size
    tail = _read_descriptor(descriptor, tail_size, tail_offset)
    eocd_index = tail.rfind(b"PK\x05\x06")
    if eocd_index < 0 or eocd_index + _ZIP_EOCD_SIZE > len(tail):
        return False
    try:
        (
            _signature,
            disk_number,
            central_directory_disk,
            entries_on_disk,
            total_entries,
            central_directory_size,
            central_directory_offset,
            comment_size,
        ) = struct.unpack_from("<4s4H2LH", tail, eocd_index)
    except struct.error:
        return False
    if eocd_index + _ZIP_EOCD_SIZE + comment_size != len(tail):
        return False
    needs_zip64 = (
        disk_number == 0xFFFF
        or central_directory_disk == 0xFFFF
        or entries_on_disk == 0xFFFF
        or total_entries == 0xFFFF
        or central_directory_size == 0xFFFFFFFF
        or central_directory_offset == 0xFFFFFFFF
    )
    eocd_offset = tail_offset + eocd_index
    locator_offset = eocd_offset - _ZIP64_EOCD_LOCATOR_SIZE
    locator = (
        _read_descriptor(
            descriptor,
            _ZIP64_EOCD_LOCATOR_SIZE,
            locator_offset,
        )
        if locator_offset >= 0
        else b""
    )
    has_zip64 = locator.startswith(b"PK\x06\x07")

    if needs_zip64 and not has_zip64:
        return False
    if has_zip64:
        if len(locator) != _ZIP64_EOCD_LOCATOR_SIZE:
            return False
        try:
            (
                _locator_signature,
                locator_disk,
                zip64_offset,
                total_disks,
            ) = struct.unpack("<4sLQL", locator)
        except struct.error:
            return False
        if locator_disk != 0 or total_disks != 1:
            return False
        located_record = _find_zip64_eocd_record(
            descriptor,
            locator_offset,
            zip64_offset,
            (
                disk_number,
                central_directory_disk,
                entries_on_disk,
                total_entries,
                central_directory_size,
                central_directory_offset,
            ),
        )
        if located_record is None:
            return False
        physical_zip64_offset, fixed, zip64_total_size = located_record
        try:
            (
                zip64_signature,
                zip64_record_size,
                _version_made,
                version_needed,
                zip64_disk,
                zip64_central_disk,
                zip64_entries_on_disk,
                zip64_total_entries,
                zip64_central_size,
                zip64_central_offset,
            ) = struct.unpack("<4sQ2H2L4Q", fixed)
        except struct.error:
            return False
        archive_prefix = physical_zip64_offset - zip64_offset
        if (
            zip64_signature != b"PK\x06\x06"
            or zip64_record_size < 44
            or zip64_total_size > _MAX_ZIP64_EOCD_BYTES
            or 12 + zip64_record_size != zip64_total_size
            or archive_prefix < 0
            or version_needed < 45
            or zip64_disk != 0
            or zip64_central_disk != 0
            or zip64_entries_on_disk != zip64_total_entries
            or zip64_central_offset + zip64_central_size
            != zip64_offset
        ):
            return False
        classic_pairs = (
            (disk_number, 0xFFFF, zip64_disk),
            (
                central_directory_disk,
                0xFFFF,
                zip64_central_disk,
            ),
            (entries_on_disk, 0xFFFF, zip64_entries_on_disk),
            (total_entries, 0xFFFF, zip64_total_entries),
            (
                central_directory_size,
                0xFFFFFFFF,
                zip64_central_size,
            ),
            (
                central_directory_offset,
                0xFFFFFFFF,
                zip64_central_offset,
            ),
        )
        if any(
            classic not in {sentinel, resolved}
            for classic, sentinel, resolved in classic_pairs
        ):
            return False
        total_entries = zip64_total_entries
        central_directory_size = zip64_central_size
        central_directory_offset = zip64_central_offset
        central_start = central_directory_offset + archive_prefix
        central_boundary = physical_zip64_offset
    else:
        if (
            disk_number != 0
            or central_directory_disk != 0
            or entries_on_disk != total_entries
        ):
            return False
        archive_prefix = (
            eocd_offset
            - central_directory_size
            - central_directory_offset
        )
        if archive_prefix < 0:
            return False
        central_start = central_directory_offset + archive_prefix
        central_boundary = eocd_offset

    if (
        total_entries > _MAX_OOXML_ENTRIES
        or central_directory_size > _MAX_OOXML_CENTRAL_DIRECTORY_BYTES
    ):
        return False
    return (
        central_start >= archive_prefix
        and central_directory_size >= 0
        and central_start + central_directory_size == central_boundary
        and (
            not validate_local_bounds
            or _validate_zipfile_bounds(
                descriptor,
                central_start,
                total_entries,
                archive_prefix,
            )
        )
    )


def _inspect_ooxml_descriptor(descriptor: int) -> str | None:
    if not _preflight_zip_descriptor(descriptor):
        return None
    position = os.lseek(descriptor, 0, os.SEEK_CUR)
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        with os.fdopen(os.dup(descriptor), "rb") as source:
            with zipfile.ZipFile(source) as archive:
                members = archive.infolist()
                if len(members) > _MAX_OOXML_ENTRIES:
                    return None
                names: set[str] = set()
                content_types_info = None
                for info in members:
                    if not _safe_zip_member(info.filename) or info.filename in names:
                        return None
                    names.add(info.filename)
                    if info.filename == "[Content_Types].xml":
                        content_types_info = info
                if content_types_info is None:
                    return None
                if (
                    content_types_info.flag_bits & 0x1
                    or content_types_info.file_size > 2 * 1024 * 1024
                ):
                    return None
                content_types = archive.read(content_types_info).lower()
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return None
    finally:
        os.lseek(descriptor, position, os.SEEK_SET)

    matches = [
        kind
        for kind, marker in _OOXML_MARKERS.items()
        if marker in content_types
        and any(name.startswith(_OOXML_PREFIXES[kind]) for name in names)
    ]
    return matches[0] if len(matches) == 1 else None


def _read_descriptor(descriptor: int, size: int, offset: int = 0) -> bytes:
    if hasattr(os, "pread"):
        return os.pread(descriptor, size, offset)
    position = os.lseek(descriptor, 0, os.SEEK_CUR)
    try:
        os.lseek(descriptor, offset, os.SEEK_SET)
        return os.read(descriptor, size)
    finally:
        os.lseek(descriptor, position, os.SEEK_SET)


def _detect_artifact_kind_from_descriptor(
    descriptor: int,
    logical_path: str,
    mime: str,
) -> str | None:
    detected_mime = _normalized_mime(mime)
    specific_kind = _SPECIFIC_MIME_KINDS.get(detected_mime)
    if specific_kind is not None:
        return specific_kind
    if detected_mime not in _GENERIC_MIMES:
        return None

    signature = _read_descriptor(descriptor, 16)
    if signature.startswith(b"%PDF-"):
        return "pdf"
    if (
        signature.startswith(b"\x89PNG\r\n\x1a\n")
        or signature.startswith(b"\xff\xd8\xff")
        or (signature.startswith(b"RIFF") and signature[8:12] == b"WEBP")
        or signature.startswith((b"II*\x00", b"MM\x00*"))
    ):
        return "image"

    suffix = os.path.splitext(logical_path)[1].lower()
    is_zip = signature.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"))
    if is_zip or detected_mime in _ZIP_MIMES:
        return _inspect_ooxml_descriptor(descriptor)
    if signature.startswith(_OLE_SIGNATURE):
        return _SUFFIX_KINDS.get(suffix) if suffix in {".xls", ".doc", ".ppt"} else None
    return _SUFFIX_KINDS.get(suffix)


def detect_artifact_kind(path: str, mime: str | None = None) -> str | None:
    """Return a supported normalized artifact kind, or ``None``.

    A specific MIME type is authoritative. Generic MIME types are resolved by
    safe signature/container inspection before a supported suffix is used.
    """
    _require_secure_filesystem_capabilities()
    detected_mime = _normalized_mime(mime)
    if mime is not None:
        specific_kind = _SPECIFIC_MIME_KINDS.get(detected_mime)
        if specific_kind is not None:
            return specific_kind
        if detected_mime not in _GENERIC_MIMES:
            return None

    descriptor = os.open(os.fspath(path), _file_flags())
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"non-regular artifact is not allowed: {path}")
        if mime is None:
            detected_mime = _mime_from_descriptor(descriptor)
        return _detect_artifact_kind_from_descriptor(
            descriptor,
            os.fspath(path),
            detected_mime,
        )
    finally:
        os.close(descriptor)


def _hash_descriptor(descriptor: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = _read_descriptor(descriptor, _HASH_CHUNK_SIZE, size)
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
    return size, digest.hexdigest()


def _open_absolute_directory(path: str) -> int:
    path = os.path.abspath(path)
    current = os.open(os.path.abspath(os.sep), _directory_flags())
    try:
        for part in path.split(os.sep)[1:]:
            try:
                next_descriptor = os.open(part, _directory_flags(), dir_fd=current)
            except OSError as exc:
                _raise_artifact_open_error(
                    exc,
                    validation_message=f"directory path must not contain symlinks: {path}",
                    collection_message=f"directory path could not be opened: {path}",
                )
            os.close(current)
            current = next_descriptor
        return current
    except Exception:
        os.close(current)
        raise


def _manifest_entry_from_descriptor(
    descriptor: int,
    rel: str,
    *,
    require_single_link: bool,
) -> ManifestEntry:
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"non-regular artifact is not allowed: {rel}")
    if require_single_link and before.st_nlink != 1:
        raise ValueError(f"artifact must be a single-link regular file: {rel}")
    size, sha256 = _hash_descriptor(descriptor)
    mime = _mime_from_descriptor(descriptor)
    kind = _detect_artifact_kind_from_descriptor(descriptor, rel, mime)
    after = os.fstat(descriptor)
    if (
        _stat_identity(before) != _stat_identity(after)
        or size != after.st_size
        or (require_single_link and after.st_nlink != 1)
    ):
        raise ValueError(f"artifact changed while manifest was built: {rel}")
    return ManifestEntry(path=rel, size=size, sha256=sha256, mime=mime, kind=kind)


def _scan_directory(
    directory_descriptor: int,
    rel_parts: tuple[str, ...],
    rows: dict[str, ManifestEntry],
    directories: set[str],
    *,
    skip_runtime: bool,
    require_single_link: bool,
    required_file_mode: int | None,
    required_directory_mode: int | None,
) -> None:
    with os.scandir(directory_descriptor) as entries:
        names = sorted(entry.name for entry in entries)
    for name in names:
        rel = "/".join((*rel_parts, name))
        info = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(
                f"symlink directory or file is not allowed in artifact workspace: {rel}"
            )
        if stat.S_ISDIR(info.st_mode):
            try:
                child_descriptor = os.open(name, _directory_flags(), dir_fd=directory_descriptor)
            except OSError as exc:
                _raise_artifact_open_error(
                    exc,
                    validation_message=f"symlink directory is not allowed: {rel}",
                    collection_message=f"artifact directory could not be opened: {rel}",
                )
            try:
                opened = os.fstat(child_descriptor)
                if _stat_identity(info) != _stat_identity(opened):
                    raise ValueError(f"artifact directory changed while manifest was built: {rel}")
                if (
                    required_directory_mode is not None
                    and stat.S_IMODE(opened.st_mode) != required_directory_mode
                ):
                    raise ValueError(f"artifact directory has invalid mode: {rel}")
                if not (skip_runtime and name in _SKIP_ROOTS):
                    directories.add(rel)
                    _scan_directory(
                        child_descriptor,
                        (*rel_parts, name),
                        rows,
                        directories,
                        skip_runtime=skip_runtime,
                        require_single_link=require_single_link,
                        required_file_mode=required_file_mode,
                        required_directory_mode=required_directory_mode,
                    )
                current = os.stat(
                    name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                if not stat.S_ISDIR(current.st_mode) or not _same_filesystem_object(
                    opened,
                    current,
                ):
                    raise ValueError(
                        f"artifact directory changed while manifest was built: {rel}"
                    )
            finally:
                os.close(child_descriptor)
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"non-regular artifact is not allowed: {rel}")
        if skip_runtime and (
            name in _SKIP_ROOTS
            or (not rel_parts and name in _SKIP_ROOT_FILES)
        ):
            continue
        try:
            descriptor = os.open(name, _file_flags(), dir_fd=directory_descriptor)
        except OSError as exc:
            _raise_artifact_open_error(
                exc,
                validation_message=f"symlink is not allowed in artifact workspace: {rel}",
                collection_message=f"artifact file could not be opened: {rel}",
            )
        try:
            opened = os.fstat(descriptor)
            if _stat_identity(info) != _stat_identity(opened):
                raise ValueError(f"artifact changed while manifest was built: {rel}")
            if (
                required_file_mode is not None
                and stat.S_IMODE(opened.st_mode) != required_file_mode
            ):
                raise ValueError(f"artifact file has invalid mode: {rel}")
            rows[rel] = _manifest_entry_from_descriptor(
                descriptor,
                rel,
                require_single_link=require_single_link,
            )
        finally:
            os.close(descriptor)


def _scan_manifest(
    root: str,
    *,
    skip_runtime: bool,
    require_single_link: bool,
    required_file_mode: int | None = None,
    required_directory_mode: int | None = None,
) -> tuple[dict[str, ManifestEntry], set[str]]:
    root = os.path.abspath(os.fspath(root))
    root_descriptor = _open_absolute_directory(root)
    try:
        return _scan_manifest_descriptor(
            root_descriptor,
            skip_runtime=skip_runtime,
            require_single_link=require_single_link,
            required_file_mode=required_file_mode,
            required_directory_mode=required_directory_mode,
        )
    finally:
        os.close(root_descriptor)


def _scan_manifest_descriptor(
    root_descriptor: int,
    *,
    skip_runtime: bool,
    require_single_link: bool,
    required_file_mode: int | None = None,
    required_directory_mode: int | None = None,
) -> tuple[dict[str, ManifestEntry], set[str]]:
    scan_descriptor = os.open(".", _directory_flags(), dir_fd=root_descriptor)
    rows: dict[str, ManifestEntry] = {}
    directories: set[str] = set()
    try:
        if (
            required_directory_mode is not None
            and stat.S_IMODE(os.fstat(scan_descriptor).st_mode) != required_directory_mode
        ):
            raise ValueError("artifact root directory has invalid mode")
        _scan_directory(
            scan_descriptor,
            (),
            rows,
            directories,
            skip_runtime=skip_runtime,
            require_single_link=require_single_link,
            required_file_mode=required_file_mode,
            required_directory_mode=required_directory_mode,
        )
    finally:
        os.close(scan_descriptor)
    return {path: rows[path] for path in sorted(rows)}, directories


def build_manifest(root: str) -> dict[str, ManifestEntry]:
    """Build a deterministic manifest of regular, non-runtime files in *root*."""
    _require_secure_filesystem_capabilities()
    try:
        rows, _directories = _scan_manifest(
            root,
            skip_runtime=True,
            require_single_link=True,
        )
    except ArtifactValidationError:
        raise
    except ValueError as exc:
        raise ArtifactValidationError(str(exc)) from exc
    except FileNotFoundError as exc:
        raise ArtifactValidationError(
            f"artifact workspace changed during collection: {root}"
        ) from exc
    except OSError as exc:
        raise ArtifactCollectionError(
            f"artifact workspace could not be collected: {root}: {exc}"
        ) from exc
    return rows


def diff_manifests(
    before: dict[str, ManifestEntry],
    after: dict[str, ManifestEntry],
) -> list[dict]:
    """Return sorted created and content-modified output rows."""
    rows: list[dict] = []
    for path in sorted(after):
        old = before.get(path)
        if old is None:
            change = "created"
        elif old.sha256 != after[path].sha256:
            change = "modified"
        else:
            continue
        rows.append({**asdict(after[path]), "change": change})
    return rows


def is_binary_output(row: dict) -> bool:
    """Whether a manifest row is a supported binary artifact."""
    return row.get("kind") in _SUPPORTED_KINDS


def _tree_hash(entries: list[ManifestEntry] | tuple[ManifestEntry, ...]) -> str:
    payload = json.dumps(
        [asdict(entry) for entry in sorted(entries, key=lambda entry: entry.path)],
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _manifest_directories(entries: tuple[ManifestEntry, ...]) -> set[str]:
    directories: set[str] = set()
    for entry in entries:
        parts = PurePosixPath(entry.path).parts[:-1]
        for index in range(1, len(parts) + 1):
            directories.add("/".join(parts[:index]))
    return directories


def _logical_parts(raw_path: object) -> tuple[str, ...]:
    if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path or "\\" in raw_path:
        raise ValueError(f"invalid relative artifact path: {raw_path!r}")
    logical = PurePosixPath(raw_path)
    parts = logical.parts
    if logical.is_absolute() or not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"invalid relative artifact path: {raw_path!r}")
    if logical.as_posix() != raw_path:
        raise ValueError(f"invalid relative artifact path: {raw_path!r}")
    if any(part in _SKIP_ROOTS for part in parts) or raw_path in _SKIP_ROOT_FILES:
        raise ValueError(f"runtime path is not candidate evidence: {raw_path!r}")
    return parts


def _ensure_real_directory(path: str) -> int:
    path = os.path.abspath(path)
    current = os.open(os.path.abspath(os.sep), _directory_flags())
    try:
        for part in path.split(os.sep)[1:]:
            if not part:
                continue
            try:
                next_descriptor = os.open(part, _directory_flags(), dir_fd=current)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o755, dir_fd=current)
                except FileExistsError:
                    pass
                try:
                    next_descriptor = os.open(part, _directory_flags(), dir_fd=current)
                except OSError as exc:
                    raise ValueError(
                        f"destination parent must be a real directory: {path}"
                    ) from exc
            except OSError as exc:
                raise ValueError(
                    f"destination parent must be a real directory: {path}"
                ) from exc
            os.close(current)
            current = next_descriptor
        return current
    except Exception:
        os.close(current)
        raise


def _same_filesystem_object(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        stat.S_IFMT(left.st_mode),
    ) == (
        right.st_dev,
        right.st_ino,
        stat.S_IFMT(right.st_mode),
    )


def _directory_is_at_or_below(
    directory_descriptor: int,
    ancestor_info: os.stat_result,
) -> bool:
    current = os.dup(directory_descriptor)
    try:
        while True:
            current_info = os.fstat(current)
            if _same_filesystem_object(current_info, ancestor_info):
                return True
            parent = os.open("..", _directory_flags(), dir_fd=current)
            parent_info = os.fstat(parent)
            if _same_filesystem_object(current_info, parent_info):
                os.close(parent)
                return False
            os.close(current)
            current = parent
    finally:
        os.close(current)


def _open_existing_directory_prefix(path: str) -> tuple[int, tuple[str, ...]]:
    path = os.path.abspath(path)
    parts = tuple(part for part in path.split(os.sep)[1:] if part)
    current = os.open(os.path.abspath(os.sep), _directory_flags())
    try:
        for index, part in enumerate(parts):
            try:
                next_descriptor = os.open(part, _directory_flags(), dir_fd=current)
            except FileNotFoundError:
                return current, parts[index:]
            except OSError as exc:
                raise ArtifactConfigurationError(
                    f"judge root path must contain only real directories: {path}"
                ) from exc
            os.close(current)
            current = next_descriptor
        return current, ()
    except Exception:
        os.close(current)
        raise


def _reject_overlapping_roots(
    work_descriptor: int,
    work_dir: str,
    judge_root: str,
) -> None:
    judge_prefix_descriptor, remaining = _open_existing_directory_prefix(judge_root)
    try:
        work_info = os.fstat(work_descriptor)
        judge_prefix_info = os.fstat(judge_prefix_descriptor)
        judge_inside_work = _directory_is_at_or_below(
            judge_prefix_descriptor,
            work_info,
        )
        work_inside_judge = (
            not remaining
            and _directory_is_at_or_below(work_descriptor, judge_prefix_info)
        )
        if judge_inside_work or work_inside_judge:
            raise ArtifactConfigurationError(
                "artifact workspace and judge root overlap: "
                f"work_dir={work_dir!r}, judge_root={os.path.abspath(judge_root)!r}"
            )
    finally:
        os.close(judge_prefix_descriptor)


def _remove_directory_at(
    parent_descriptor: int,
    name: str,
    *,
    expected: os.stat_result | None = None,
) -> None:
    before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise ValueError(f"refusing to remove non-directory entry: {name}")
    if expected is not None and not _same_filesystem_object(before, expected):
        raise ValueError(f"refusing to remove replaced directory: {name}")
    try:
        directory_descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
    except OSError as exc:
        raise ValueError(f"refusing to follow directory during cleanup: {name}") from exc
    try:
        opened = os.fstat(directory_descriptor)
        if not _same_filesystem_object(before, opened):
            raise ValueError(f"directory changed during cleanup: {name}")
        os.fchmod(directory_descriptor, 0o700)
        with os.scandir(directory_descriptor) as entries:
            child_names = sorted(entry.name for entry in entries)
        for child_name in child_names:
            child_info = os.stat(
                child_name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if stat.S_ISDIR(child_info.st_mode) and not stat.S_ISLNK(child_info.st_mode):
                _remove_directory_at(directory_descriptor, child_name)
            else:
                os.unlink(child_name, dir_fd=directory_descriptor)
        current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if not stat.S_ISDIR(current.st_mode) or not _same_filesystem_object(opened, current):
            raise ValueError(f"directory changed during cleanup: {name}")
    finally:
        os.close(directory_descriptor)
    os.rmdir(name, dir_fd=parent_descriptor)


def remove_locked_tree(path: str) -> None:
    """Best-effort removal of a judge-created tree that may hold a locked
    evidence snapshot (0o555 directories / 0o444 files).

    ``shutil.rmtree`` cannot unlink entries from a non-writable directory, so a
    plain quiet rmtree orphans the locked evidence subtree under the system temp
    dir on every successful judgment. This mirrors the snapshot-creation failure
    path (``_remove_directory_at``): it reopens each directory by descriptor,
    restores the write bit before unlinking its children, and never follows a
    symlink out of the tree. It is best-effort and never raises.
    """
    path = os.path.abspath(os.fspath(path))
    parent = os.path.dirname(path)
    name = os.path.basename(path)
    if not name or name in (os.curdir, os.pardir):
        return
    try:
        parent_descriptor = _open_absolute_directory(parent)
    except OSError:
        shutil.rmtree(path, ignore_errors=True)
        return
    try:
        _remove_directory_at(parent_descriptor, name)
    except FileNotFoundError:
        pass
    except (ValueError, OSError):
        # The strict fd-walk refused an unexpected state (e.g. a non-directory
        # top entry); fall back to a best-effort quiet rmtree.
        shutil.rmtree(path, ignore_errors=True)
    finally:
        os.close(parent_descriptor)


def _open_created_directory(parent_descriptor: int, name: str, mode: int) -> int:
    os.mkdir(name, mode, dir_fd=parent_descriptor)
    created_info: os.stat_result | None = None
    descriptor = -1
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
        opened = os.fstat(descriptor)
        created_info = opened
        if not stat.S_ISDIR(created_info.st_mode):
            raise ValueError(f"created entry is not a directory: {name}")
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISDIR(named.st_mode)
            or not _same_filesystem_object(created_info, opened)
            or not _same_filesystem_object(created_info, named)
        ):
            raise ValueError(f"created directory changed unexpectedly: {name}")
        return descriptor
    except Exception:
        if created_info is None and descriptor >= 0:
            try:
                created_info = os.fstat(descriptor)
            except OSError:
                pass
        if descriptor >= 0:
            os.close(descriptor)
        if created_info is not None:
            try:
                _remove_directory_at(
                    parent_descriptor,
                    name,
                    expected=created_info,
                )
            except (FileNotFoundError, ValueError, OSError):
                pass
        raise


def _validate_absolute_directory_identity(path: str, expected_descriptor: int) -> None:
    current_descriptor = _open_absolute_directory(path)
    try:
        if not _same_filesystem_object(
            os.fstat(expected_descriptor),
            os.fstat(current_descriptor),
        ):
            raise ValueError(f"directory path changed after validation: {path}")
    finally:
        os.close(current_descriptor)


def _prepare_judge_dirs(judge_root: str) -> _JudgeDirs:
    judge_root = os.path.abspath(os.fspath(judge_root))
    parent = os.path.dirname(judge_root)
    root_name = os.path.basename(judge_root)
    if not root_name:
        raise ValueError("judge root must not be the filesystem root")
    parent_descriptor = _ensure_real_directory(parent)
    root_descriptor = -1
    evidence_descriptor = -1
    scratch_descriptor = -1
    created_root = False
    try:
        try:
            existing = os.stat(
                root_name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if stat.S_ISLNK(existing.st_mode):
                raise ValueError(f"judge root must not be a symlink: {judge_root}")
            if not stat.S_ISDIR(existing.st_mode):
                raise ValueError(f"judge root must be a directory: {judge_root}")
            _remove_directory_at(parent_descriptor, root_name)
        root_descriptor = _open_created_directory(parent_descriptor, root_name, 0o755)
        created_root = True
        evidence_descriptor = _open_created_directory(root_descriptor, "evidence", 0o700)
        scratch_descriptor = _open_created_directory(root_descriptor, "scratch", 0o700)
        _validate_absolute_directory_identity(parent, parent_descriptor)
        named_root = os.stat(
            root_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if not _same_filesystem_object(named_root, os.fstat(root_descriptor)):
            raise ValueError("judge root changed after creation")
    except Exception:
        expected_root = (
            os.fstat(root_descriptor)
            if created_root and root_descriptor >= 0
            else None
        )
        for descriptor in (scratch_descriptor, evidence_descriptor, root_descriptor):
            if descriptor >= 0:
                os.close(descriptor)
        if created_root:
            try:
                _remove_directory_at(
                    parent_descriptor,
                    root_name,
                    expected=expected_root,
                )
            except (FileNotFoundError, ValueError, OSError):
                pass
        os.close(parent_descriptor)
        raise
    evidence = os.path.join(judge_root, "evidence")
    scratch = os.path.join(judge_root, "scratch")
    return _JudgeDirs(
        root_path=judge_root,
        evidence_path=evidence,
        scratch_path=scratch,
        root_name=root_name,
        parent_descriptor=parent_descriptor,
        root_descriptor=root_descriptor,
        evidence_descriptor=evidence_descriptor,
        scratch_descriptor=scratch_descriptor,
    )


def _open_relative_file(root_descriptor: int, parts: tuple[str, ...]) -> int:
    current = os.dup(root_descriptor)
    try:
        for part in parts[:-1]:
            try:
                next_descriptor = os.open(part, _directory_flags(), dir_fd=current)
            except OSError as exc:
                rel = "/".join(parts)
                _raise_artifact_open_error(
                    exc,
                    validation_message=f"artifact parent must be a real directory: {rel}",
                    collection_message=f"artifact parent could not be opened: {rel}",
                )
            os.close(current)
            current = next_descriptor
        try:
            return os.open(
                parts[-1],
                _file_flags(),
                dir_fd=current,
            )
        except OSError as exc:
            rel = "/".join(parts)
            _raise_artifact_open_error(
                exc,
                validation_message=f"artifact must be a regular file, not a symlink: {rel}",
                collection_message=f"artifact file could not be opened: {rel}",
            )
    finally:
        os.close(current)


def _open_destination_file(root_descriptor: int, parts: tuple[str, ...]) -> int:
    current = os.dup(root_descriptor)
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, 0o700, dir_fd=current)
            except FileExistsError:
                pass
            next_descriptor = os.open(part, _directory_flags(), dir_fd=current)
            os.close(current)
            current = next_descriptor
        return os.open(
            parts[-1],
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=current,
        )
    finally:
        os.close(current)


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write while copying evidence")
        view = view[written:]


def _copy_validated_file(
    source_descriptor: int,
    destination_descriptor: int,
    *,
    rel: str,
    expected_size: object,
    expected_sha256: object,
    initial_info: os.stat_result,
) -> int:
    if isinstance(expected_size, bool) or not isinstance(expected_size, int):
        raise ValueError(f"artifact manifest size is invalid: {rel}")
    if initial_info.st_size != expected_size:
        raise ValueError(
            f"artifact size mismatch for {rel}: expected {expected_size}, got {initial_info.st_size}"
        )
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        raise ValueError(f"artifact manifest hash is invalid: {rel}")

    source_digest = hashlib.sha256()
    copied = 0
    remaining = initial_info.st_size
    while remaining:
        chunk = os.read(source_descriptor, min(_HASH_CHUNK_SIZE, remaining))
        if not chunk:
            raise ValueError(f"artifact ended before its validated size: {rel}")
        source_digest.update(chunk)
        copied += len(chunk)
        remaining -= len(chunk)
        _write_all(destination_descriptor, chunk)
    if os.read(source_descriptor, 1):
        raise ValueError(f"artifact grew beyond its validated size: {rel}")

    final_info = os.fstat(source_descriptor)
    if (
        not stat.S_ISREG(final_info.st_mode)
        or final_info.st_nlink != 1
        or _stat_identity(final_info) != _stat_identity(initial_info)
        or copied != initial_info.st_size
    ):
        raise ValueError(f"artifact changed while evidence was copied: {rel}")
    source_hash = source_digest.hexdigest()
    if source_hash != expected_sha256:
        raise ValueError(
            f"artifact hash mismatch for {rel}: expected {expected_sha256}, got {source_hash}"
        )

    os.fsync(destination_descriptor)
    os.lseek(destination_descriptor, 0, os.SEEK_SET)
    copied_digest = hashlib.sha256()
    copied_size = 0
    while True:
        chunk = os.read(destination_descriptor, _HASH_CHUNK_SIZE)
        if not chunk:
            break
        copied_digest.update(chunk)
        copied_size += len(chunk)
    if copied_size != copied or copied_digest.hexdigest() != source_hash:
        raise RuntimeError(f"copied evidence verification failed: {rel}")
    os.fchmod(destination_descriptor, 0o444)
    destination_info = os.fstat(destination_descriptor)
    if not stat.S_ISREG(destination_info.st_mode) or destination_info.st_nlink != 1:
        raise ValueError(f"copied evidence must be a single-link regular file: {rel}")
    return copied


def _revalidate_source_entry(
    work_descriptor: int,
    parts: tuple[str, ...],
    initial_info: os.stat_result,
    row: dict,
) -> ManifestEntry:
    rel = "/".join(parts)
    named_descriptor = _open_relative_file(work_descriptor, parts)
    try:
        named_info = os.fstat(named_descriptor)
        if _stat_identity(named_info) != _stat_identity(initial_info):
            raise ValueError(f"artifact source name changed after evidence copy: {rel}")
        entry = _manifest_entry_from_descriptor(
            named_descriptor,
            rel,
            require_single_link=True,
        )
        final_named_info = os.fstat(named_descriptor)
        if _stat_identity(final_named_info) != _stat_identity(initial_info):
            raise ValueError(f"artifact source changed after evidence copy: {rel}")
    finally:
        os.close(named_descriptor)

    confirmation_descriptor = _open_relative_file(work_descriptor, parts)
    try:
        if _stat_identity(os.fstat(confirmation_descriptor)) != _stat_identity(
            final_named_info
        ):
            raise ValueError(f"artifact source name changed after evidence copy: {rel}")
    finally:
        os.close(confirmation_descriptor)

    if entry.size != row.get("size") or entry.sha256 != row.get("sha256"):
        raise ValueError(f"artifact source content changed after evidence copy: {rel}")
    return entry


def _revalidate_destination_entry(
    evidence_descriptor: int,
    parts: tuple[str, ...],
    copied_info: os.stat_result,
    expected: ManifestEntry,
) -> ManifestEntry:
    rel = "/".join(parts)
    named_descriptor = _open_relative_file(evidence_descriptor, parts)
    try:
        named_info = os.fstat(named_descriptor)
        if (
            not stat.S_ISREG(named_info.st_mode)
            or named_info.st_nlink != 1
            or not _same_filesystem_object(copied_info, named_info)
            or named_info.st_size != expected.size
            or stat.S_IMODE(named_info.st_mode) != 0o444
        ):
            raise ValueError(f"copied evidence destination name changed: {rel}")
        entry = _manifest_entry_from_descriptor(
            named_descriptor,
            rel,
            require_single_link=True,
        )
        final_named_info = os.fstat(named_descriptor)
        if not _same_filesystem_object(copied_info, final_named_info):
            raise ValueError(f"copied evidence destination changed: {rel}")
    finally:
        os.close(named_descriptor)

    confirmation_descriptor = _open_relative_file(evidence_descriptor, parts)
    try:
        confirmation_info = os.fstat(confirmation_descriptor)
        if (
            not stat.S_ISREG(confirmation_info.st_mode)
            or confirmation_info.st_nlink != 1
            or not _same_filesystem_object(copied_info, confirmation_info)
            or confirmation_info.st_size != expected.size
            or stat.S_IMODE(confirmation_info.st_mode) != 0o444
        ):
            raise ValueError(f"copied evidence destination name changed: {rel}")
    finally:
        os.close(confirmation_descriptor)

    if entry != expected:
        raise ValueError(f"copied evidence destination content changed: {rel}")
    return entry


def _lock_evidence_directories(evidence_descriptor: int) -> None:
    with os.scandir(evidence_descriptor) as entries:
        names = sorted(entry.name for entry in entries)
    for name in names:
        before = os.stat(name, dir_fd=evidence_descriptor, follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode):
            raise RuntimeError(f"symlink appeared while locking evidence: {name}")
        if stat.S_ISDIR(before.st_mode):
            child_descriptor = os.open(name, _directory_flags(), dir_fd=evidence_descriptor)
            try:
                opened = os.fstat(child_descriptor)
                if not _same_filesystem_object(before, opened):
                    raise RuntimeError(f"evidence directory changed while locking: {name}")
                _lock_evidence_directories(child_descriptor)
                current = os.stat(
                    name,
                    dir_fd=evidence_descriptor,
                    follow_symlinks=False,
                )
                if not stat.S_ISDIR(current.st_mode) or not _same_filesystem_object(
                    opened,
                    current,
                ):
                    raise RuntimeError(f"evidence directory changed while locking: {name}")
            finally:
                os.close(child_descriptor)
            continue
        if not stat.S_ISREG(before.st_mode):
            raise RuntimeError(f"non-regular evidence appeared while locking: {name}")
        file_descriptor = os.open(name, _file_flags(), dir_fd=evidence_descriptor)
        try:
            opened = os.fstat(file_descriptor)
            if (
                not _same_filesystem_object(before, opened)
                or opened.st_nlink != 1
            ):
                raise RuntimeError(f"evidence file changed while locking: {name}")
            os.fchmod(file_descriptor, 0o444)
        finally:
            os.close(file_descriptor)
    os.fchmod(evidence_descriptor, 0o555)


def create_evidence_snapshot(
    work_dir: str,
    outputs: list[dict],
    judge_root: str,
    *,
    max_bytes: int = _DEFAULT_MAX_BYTES,
) -> EvidenceSnapshot:
    """Copy declared outputs into a byte-verified, read-only evidence tree."""
    _require_secure_filesystem_capabilities()
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    work_dir = os.path.abspath(os.fspath(work_dir))
    judge_root = os.path.abspath(os.fspath(judge_root))

    selected: list[tuple[dict, tuple[str, ...]]] = []
    seen: set[str] = set()
    for row in outputs:
        if row.get("change") not in {"created", "modified"}:
            continue
        parts = _logical_parts(row.get("path"))
        rel = "/".join(parts)
        if rel in seen:
            raise ValueError(f"duplicate artifact output path: {rel}")
        seen.add(rel)
        selected.append((row, parts))
    selected.sort(key=lambda item: "/".join(item[1]))

    work_descriptor = _open_absolute_directory(work_dir)
    prepared: _JudgeDirs | None = None
    validated_sources: list[tuple[dict, tuple[str, ...], os.stat_result]] = []
    copied_destinations: list[tuple[tuple[str, ...], os.stat_result]] = []
    try:
        _reject_overlapping_roots(work_descriptor, work_dir, judge_root)
        prepared = _prepare_judge_dirs(judge_root)
        evidence = prepared.evidence_path
        scratch = prepared.scratch_path
        total_bytes = 0
        for row, parts in selected:
            rel = "/".join(parts)
            source_descriptor = _open_relative_file(work_descriptor, parts)
            try:
                info = os.fstat(source_descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError(
                        f"artifact must be a single-link regular file: {rel}"
                    )
            finally:
                os.close(source_descriptor)
            validated_sources.append((row, parts, info))
            total_bytes += info.st_size
            if total_bytes > max_bytes:
                raise EvidenceLimitError(
                    f"candidate output bytes exceed configured limit {max_bytes}"
                )

        copied_total = 0
        for row, parts, info in validated_sources:
            rel = "/".join(parts)
            if info.st_size > max_bytes - copied_total:
                raise EvidenceLimitError(
                    f"candidate output bytes exceed configured limit {max_bytes}"
                )
            source_descriptor = _open_relative_file(work_descriptor, parts)
            destination_descriptor = -1
            try:
                if _stat_identity(os.fstat(source_descriptor)) != _stat_identity(info):
                    raise ValueError(f"artifact source changed before evidence copy: {rel}")
                destination_descriptor = _open_destination_file(
                    prepared.evidence_descriptor,
                    parts,
                )
                copied = _copy_validated_file(
                    source_descriptor,
                    destination_descriptor,
                    rel=rel,
                    expected_size=row.get("size"),
                    expected_sha256=row.get("sha256"),
                    initial_info=info,
                )
                copied_total += copied
                copied_destinations.append(
                    (parts, os.fstat(destination_descriptor))
                )
            finally:
                if destination_descriptor >= 0:
                    os.close(destination_descriptor)
                os.close(source_descriptor)

        evidence_rows, evidence_directories = _scan_manifest_descriptor(
            prepared.evidence_descriptor,
            skip_runtime=False,
            require_single_link=True,
        )
        entries = tuple(evidence_rows.values())
        if evidence_directories != _manifest_directories(entries):
            raise RuntimeError("unexpected directory in copied evidence")
        manifest_descriptor = os.open(
            "artifact-manifest.json",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=prepared.scratch_descriptor,
        )
        with os.fdopen(manifest_descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                [asdict(entry) for entry in entries],
                handle,
                ensure_ascii=True,
                sort_keys=True,
                indent=2,
            )
            handle.write("\n")
        _lock_evidence_directories(prepared.evidence_descriptor)
        final_rows, final_directories = _scan_manifest_descriptor(
            prepared.evidence_descriptor,
            skip_runtime=False,
            require_single_link=True,
            required_file_mode=0o444,
            required_directory_mode=0o555,
        )
        final_entries = tuple(final_rows.values())
        expected_entries = tuple(
            _revalidate_source_entry(
                work_descriptor,
                parts,
                info,
                row,
            )
            for row, parts, info in validated_sources
        )
        if len(copied_destinations) != len(expected_entries):
            raise RuntimeError("copied evidence destination set is incomplete")
        destination_entries = tuple(
            _revalidate_destination_entry(
                prepared.evidence_descriptor,
                parts,
                copied_info,
                expected_entries[index],
            )
            for index, (parts, copied_info) in enumerate(
                copied_destinations
            )
        )
        expected_directories = _manifest_directories(expected_entries)
        if (
            entries != expected_entries
            or final_entries != expected_entries
            or destination_entries != expected_entries
            or evidence_directories != expected_directories
            or final_directories != expected_directories
        ):
            raise RuntimeError("evidence does not match finalized rollout outputs")
        _validate_absolute_directory_identity(
            prepared.root_path,
            prepared.root_descriptor,
        )
        _validate_absolute_directory_identity(
            prepared.evidence_path,
            prepared.evidence_descriptor,
        )
        _validate_absolute_directory_identity(
            prepared.scratch_path,
            prepared.scratch_descriptor,
        )
        return EvidenceSnapshot(
            evidence_dir=evidence,
            scratch_dir=scratch,
            tree_hash=_tree_hash(final_entries),
            files=final_entries,
        )
    except Exception:
        if work_descriptor >= 0:
            os.close(work_descriptor)
            work_descriptor = -1
        if prepared is not None:
            expected_root = None
            try:
                if prepared.root_descriptor >= 0:
                    expected_root = os.fstat(prepared.root_descriptor)
            except OSError:
                expected_root = None
            finally:
                prepared.close_children()
            if expected_root is not None:
                try:
                    _remove_directory_at(
                        prepared.parent_descriptor,
                        prepared.root_name,
                        expected=expected_root,
                    )
                except (FileNotFoundError, ValueError, OSError):
                    pass
            prepared.close()
        raise
    finally:
        if work_descriptor >= 0:
            os.close(work_descriptor)
        if prepared is not None:
            prepared.close()


def verify_evidence_snapshot(snapshot: EvidenceSnapshot) -> None:
    """Raise if an evidence snapshot's path set or bytes have changed."""
    try:
        _require_secure_filesystem_capabilities()
        current_rows, current_directories = _scan_manifest(
            snapshot.evidence_dir,
            skip_runtime=False,
            require_single_link=True,
            required_file_mode=0o444,
            required_directory_mode=0o555,
        )
        current = tuple(current_rows.values())
    except ArtifactCollectionError:
        raise
    except Exception as exc:
        raise RuntimeError("evidence changed while judge was running") from exc
    if (
        current != snapshot.files
        or current_directories != _manifest_directories(snapshot.files)
        or _tree_hash(current) != snapshot.tree_hash
    ):
        raise RuntimeError("evidence changed while judge was running")
