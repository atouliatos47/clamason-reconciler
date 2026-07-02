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
