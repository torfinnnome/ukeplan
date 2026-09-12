import datetime
import pytest
from httpx import AsyncClient, ASGITransport
from ukeplan import app
from ukeplan.models import ItemType
from ukeplan.db import init_db


@pytest.mark.asyncio
async def test_end_to_end_calendar_and_web():
    init_db()  # ensure schema is current (this test bypasses app startup events)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # Create 2 items for different kids
        res1 = await ac.post("/api/items", json={
            "title": "Swimming Class",
            "item_type": "gear",
            "person": "Berit",
            "date": "2026-09-07",
        })
        assert res1.status_code == 200
        id1 = res1.json()["id"]

        res2 = await ac.post("/api/items", json={
            "title": "Soccer Match",
            "item_type": "event",
            "person": "Ann",
            "date": "2026-09-08",
            "start_time": "18:00:00",
            "location": "Pitch A",
        })
        assert res2.status_code == 200
        id2 = res2.json()["id"]

        # Verify members endpoint works end-to-end
        members_res = await ac.get("/api/members")
        assert members_res.status_code == 200
        member_list = members_res.json()
        assert len(member_list) >= 3

        # Fetch items
        list_res = await ac.get("/api/items?start_date=2026-09-01&end_date=2026-09-15")
        assert list_res.status_code == 200
        fetched = list_res.json()
        titles = [item["title"] for item in fetched]
        assert "Swimming Class" in titles
        assert "Soccer Match" in titles

        # Verify iCal feed contains items and correct headers
        cal_res = await ac.get("/api/calendar.ics")
        assert cal_res.status_code == 200
        assert "text/calendar" in cal_res.headers["content-type"]
        cal_text = cal_res.text
        assert "Swimming Class" in cal_text
        assert "Soccer Match" in cal_text

        # Cleanup
        await ac.delete(f"/api/items/{id1}")
        await ac.delete(f"/api/items/{id2}")
