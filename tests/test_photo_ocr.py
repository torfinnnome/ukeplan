import asyncio
import io
from PIL import Image, ImageDraw
from ukeplan.config import settings
from ukeplan import extractor
from ukeplan.extractor import extract_text_and_images_from_bytes


def test_photo_ocr_extraction(monkeypatch):
    # Create image with text simulating a printed activity note
    img = Image.new("RGB", (600, 150), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.text((20, 30), "Wednesday 17:30: Football practice", fill=(0, 0, 0))
    draw.text((20, 80), "Remember water bottle and shin guards", fill=(0, 0, 0))

    img_byte_arr = io.BytesIO()
    img.save(img_byte_arr, format="PNG")
    data = img_byte_arr.getvalue()

    monkeypatch.setattr(settings, "ocr_enabled", True)

    async def fake_ocr(page_images):
        assert len(page_images) == 1
        assert page_images[0].startswith(b"\x89PNG")
        return ["Wednesday 17:30: Football practice", "Remember water bottle and shin guards"]

    monkeypatch.setattr(extractor, "ocr_pages_with_client", fake_ocr)

    result = asyncio.run(extract_text_and_images_from_bytes(data, "snapshot.png"))
    assert result["has_images"] is True
    assert "Football practice" in result["text"]
    assert "water bottle" in result["text"]


def test_photo_ocr_disabled_yields_no_text(monkeypatch):
    img = Image.new("RGB", (120, 40), color=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    monkeypatch.setattr(settings, "ocr_enabled", False)

    result = asyncio.run(extract_text_and_images_from_bytes(buf.getvalue(), "snapshot.png"))
    assert result["has_images"] is True
    assert result["text"] == ""
