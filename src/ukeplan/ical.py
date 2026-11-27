import datetime
from typing import List
from icalendar import Calendar, Event, vText
from ukeplan.models import PlanItem, ItemType


def _dedupe_identical_items(items: List[PlanItem]) -> List[tuple]:
    """
    Group plan items that are identical apart from ``person`` (the same activity
    saved once per child) so the calendar gets ONE event per activity, with all
    children's names in the summary. Items differing in any other field stay
    separate. Returns (representative, persons, sorted_group_ids) per group.
    """
    def key(item):
        return (
            item.item_type, item.title, (item.description or "").strip(),
            item.date, item.start_time, item.end_time, item.location,
            item.recurring_weekly, item.day_of_week,
        )
    groups = {}
    order = []
    for item in items:
        k = key(item)
        if k not in groups:
            groups[k] = {"item": item, "persons": [], "ids": []}
            order.append(k)
        g = groups[k]
        if item.person and item.person not in g["persons"]:
            g["persons"].append(item.person)
        g["ids"].append(item.id)
    out = []
    for k in order:
        g = groups[k]
        persons = g["persons"] or [g["item"].person]
        # Stable UID over *all* merged row ids: deleting one duplicate keeps the
        # surviving event's UID intact instead of spawning a new event in Apple.
        group_ids = "+".join(sorted(str(i) for i in g["ids"]))
        out.append((g["item"], persons, group_ids))
    return out


def generate_ical_feed(items: List[PlanItem], cal_name: str = "Family Week Planner") -> bytes:
    """
    Generates standard RFC 5545 iCalendar data (.ics) suitable for Apple Calendar subscription.
    """
    cal = Calendar()
    cal.add("prodid", "-//WeekPlanner//Family Week Planner//EN")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", cal_name)
    cal.add("x-wr-timezone", "Europe/Oslo")

    for item, persons, group_ids in _dedupe_identical_items(items):
        ev = Event()
        named = [p for p in persons if p]
        summary_prefix = f"[{', '.join(named)}] " if named else ""

        type_emoji = {
            ItemType.EVENT: "📅",
            ItemType.GEAR: "🎒",
            ItemType.TASK: "📝",
            ItemType.NOTE: "📌",
            ItemType.MIDDAG: "🍽️",
        }.get(item.item_type, "📅")

        ev.add("summary", f"{type_emoji} {summary_prefix}{item.title}")
        # UID covers all merged row ids (see _dedupe_identical_items): deleting
        # one duplicate keeps the surviving event's UID stable in Apple.
        ev.add("uid", f"ukeplan-{group_ids}@{cal_name.lower().replace(' ', '-')}")

        # Date / time handling
        if item.start_time:
            dtstart = datetime.datetime.combine(item.date, item.start_time)
            if item.end_time:
                dtend = datetime.datetime.combine(item.date, item.end_time)
            else:
                dtend = dtstart + datetime.timedelta(hours=1)
            ev.add("dtstart", dtstart)
            ev.add("dtend", dtend)
        else:
            # All-day event
            ev.add("dtstart", item.date)
            ev.add("dtend", item.date + datetime.timedelta(days=1))
        desc_lines = []
        if item.description:
            desc_lines.append(item.description)
        if item.item_type == ItemType.GEAR:
            desc_lines.append("Remember to pack / bring!")
        if item.source_filename:
            desc_lines.append(f"Source: {item.source_filename}")
        if item.recurring_weekly:
            desc_lines.append("Gjentar hver uke (Repeats every week)")
        if desc_lines:
            ev.add("description", "\n\n".join(desc_lines))

        if item.location:
            ev.add("location", item.location)
        if item.recurring_weekly and item.day_of_week:
            byday = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")[min(max(int(item.day_of_week), 1), 7) - 1]
            ev.add("rrule", {"FREQ": "WEEKLY", "BYDAY": byday})

        cal.add_component(ev)

    return cal.to_ical()
