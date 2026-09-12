import asyncio
import base64
import datetime
import pytest
from fastapi.testclient import TestClient
from ukeplan import app
from ukeplan.db import init_db
from ukeplan.models import PlanItem, ItemType
from ukeplan.extractor import extract_text_and_images_from_bytes, render_photo_image
from ukeplan.ai import clean_json_response
from ukeplan.ical import generate_ical_feed


@pytest.fixture(scope="module", autouse=True)
def setup_database():
    init_db()


def test_homepage_render():
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "Family Week Planner" in response.text
        assert "Upload Schedule" in response.text


def test_shortcut_setup_page_render():
    with TestClient(app) as client:
        response = client.get("/setup/apple-shortcut")
        assert response.status_code == 200
        assert "Share to Week Planner" in response.text
        assert "Calendar subscription" in response.text


def test_crud_plan_items():
    with TestClient(app) as client:
        # Create item
        payload = {
            "title": "Soccer Practice",
            "description": "Field 2",
            "item_type": "event",
            "person": "Child 1 (11)",
            "date": "2026-09-08",
            "start_time": "17:30:00",
            "end_time": "18:45:00",
            "location": "Sports Ground",
        }
        create_res = client.post("/api/items", json=payload)
        assert create_res.status_code == 200
        created = create_res.json()
        assert created["id"] is not None
        assert created["title"] == "Soccer Practice"
        item_id = created["id"]

        # List items
        list_res = client.get("/api/items?start_date=2026-09-01&end_date=2026-09-15")
        assert list_res.status_code == 200
        items = list_res.json()
        assert any(it["id"] == item_id for it in items)

        # Toggle item
        toggle_res = client.post(f"/api/items/{item_id}/toggle")
        assert toggle_res.status_code == 200
        assert toggle_res.json()["is_completed"] is True

        # Delete item
        del_res = client.delete(f"/api/items/{item_id}")
        assert del_res.status_code == 200


def test_clean_json_response_helper():
    markdown_json = "```json\n{\"week_number\": 37, \"items\": []}\n```"
    cleaned = clean_json_response(markdown_json)
    assert cleaned["week_number"] == 37
    assert cleaned["items"] == []


def test_ical_generation():
    test_item = PlanItem(
        id=1,
        title="Swimming Gear",
        item_type=ItemType.GEAR,
        person="Child 2 (9)",
        date=datetime.date(2026, 9, 9),
    )
    ical_bytes = generate_ical_feed([test_item])
    ical_text = ical_bytes.decode("utf-8")
    assert "BEGIN:VCALENDAR" in ical_text
    assert "Swimming Gear" in ical_text
    assert "Child 2 (9)" in ical_text


def test_pdf_extraction_with_pymupdf():
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 72), "Weekly Schedule Week 38 - 4th Grade\nRemember gym clothes on Tuesday!")
    pdf_bytes = doc.tobytes()
    doc.close()

    result = asyncio.run(extract_text_and_images_from_bytes(pdf_bytes, "schedule_week38.pdf"))
    assert "Remember gym clothes on Tuesday" in result["text"]
    assert result["page_count"] == 1


def test_crud_family_members():
    with TestClient(app) as client:
        # List seeded members
        list_res = client.get("/api/members")
        assert list_res.status_code == 200
        members = list_res.json()
        assert len(members) >= 3
        names = [m["name"] for m in members]
        assert "Ann" in names or any(m["school_class"] == "1A" for m in members)

        # Create a new kid with name and class
        new_kid = {
            "name": "Oliver",
            "school_class": "3C",
            "display_order": 6,
        }
        create_res = client.post("/api/members", json=new_kid)
        assert create_res.status_code == 200
        created = create_res.json()
        assert created["name"] == "Oliver"
        assert created["school_class"] == "3C"
        member_id = created["id"]

        # Update kid's class
        update_res = client.put(f"/api/members/{member_id}", json={
            "name": "Oliver",
            "school_class": "4C",
        })
        assert update_res.status_code == 200
        assert update_res.json()["school_class"] == "4C"

        # Delete kid
        del_res = client.delete(f"/api/members/{member_id}")
        assert del_res.status_code == 200


