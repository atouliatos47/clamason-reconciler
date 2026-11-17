"""
Parser for the SFC "Daily Unplanned Downtime" PDF — the second report SFC
emails every morning, next to the Daily Downtime Summary.

Why it's needed: the Daily Downtime Summary PDF is one whole-site table.
It says "MA-FAULT PRESS, 4 events" but never which press. This report
has one page per machine, each with its own reason table, so it can.

What's in it (and what isn't):
  - Only machines that had UNPLANNED downtime that day get a page.
    A press that was planned offline all day doesn't appear at all.
  - Only unplanned reasons are listed. Planned Offline, NO PRODUCTION
    PLANNED, Changeover, LUNCH / BREAK / MEETING and Control Setup
    Transition are left out by SFC itself.
  - For every reason it DOES list, the per-machine numbers add up
    exactly to the Summary PDF's site total (checked on the 16/17 Nov
    2026 reports, to the second). merge_machine_breakdown() below
    re-checks that every time, so a file from the wrong day can't
    quietly show the wrong presses.

Each machine's table ends in a "Totals" row. The events parsed for that
machine must add up to it, the same self-check the Summary parser does
with its "Grand Totals" row.
"""
import re

from parsers.sfc_daily_downtime_pdf import ROW_RE, _extract_pages, _parse_period
from time_utils import hms_to_hours

# "Machine: Bruderer 1 - Primary: All Oper: All" — SFC wraps the asset
# code onto the NEXT line ("00031"), because the name box is narrow.
# Everything between "Machine:" and "Primary:" is the name text.
MACHINE_RE = re.compile(r'^Machine:\s*(.*?)\s*Primary:')
ASSET_CODE_LINE_RE = re.compile(r'^\d{3,6}$')
ENDS_WITH_CODE_RE = re.compile(r'-\s*\d{3,6}$')


def _machine_name(name_text, next_line):
    """Rebuild 'Bruderer 1 - 00031' from 'Bruderer 1 -' plus the wrapped
    '00031' line. If SFC ever prints the name on one line, it's used as is."""
    name = name_text.strip()
    if ENDS_WITH_CODE_RE.search(name):
        return name
    name = name.rstrip('- ').strip()
    if next_line and ASSET_CODE_LINE_RE.match(next_line):
        return f'{name} - {next_line}'
    return name


def parse_daily_unplanned_pdf(filepath):
    """Returns:
        {
          'period': '11/16/2026 6:30:00 AM to 11/17/2026 6:30:00 AM',
          'detected_date': '2026-11-16',
          'machines': {
              'Chin Fong 110 - 00044': {
                  'reasons': {'MA-FAULT PRESS': 0.069, ...},      # hours
                  'reason_events': {'MA-FAULT PRESS': 4, ...},
              }, ...
          },
        }
    Raises ValueError if this isn't the per-machine report, or if any
    machine's rows don't add up to its own Totals row."""
    pages = _extract_pages(filepath)
    period, _period_hrs, detected_date = _parse_period(pages[0] if pages else '')

    # Walk every line of every page in order. If a long table ever runs
    # onto a second page, its rows still belong to the last machine seen.
    lines = [ln.strip() for page in pages for ln in page.splitlines() if ln.strip()]

    machines = {}
    current = None
    in_table = False
    for i, line in enumerate(lines):
        m = MACHINE_RE.match(line)
        if m:
            next_line = lines[i + 1] if i + 1 < len(lines) else ''
            current = _machine_name(m.group(1), next_line)
            machines.setdefault(current, {'reasons': {}, 'reason_events': {}})
            in_table = False
            continue
        if line.startswith('Downtime Reason'):
            in_table = current is not None
            continue
        if not in_table:
            continue

        row = ROW_RE.match(line)
        if not row:
            continue
        reason, events_s, duration_s = row.groups()
        reason = reason.strip()
        events_n = int(events_s)
        hrs = round(hms_to_hours(duration_s), 3)
        data = machines[current]

        if reason.lower() == 'totals':
            parsed = sum(data['reason_events'].values())
            if parsed != events_n:
                raise ValueError(
                    f'Unplanned Downtime PDF: {current} rows add up to {parsed} '
                    f'events but its Totals row says {events_n} — a row was '
                    f'likely missed. Check the PDF layout hasn\'t changed.'
                )
            in_table = False
            continue

        data['reasons'][reason] = round(data['reasons'].get(reason, 0) + hrs, 3)
        data['reason_events'][reason] = data['reason_events'].get(reason, 0) + events_n

    if not machines:
        raise ValueError(
            'This doesn\'t look like the SFC "Daily Unplanned Downtime" PDF — '
            'no "Machine:" pages found. (The "Daily Downtime Summary" PDF goes '
            'in the first box, this one in the second.)'
        )

    return {
        'period': period,
        'detected_date': detected_date,
        'machines': machines,
    }


def merge_machine_breakdown(summary, unplanned):
    """Adds to the Summary PDF's result:
        summary['reason_machines'] = {
            'MA-FAULT PRESS': [{'machine': 'Chin Fong 110 - 00044', 'events': 4, 'hrs': 0.069}],
            ...
        }
        summary['machine_breakdown_warning'] = None or a short message

    Two safety checks, so the page never shows the wrong presses:
      1. Both PDFs must cover the same report period. If not, nothing is
         added — the second file is probably from another day.
      2. For each reason, the machines' events must add up to the
         Summary's site total for that reason. A reason that doesn't
         add up gets no machine list (and is named in the warning),
         rather than showing a list that's missing someone.
    """
    summary['reason_machines'] = {}
    summary['machine_breakdown_warning'] = None

    if summary.get('period') != unplanned.get('period'):
        summary['machine_breakdown_warning'] = (
            f'The Unplanned Downtime PDF is for {unplanned.get("period") or "an unknown period"}, '
            f'but the Summary PDF is for {summary.get("period") or "an unknown period"}. '
            'Machine lists not shown — upload both reports from the same morning.'
        )
        return summary

    by_reason = {}
    for machine, data in unplanned['machines'].items():
        for reason, events in data['reason_events'].items():
            by_reason.setdefault(reason, []).append({
                'machine': machine,
                'events': events,
                'hrs': round(data['reasons'].get(reason, 0), 3),
            })

    site_events = summary.get('reason_events') or {}
    mismatched = []
    for reason, rows in by_reason.items():
        if sum(r['events'] for r in rows) != site_events.get(reason):
            mismatched.append(reason)
            continue
        rows.sort(key=lambda r: (-r['hrs'], r['machine']))
        summary['reason_machines'][reason] = rows

    if mismatched:
        summary['machine_breakdown_warning'] = (
            'Machine list not shown for: ' + ', '.join(sorted(mismatched)) +
            ' — the two PDFs don\'t agree on the number of events.'
        )
    return summary
