import datetime
import logging
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import FileResponse, Response
from sqlmodel import Session, select, SQLModel
from ukeplan.db import get_session
from ukeplan.models import PlanItem, IngestLog, PlanItemBase, FamilyMember, FamilyMemberBase, Document, DocumentUpdate, SchoolSchedule
from ukeplan.config import settings
from ukeplan.extractor import extract_text_and_images_from_bytes
from ukeplan.storage import store_document, document_path, remove_stored_file, mime_for
from ukeplan.ai import parse_plan_text_with_llm, resolve_item_date, infer_person_from_items, parse_command_with_llm, next_weekday_date
from ukeplan.ical import generate_ical_feed

logger = logging.getLogger("ukeplan.api")
api_router = APIRouter(prefix="/api")


@api_router.get("/members", response_model=List[FamilyMember])
def list_members(
    session: Session = Depends(get_session),
):
    query = select(FamilyMember).order_by(FamilyMember.display_order, FamilyMember.id)
    return session.exec(query).all()


@api_router.post("/members", response_model=FamilyMember)
def create_member(
    member_in: FamilyMemberBase,
    session: Session = Depends(get_session),
):
    member = FamilyMember.model_validate(member_in)
    session.add(member)
    session.commit()
    session.refresh(member)
    return member


@api_router.put("/members/{member_id}", response_model=FamilyMember)
def update_member(
    member_id: int,
    member_in: FamilyMemberBase,
    session: Session = Depends(get_session),
):
    member = session.get(FamilyMember, member_id)
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")
    old_name = member.name
    member_data = member_in.model_dump(exclude_unset=True)
    for key, value in member_data.items():
        setattr(member, key, value)
    member.updated_at = datetime.datetime.now(datetime.timezone.utc)
    session.add(member)

    # Also optionally update plan items if name was renamed
    if old_name != member.name:
        items = session.exec(select(PlanItem).where(PlanItem.person == old_name)).all()
        for it in items:
            it.person = member.name
            session.add(it)

    session.commit()
    session.refresh(member)
    return member


@api_router.delete("/members/{member_id}")
def delete_member(
    member_id: int,
    session: Session = Depends(get_session),
):
    member = session.get(FamilyMember, member_id)
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")
    session.delete(member)
    session.commit()
    return {"status": "deleted", "id": member_id}


def _item_visible_in_range(item: PlanItem, start_date: Optional[datetime.date], end_date: Optional[datetime.date]) -> bool:
    """True if the item's date lies in [start_date, end_date], or if it is a weekly
    recurring item with an occurrence inside the range (it shows up in every week)."""
    d = item.date
    if (start_date and d < start_date) or (end_date and d > end_date):
        if item.recurring_weekly and item.day_of_week:
            lo = start_date or (d - datetime.timedelta(days=365))
            hi = end_date or (d + datetime.timedelta(days=365))
            target = min(max(int(item.day_of_week), 1), 7)
            next_occ = lo + datetime.timedelta(days=(target - 1 - lo.weekday()) % 7)
            return next_occ <= hi
        return False
    return True


@api_router.get("/items", response_model=List[PlanItem])
def list_items(
    start_date: Optional[datetime.date] = None,
    end_date: Optional[datetime.date] = None,
    person: Optional[str] = None,
    session: Session = Depends(get_session),
):
    query = select(PlanItem)
    if person:
        query = query.where(PlanItem.person == person)
    items = session.exec(query).all()
    if start_date or end_date:
        items = [it for it in items if _item_visible_in_range(it, start_date, end_date)]
    items.sort(key=lambda i: (i.date, i.start_time is not None, i.start_time or datetime.time.min))
    return items


@api_router.post("/items", response_model=PlanItem)
def create_item(
    item_in: PlanItemBase,
    session: Session = Depends(get_session),
):
    item = PlanItem.model_validate(item_in)
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


