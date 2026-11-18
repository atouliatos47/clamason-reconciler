"""
PO Approvals — the rules for the quote tracker (Maintenance tab).

A supplier quote arrives, gets sent to a director or Purchasing for
approval, and then waits — sometimes for days. Each quote is one row in
the `quotes` table (see schema.sql); this file holds the rules around
those rows, so routes.py only has to pass data in and out:

  - STATUSES: the stages a quote goes through, in order, with the label
    the page shows for each.
  - chase_date_after(): when to chase an approval — CHASE_AFTER_WORKING_DAYS
    working days after it was sent, skipping weekends and the site's
    non-working days (the same calendar the upload reminders use).
  - clean_quote(): checks a quote typed into the form before it is saved,
    and returns a list of plain-English problems if anything is wrong.

The quote file itself is never uploaded. It stays in the Purchase Orders
folder on Clamason's OneDrive, and the quote stores the "Copy link" web
address to it (file_link).
"""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from reminders import is_working_day, london_today

# How long to wait for an approval before chasing it. Set by Andreas,
# 18 Nov 2026. Each quote's chase date can still be changed by hand.
CHASE_AFTER_WORKING_DAYS = 2

# Every stage a quote can be in, in the order it normally moves through
# them. The key is what's saved in the database; the value is what the
# page shows. 'cancelled' is for a quote that isn't going ahead.
STATUSES = {
    'received':  'Quote received',
    'sent':      'Sent for approval',
    'approved':  'Approved',
    'po_raised': 'PO raised',
    'done':      'Done',
    'cancelled': 'Cancelled',
}

# The people quotes are sent to for approval — the "Sent to" drop-down.
# Set by Andreas, 18 Nov 2026. The page also offers "Other…" for anyone
# not on the list, so a name missing here never blocks a save; add them
# here if they come up often.
APPROVERS = [
    'Ian Ford',
    'Paul Edwards',
    'James Hadley',
    'Clare Whitehouse',
    'Gurdeep Sahota',
]

# Stages where we're still waiting on someone else's approval — the only
# ones that get a chase date.
WAITING_STATUSES = {'sent'}

# Longest text accepted in each box, so a mistaken paste can't fill the
# database. Generous for normal use.
_MAX_LEN = {
    'supplier': 120,
    'description': 500,
    'asset_no': 40,
    'quote_ref': 80,
    'file_link': 2000,
    'sent_to': 120,
    'po_number': 40,
}


def chase_date_after(sent_date, working_days=CHASE_AFTER_WORKING_DAYS):
    """The date `working_days` working days after `sent_date`.

    Sent on a Monday -> chase on Wednesday. Sent on a Friday -> the
    weekend doesn't count, so chase on Tuesday.
    """
    d, counted = sent_date, 0
    while counted < working_days:
        d += timedelta(days=1)
        if is_working_day(d):
            counted += 1
    return d


def _text(data, key):
    """The trimmed text from a form box, or None if it was left empty."""
    value = data.get(key)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _date(data, key, problems, label):
    """A date from the form ('2026-11-18'), or None if left empty."""
    value = _text(data, key)
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        problems.append(f'{label} is not a valid date.')
        return None