def test_build_system_prompt_with_kids_classes():
    from ukeplan.ai import build_system_prompt
    custom_members = [
        {"name": "Ann", "school_class": "1A"},
        {"name": "Berit", "school_class": "6B"},
        {"name": "Matthew", "school_class": "10C"},
        {"name": "Mor", "school_class": None},
    ]
    prompt = build_system_prompt(family_members=custom_members)
    assert "De registrerte familiemedlemmene og barna er: Ann (1A), Berit (6B), Matthew (10C), Mor" in prompt
    assert "(f.eks. 'Ann', 'Berit')" in prompt
    assert "Beregn ALDRI kalenderdatoer selv" in prompt
    assert '"date": KUN hvis dokumentet eksplisitt inneholder en kalenderdato' in prompt
    assert f"bruk det nåværende år ({datetime.date.today().year})" in prompt


def test_resolve_item_date_uses_week_and_day_of_week():
    """Backend resolves week+weekday deterministically when 'date' is null; explicit dates always win."""
    import datetime
    from ukeplan.ai import ExtractedItem, ExtractedWeekPlan, resolve_item_date

    # Week 36 in 2026 is 2026-08-31 (Mon) .. 2026-09-06 (Sun)
    plan = ExtractedWeekPlan(week_number=36, year=2026, summary="Uke 36")

    # Item on Wednesday of week 36 -> 2026-09-02
    wed = ExtractedItem(title="Uteskole", item_type="gear", date=None, day_of_week=3)
    assert resolve_item_date(wed, plan) == datetime.date(2026, 9, 2)

    # Item with no weekday, no date -> Monday of week 36 -> 2026-08-31
    monday = ExtractedItem(title="Innesko", item_type="gear", date=None, day_of_week=None)
    assert resolve_item_date(monday, plan) == datetime.date(2026, 8, 31)

    # Explicit document date wins even if it is in another week (e.g. foreldremøte in week 38)
    explicit = ExtractedItem(title="Foreldremøte", item_type="event", date="2026-09-14", day_of_week=None)
    assert resolve_item_date(explicit, plan) == datetime.date(2026, 9, 14)


def test_build_shortcut_plist_structure():
    """The generated shortcut must be valid XML wiring a POST form-data upload to /api/ingest."""
    from ukeplan.shortcut import build_shortcut_plist
    import xml.etree.ElementTree as ET

    plist = build_shortcut_plist("http://192.168.1.50:8000/api/ingest", "Del til Ukeplan")
    root = ET.fromstring(plist)  # raises if not valid XML
    text = ET.tostring(root, encoding="unicode")

    # HTTP upload action with multipart form data
    assert "com.apple.shortcuts.SendHTTPRequest" in text
    assert "<string>POST</string>" in text
    assert "<string>FormData</string>" in text
    assert "<string>http://192.168.1.50:8000/api/ingest</string>" in text
    assert "<string>file</string>" in text
    assert "<string>channel</string>" in text
    # Share sheet availability + file/image input
    assert "<string>ActionExtension</string>" in text
    assert "<string>WFGenericFileContentItem</string>" in text
    assert "<string>WFImageContentItem</string>" in text
    # Name round-trips
    assert "<string>Del til Ukeplan</string>" in text


def test_shortcut_filename_slug():
    from ukeplan.shortcut import shortcut_filename

    assert shortcut_filename("Del til Ukeplan", True) == "del-til-ukeplan.shortcut"
    assert shortcut_filename("Share to Week Planner", False) == "share-to-week-planner.plist"
    assert shortcut_filename("!!!", True) == "ukeplan.shortcut"


def test_shortcut_download_endpoint_signed(monkeypatch):
    """When the signing service succeeds, serve an AEA1 file for one-tap import."""
    import ukeplan.shortcut as sc

    sc._cache.clear()
    monkeypatch.setattr(sc, "sign_shortcut", lambda plist, name: b"AEA1" + b"\x00" * 16)
    with TestClient(app) as client:
        res = client.get("/setup/apple-shortcut/download?name=Share%20to%20Week%20Planner")
        assert res.status_code == 200
        assert res.content.startswith(b"AEA1")
        assert 'filename="share-to-week-planner.shortcut"' in res.headers["content-disposition"]


