"""Content-based file type detection (magic bytes + container inspection, never extension)."""

from __future__ import annotations

import hashlib
import zipfile
from dataclasses import dataclass
from pathlib import Path

from pps_ats.schemas.common import FileFormat

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
# Password-protected .docx/.xlsx are stored as OLE containers with an "EncryptedPackage" stream.
_ENCRYPTED_PACKAGE_UTF16 = "EncryptedPackage".encode("utf-16-le")

_EXTENSION_FORMAT = {
    ".pdf": FileFormat.PDF,
    ".docx": FileFormat.DOCX,
    ".doc": FileFormat.DOC_LEGACY,
}


@dataclass(frozen=True)
class FileProbe:
    path: Path
    size_bytes: int
    sha256: str
    detected_format: FileFormat
    declared_extension: str
    extension_mismatch: bool
    detail: str = ""
    docx_uncompressed_bytes: int | None = None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inspect_zip(path: Path) -> tuple[FileFormat, str, int | None]:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = {info.filename for info in infos}
            uncompressed = sum(info.file_size for info in infos)
            if "word/document.xml" in names and "[Content_Types].xml" in names:
                return FileFormat.DOCX, "", uncompressed
            if "mimetype" in names:
                mime = archive.read("mimetype")[:100].decode("ascii", "replace").strip()
                if mime == "application/vnd.oasis.opendocument.text":
                    return FileFormat.ODT, mime, uncompressed
            return FileFormat.ZIP_OTHER, "zip archive without word/document.xml", uncompressed
    except zipfile.BadZipFile as exc:
        # Has a ZIP signature but the archive is broken; if the extension claims DOCX,
        # report it as DOCX so the parser stage records it as corrupted.
        return FileFormat.ZIP_OTHER, f"corrupted zip: {exc}", None


def _looks_like_text(head: bytes) -> bool:
    if not head or b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def detect_format(path: Path) -> FileProbe:
    """Identify the real format of ``path`` from its bytes."""
    size = path.stat().st_size
    ext = path.suffix.lower()
    if size == 0:
        return FileProbe(path, 0, _sha256(path), FileFormat.EMPTY_FILE, ext, False)

    with path.open("rb") as handle:
        head = handle.read(4096)

    fmt, detail, uncompressed = FileFormat.UNKNOWN, "", None
    if b"%PDF-" in head[:1024]:
        fmt = FileFormat.PDF
    elif head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        fmt, detail, uncompressed = _inspect_zip(path)
        if fmt is FileFormat.ZIP_OTHER and detail.startswith("corrupted") and ext == ".docx":
            fmt = FileFormat.DOCX
    elif head.startswith(_OLE_MAGIC):
        data = path.read_bytes() if size <= 50 * 1024 * 1024 else head
        fmt = (
            FileFormat.ENCRYPTED_OFFICE
            if _ENCRYPTED_PACKAGE_UTF16 in data
            else FileFormat.DOC_LEGACY
        )
    elif head.startswith(b"{\\rtf"):
        fmt = FileFormat.RTF
    elif head.startswith(b"\x89PNG\r\n\x1a\n"):
        fmt = FileFormat.PNG
    elif head.startswith(b"\xff\xd8\xff"):
        fmt = FileFormat.JPEG
    elif head[:6] in (b"GIF87a", b"GIF89a"):
        fmt = FileFormat.GIF
    elif _looks_like_text(head):
        fmt = FileFormat.TEXT

    expected = _EXTENSION_FORMAT.get(ext)
    # A password-protected .docx legitimately lives in an OLE container.
    mismatch = (
        expected is not None
        and fmt != expected
        and not (ext == ".docx" and fmt is FileFormat.ENCRYPTED_OFFICE)
    )
    if expected is None:
        mismatch = fmt in (FileFormat.PDF, FileFormat.DOCX)

    return FileProbe(
        path=path,
        size_bytes=size,
        sha256=_sha256(path),
        detected_format=fmt,
        declared_extension=ext,
        extension_mismatch=mismatch,
        detail=detail,
        docx_uncompressed_bytes=uncompressed,
    )