def clean_quote(data):
    """Check a quote from the form and tidy it up for saving.

    Returns (quote, problems). `quote` is a dict ready for
    db.add_quote(); `problems` is a list of messages to show the user.
    If `problems` is not empty, nothing should be saved.
    """
    problems = []
    quote = {key: _text(data, key) for key in _MAX_LEN}

    if not quote['supplier']:
        problems.append('Supplier is required.')
    if not quote['description']:
        problems.append('"What it\'s for" is required.')

    for key, limit in _MAX_LEN.items():
        if quote[key] and len(quote[key]) > limit:
            problems.append(f'{key.replace("_", " ").capitalize()} is too long '
                            f'(max {limit} characters).')

    # Only web links are allowed. A link is shown on the page as something
    # you can click, and a "link" starting with javascript: would run code
    # when clicked instead of opening a file.
    link = quote['file_link']
    if link and not link.lower().startswith(('https://', 'http://')):
        problems.append('File link must be a web link starting with https:// '
                        '(right-click the file in OneDrive and choose Copy link).')

    # Money: stored exactly (Decimal), never as a float. "£1,250.00" is fine.
    amount = _text(data, 'amount')
    quote['amount'] = None
    if amount:
        try:
            value = Decimal(amount.replace('£', '').replace(',', ''))
            if value < 0:
                raise InvalidOperation
            quote['amount'] = value.quantize(Decimal('0.01'))
        except InvalidOperation:
            problems.append('Amount must be a number, like 1250.00.')

    status = _text(data, 'status') or 'received'
    if status not in STATUSES:
        problems.append(f'Unknown status "{status}".')
    quote['status'] = status

    quote['received_date'] = _date(data, 'received_date', problems,
                                   'Date received') or london_today()
    quote['sent_date'] = _date(data, 'sent_date', problems, 'Date sent')
    quote['chase_date'] = _date(data, 'chase_date', problems, 'Chase date')

    if status in WAITING_STATUSES:
        # Sent for approval: if no date was typed, it was sent today, and
        # if no chase date was typed, it's 2 working days after sending.
        if quote['sent_date'] is None:
            quote['sent_date'] = london_today()
        if quote['chase_date'] is None:
            quote['chase_date'] = chase_date_after(quote['sent_date'])
    else:
        # Not sent yet, or already past waiting (approved, PO raised, done,
        # cancelled): nothing to chase, so no chase date. Moving a quote
        # back to "sent" gives it a fresh one.
        quote['chase_date'] = None

    note = _text(data, 'note')
    if note and len(note) > 1000:
        problems.append('Note is too long (max 1000 characters).')
    quote['note'] = note

    return quote, problems


def clean_note(data):
    """Check a note added to an existing quote.

    Returns (note, note_date, problems). The date defaults to today if
    it was left empty.
    """
    problems = []
    note = _text(data, 'note')
    if not note:
        problems.append('Type a note first.')
    elif len(note) > 1000:
        problems.append('Note is too long (max 1000 characters).')
    note_date = _date(data, 'note_date', problems, 'Note date') or london_today()
    return note, note_date, problems


def status_change_note(old_status, new_status):
    """The note logged automatically when a quote's status changes, e.g.
    'Status: Sent for approval -> Approved'. None if it didn't change."""
    if old_status == new_status:
        return None
    return (f'Status: {STATUSES.get(old_status, old_status)} → '
            f'{STATUSES.get(new_status, new_status)}')


def _money_text(amount):
    return f'£{Decimal(str(amount)):,.2f}' if amount not in (None, '') else 'none'


def _uk_date_text(iso):
    return date.fromisoformat(str(iso)[:10]).strftime('%d/%m/%Y') if iso else 'none'


def new_version_changes(existing, found, filename):
    """A new version of a quote we already have (same quote reference) has
    arrived as a PDF. Works out what to change on the saved quote and the
    note to log.

    Only the amount and the quote's date are taken from the new PDF —
    the details that change when a supplier revises a quote. Status,
    "sent to" and chase date stay as they were: the approval is still
    in progress.

    existing: the saved quote (from db.get_quote); found: what
    parsers/quote_pdf.py read from the new PDF. Returns (changes, note).
    """
    changes, parts = {}, []
    new_amount = found.get('amount')
    if new_amount and (existing.get('amount') is None
                       or Decimal(str(new_amount)) != Decimal(str(existing['amount']))):
        changes['amount'] = Decimal(str(new_amount))
        parts.append(f"amount {_money_text(existing.get('amount'))} → {_money_text(new_amount)}")
    new_date = found.get('received_date')
    if new_date and new_date != (existing.get('received_date') or '')[:10]:
        changes['received_date'] = date.fromisoformat(new_date)
        parts.append(f"date {_uk_date_text(existing.get('received_date'))} → {_uk_date_text(new_date)}")
    note = f'New version of the quote added ({filename})'
    note += ': ' + ', '.join(parts) if parts else ' — amount and date unchanged'
    return changes, note


def chased_today(quote, note=None, today=None):
    """The "Chased today" button: you've chased the approval, so wait
    another CHASE_AFTER_WORKING_DAYS working days before the next chase.
    Returns (new_chase_date, note_text). The note defaults to
    'Chased Paul Edwards' (whoever it was sent to)."""
    today = today or london_today()
    text = (note or '').strip()[:1000]
    if not text:
        text = f"Chased {quote['sent_to']}" if quote.get('sent_to') else 'Chased'
    return chase_date_after(today), text