def test_shortcut_download_endpoint_fallback_unsigned(monkeypatch):
    """When signing is unavailable, fall back to the unsigned plist (Mac can sign it)."""
    import ukeplan.shortcut as sc

    sc._cache.clear()
    monkeypatch.setattr(sc, "sign_shortcut", lambda plist, name: None)
    with TestClient(app) as client:
        res = client.get("/setup/apple-shortcut/download?name=Del%20til%20Ukeplan")
        assert res.status_code == 200
        assert res.content.startswith(b"<?xml")
        assert 'filename="del-til-ukeplan.plist"' in res.headers["content-disposition"]
    sc._cache.clear()


def test_render_photo_image():
    """OCR stage input: photos are re-encoded as PNG for the vision model."""
    import io as _io
    from PIL import Image
    from ukeplan.extractor import render_photo_image

    img = Image.new("RGB", (64, 64), (200, 30, 30))
    buf = _io.BytesIO()
    img.save(buf, format="JPEG")
    png = render_photo_image(buf.getvalue(), "ukeplan.jpg")
    assert png.startswith(b"\x89PNG")


def test_pdf_always_ocr(monkeypatch):
    """PDF OCR: whole file first; rendered pages on rejection; digital text last resort."""
    import pymupdf as fitz
    from ukeplan import extractor
    from ukeplan.config import settings

    doc = fitz.open()
    for label in ("page one digital", "page two digital"):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), label, fontsize=20)
    pdf = doc.tobytes()

    monkeypatch.setattr(settings, "ocr_enabled", True)

    # Whole-PDF OCR succeeds -> its text wins, no page rendering
    pdf_calls, page_calls = [], []

    async def ok_pdf_ocr(pdf_bytes, filename):
        pdf_calls.append((pdf_bytes, filename))
        return "ocr: whole document"

    async def tracking_page_ocr(page_images):
        page_calls.append(page_images)
        return ["should not be called"]

    monkeypatch.setattr(extractor, "ocr_pdf_with_client", ok_pdf_ocr)
    monkeypatch.setattr(extractor, "ocr_pages_with_client", tracking_page_ocr)
    result = asyncio.run(extract_text_and_images_from_bytes(pdf, "ukeplan.pdf"))
    assert result["text"] == "ocr: whole document"
    assert pdf_calls == [(pdf, "ukeplan.pdf")]
    assert page_calls == []

    # Whole-PDF OCR rejected by the endpoint -> rendered page images are used
    async def rejected_pdf_ocr(pdf_bytes, filename):
        raise RuntimeError("OCR error (502): gateway cannot ingest PDFs")

    async def page_ocr(page_images):
        page_calls.append(page_images)
        assert len(page_images) == 2
        assert all(p.startswith(b"\x89PNG") for p in page_images)
        return [f"ocr page {i+1}" for i in range(len(page_images))]

    monkeypatch.setattr(extractor, "ocr_pdf_with_client", rejected_pdf_ocr)
    monkeypatch.setattr(extractor, "ocr_pages_with_client", page_ocr)
    result = asyncio.run(extract_text_and_images_from_bytes(pdf, "ukeplan.pdf"))
    assert result["text"] == "ocr page 1\nocr page 2"
    assert len(page_calls) == 1

    # Both OCR stages fail -> digital text layer wins
    async def broken_page_ocr(page_images):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(extractor, "ocr_pages_with_client", broken_page_ocr)
    result = asyncio.run(extract_text_and_images_from_bytes(pdf, "ukeplan.pdf"))
    assert "page one digital" in result["text"]
    assert "page two digital" in result["text"]

    # OCR disabled -> digital text only, no OCR call at all
    monkeypatch.setattr(settings, "ocr_enabled", False)

    async def not_called_pdf(pdf_bytes, filename):
        raise AssertionError("OCR must not be called when disabled")

    async def not_called_pages(page_images):
        raise AssertionError("OCR must not be called when disabled")

    monkeypatch.setattr(extractor, "ocr_pdf_with_client", not_called_pdf)
    monkeypatch.setattr(extractor, "ocr_pages_with_client", not_called_pages)
    result = asyncio.run(extract_text_and_images_from_bytes(pdf, "ukeplan.pdf"))
    assert "page one digital" in result["text"]


