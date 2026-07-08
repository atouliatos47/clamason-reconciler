"""
Database module for storing monthly reconciliation runs, so the trend
dashboard has history to chart. The connection string is read from the
DATABASE_URL environment variable ONLY — never hardcoded here, never
committed to git. Set it locally when testing, and as an environment
variable in Render's dashboard once deployed.
"""
import os
import json
import psycopg2
import psycopg2.extras


def _get_conn():
    url = os.environ.get('DATABASE_URL')
    if not url:
        raise RuntimeError(
            'DATABASE_URL environment variable is not set. '
            'Set it to your Neon connection string before running this.'
        )
    return psycopg2.connect(url)


def init_schema():
    """Creates the monthly_runs table if it doesn't already exist.
    Safe to run multiple times."""
    schema_path = os.path.join(os.path.dirname(__file__), 'schema.sql')
    with open(schema_path) as f:
        schema_sql = f.read()
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(schema_sql)
        conn.commit()


def save_run(result, period_label):
    """Store one month's reconciliation result. result is the dict
    returned by reconciliation.reconcile(). Re-saving the same period
    (matched on the raw `period` string from SFC) overwrites the
    previous row rather than creating a duplicate."""
    sfc = result['sfc_summary']
    row = {
        'period': sfc.get('period', ''),
        'period_label': period_label,
        'machine_count': sfc.get('machine_count'),
        'total_hrs': sfc.get('total_hrs'),
        'total_events': sfc.get('total_events'),
        'maintenance_hrs': sfc.get('maintenance_hrs'),
        'toolroom_hrs': sfc.get('toolroom_hrs'),
        'agility_maintenance_hrs': result.get('agility_maintenance_hrs'),
        'gap_hrs': result.get('gap_hrs'),
        'gap_pct': result.get('gap_pct'),
        'wo_count': result.get('wo_count'),
        'machine_breakdown': json.dumps(result.get('machine_breakdown', [])),
    }

    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO monthly_runs
                    (period, period_label, machine_count, total_hrs, total_events,
                     maintenance_hrs, toolroom_hrs, agility_maintenance_hrs,
                     gap_hrs, gap_pct, wo_count, machine_breakdown)
                VALUES
                    (%(period)s, %(period_label)s, %(machine_count)s, %(total_hrs)s, %(total_events)s,
                     %(maintenance_hrs)s, %(toolroom_hrs)s, %(agility_maintenance_hrs)s,
                     %(gap_hrs)s, %(gap_pct)s, %(wo_count)s, %(machine_breakdown)s)
                ON CONFLICT (period) DO UPDATE SET
                    period_label = EXCLUDED.period_label,
                    machine_count = EXCLUDED.machine_count,
                    total_hrs = EXCLUDED.total_hrs,
                    total_events = EXCLUDED.total_events,
                    maintenance_hrs = EXCLUDED.maintenance_hrs,
                    toolroom_hrs = EXCLUDED.toolroom_hrs,
                    agility_maintenance_hrs = EXCLUDED.agility_maintenance_hrs,
                    gap_hrs = EXCLUDED.gap_hrs,
                    gap_pct = EXCLUDED.gap_pct,
                    wo_count = EXCLUDED.wo_count,
                    machine_breakdown = EXCLUDED.machine_breakdown
            """, row)
        conn.commit()


def get_all_runs():
    """All stored runs, oldest first — what the trend dashboard reads.
    Postgres NUMERIC columns come back from psycopg2 as Decimal, which
    Flask's jsonify silently turns into STRINGS in the JSON output
    (e.g. "92.9" instead of 92.9) — that would quietly break any chart
    math on the front-end. Converting explicitly to float here instead."""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM monthly_runs ORDER BY created_at ASC")
            rows = cur.fetchall()

    numeric_fields = [
        'total_hrs', 'maintenance_hrs', 'toolroom_hrs',
        'agility_maintenance_hrs', 'gap_hrs', 'gap_pct',
    ]
    result = []
    for r in rows:
        row = dict(r)
        for field in numeric_fields:
            if row.get(field) is not None:
                row[field] = float(row[field])
        if row.get('created_at') is not None:
            row['created_at'] = row['created_at'].isoformat()
        result.append(row)
    return result


def save_daily_snapshot(summary, date):
    """Store one day's Daily View result. summary is the dict returned
    by daily.compute_daily_summary(); date is a 'YYYY-MM-DD' string (or
    a date object) for the day the WOs were pulled for. Re-saving the
    same date (e.g. a corrected re-upload) overwrites the previous row
    rather than creating a duplicate — same pattern as save_run()."""
    row = {
        'date': date,
        'total_wos': summary.get('total_wos'),
        'press_machine_wos': summary.get('press_machine_wos'),
        'sitewide_wos': summary.get('sitewide_wos'),
        'breakdowns_total': summary['breakdowns'].get('total'),
        'breakdowns_completed': summary['breakdowns'].get('completed'),
        'planned_total': summary['planned'].get('total'),
        'planned_completed': summary['planned'].get('completed'),
        'project_ci_total': summary['project_ci'].get('total'),
        'project_ci_completed': summary['project_ci'].get('completed'),
        'other_total': summary['other'].get('total'),
        'mttr_hrs': summary.get('mttr_hrs'),
        'mttr_matched': summary.get('mttr_matched'),
        'mttr_unmatched': summary.get('mttr_unmatched'),
    }

    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO daily_snapshots
                    (date, total_wos, press_machine_wos, sitewide_wos,
                     breakdowns_total, breakdowns_completed,
                     planned_total, planned_completed,
                     project_ci_total, project_ci_completed,
                     other_total, mttr_hrs, mttr_matched, mttr_unmatched)
                VALUES
                    (%(date)s, %(total_wos)s, %(press_machine_wos)s, %(sitewide_wos)s,
                     %(breakdowns_total)s, %(breakdowns_completed)s,
                     %(planned_total)s, %(planned_completed)s,
                     %(project_ci_total)s, %(project_ci_completed)s,
                     %(other_total)s, %(mttr_hrs)s, %(mttr_matched)s, %(mttr_unmatched)s)
                ON CONFLICT (date) DO UPDATE SET
                    total_wos = EXCLUDED.total_wos,
                    press_machine_wos = EXCLUDED.press_machine_wos,
                    sitewide_wos = EXCLUDED.sitewide_wos,
                    breakdowns_total = EXCLUDED.breakdowns_total,
                    breakdowns_completed = EXCLUDED.breakdowns_completed,
                    planned_total = EXCLUDED.planned_total,
                    planned_completed = EXCLUDED.planned_completed,
                    project_ci_total = EXCLUDED.project_ci_total,
                    project_ci_completed = EXCLUDED.project_ci_completed,
                    other_total = EXCLUDED.other_total,
                    mttr_hrs = EXCLUDED.mttr_hrs,
                    mttr_matched = EXCLUDED.mttr_matched,
                    mttr_unmatched = EXCLUDED.mttr_unmatched
            """, row)
        conn.commit()


def get_daily_snapshots(start_date=None, end_date=None):
    """Saved daily snapshots, oldest first — what the weekly and
    monthly rollup views will read. start_date/end_date (both
    inclusive, 'YYYY-MM-DD' strings) let a view ask for just this week
    or this month instead of pulling the whole history every time."""
    query = "SELECT * FROM daily_snapshots WHERE 1=1"
    params = {}
    if start_date:
        query += " AND date >= %(start_date)s"
        params['start_date'] = start_date
    if end_date:
        query += " AND date <= %(end_date)s"
        params['end_date'] = end_date
    query += " ORDER BY date ASC"

    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

    result = []
    for r in rows:
        row = dict(r)
        if row.get('mttr_hrs') is not None:
            row['mttr_hrs'] = float(row['mttr_hrs'])
        if row.get('date') is not None:
            row['date'] = row['date'].isoformat()
        if row.get('created_at') is not None:
            row['created_at'] = row['created_at'].isoformat()
        result.append(row)
    return result