# --- Outlook reminder ----------------------------------------------------------
#
# A calendar file (.ics — the standard format Outlook, Google and Apple
# calendars all open) for a quote's chase date. Opening it in Outlook shows
# the event, and "Save & Close" puts it in the calendar with a pop-up
# reminder. Nothing is sent anywhere: it's a file for you to open.

# When the chase reminder appears on the chase date (UK time).
OUTLOOK_REMINDER_TIME = (8, 30)
OUTLOOK_REMINDER_MINUTES = 15


def _ics_text(value):
    """Calendar files give ',', ';', '\\' and new lines a special meaning,
    so they're escaped inside any text."""
    text = str(value or '')
    text = text.replace('\\', '\\\\')          # a backslash first, so the ones added below aren't doubled
    text = text.replace(';', '\\;')
    text = text.replace(',', '\\,')
    text = text.replace('\r\n', '\n')
    text = text.replace('\n', '\\n')
    return text


def _ics_fold(line):
    """Calendar files want lines of at most 75 bytes; longer ones carry on
    on the next line, which starts with a space."""
    out, current = [], ''
    for ch in line:
        if len((current + ch).encode('utf-8')) > 75:
            out.append(current)
            current = ' ' + ch
        else:
            current += ch
    out.append(current)
    return '\r\n'.join(out)


def outlook_reminder_ics(quote, hub_url):
    """The .ics file for one quote's chase date, as text."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    chase = date.fromisoformat(str(quote['chase_date'])[:10])
    hour, minute = OUTLOOK_REMINDER_TIME
    # 8:30 in the UK, written as UTC (the 'Z' times) so every calendar
    # shows it at the right moment in summer and winter time alike.
    start = datetime(chase.year, chase.month, chase.day, hour, minute,
                     tzinfo=ZoneInfo('Europe/London')).astimezone(timezone.utc)
    end = start + timedelta(minutes=OUTLOOK_REMINDER_MINUTES)
    stamp = datetime.now(timezone.utc)
    fmt = '%Y%m%dT%H%M%SZ'

    ref = f" ({quote['quote_ref']})" if quote.get('quote_ref') else ''
    summary = f"Chase PO approval: {quote['supplier']} \u2014 {quote['description']}{ref}"
    details = [f"Waiting for approval: {quote['supplier']} \u2014 {quote['description']}{ref}"]
    if quote.get('sent_to'):
        sent = f" on {date.fromisoformat(str(quote['sent_date'])[:10]):%d/%m/%Y}" if quote.get('sent_date') else ''
        details.append(f"Sent to {quote['sent_to']}{sent}.")
    if quote.get('amount') not in (None, ''):
        details.append(f"Amount: {_money_text(quote['amount'])} (net).")
    details.append(f"PO Approvals in the Hub: {hub_url.rstrip('/')}/maintenance-po-approvals")

    lines = [
        'BEGIN:VCALENDAR',
        'VERSION:2.0',
        'PRODID:-//Clamason//Performance Hub PO Approvals//EN',
        'METHOD:PUBLISH',
        'BEGIN:VEVENT',
        # Same id for the same quote and chase date, so opening the file twice
        # updates the one event instead of adding a second.
        f"UID:quote-{quote['id']}-chase-{chase.isoformat()}@clamason-performance-hub",
        f'DTSTAMP:{stamp.strftime(fmt)}',
        f'DTSTART:{start.strftime(fmt)}',
        f'DTEND:{end.strftime(fmt)}',
        f'SUMMARY:{_ics_text(summary)}',
        f"DESCRIPTION:{_ics_text(chr(10).join(details))}",
        'TRANSP:TRANSPARENT',          # shows you as free, not busy
        'BEGIN:VALARM',
        'ACTION:DISPLAY',
        f'DESCRIPTION:{_ics_text(summary)}',
        'TRIGGER:-PT0M',               # pop up at the start time
        'END:VALARM',
        'END:VEVENT',
        'END:VCALENDAR',
    ]
    return '\r\n'.join(_ics_fold(line) for line in lines) + '\r\n'
