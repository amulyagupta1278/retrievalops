import hashlib
import multiprocessing
from dataclasses import dataclass
from io import BytesIO
from multiprocessing.connection import Connection
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
    upload: UploadFile,
    *,
    max_bytes: int,
    max_pdf_pages: int,
    validation_timeout_seconds: float,
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
        _validate_pdf_bounded(content, max_pdf_pages, validation_timeout_seconds)
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


def _validate_pdf_bounded(content: bytes, max_pdf_pages: int, timeout_seconds: float) -> None:
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_pdf_validation_process, args=(content, max_pdf_pages, sender))
    process.start()
    sender.close()
    try:
        if not receiver.poll(timeout_seconds):
            process.terminate()
            process.join(timeout=1)
            raise ServiceError(
                422,
                "DOCUMENT_VALIDATION_TIMEOUT",
                "The document could not be validated within the time limit.",
            )
        try:
            status_code, code, message = receiver.recv()
        except (EOFError, OSError):
            status_code, code, message = (
                422,
                "INVALID_DOCUMENT",
                "The PDF file is malformed.",
            )
    finally:
        receiver.close()
        if process.is_alive():
            process.terminate()
        process.join(timeout=1)
    if code is not None:
        raise ServiceError(status_code, code, message)


def _pdf_validation_process(content: bytes, max_pdf_pages: int, sender: Connection) -> None:
    try:
        _validate_pdf(content, max_pdf_pages)
        sender.send((200, None, ""))
    except ServiceError as error:
        sender.send((error.status_code, error.code, error.message))
    except Exception:
        sender.send((422, "INVALID_DOCUMENT", "The PDF file is malformed."))
    finally:
        sender.close()