@api_router.put("/items/{item_id}", response_model=PlanItem)
def update_item(
    item_id: int,
    item_in: PlanItemBase,
    session: Session = Depends(get_session),
):
    item = session.get(PlanItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    item_data = item_in.model_dump(exclude_unset=True)
    for key, value in item_data.items():
        setattr(item, key, value)
    item.updated_at = datetime.datetime.now(datetime.timezone.utc)
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


@api_router.delete("/items/{item_id}")
def delete_item(
    item_id: int,
    session: Session = Depends(get_session),
):
    item = session.get(PlanItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    session.delete(item)
    session.commit()


def _iso_week_bounds(year: int, week: int) -> tuple[datetime.date, datetime.date]:
    """Monday and Sunday of an ISO week."""
    monday = datetime.date.fromisocalendar(year, week, 1)
    return monday, monday + datetime.timedelta(days=6)


@api_router.delete("/items")
def delete_all_items(
    week: Optional[int] = Query(default=None, ge=1, le=53),
    year: Optional[int] = Query(default=None, ge=2000, le=2100),
    session: Session = Depends(get_session),
):
    """Delete all plan items, or only the items in one ISO week when week+year are given."""
    query = select(PlanItem)
    if week is not None:
        if year is None:
            raise HTTPException(status_code=400, detail="year is required together with week")
        monday, sunday = _iso_week_bounds(year, week)
        query = query.where(PlanItem.date >= monday, PlanItem.date <= sunday)
    items = session.exec(query).all()
    for item in items:
        session.delete(item)
    session.commit()
    return {"status": "deleted", "count": len(items)}


@api_router.post("/items/{item_id}/toggle")
def toggle_item_completed(
    item_id: int,
    session: Session = Depends(get_session),
):
    item = session.get(PlanItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    item.is_completed = not item.is_completed
    item.updated_at = datetime.datetime.now(datetime.timezone.utc)
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


class CommandIn(SQLModel):
    text: str


@api_router.post("/commands")
async def run_command(cmd: CommandIn, session: Session = Depends(get_session)):
    """
    Natural-language command -> structured calendar items.
    E.g. "Sigrid har sjakktrening hver onsdag 18:00-19:50".
    """
    if not cmd.text or not cmd.text.strip():
        raise HTTPException(status_code=400, detail="Empty command")

    members_db = session.exec(select(FamilyMember).order_by(FamilyMember.display_order, FamilyMember.id)).all()
    members_list = [{"name": m.name, "school_class": m.school_class} for m in members_db]

    try:
        result = await parse_command_with_llm(text=cmd.text, family_members=members_list)
    except Exception as e:
        logger.exception(f"Command parse failed for '{cmd.text}': {e}")
        raise HTTPException(status_code=500, detail=f"Could not process command: {str(e)}")

    saved_items = []
    for it in result.items:
        if it.date:
            try:
                item_date = datetime.date.fromisoformat(it.date)
            except ValueError:
                logger.warning(f"Ignoring invalid date '{it.date}' in command item '{it.title}'")
                item_date = next_weekday_date(it.day_of_week) if it.day_of_week else datetime.date.today()
        elif it.day_of_week:
            item_date = next_weekday_date(it.day_of_week)
        else:
            item_date = datetime.date.today()

        st = None
        et = None
        if it.start_time:
            try:
                st = datetime.time.fromisoformat(it.start_time)
            except ValueError:
                pass
        if it.end_time:
            try:
                et = datetime.time.fromisoformat(it.end_time)
            except ValueError:
                pass

        recurring = bool(it.recurring_weekly and it.day_of_week)
        item = PlanItem(
            title=it.title,
            description=it.description,
            item_type=it.item_type,
            person=it.person,
            date=item_date,
            start_time=st,
            end_time=et,
            location=it.location,
            recurring_weekly=recurring,
            day_of_week=it.day_of_week if recurring else None,
            source_filename="command",
        )
        session.add(item)
        saved_items.append(item)
    session.commit()
    for item in saved_items:
        session.refresh(item)

    logger.info(f"Command '{cmd.text}' -> {len(saved_items)} item(s). Summary: '{result.summary}'")
    return {
        "status": "success",
        "summary": result.summary,
        "items": saved_items,
    }


@api_router.post("/ingest")
async def ingest_document(
    file: UploadFile = File(...),
    person: Optional[str] = Form(default=None),
    channel: str = Form(default="shortcut"),
    session: Session = Depends(get_session),
):
    """
    Ingestion endpoint for Apple Shortcut, Web upload, or Email webhook.
    Receives PDF or image, extracts text, calls AI model, and stores items in DB.
    """
    content = await file.read()
    filename = file.filename or "uploaded_file"

    log_entry = IngestLog(
        source_channel=channel,
        filename=filename,
        status="pending",
    )
    session.add(log_entry)
    session.commit()
    session.refresh(log_entry)

    try:
        logger.info(f"--- [INGEST START] filename='{filename}', channel='{channel}', person='{person}' ---")
        members_db = session.exec(select(FamilyMember).order_by(FamilyMember.display_order, FamilyMember.id)).all()
        members_list = [{"name": m.name, "school_class": m.school_class} for m in members_db]
        logger.info(f"Registered members for matching: {members_list}")

        # Archive the uploaded file (kept even if AI parsing fails).
        stored_name = store_document(content, filename)
        doc_record = Document(
            filename=filename,
            stored_name=stored_name,
            person=person,
            source_channel=channel,
            mime_type=mime_for(filename),
            size_bytes=len(content),
        )
        session.add(doc_record)
        session.commit()
        session.refresh(doc_record)

        doc_data = await extract_text_and_images_from_bytes(content, filename)
        text = doc_data["text"]
        logger.info(f"Document extraction finished. Text length: {len(text)} chars, page_count: {doc_data.get('page_count')}")
        if not text:
            logger.warning(f"No readable text found in document: '{filename}'")
            raise ValueError("No readable text found in the document.")
        log_entry.raw_preview = (text + "...") if len(text) > 400 else text
        logger.info(f"Raw text preview:\n{text}...")
        extracted_plan = await parse_plan_text_with_llm(
            text=text,
            target_person=person,
            family_members=members_list,
        )

        # Tag the archived document with the plan's week and (if known) the child.
        doc_record.week_number = extracted_plan.week_number
        doc_record.year = extracted_plan.year or datetime.date.today().year
        if not doc_record.person:
            doc_record.person = infer_person_from_items(extracted_plan.items)
        session.add(doc_record)
        session.commit()
        saved_items = []
        for it in extracted_plan.items:
            item_date = resolve_item_date(it, extracted_plan)
            st = None
            et = None
            if it.start_time:
                try:
                    st = datetime.time.fromisoformat(it.start_time)
                except ValueError:
                    pass
            if it.end_time:
                try:
                    et = datetime.time.fromisoformat(it.end_time)
                except ValueError:
                    pass

            item = PlanItem(
                title=it.title,
                description=it.description,
                item_type=it.item_type,
                person=it.person or person,
                date=item_date,
                start_time=st,
                end_time=et,
                location=it.location,
                source_filename=filename,
            )
            session.add(item)
            saved_items.append(item)
        session.commit()
        log_entry.status = "success"
        log_entry.extracted_items_count = len(saved_items)
        session.add(log_entry)
        session.commit()
        logger.info(f"--- [INGEST COMPLETE] Saved {len(saved_items)} items for '{filename}'. Summary: '{extracted_plan.summary}' ---")
        return {
            "status": "success",
            "filename": filename,
            "items_count": len(saved_items),
            "summary": extracted_plan.summary,
            "items": saved_items,
        }

    except Exception as e:
        logger.exception(f"--- [INGEST ERROR] Failed to ingest '{filename}': {e} ---")
        log_entry.status = "failed"
        log_entry.error_message = str(e)
        session.add(log_entry)
        session.commit()
        raise HTTPException(status_code=500, detail=f"Error processing document: {str(e)}")


@api_router.get("/documents", response_model=List[Document])
def list_documents(
    person: Optional[str] = Query(default=None),
    week: Optional[int] = Query(default=None),
    session: Session = Depends(get_session),
):
    query = select(Document)
    if person:
        query = query.where(Document.person == person)
    if week is not None:
        query = query.where(Document.week_number == week)
    query = query.order_by(Document.created_at.desc(), Document.id.desc())
    return session.exec(query).all()


@api_router.get("/documents/{document_id}/file")
def get_document_file(document_id: int, session: Session = Depends(get_session)):
    doc = session.get(Document, document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    path = document_path(doc.stored_name)
    if not path.exists():
        raise HTTPException(status_code=410, detail="Document file is missing on disk")
    return FileResponse(path, media_type=doc.mime_type)


@api_router.put("/documents/{document_id}", response_model=Document)
def update_document(document_id: int, doc_in: DocumentUpdate, session: Session = Depends(get_session)):
    doc = session.get(Document, document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    doc.person = doc_in.person.strip() or None
    doc.week_number = doc_in.week_number
    session.add(doc)
    session.commit()
    session.refresh(doc)
    return doc


@api_router.delete("/documents/{document_id}")
def delete_document(document_id: int, session: Session = Depends(get_session)):
    doc = session.get(Document, document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    remove_stored_file(doc.stored_name)
    session.delete(doc)
    session.commit()
    return {"status": "deleted", "id": document_id}




# ---------------------------------------------------------------------------
# School schedules: one standing timetable PDF per child. Re-uploading for the
# same child replaces the previous schedule (old file removed from disk).
# ---------------------------------------------------------------------------


@api_router.post("/schedules", response_model=SchoolSchedule)
async def upsert_schedule(
    file: UploadFile = File(...),
    person: str = Form(...),
    year: Optional[int] = Form(default=None),
    session: Session = Depends(get_session),
):
    """Store a child's school timetable. One row per child — a new upload for
    the same ``person`` replaces the existing schedule (file and record)."""
    content = await file.read()
    filename = file.filename or "schedule.pdf"
    person = (person or "").strip()
    if not person:
        raise HTTPException(status_code=422, detail="person is required")
    if not content:
        raise HTTPException(status_code=422, detail="Empty file")

    stored_name = store_document(content, filename)
    existing = session.exec(select(SchoolSchedule).where(SchoolSchedule.person == person)).first()

    if existing:
        # Replace: drop the old file on disk, overwrite this row.
        remove_stored_file(existing.stored_name)
        existing.filename = filename
        existing.stored_name = stored_name
        existing.year = year
        existing.mime_type = mime_for(filename)
        existing.size_bytes = len(content)
        existing.updated_at = datetime.datetime.now(datetime.timezone.utc)
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return existing

    schedule = SchoolSchedule(
        person=person,
        filename=filename,
        stored_name=stored_name,
        year=year,
        mime_type=mime_for(filename),
        size_bytes=len(content),
    )
    session.add(schedule)
    session.commit()
    session.refresh(schedule)
    return schedule


@api_router.get("/schedules", response_model=List[SchoolSchedule])
def list_schedules(
    person: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
):
    query = select(SchoolSchedule)
    if person:
        query = query.where(SchoolSchedule.person == person)
    query = query.order_by(SchoolSchedule.person, SchoolSchedule.updated_at.desc())
    return session.exec(query).all()


@api_router.get("/schedules/{schedule_id}/file")
def get_schedule_file(schedule_id: int, session: Session = Depends(get_session)):
    schedule = session.get(SchoolSchedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    path = document_path(schedule.stored_name)
    if not path.exists():
        raise HTTPException(status_code=410, detail="Schedule file is missing on disk")
    return FileResponse(path, media_type=schedule.mime_type)


@api_router.delete("/schedules/{schedule_id}")
def delete_schedule(schedule_id: int, session: Session = Depends(get_session)):
    schedule = session.get(SchoolSchedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    remove_stored_file(schedule.stored_name)
    session.delete(schedule)
    session.commit()
    return {"status": "deleted", "id": schedule_id}


@api_router.get("/calendar.ics")
def get_icalendar(
    token: Optional[str] = Query(default=None),
    person: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
):
    """
    Subscribable iCal (.ics) feed for Apple Calendar / iOS devices.
    """
    query = select(PlanItem)
    if person:
        query = query.where(PlanItem.person == person)
    start_boundary = datetime.date.today() - datetime.timedelta(days=14)
    # Weekly recurring items are always included (their RRULE keeps them alive in every week).
    items = [
        it for it in session.exec(query.order_by(PlanItem.date)).all()
        if it.date >= start_boundary or it.recurring_weekly
    ]

    cal_name = f"Ukeplan ({person})" if person else "Ukeplan Familie"
    ical_bytes = generate_ical_feed(items, cal_name=cal_name)
    return Response(content=ical_bytes, media_type="text/calendar")
