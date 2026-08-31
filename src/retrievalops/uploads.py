import hashlib
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath

from fastapi import UploadFile
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from retrievalops.contracts import SupportedMediaType
from retrievalops.errors import ServiceError


@dataclass(frozen=True, slots=True)
class ValidatedUpload:
    filename: str
    media_type: SupportedMediaType
    content: bytes
    sha256: str


_MEDIA_TYPES: dict[str, frozenset[str]] = {
    ".pdf": frozenset({"application/pdf"}),
    ".txt": frozenset({"text/plain"}),
    ".md": frozenset({"text/markdown", "text/plain"}),
}


async def validate_upload(
    upload: UploadFile, *, max_bytes: int, max_pdf_pages: int
) -> ValidatedUpload:
    filename = _safe_filename(upload.filename)
    extension = PurePosixPath(filename).suffix.lower()
    allowed_media_types = _MEDIA_TYPES.get(extension)
    if allowed_media_types is None:
        raise ServiceError(415, "UNSUPPORTED_DOCUMENT_TYPE", "Upload a PDF, TXT, or MD file.")

    declared_media_type = (upload.content_type or "").split(";", maxsplit=1)[0].lower()
    if declared_media_type not in allowed_media_types:
        raise ServiceError(
            415,
            "UNSUPPORTED_DOCUMENT_TYPE",
            "The file extension and media type do not match.",
        )

    content = await upload.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise ServiceError(413, "DOCUMENT_TOO_LARGE", "The document exceeds the upload limit.")
    if not content:
        raise ServiceError(422, "EMPTY_DOCUMENT", "The document contains no text.")

    if extension == ".pdf":
        _validate_pdf(content, max_pdf_pages)
        canonical_media_type: SupportedMediaType = "application/pdf"
    else:
        _validate_text(content)
        canonical_media_type = "text/markdown" if extension == ".md" else "text/plain"

    return ValidatedUpload(
        filename=filename,
        media_type=canonical_media_type,
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _safe_filename(raw_filename: str | None) -> str:
    filename = (raw_filename or "").replace("\\", "/").rsplit("/", maxsplit=1)[-1]
    if not filename or len(filename) > 255:
        raise ServiceError(422, "INVALID_FILENAME", "The filename is invalid.")
    return filename


def _validate_text(content: bytes) -> None:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ServiceError(422, "INVALID_DOCUMENT", "Text files must use UTF-8.") from error
    if "\x00" in text or not text.strip():
        raise ServiceError(422, "EMPTY_DOCUMENT", "The document contains no text.")


def _validate_pdf(content: bytes, max_pdf_pages: int) -> None:
    if not content.startswith(b"%PDF-"):
        raise ServiceError(422, "INVALID_DOCUMENT", "The PDF file is malformed.")
    try:
        reader = PdfReader(BytesIO(content), strict=True)
        if reader.is_encrypted:
            raise ServiceError(422, "ENCRYPTED_PDF", "Encrypted PDFs are not supported.")
        if not 1 <= len(reader.pages) <= max_pdf_pages:
            raise ServiceError(422, "INVALID_PAGE_COUNT", "The PDF page count is unsupported.")
        sample = "".join((page.extract_text() or "") for page in reader.pages[:10])
    except ServiceError:
        raise
    except (PdfReadError, OSError, ValueError) as error:
        raise ServiceError(422, "INVALID_DOCUMENT", "The PDF file is malformed.") from error
    if len("".join(sample.split())) < 20:
        raise ServiceError(
            422,
            "SCANNED_OR_EMPTY_PDF",
            "The PDF must contain extractable text.",
        )
