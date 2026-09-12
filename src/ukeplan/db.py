import datetime
import pathlib
from typing import Generator, Optional
from sqlmodel import SQLModel, create_engine, Session, select
from ukeplan.config import settings

# SQLite connection with connect_args for multithreading
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, echo=False, connect_args=connect_args)


def init_db():
    from ukeplan.models import FamilyMember, PlanItem
    pathlib.Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(engine)
    # Lightweight migration for older databases: add new columns if missing
    with engine.begin() as conn:
        table_name = PlanItem.__table__.name  # "planitem"
        existing_cols = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table_name})")}
        if "recurring_weekly" not in existing_cols:
            conn.exec_driver_sql(f"ALTER TABLE {table_name} ADD COLUMN recurring_weekly BOOLEAN NOT NULL DEFAULT 0")
        if "day_of_week" not in existing_cols:
            conn.exec_driver_sql(f"ALTER TABLE {table_name} ADD COLUMN day_of_week INTEGER")
    with Session(engine) as session:
        from sqlmodel import select
        existing = session.exec(select(FamilyMember)).first()
        if not existing:
            for item in settings.default_members:
                member = FamilyMember(**item)
                session.add(member)
            session.commit()


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session


def purge_stale_items(
    cutoff_weeks: Optional[int] = None,
    as_of: Optional[datetime.date] = None,
    session: Optional[Session] = None,
) -> int:
    """
    Remove data that is older than the retention window:

    * calendar entries (``PlanItem``) whose ``date`` is more than ``cutoff_weeks``
      in the past, and
    * uploaded documents (``Document``) whose ``created_at`` is more than
      ``cutoff_weeks`` in the past, together with their file on disk.

    Recurring-weekly items are standing entries (they repeat every week) and are
    never auto-removed. Returns the total number of rows removed (calendar items
    plus documents). When ``session`` is omitted a short-lived session on the app
    engine is used and committed.
    """
    weeks = settings.item_retention_weeks if cutoff_weeks is None else cutoff_weeks
    if weeks <= 0:
        return 0
    today = datetime.date.today() if as_of is None else as_of
    cutoff = today - datetime.timedelta(weeks=weeks)

    if session is None:
        with Session(engine) as s:
            return purge_stale_items(weeks, today, s)

    from ukeplan.models import PlanItem, Document
    stale = session.exec(
        select(PlanItem).where(
            PlanItem.date < cutoff,
            PlanItem.recurring_weekly == False,  # noqa: E712
        )
    ).all()
    for item in stale:
        session.delete(item)

    # Documents (PDFs/photos uploaded for OCR) are pruned by upload age, and
    # their stored file is removed from disk. created_at is stored as UTC, so
    # compare against the same cutoff expressed as a UTC datetime.
    from ukeplan.storage import remove_stored_file
    cutoff_dt = datetime.datetime.combine(
        cutoff, datetime.time.min, tzinfo=datetime.timezone.utc
    )
    stale_docs = session.exec(
        select(Document).where(Document.created_at < cutoff_dt)
    ).all()
    for doc in stale_docs:
        remove_stored_file(doc.stored_name)
        session.delete(doc)

    if stale or stale_docs:
        session.commit()
    return len(stale) + len(stale_docs)
