import datetime
from typing import List
from icalendar import Calendar, Event, vText
from ukeplan.models import PlanItem, ItemType


def generate_ical_feed(items: List[PlanItem], cal_name: str = "Family Week Planner") -> bytes:
    """
    Generates standard RFC 5545 iCalendar data (.ics) suitable for Apple Calendar subscription.
    """
    cal = Calendar()
    cal.add("prodid", "-//WeekPlanner//Family Week Planner//EN")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", cal_name)
    cal.add("x-wr-timezone", "Europe/Oslo")

    for item in items:
        ev = Event()
        summary_prefix = ""
        if item.person:
            summary_prefix = f"[{item.person}] "

        type_emoji = {
            ItemType.EVENT: "📅",
            ItemType.GEAR: "🎒",
            ItemType.TASK: "📝",
            ItemType.NOTE: "📌",
        }.get(item.item_type, "📅")

        ev.add("summary", f"{type_emoji} {summary_prefix}{item.title}")
        ev.add("uid", f"ukeplan-{item.id}@{cal_name.lower().replace(' ', '-')}")

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