def test_pdf_native_flag_off_skips_whole_pdf(monkeypatch):
    """ocr_pdf_native=False (llama.cpp): no whole-PDF call; rendered pages are OCR'd."""
    import pymupdf as fitz
    from ukeplan import extractor
    from ukeplan.config import settings

    doc = fitz.open()
    for label in ("page one digital", "page two digital"):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), label, fontsize=20)
    pdf = doc.tobytes()

    monkeypatch.setattr(settings, "ocr_enabled", True)
    monkeypatch.setattr(settings, "ocr_pdf_native", False)

    async def not_called_pdf(pdf_bytes, filename):
        raise AssertionError("whole-PDF OCR must not be called when ocr_pdf_native=False")

    async def page_ocr(page_images):
        assert len(page_images) == 2
        assert all(p.startswith(b"\x89PNG") for p in page_images)
        return [f"ocr page {i+1}" for i in range(len(page_images))]

    monkeypatch.setattr(extractor, "ocr_pdf_with_client", not_called_pdf)
    monkeypatch.setattr(extractor, "ocr_pages_with_client", page_ocr)
    result = asyncio.run(extract_text_and_images_from_bytes(pdf, "ukeplan.pdf"))
    assert result["text"] == "ocr page 1\nocr page 2"


def test_ocr_pages_payload(monkeypatch):
    """OCR client: one call per page, image part + markdown prompt to the configured model."""
    from ukeplan import ocr
    from ukeplan.config import settings

    monkeypatch.setattr(settings, "ocr_model", "my-vision-model")
    monkeypatch.setattr(settings, "ocr_base_url", "http://localhost:8099/v1")
    monkeypatch.setattr(settings, "ocr_api_key", "secret")

    class FakeResponse:
        status_code = 200
        text = "ok"

        def __init__(self, text):
            self._text = text

        def json(self):
            return {"choices": [{"message": {"content": self._text}}]}

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def post(self, url, **kwargs):
            self.calls.append({"url": url, **kwargs})
            return FakeResponse(f"page {len(self.calls)}")

    client = FakeClient()
    results = asyncio.run(ocr.ocr_pages(client, [b"img-one", b"img-two"]))
    assert results == ["page 1", "page 2"]
    assert len(client.calls) == 2

    call = client.calls[0]
    assert call["url"] == "http://localhost:8099/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer secret"
    assert call["json"]["model"] == "my-vision-model"
    content = call["json"]["messages"][0]["content"]
    assert content[0]["type"] == "image_url"
    data_url = content[0]["image_url"]["url"]
    assert data_url.startswith("data:image/png;base64,")
    assert base64.b64decode(data_url.split(",", 1)[1]) == b"img-one"
    assert content[1] == {"type": "text", "text": ocr.OCR_PROMPT}


def test_ocr_pdf_payload(monkeypatch):
    """OCR client: whole PDF sent as one base64 data URI in a single call."""
    from ukeplan import ocr
    from ukeplan.config import settings

    monkeypatch.setattr(settings, "ocr_model", "glm-ocr")
    monkeypatch.setattr(settings, "ocr_base_url", "http://localhost:8099/v1")
    monkeypatch.setattr(settings, "ocr_api_key", "secret")

    class FakeResponse:
        status_code = 200
        text = "ok"

        def json(self):
            return {"choices": [{"message": {"content": "# UKE 36"}}]}

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def post(self, url, **kwargs):
            self.calls.append({"url": url, **kwargs})
            return FakeResponse()

    client = FakeClient()
    result = asyncio.run(ocr.ocr_pdf(client, b"%PDF-1.4 fake", "ukebrev.pdf"))
    assert result == "# UKE 36"
    assert len(client.calls) == 1

    call = client.calls[0]
    content = call["json"]["messages"][0]["content"]
    assert content[0]["type"] == "image_url"
    data_url = content[0]["image_url"]["url"]
    assert data_url.startswith("data:application/pdf;base64,")
    assert base64.b64decode(data_url.split(",", 1)[1]) == b"%PDF-1.4 fake"
    assert content[1] == {"type": "text", "text": ocr.OCR_PROMPT}


