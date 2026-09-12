"""On-disk storage for uploaded documents (PDFs, photos).

Files are stored flat in ``settings.upload_dir`` under a unique, safe name;
the original filename is kept in the database (``Document.filename``).
"""

import datetime
import pathlib
import re
import uuid

from ukeplan.config import settings

MIME_BY_EXT = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".txt": "text/plain",
}


def upload_dir() -> pathlib.Path:
    d = pathlib.Path(settings.upload_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def mime_for(filename: str) -> str:
    return MIME_BY_EXT.get(pathlib.PurePosixPath(filename).suffix.lower(), "application/octet-stream")


def store_document(data: bytes, filename: str) -> str:
    """Write document bytes to disk; returns the stored (collision-free) file name."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename)[:80] or "document"
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    stored_name = f"{stamp}_{uuid.uuid4().hex[:8]}_{safe}"
    (upload_dir() / stored_name).write_bytes(data)
    return stored_name


def document_path(stored_name: str) -> pathlib.Path:
    return upload_dir() / stored_name


def remove_stored_file(stored_name: str) -> None:
    try:
        document_path(stored_name).unlink(missing_ok=True)
    except OSError:
        pass
