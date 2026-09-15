import datetime
from enum import Enum
from typing import Optional, List
from sqlmodel import SQLModel, Field, Relationship


class ItemType(str, Enum):
    EVENT = "event"          # Timed or schedule event (e.g. soccer practice 17:30)
    GEAR = "gear"            # Things to remember/pack (e.g. gym kit, swimming gear, outdoor kit)
    TASK = "task"            # Homework or task (e.g. read pages 20-25, math sheet)
    NOTE = "note"            # General announcement or info (e.g. teacher planning day)
    MIDDAG = "middag"        # What's for dinner (e.g. "Lasagne"); person = who is responsible

class FamilyMemberBase(SQLModel):
    name: str
    school_class: Optional[str] = None  # e.g., "1A", "6B", "10C"
    color: Optional[str] = None         # e.g., hex or color name for UI
    display_order: int = 0


class FamilyMember(FamilyMemberBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))
    updated_at: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))


class PlanItemBase(SQLModel):
    title: str
    description: Optional[str] = None
    item_type: ItemType = ItemType.EVENT
    person: Optional[str] = None  # e.g., "Child 1 (11)" or "All"
    date: datetime.date
    recurring_weekly: bool = False  # repeats every week (e.g. from "hver onsdag" commands)
    day_of_week: Optional[int] = None  # 1=Monday..7=Sunday; used together with recurring_weekly
    start_time: Optional[datetime.time] = None
    end_time: Optional[datetime.time] = None
    location: Optional[str] = None
    is_completed: bool = False
    source_filename: Optional[str] = None


class PlanItem(PlanItemBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))
    updated_at: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))


class IngestLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))
    source_channel: str = "web"  # "shortcut", "email", "web"
    filename: str
    status: str = "success"  # "success", "failed", "pending"
    extracted_items_count: int = 0
    raw_preview: Optional[str] = None
    error_message: Optional[str] = None


class DocumentBase(SQLModel):
    filename: str
    stored_name: str
    person: Optional[str] = Field(default=None, index=True)  # child/owner tag, e.g. "Ann"
    week_number: Optional[int] = Field(default=None, index=True)  # e.g. 36
    year: Optional[int] = None
    source_channel: str = "web"  # "shortcut", "email", "web"
    mime_type: str = "application/octet-stream"
    size_bytes: int = 0


class Document(DocumentBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))


class DocumentUpdate(SQLModel):
    person: str = ""
    week_number: Optional[int] = None


class SchoolSchedule(SQLModel, table=True):
    """Standing school timetable for one child (one row per child).
    Re-uploading for the same child replaces the previous schedule."""
    id: Optional[int] = Field(default=None, primary_key=True)
    person: str = Field(index=True)  # the child it belongs to, e.g. "Ann"
    filename: str
    stored_name: str
    year: Optional[int] = None  # academic year tag (e.g. 2026)
    mime_type: str = "application/pdf"
    size_bytes: int = 0
    created_at: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))
    updated_at: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))