def test_documents_archive_lifecycle(tmp_path, monkeypatch):
    """Uploads are stored on disk, auto-tagged (person/week), listable, retaggable, servable, deletable."""
    import pymupdf as fitz
    from ukeplan import api as api_mod
    from ukeplan.config import settings
    from ukeplan.ai import ExtractedWeekPlan, ExtractedItem

    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))

    # Fixed LLM output: week 36 of 2026, single person
    fake_plan = ExtractedWeekPlan(
        week_number=36,
        year=2026,
        summary="Ukeplan for 1A",
        items=[ExtractedItem(title="Gymtøy", item_type="gear", person="Ann", date=None, day_of_week=3)],
    )

    async def fake_parse(text, target_person=None, family_members=None, client=None):
        return fake_plan

    monkeypatch.setattr(api_mod, "parse_plan_text_with_llm", fake_parse)

    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 100), "Ukeplan uke 36", fontsize=20)
    pdf_bytes = doc.tobytes()

    with TestClient(app) as client:
        up = client.post(
            "/api/ingest",
            files={"file": ("ukeplan-36.pdf", pdf_bytes, "application/pdf")},
            data={"person": "Ann", "channel": "web"},
        )
        assert up.status_code == 200, up.text

        docs = client.get("/api/documents").json()
        assert len(docs) == 1
        d = docs[0]
        assert d["filename"] == "ukeplan-36.pdf"
        assert d["person"] == "Ann"
        assert d["week_number"] == 36
        assert d["year"] == 2026
        assert d["mime_type"] == "application/pdf"
        assert d["size_bytes"] > 0

        # Stored file is served back with the right media type
        f = client.get(f"/api/documents/{d['id']}/file")
        assert f.status_code == 200
        assert f.headers["content-type"].startswith("application/pdf")
        assert f.content[:4] == b"%PDF"

        # Re-tag (person + week)
        up2 = client.put(f"/api/documents/{d['id']}", json={"person": "Berit", "week_number": 37})
        assert up2.status_code == 200
        assert up2.json()["person"] == "Berit"
        assert up2.json()["week_number"] == 37

        # Filters
        assert len(client.get("/api/documents", params={"person": "Ann"}).json()) == 0
        assert len(client.get("/api/documents", params={"person": "Berit"}).json()) == 1
        assert len(client.get("/api/documents", params={"week": 37}).json()) == 1
        assert len(client.get("/api/documents", params={"week": 36}).json()) == 0

        # Delete removes DB row and file on disk
        delres = client.delete(f"/api/documents/{d['id']}")
        assert delres.status_code == 200
        assert len(client.get("/api/documents").json()) == 0
        assert list(tmp_path.glob("*.pdf")) == []


def test_documents_page_renders():
    with TestClient(app) as client:
        response = client.get("/documents")
        assert response.status_code == 200
        assert "Dokumenter" in response.text
        assert "/api/documents" in response.text


