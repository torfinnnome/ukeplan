"""
OCR client: a dedicated vision model (any OpenAI-compatible endpoint, e.g.
vLLM/SGLang/llama.cpp serving GLM-OCR) that transcribes documents to markdown.

PDFs are sent whole in one call only when the endpoint ingests PDFs
natively (z.ai API/SDK, settings.ocr_pdf_native); llama.cpp rejects PDF
media parts, so there the extractor renders pages to PNG (one call each).
Photos are sent as base64 PNG (one call per image).

The extraction LLM (settings.ai_*) never sees documents: the OCR model
produces markdown first, and the extraction LLM structures that text.
"""
import base64
import logging
from typing import List, Optional

import httpx

from ukeplan.config import settings

logger = logging.getLogger("ukeplan.ocr")

# GLM-OCR is a "prompt-limited" model (z.ai model card; llama.cpp PR #19677):
# it is trained on short task triggers and degrades with long free-form
# instructions. "OCR" is the canonical single-shot document-parsing prompt;
# the model outputs the transcription as markdown.
#OCR_PROMPT = "OCR"
OCR_PROMPT = """You are an expert OCR parser. Output raw document Markdown only.

Transcribe the entire image content verbatim into Github-flavored Markdown, maintaining headings, paragraphs, lists, and tables.

Transcribe the complete content of this document image verbatim without omitting any text.
Preserve the exact document layout and structure using Github-flavored Markdown:

Use #, ##, ### for title, main headings, and subheadings.
Keep regular paragraphs intact.
Format all lists using - or numbered 1..
Convert all tables into standard Markdown pipe tables (| header | header |).
Wrap code blocks in standard triple backticks (```).
Do not summarize, skip footnotes, or leave out caption text.
"""

# One call per document; GLM-OCR handles up to 100 pages / 50MB in one request.
OCR_TIMEOUT_SECONDS = 300.0


def _headers() -> dict:
    headers = {}
    if settings.ocr_api_key and settings.ocr_api_key != "not-needed":
        headers["Authorization"] = f"Bearer {settings.ocr_api_key}"
    return headers


async def _transcribe(client: httpx.AsyncClient, part: dict, what: str) -> str:
    """One chat/completions call: media part + OCR_PROMPT. Returns the markdown."""
    url = f"{settings.ocr_base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": settings.ocr_model,
        "messages": [
            {
                "role": "user",
                "content": [part, {"type": "text", "text": OCR_PROMPT}],
            }
        ],
        "temperature": 0.1,
        "max_tokens": 8192,
    }
    resp = await client.post(url, headers=_headers(), json=payload)
    if resp.status_code != 200:
        raise RuntimeError(f"OCR error ({resp.status_code}) for {what}: {resp.text}")
    text = resp.json()["choices"][0]["message"]["content"] or ""
    logger.info(f"OCR {what}: {len(text)} chars")
    return text


async def ocr_pdf(client: httpx.AsyncClient, pdf_bytes: bytes, filename: str) -> str:
    """
    Transcribe a whole PDF in a single call.

    GLM-OCR accepts PDFs natively, so the file is sent as a base64 data URI
    instead of being rendered to page images. Returns the transcription;
    raises on failure so the caller can degrade to the digital text layer.
    """
    data_uri = f"data:application/pdf;base64,{base64.b64encode(pdf_bytes).decode('ascii')}"
    logger.info(f"OCR: sending PDF '{filename}' ({len(pdf_bytes)} bytes) to {settings.ocr_base_url} (model: {settings.ocr_model})")
    return await _transcribe(
        client,
        {"type": "image_url", "image_url": {"url": data_uri}},
        f"PDF '{filename}'",
    )


async def ocr_pdf_with_client(pdf_bytes: bytes, filename: str) -> str:
    """ocr_pdf with its own short-lived httpx client (per-request timeout)."""
    async with httpx.AsyncClient(timeout=OCR_TIMEOUT_SECONDS) as client:
        return await ocr_pdf(client, pdf_bytes, filename)


async def ocr_pages(client: httpx.AsyncClient, page_images: List[bytes]) -> List[str]:
    """
    Transcribe page images with the OCR model, sequentially (one image per call).

    Returns one transcription per image, in input order. Raises on the first
    failed image so the caller can degrade to whatever text it already has.
    """
    if not page_images:
        raise ValueError("ocr_pages requires at least one page image")

    logger.info(f"OCR: {len(page_images)} image(s) -> {settings.ocr_base_url} (model: {settings.ocr_model})")
    results: List[str] = []
    for i, img in enumerate(page_images):
        data_uri = f"data:image/png;base64,{base64.b64encode(img).decode('ascii')}"
        results.append(await _transcribe(client, {"type": "image_url", "image_url": {"url": data_uri}}, f"page {i + 1}/{len(page_images)}"))
    return results


async def ocr_pages_with_client(page_images: List[bytes]) -> List[str]:
    """ocr_pages with its own short-lived httpx client (per-request timeout)."""
    async with httpx.AsyncClient(timeout=OCR_TIMEOUT_SECONDS) as client:
        return await ocr_pages(client, page_images)
