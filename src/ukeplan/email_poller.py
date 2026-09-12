import asyncio
import email
from email import policy
import imaplib
import logging
from typing import Optional
from sqlmodel import Session, select
from ukeplan.config import settings
from ukeplan.db import engine
from ukeplan.models import PlanItem, IngestLog, FamilyMember, Document
from ukeplan.extractor import extract_text_and_images_from_bytes
from ukeplan.ai import parse_plan_text_with_llm, resolve_item_date, infer_person_from_items
from ukeplan.storage import store_document, mime_for
import datetime

logger = logging.getLogger("ukeplan.email_poller")


async def process_email_message(raw_bytes: bytes):
    """Parses an email message, extracts PDF/image attachments or body text, and ingests them."""
    msg = email.message_from_bytes(raw_bytes, policy=policy.default)
    subject = msg.get("Subject", "Uten emne")
    sender = msg.get("From", "Ukjent avsender")
    logger.info(f"Behandler e-post: '{subject}' fra {sender}")

    attachments = []
    text_body = ""

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            filename = part.get_filename()
            if filename:
                payload = part.get_payload(decode=True)
                attachments.append((filename, payload))
            elif content_type == "text/plain":
                text_body += part.get_content()
    else:
        text_body = msg.get_content()

    with Session(engine) as session:
        members_db = session.exec(select(FamilyMember).order_by(FamilyMember.display_order, FamilyMember.id)).all()
        members_list = [{"name": m.name, "school_class": m.school_class} for m in members_db]
        # If there are attachments (e.g. school weekplan PDF), process each
        if attachments:
            for filename, payload in attachments:
                logger.info(f"Fant vedlegg: {filename}")
                doc_data = await extract_text_and_images_from_bytes(payload, filename)
                text = doc_data["text"] or text_body
                if not text:
                    continue
                preview = text[:400]
                parse_call = parse_plan_text_with_llm(text=text, family_members=members_list)

                log_entry = IngestLog(
                    source_channel="email",
                    filename=f"{filename} (fra: {sender})",
                    status="pending",
                    raw_preview=preview,
                )
                session.add(log_entry)
                session.commit()

                # Archive the attachment (kept even if AI parsing fails).
                stored_name = store_document(payload, filename)
                doc_record = Document(
                    filename=filename,
                    stored_name=stored_name,
                    source_channel="email",
                    mime_type=mime_for(filename),
                    size_bytes=len(payload),
                )
                session.add(doc_record)
                session.commit()
                session.refresh(doc_record)

                try:
                    plan = await parse_call
                    doc_record.week_number = plan.week_number
                    doc_record.year = plan.year or datetime.date.today().year
                    doc_record.person = doc_record.person or infer_person_from_items(plan.items)
                    session.add(doc_record)
                    count = 0
                    for it in plan.items:
                        d = resolve_item_date(it, plan)
                        st = datetime.time.fromisoformat(it.start_time) if it.start_time else None
                        et = datetime.time.fromisoformat(it.end_time) if it.end_time else None
                        item = PlanItem(
                            title=it.title,
                            description=it.description,
                            item_type=it.item_type,
                            person=it.person,
                            date=d,
                            start_time=st,
                            end_time=et,
                            location=it.location,
                            source_filename=f"Email: {filename}",
                        )
                        session.add(item)
                        count += 1
                    log_entry.status = "success"
                    log_entry.extracted_items_count = count
                    session.add(log_entry)
                    session.commit()
                except Exception as e:
                    logger.error(f"Feil ved AI-tolking av e-postvedlegg {filename}: {e}")
                    log_entry.status = "failed"
                    log_entry.error_message = str(e)
                    session.add(log_entry)
                    session.commit()
        elif text_body:
            # Body text itself might be the week schedule
            logger.info("Ingen vedlegg funnet, behandler selve e-postteksten")
            log_entry = IngestLog(
                source_channel="email",
                filename=f"Tekst: {subject} (fra: {sender})",
                status="pending",
                raw_preview=text_body[:400],
            )
            session.add(log_entry)
            session.commit()
            try:
                plan = await parse_plan_text_with_llm(text=text_body, family_members=members_list)
                count = 0
                for it in plan.items:
                    d = resolve_item_date(it, plan)
                    st = datetime.time.fromisoformat(it.start_time) if it.start_time else None
                    et = datetime.time.fromisoformat(it.end_time) if it.end_time else None
                    item = PlanItem(
                        title=it.title,
                        description=it.description,
                        item_type=it.item_type,
                        person=it.person,
                        date=d,
                        start_time=st,
                        end_time=et,
                        location=it.location,
                        source_filename=f"Email: {subject}",
                    )
                    session.add(item)
                    count += 1
                log_entry.status = "success"
                log_entry.extracted_items_count = count
                session.add(log_entry)
                session.commit()
            except Exception as e:
                logger.error(f"Feil ved AI-tolking av e-postinnhold: {e}")
                log_entry.status = "failed"
                log_entry.error_message = str(e)
                session.add(log_entry)
                session.commit()


def check_mailbox_sync():
    """Synchronously connect and fetch unseen emails via IMAP."""
    if not settings.imap_enabled or not settings.imap_password:
        return []

    mail_messages = []
    try:
        mail = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
        mail.login(settings.imap_user, settings.imap_password)
        mail.select(settings.imap_folder)

        status, response = mail.search(None, "UNSEEN")
        if status == "OK" and response[0]:
            msg_ids = response[0].split()
            for msg_id in msg_ids:
                fetch_status, fetch_data = mail.fetch(msg_id, "(RFC822)")
                if fetch_status == "OK":
                    for response_part in fetch_data:
                        if isinstance(response_part, tuple):
                            mail_messages.append(response_part[1])
                # Mark as seen
                mail.store(msg_id, "+FLAGS", "\\Seen")

        mail.close()
        mail.logout()
    except Exception as e:
        logger.error(f"IMAP feil ved henting av e-post: {e}")

    return mail_messages


async def start_imap_poller():
    """Background task running in the FastAPI event loop to poll emails at regular intervals."""
    if not settings.imap_enabled:
        logger.info("IMAP polling er deaktivert i innstillingene (imap_enabled=False).")
        return

    logger.info(f"Starter IMAP polling for {settings.imap_user} hvert {settings.imap_poll_interval_seconds} sek.")
    while True:
        try:
            raw_emails = await asyncio.to_thread(check_mailbox_sync)
            for raw_msg in raw_emails:
                await process_email_message(raw_msg)
        except Exception as e:
            logger.error(f"Uventet feil i IMAP polling loop: {e}")

        await asyncio.sleep(settings.imap_poll_interval_seconds)
