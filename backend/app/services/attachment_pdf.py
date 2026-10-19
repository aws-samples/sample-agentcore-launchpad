"""Resource-bounded PDF inspection worker; invoked in an isolated child process.

Only normalized text and small diagnostics leave the worker. No document bytes,
paths, tracebacks or parser internals become user-facing error messages.
"""

import json
import resource
import sys
from io import BytesIO

from app.schemas.attachments import MAX_FILE_BYTES, MAX_PDF_PAGES, MAX_TEXT_CHARS


def inspect_pdf(data: bytes, *, extract: bool) -> dict:
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(data))
    if reader.is_encrypted:
        return {"error": "pdf_unreadable"}
    if not 0 < len(reader.pages) <= MAX_PDF_PAGES:
        return {"error": "pdf_too_long"}
    if not extract:
        return {"pages": len(reader.pages)}
    texts: list[str] = []
    size = 0
    for page in reader.pages:
        text = (page.extract_text() or "").strip()
        contents = page.get_contents()
        # A genuinely blank page is harmless. A page with drawing operations but
        # no readable text requires native PDF support (scans/vector diagrams).
        if not text and contents and contents.get_data().strip():
            return {"error": "pdf_text_unavailable"}
        size += len(text)
        if size > MAX_TEXT_CHARS:
            return {"error": "text_too_large"}
        if text:
            texts.append(text)
    if not texts:
        return {"error": "pdf_text_unavailable"}
    return {"text": "\n\n".join(texts), "pages": len(reader.pages)}


def main() -> None:
    for which, value in ((resource.RLIMIT_AS, 512 * 1024 * 1024), (resource.RLIMIT_CPU, 5)):
        try:
            resource.setrlimit(which, (value, value))
        except (ValueError, OSError):
            # macOS refuses RLIMIT_AS; the parent's timeout still bounds the run
            pass
    try:
        data = sys.stdin.buffer.read(MAX_FILE_BYTES + 1)
        output = (
            {"error": "too_large"}
            if len(data) > MAX_FILE_BYTES
            else inspect_pdf(data, extract="--extract" in sys.argv)
        )
    except Exception:
        output = {"error": "pdf_unreadable"}
    print(json.dumps(output))


if __name__ == "__main__":
    main()