def test_schedules_lifecycle(tmp_path, monkeypatch):
    """One standing timetable per child; re-upload replaces (old file removed), listable, servable, deletable."""
    import pymupdf as fitz
    from ukeplan.config import settings

    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))

    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 100), "Skolemøter 2026", fontsize=20)
    pdf_a = doc.tobytes()

    doc2 = fitz.open()
    page2 = doc2.new_page(width=595, height=842)
    page2.insert_text((72, 100), "Skolemøter 2027", fontsize=20)
    pdf_b = doc2.tobytes()

    with TestClient(app) as client:
        # First upload for a child
        up = client.post(
            "/api/schedules",
            files={"file": ("timetable-2026.pdf", pdf_a, "application/pdf")},
            data={"person": "Ann", "year": "2026"},
        )
        assert up.status_code == 200, up.text
        s = up.json()
        assert s["person"] == "Ann"
        assert s["filename"] == "timetable-2026.pdf"
        assert s["year"] == 2026
        assert s["size_bytes"] > 0
        sid = s["id"]

        # Re-upload for the same child: replaces the row and removes the old file
        up2 = client.post(
            "/api/schedules",
            files={"file": ("timetable-2027.pdf", pdf_b, "application/pdf")},
            data={"person": "Ann", "year": "2027"},
        )
        assert up2.status_code == 200, up2.text
        s2 = up2.json()
        assert s2["id"] == sid
        assert s2["filename"] == "timetable-2027.pdf"
        assert s2["year"] == 2027
        # still exactly one row, and only the newest file remains on disk
        assert len(client.get("/api/schedules").json()) == 1
        assert len(list(tmp_path.glob("*.pdf"))) == 1

        # Served back with the right media type and content
        f = client.get(f"/api/schedules/{sid}/file")
        assert f.status_code == 200
        assert f.headers["content-type"].startswith("application/pdf")
        assert f.content[:4] == b"%PDF"

        # Second child gets their own row
        up3 = client.post(
            "/api/schedules",
            files={"file": ("berit-2026.pdf", pdf_a, "application/pdf")},
            data={"person": "Berit"},
        )
        assert up3.status_code == 200, up3.text
        assert len(client.get("/api/schedules").json()) == 2
        assert len(client.get("/api/schedules", params={"person": "Ann"}).json()) == 1
        assert len(client.get("/api/schedules", params={"person": "Berit"}).json()) == 1

        # Delete removes the DB row and the file on disk
        delres = client.delete(f"/api/schedules/{sid}")
        assert delres.status_code == 200
        assert len(client.get("/api/schedules", params={"person": "Ann"}).json()) == 0

        # Missing-file and missing-row error paths
        missing = client.get("/api/schedules/999999/file")
        assert missing.status_code == 404
        missing_del = client.delete("/api/schedules/999999")
        assert missing_del.status_code == 404

        # Clean up remaining row + file so the suite stays green
        berit = client.get("/api/schedules", params={"person": "Berit"}).json()[0]
        assert client.delete(f"/api/schedules/{berit['id']}").status_code == 200
        assert client.get("/api/schedules").json() == []
        assert list(tmp_path.glob("*.pdf")) == []


def test_command_endpoint_adds_recurring_item(monkeypatch):
    """Natural-language command -> structured recurring item saved to the calendar."""
    import datetime as dt
    from ukeplan import api as api_mod
    from ukeplan.ai import CommandResult, CommandItem

    result = CommandResult(
        summary="La til sjakktrening for Sigrid hver onsdag 18:00–19:50.",
        items=[CommandItem(
            title="Sjakktrening",
            item_type="event",
            person="Sigrid",
            day_of_week=3,
            start_time="18:00",
            end_time="19:50",
            recurring_weekly=True,
        )],
    )

    async def fake_parse(text, family_members=None, client=None):
        assert "sjakktrening" in text
        return result

    monkeypatch.setattr(api_mod, "parse_command_with_llm", fake_parse)

    today = dt.date.today()
    with TestClient(app) as client:
        res = client.post("/api/commands", json={"text": "Sigrid har sjakktrening hver onsdag 18:00-19:50"})
        assert res.status_code == 200, res.text
        data = res.json()
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["title"] == "Sjakktrening"
        assert item["person"] == "Sigrid"
        assert item["recurring_weekly"] is True
        assert item["day_of_week"] == 3
        # Anchored to the coming Wednesday
        assert dt.date.fromisoformat(item["date"]) == api_mod.next_weekday_date(3)
        assert item["start_time"].startswith("18:00")
        assert item["end_time"].startswith("19:50")

        # A weekly recurring item shows up in ANY week's listing
        future_monday = today + dt.timedelta(days=35)
        future_monday = future_monday - dt.timedelta(days=future_monday.weekday())
        listed = client.get(
            "/api/items",
            params={"start_date": future_monday, "end_date": future_monday + dt.timedelta(days=6)},
        ).json()
        assert any(i["id"] == item["id"] for i in listed)

        client.delete(f"/api/items/{item['id']}")


