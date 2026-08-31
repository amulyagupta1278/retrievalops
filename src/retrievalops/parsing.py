import hashlib
import re
from dataclasses import dataclass
from io import BytesIO

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from retrievalops.contracts import Chunk, Document

CHUNK_TOKENS = 512
CHUNK_OVERLAP_TOKENS = 64
_TOKEN_PATTERN = re.compile(r"\S+")


class ExtractionError(ValueError):
    pass


class DocumentIntegrityError(ExtractionError):
    pass


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    text: str
    chunks: tuple[Chunk, ...]


def extract_and_chunk(document: Document, content: bytes) -> ExtractedDocument:
    _verify_integrity(document, content)
    if document.media_type == "application/pdf":
        text = _extract_pdf(content)
    else:
        text = _extract_utf8(content)
    chunks = _chunk(document, text)
    if not chunks:
        raise ExtractionError("document contains no extractable tokens")
    return ExtractedDocument(text=text, chunks=chunks)


def _verify_integrity(document: Document, content: bytes) -> None:
    if len(content) != document.size_bytes:
        raise DocumentIntegrityError("content size does not match document metadata")
    if hashlib.sha256(content).hexdigest() != document.sha256:
        raise DocumentIntegrityError("content hash does not match document metadata")


def _extract_utf8(content: bytes) -> str:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ExtractionError("text document is not valid UTF-8") from error
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise ExtractionError("document contains no extractable text")
    return normalized


def _extract_pdf(content: bytes) -> str:
    try:
        reader = PdfReader(BytesIO(content), strict=True)
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
    except (PdfReadError, OSError, ValueError) as error:
        raise ExtractionError("PDF extraction failed") from error
    text = "\n\n".join(page for page in pages if page).strip()
    if not text:
        raise ExtractionError("PDF contains no extractable text")
    return text


def _chunk(document: Document, text: str) -> tuple[Chunk, ...]:
    tokens = list(_TOKEN_PATTERN.finditer(text))
    chunks: list[Chunk] = []
    start = 0
    step = CHUNK_TOKENS - CHUNK_OVERLAP_TOKENS
    while start < len(tokens):
        window = tokens[start : start + CHUNK_TOKENS]
        chunk_text = text[window[0].start() : window[-1].end()]
        digest = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()
        ordinal = len(chunks)
        chunks.append(
            Chunk(
                id=f"{document.sandbox_id}:{document.sha256[:16]}:{ordinal:06d}",
                document_id=document.id,
                ordinal=ordinal,
                text=chunk_text,
                token_count=len(window),
                sha256=digest,
            )
        )
        if len(window) < CHUNK_TOKENS:
            break
        start += step
    return tuple(chunks)
