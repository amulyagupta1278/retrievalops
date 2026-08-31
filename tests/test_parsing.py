import hashlib
import time
from typing import cast
from uuid import UUID, uuid4

import pytest

import retrievalops.parsing as parsing
from retrievalops.contracts import Document, SupportedMediaType
from retrievalops.parsing import (
    DocumentIntegrityError,
    ExtractionError,
    extract_and_chunk,
    extract_and_chunk_with_timeout,
)


def _document(filename: str, media_type: str, content: bytes) -> Document:
    return Document(
        id=uuid4(),
        sandbox_id=UUID("11111111-1111-1111-1111-111111111111"),
        filename=filename,
        media_type=cast(SupportedMediaType, media_type),
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _text_pdf(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode())
    trailer = (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n"
    )
    pdf.extend(trailer.encode())
    return bytes(pdf)


@pytest.mark.parametrize(
    ("filename", "media_type", "content", "expected"),
    [
        ("guide.txt", "text/plain", b"Alpha beta gamma", "Alpha beta gamma"),
        ("guide.md", "text/markdown", b"# Heading\n\nAlpha beta", "# Heading\n\nAlpha beta"),
    ],
)
def test_extracts_utf8_text_deterministically(
    filename: str, media_type: str, content: bytes, expected: str
) -> None:
    document = _document(filename, media_type, content)

    first = extract_and_chunk(document, content)
    second = extract_and_chunk(document, content)

    assert first.text == expected
    assert first == second


def test_extracts_text_pdf() -> None:
    content = _text_pdf("RetrievalOps extracts text from PDF documents for indexing.")
    document = _document("guide.pdf", "application/pdf", content)

    extracted = extract_and_chunk(document, content)

    assert "RetrievalOps extracts text" in extracted.text
    assert extracted.chunks[0].text == extracted.text


def test_chunks_at_512_tokens_with_64_token_overlap() -> None:
    content = " ".join(f"token-{index}" for index in range(600)).encode()
    document = _document("large.txt", "text/plain", content)

    extracted = extract_and_chunk(document, content)

    assert [chunk.token_count for chunk in extracted.chunks] == [512, 152]
    first_tokens = extracted.chunks[0].text.split()
    second_tokens = extracted.chunks[1].text.split()
    assert first_tokens[-64:] == second_tokens[:64]
    assert all(chunk.id.startswith(f"{document.sandbox_id}:") for chunk in extracted.chunks)


def test_rejects_content_that_does_not_match_document_hash() -> None:
    original = b"trusted bytes"
    document = _document("guide.txt", "text/plain", original)

    with pytest.raises(DocumentIntegrityError, match="hash"):
        extract_and_chunk(document, b"changed bytes")


def test_extraction_deadline_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    content = b"bounded extraction"
    document = _document("guide.txt", "text/plain", content)

    def slow_extraction(_content: bytes) -> str:
        time.sleep(1)
        return "too late"

    monkeypatch.setattr(parsing, "_extract_utf8", slow_extraction)

    with pytest.raises(ExtractionError, match="time limit"):
        extract_and_chunk_with_timeout(document, content, 0.01)
