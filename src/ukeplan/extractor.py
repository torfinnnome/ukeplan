import io
import logging
import pymupdf as fitz
from PIL import Image
import pillow_heif

from ukeplan.config import settings
from ukeplan.ocr import ocr_pages_with_client, ocr_pdf_with_client

logger = logging.getLogger("ukeplan.extractor")

# Register HEIF / HEIC opener for native iPhone photo support
pillow_heif.register_heif_opener()

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif")

# Max pages sent through the OCR stage (keeps payloads/inference bounded).
MAX_OCR_PAGES = 8


def render_photo_image(data: bytes, filename: str) -> bytes:
    """
Re-encode a photo upload as a single PNG for the OCR stage.

PDFs do not go through rendering: they are sent to the OCR model whole
(ocr_pdf, when the endpoint ingests PDFs natively) or as rendered pages.
    """
    image = Image.open(io.BytesIO(data))
    if image.mode != "RGB":
        image = image.convert("RGB")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    logger.info(f"Re-encoded image '{filename}' as PNG ({len(buf.getvalue())} bytes)")
    return buf.getvalue()


def pdf_page_images(data: bytes, filename: str) -> list[bytes]:
    """Render a PDF's pages to 150 DPI PNGs (capped at MAX_OCR_PAGES) for the OCR stage."""
    doc = fitz.open(stream=data, filetype="pdf")
    page_count = len(doc)
    if page_count > MAX_OCR_PAGES:
        logger.warning(f"PDF '{filename}' has {page_count} pages; sending only the first {MAX_OCR_PAGES} for OCR")
    images = []
    for i, page in enumerate(doc):
        if i >= MAX_OCR_PAGES:
            break
        pix = page.get_pixmap(dpi=150)
        images.append(pix.tobytes("png"))
    logger.info(f"Rendered {len(images)} page image(s) from PDF '{filename}'")
    return images


async def extract_text_and_images_from_bytes(data: bytes, filename: str) -> dict:
    """
Extracts text content from:
- PDF documents: sent whole to the OCR model when the endpoint ingests
  PDFs natively (settings.ocr_pdf_native); otherwise the pages are
  rendered to images and transcribed. The digital text layer is the last
  resort when OCR is disabled or every OCR attempt fails.
- Camera photos / images (.jpg, .jpeg, .png, .heic, .webp) via the OCR model
- Plain text / markdown files

When OCR is disabled or unavailable, PDFs fall back to their digital text
(scanned pages then yield no text) and photos yield no text.
    """
    lower_fn = filename.lower()

    if lower_fn.endswith(".pdf"):
        doc = fitz.open(stream=data, filetype="pdf")
        page_count = len(doc)
        logger.info(f"Extracting text from PDF '{filename}' ({page_count} pages)")
        full_text = "\n".join(page.get_text("text").strip() for page in doc).strip()
        if full_text:
            logger.info(f"  Extracted {len(full_text)} chars digital text (OCR fallback)")

        if settings.ocr_enabled:
            ocr_text = ""
            if settings.ocr_pdf_native:
                try:
                    ocr_text = await ocr_pdf_with_client(data, filename)
                except Exception:
                    logger.exception("Whole-PDF OCR failed for '%s'; falling back to rendered page images", filename)
            else:
                logger.info("Whole-PDF OCR disabled for '%s' (endpoint cannot ingest PDFs); rendering pages", filename)
            if not ocr_text:
                try:
                    page_images = pdf_page_images(data, filename)
                    ocr_text = "\n".join(t for t in await ocr_pages_with_client(page_images) if t).strip()
                except Exception:
                    logger.exception(f"Page-image OCR also failed for '{filename}'; using digital text only")
            if ocr_text:
                logger.info(f"  OCR extracted {len(ocr_text)} chars")
                full_text = ocr_text
            else:
                logger.warning(f"  OCR returned no text; using digital text ({len(full_text)} chars)")
        else:
            logger.info("  OCR disabled — using digital text only")

        logger.info(f"PDF extraction summary: total {len(full_text)} chars across {page_count} pages")
        return {
            "text": full_text,
            "has_images": False,
            "page_count": page_count,
        }

    elif any(lower_fn.endswith(ext) for ext in IMAGE_EXTENSIONS):
        # Camera snapshot or photo upload: run the OCR stage
        image = Image.open(io.BytesIO(data))
        extracted_text = ""
        if settings.ocr_enabled:
            try:
                extracted_text = "\n".join(t for t in await ocr_pages_with_client([render_photo_image(data, filename)]) if t).strip()
            except Exception:
                logger.exception(f"OCR failed for image '{filename}'")
        return {
            "text": extracted_text,
            "has_images": True,
            "page_count": 1,
            "image_format": image.format or "IMAGE",
        }

    else:
        # Plain text fallback
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1", errors="ignore")
        return {
            "text": text,
            "has_images": False,
            "page_count": 1,
        }