def test_ical_feed_recurring_rule():
    """Weekly recurring items export as VEVENT + RRULE FREQ=WEEKLY."""
    import datetime as dt
    from ukeplan.models import PlanItem, ItemType
    from ukeplan.ical import generate_ical_feed

    item = PlanItem(
        id=999,
        title="Sjakktrening",
        item_type=ItemType.EVENT,
        person="Sigrid",
        date=dt.date(2026, 9, 9),
        start_time=dt.time(18, 0),
        end_time=dt.time(19, 50),
        recurring_weekly=True,
        day_of_week=3,
    )
    ical_text = generate_ical_feed([item]).decode()
    assert "RRULE:FREQ=WEEKLY;BYDAY=WE" in ical_text


def test_purge_stale_items():
    """Items dated more than the retention window ago are auto-removed; recurring-weekly items and recent items are kept."""
    import datetime as dt
    from sqlmodel import Session, select
    from ukeplan.db import engine, purge_stale_items
    from ukeplan.models import PlanItem, ItemType

    today = dt.date.today()
    created_ids = []
    with Session(engine) as session:
        def make(title, date, recurring=False):
            item = PlanItem(
                title=title,
                item_type=ItemType.EVENT,
                person="Test",
                date=date,
                recurring_weekly=recurring,
                day_of_week=3 if recurring else None,
            )
            session.add(item)
            session.commit()
            session.refresh(item)
            created_ids.append(item.id)
            return item

        stale = make("Old event", today - dt.timedelta(weeks=20))
        recent = make("New event", today - dt.timedelta(weeks=1))
        stale_recurring = make("Weekly soccer", today - dt.timedelta(weeks=20), recurring=True)
        stale_t, recent_t, rec_t = stale.title, recent.title, stale_recurring.title

        removed = purge_stale_items(cutoff_weeks=12, as_of=today, session=session)

    # The 20-week-old non-recurring item is gone; the recent and the recurring one survive
    assert removed >= 1
    with Session(engine) as session:
        remaining = {it.title for it in session.exec(select(PlanItem).where(PlanItem.id.in_(created_ids)))}
    assert stale_t not in remaining
    assert recent_t in remaining
    assert rec_t in remaining

    # Zero/negative retention is a no-op
    with Session(engine) as session:
        assert purge_stale_items(cutoff_weeks=0, as_of=today, session=session) == 0

    # Cleanup: remove the survivors we created
    with Session(engine) as session:
        for it in session.exec(select(PlanItem).where(PlanItem.id.in_(created_ids))):
            session.delete(it)
        session.commit()


def test_purge_stale_documents(tmp_path, monkeypatch):
    """Documents (uploaded PDFs/photos) older than the retention window are removed, row and file on disk; recent ones are kept."""
    import datetime as dt
    from sqlmodel import Session
    from ukeplan.db import engine, purge_stale_items
    from ukeplan.models import Document
    from ukeplan.config import settings

    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))

    today = dt.date.today()
    utc = dt.timezone.utc
    ids = {}
    with Session(engine) as session:
        for key, weeks_old in (("stale", 20), ("recent", 1)):
            created = dt.datetime.now(utc) - dt.timedelta(weeks=weeks_old)
            doc = Document(
                filename=f"doc_{key}.pdf",
                stored_name=f"stored_{key}.pdf",
                person="Test",
                source_channel="web",
                mime_type="application/pdf",
                size_bytes=123,
            )
            doc.created_at = created
            session.add(doc)
            session.commit()
            session.refresh(doc)
            ids[key] = (doc.id, doc.stored_name)
            # write the file so on-disk removal is observable
            (tmp_path / doc.stored_name).write_bytes(b"%PDF-1.4 test")

        removed = purge_stale_items(cutoff_weeks=12, as_of=today, session=session)

    assert removed >= 1
    stale_id, stale_name = ids["stale"]
    recent_id, recent_name = ids["recent"]

    with Session(engine) as session:
        assert session.get(Document, stale_id) is None
        assert session.get(Document, recent_id) is not None
    # stale file removed from disk, recent file kept
    assert not (tmp_path / stale_name).exists()
    assert (tmp_path / recent_name).exists()

    # Cleanup: remove the surviving row and its file
    with Session(engine) as session:
        doc = session.get(Document, recent_id)
        if doc:
            session.delete(doc)
        session.commit()
    (tmp_path / recent_name).unlink(missing_ok=True)
