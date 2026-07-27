"""calendar-connector — polls a calendar's private iCal (ICS) feed and POSTs
events to meeting-service. A thin adapter: all writes flow through the governed
domain-service path (OPA preflight, transactional audit, CDC) — no side doors.

Works with any ICS feed. For Google Calendar: Settings -> [your calendar] ->
"Integrate calendar" -> "Secret address in iCal format".

Env: CALENDAR_ICS_URL (required to do anything), MEETING_SERVICE_URL,
     ACTOR, PRINCIPAL, POLL_SECONDS (default 300), WINDOW_DAYS (default 14).
"""

import os
import time
from datetime import datetime, timedelta, timezone

import httpx
import recurring_ical_events
from icalendar import Calendar

ICS_URL = os.environ.get("CALENDAR_ICS_URL", "").strip()
MEETING_URL = os.environ.get("MEETING_SERVICE_URL", "http://meeting-service:8000")
ACTOR = os.environ.get("ACTOR", "leader@example.com")
PRINCIPAL = os.environ.get("PRINCIPAL", "principal-001")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "300"))
WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "14"))

HEADERS = {"X-Actor": ACTOR, "X-Principal": PRINCIPAL}


def iso(dt) -> str:
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    return f"{dt}T00:00:00+00:00"  # all-day event (date only)


def attendee_emails(event) -> list[str]:
    out = []
    attendees = event.get("ATTENDEE")
    if attendees is None:
        return out
    if not isinstance(attendees, list):
        attendees = [attendees]
    for a in attendees:
        email = str(a).replace("mailto:", "").replace("MAILTO:", "").strip()
        if "@" in email:
            out.append(email.lower())
    return out


def sync_once() -> None:
    resp = httpx.get(ICS_URL, timeout=30, follow_redirects=True)
    resp.raise_for_status()
    cal = Calendar.from_ical(resp.content)

    start = datetime.now(timezone.utc) - timedelta(days=1)
    end = datetime.now(timezone.utc) + timedelta(days=WINDOW_DAYS)
    events = recurring_ical_events.of(cal).between(start, end)

    created = updated = unchanged = failed = 0
    for ev in events:
        uid = str(ev.get("UID", "")) or None
        body = {
            "title": str(ev.get("SUMMARY", "Untitled")),
            "start_at": iso(ev.get("DTSTART").dt),
            "end_at": iso(ev.get("DTEND").dt) if ev.get("DTEND") else None,
            "attendees": attendee_emails(ev),
            "location": str(ev.get("LOCATION")) if ev.get("LOCATION") else None,
            "source": "ics",
            "ics_uid": uid,
        }
        try:
            r = httpx.post(f"{MEETING_URL}/meetings", json=body, headers=HEADERS, timeout=10)
            r.raise_for_status()
            v = r.json().get("version", 1)
            if r.status_code == 201 and v == 1:
                created += 1
            elif v > 1:
                updated += 1
            else:
                unchanged += 1
        except Exception as exc:
            failed += 1
            print(f"  event failed ({body['title']}): {exc}", flush=True)
    print(f"sync: {len(events)} events in window — {created} new, {updated} updated, "
          f"{unchanged} unchanged, {failed} failed", flush=True)


def main() -> None:
    if not ICS_URL:
        print("CALENDAR_ICS_URL not set — connector idle. Add it to platform/.env "
              "and restart to sync a real calendar.", flush=True)
        while True:
            time.sleep(3600)
    print(f"calendar-connector: polling every {POLL_SECONDS}s as {ACTOR} -> {PRINCIPAL}", flush=True)
    while True:
        try:
            sync_once()
        except Exception as exc:
            print(f"sync failed: {exc}", flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
