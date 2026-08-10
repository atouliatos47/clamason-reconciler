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
    previous row rather than creating a duplicate.

    sfc_summary itself is optional now — see routes._parse_uploads —
    so it's read with .get() rather than assumed present, and its
    'period' string (normally straight from the SFC file) falls back to
    the user-typed period_label when there's no SFC file to take it
    from. Without that fallback every SFC-less save would write the
    same '' period and silently overwrite the last one instead of
    creating its own row.
    """
    sfc = result.get('sfc_summary') or {}
    row = {
        'period': sfc.get('period') or period_label,
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

    # Repair times arrive as two nested dicts from reconciliation.reconcile().
    # Flattened into columns rather than stored as JSON because the trend
    # dashboard charts them over time, and charting means SQL needs to sort
    # and aggregate on them directly.
    #
    # .get with an empty-dict default on purpose: a result produced before
    # these keys existed (or by a caller that skipped reconciliation) writes
    # NULLs rather than raising KeyError, so an old cached result can still
    # be saved.
    for suffix, key in (('press', 'repair_times_press'), ('all', 'repair_times_all')):
        rt = result.get(key) or {}
        row[f'mtta_hrs_{suffix}'] = rt.get('mtta_hrs')
        row[f'mttr_hrs_{suffix}'] = rt.get('mttr_hrs')
        row[f'mdt_hrs_{suffix}'] = rt.get('mdt_hrs')
        row[f'mttr_jobs_{suffix}'] = rt.get('mttr_jobs')

    # OEE and MTBF are optional uploads — a check run without them stores
    # NULLs rather than failing, and an existing row keeps whatever it had.
    oee = result.get('oee') or {}
    fleet = oee.get('fleet') or {}
    row.update({
        'oee_pct': fleet.get('oee_pct'),
        'oee_availability_pct': fleet.get('availability_pct'),
        'oee_performance_pct': fleet.get('performance_pct'),
        'oee_performance_pct_raw': fleet.get('performance_pct_raw'),
        'oee_quality_pct': fleet.get('quality_pct'),
        'oee_week_count': fleet.get('week_count'),
        'oee_machine_count': fleet.get('machine_count'),
        'oee_run_hrs': fleet.get('run_time_hrs'),
        'oee_net_avail_hrs': fleet.get('net_avail_hrs'),
        'oee_total_parts': fleet.get('total_parts'),
        # Always SFC's own figure, whether or not EFACS corrected the
        # quality/OEE that were computed from it — see oee_quality_source
        # below for which one actually fed those two columns this run.
        'oee_scrap_parts': fleet.get('scrap_parts'),
        'oee_per_machine': json.dumps(oee.get('per_machine', [])) if oee else None,
        # 'sfc' / 'efacs' / NULL (no OEE file uploaded at all). See
        # oee_parser.apply_efacs_scrap_correction for why EFACS is
        # preferred when available: SFC's own scrap count is badly
        # under-populated.
        'oee_quality_source': fleet.get('quality_source'),
    })

    # EFACS Cost of Scrap — optional, same NULL-on-absence pattern as OEE/
    # MTBF above. Kept as its own pair of columns (not folded into the OEE
    # block) because it's useful board context even in months nobody
    # uploads an OEE file to correct.
    efacs = result.get('efacs_scrap') or {}
    row.update({
        'efacs_scrap_qty': efacs.get('total_quantity'),
        'efacs_scrap_cost': efacs.get('total_cost'),
    })

    # Flat columns carry the PLANT scope specifically — see schema.sql.
    # All three scopes go into mtbf_scopes so a stored run can still be
    # audited against the toolroom-inclusive figure it was derived from.
    mtbf = result.get('mtbf') or {}
    plant = mtbf.get('plant') or {}
    row.update({
        'mtbf_days': plant.get('mtbf_days'),
        'mtbf_assets': plant.get('mtbf_assets'),
        'mtbf_asset_count': plant.get('asset_count'),
        'mtbf_mttr_hrs': plant.get('mttr_hrs'),
        'mtbf_wait_hrs': plant.get('wait_hrs'),
        'mtbf_jobs': plant.get('jobs'),
        'mtbf_downtime_hrs': plant.get('downtime_hrs'),
        'mtbf_scopes': json.dumps({
            k: mtbf[k] for k in ('all', 'plant', 'tools') if k in mtbf
        }) if mtbf else None,
    })

    # NULL when no WO file was uploaded — distinct from zero, which would
    # claim the toolroom raised nothing that month.
    tw = result.get('toolroom_wos') or {}
    row.update({
        'toolroom_wo_count': tw.get('total'),
        'toolroom_wo_completed': tw.get('completed'),
        'toolroom_wo_cancelled': tw.get('cancelled'),
        'toolroom_wo_open': tw.get('open'),
    })

    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO monthly_runs
                    (period, period_label, machine_count, total_hrs, total_events,
                     maintenance_hrs, toolroom_hrs, agility_maintenance_hrs,
                     gap_hrs, gap_pct, wo_count, machine_breakdown,
                     mtta_hrs_press, mttr_hrs_press, mdt_hrs_press, mttr_jobs_press,
                     mtta_hrs_all, mttr_hrs_all, mdt_hrs_all, mttr_jobs_all,
                     oee_pct, oee_availability_pct, oee_performance_pct, oee_performance_pct_raw,
                     oee_quality_pct, oee_week_count, oee_machine_count, oee_run_hrs,
                     oee_net_avail_hrs, oee_total_parts, oee_scrap_parts, oee_per_machine,
                     mtbf_days, mtbf_assets, mtbf_asset_count, mtbf_mttr_hrs,
                     mtbf_wait_hrs, mtbf_jobs, mtbf_downtime_hrs, mtbf_scopes,
                     toolroom_wo_count, toolroom_wo_completed, toolroom_wo_cancelled,
                     toolroom_wo_open, oee_quality_source, efacs_scrap_qty, efacs_scrap_cost)
                VALUES
                    (%(period)s, %(period_label)s, %(machine_count)s, %(total_hrs)s, %(total_events)s,
                     %(maintenance_hrs)s, %(toolroom_hrs)s, %(agility_maintenance_hrs)s,
                     %(gap_hrs)s, %(gap_pct)s, %(wo_count)s, %(machine_breakdown)s,
                     %(mtta_hrs_press)s, %(mttr_hrs_press)s, %(mdt_hrs_press)s, %(mttr_jobs_press)s,
                     %(mtta_hrs_all)s, %(mttr_hrs_all)s, %(mdt_hrs_all)s, %(mttr_jobs_all)s,
                     %(oee_pct)s, %(oee_availability_pct)s, %(oee_performance_pct)s, %(oee_performance_pct_raw)s,
                     %(oee_quality_pct)s, %(oee_week_count)s, %(oee_machine_count)s, %(oee_run_hrs)s,
                     %(oee_net_avail_hrs)s, %(oee_total_parts)s, %(oee_scrap_parts)s, %(oee_per_machine)s,
                     %(mtbf_days)s, %(mtbf_assets)s, %(mtbf_asset_count)s, %(mtbf_mttr_hrs)s,
                     %(mtbf_wait_hrs)s, %(mtbf_jobs)s, %(mtbf_downtime_hrs)s, %(mtbf_scopes)s,
                     %(toolroom_wo_count)s, %(toolroom_wo_completed)s, %(toolroom_wo_cancelled)s,
                     %(toolroom_wo_open)s, %(oee_quality_source)s, %(efacs_scrap_qty)s, %(efacs_scrap_cost)s)
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
                    machine_breakdown = EXCLUDED.machine_breakdown,
                    mtta_hrs_press = EXCLUDED.mtta_hrs_press,
                    mttr_hrs_press = EXCLUDED.mttr_hrs_press,
                    mdt_hrs_press = EXCLUDED.mdt_hrs_press,
                    mttr_jobs_press = EXCLUDED.mttr_jobs_press,
                    mtta_hrs_all = EXCLUDED.mtta_hrs_all,
                    mttr_hrs_all = EXCLUDED.mttr_hrs_all,
                    mdt_hrs_all = EXCLUDED.mdt_hrs_all,
                    mttr_jobs_all = EXCLUDED.mttr_jobs_all,
                    oee_pct = EXCLUDED.oee_pct,
                    oee_availability_pct = EXCLUDED.oee_availability_pct,
                    oee_performance_pct = EXCLUDED.oee_performance_pct,
                    oee_performance_pct_raw = EXCLUDED.oee_performance_pct_raw,
                    oee_quality_pct = EXCLUDED.oee_quality_pct,
                    oee_week_count = EXCLUDED.oee_week_count,
                    oee_machine_count = EXCLUDED.oee_machine_count,
                    oee_run_hrs = EXCLUDED.oee_run_hrs,
                    oee_net_avail_hrs = EXCLUDED.oee_net_avail_hrs,
                    oee_total_parts = EXCLUDED.oee_total_parts,
                    oee_scrap_parts = EXCLUDED.oee_scrap_parts,
                    oee_per_machine = EXCLUDED.oee_per_machine,
                    mtbf_days = EXCLUDED.mtbf_days,
                    mtbf_assets = EXCLUDED.mtbf_assets,
                    mtbf_asset_count = EXCLUDED.mtbf_asset_count,
                    mtbf_mttr_hrs = EXCLUDED.mtbf_mttr_hrs,
                    mtbf_wait_hrs = EXCLUDED.mtbf_wait_hrs,
                    mtbf_jobs = EXCLUDED.mtbf_jobs,
                    mtbf_downtime_hrs = EXCLUDED.mtbf_downtime_hrs,
                    mtbf_scopes = EXCLUDED.mtbf_scopes,
                    toolroom_wo_count = EXCLUDED.toolroom_wo_count,
                    toolroom_wo_completed = EXCLUDED.toolroom_wo_completed,
                    toolroom_wo_cancelled = EXCLUDED.toolroom_wo_cancelled,
                    toolroom_wo_open = EXCLUDED.toolroom_wo_open,
                    oee_quality_source = EXCLUDED.oee_quality_source,
                    efacs_scrap_qty = EXCLUDED.efacs_scrap_qty,
                    efacs_scrap_cost = EXCLUDED.efacs_scrap_cost
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
        # Same Decimal-to-string problem as the fields above — without
        # these, an MTTR of 1.41 reaches the chart as the string "1.41"
        # and any arithmetic on it silently produces nonsense.
        'mtta_hrs_press', 'mttr_hrs_press', 'mdt_hrs_press',
        'mtta_hrs_all', 'mttr_hrs_all', 'mdt_hrs_all',
        'oee_pct', 'oee_availability_pct', 'oee_performance_pct',
        'oee_performance_pct_raw', 'oee_quality_pct', 'oee_run_hrs',
        'oee_net_avail_hrs', 'oee_total_parts', 'oee_scrap_parts',
        'mtbf_days', 'mtbf_mttr_hrs', 'mtbf_wait_hrs', 'mtbf_downtime_hrs',
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


def save_sfc_daily_snapshot(summary, date):
    """Store one day's SFC Daily Downtime Summary result. summary is the
    dict returned by parsers.sfc_daily_downtime_pdf.parse_daily_downtime_pdf();
    date is a 'YYYY-MM-DD' string (or a date object) you choose at save
    time — same as save_daily_snapshot(), this is never derived from the
    report's own Report Period. Re-saving the same date overwrites the
    previous row rather than creating a duplicate."""
    row = {
        'date': date,
        'period': summary.get('period'),
        'total_events': summary.get('total_events'),
        'total_hrs': summary.get('total_hrs'),
        'maintenance_hrs': summary.get('maintenance_hrs'),
        'toolroom_hrs': summary.get('toolroom_hrs'),
        'production_hrs': summary.get('production_hrs'),
        'machine_count': summary.get('machine_count'),
        'period_hrs': summary.get('period_hrs'),
        'max_possible_hrs': summary.get('max_possible_hrs'),
        'planned_offline_hrs': summary.get('planned_offline_hrs'),
        'scheduled_hrs': summary.get('scheduled_hrs'),
        'reasons': json.dumps(summary.get('reasons', {})),
        'reason_events': json.dumps(summary.get('reason_events', {})),
    }

    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO sfc_daily_snapshots
                    (date, period, total_events, total_hrs,
                     maintenance_hrs, toolroom_hrs, production_hrs,
                     machine_count, period_hrs, max_possible_hrs,
                     planned_offline_hrs, scheduled_hrs,
                     reasons, reason_events)
                VALUES
                    (%(date)s, %(period)s, %(total_events)s, %(total_hrs)s,
                     %(maintenance_hrs)s, %(toolroom_hrs)s, %(production_hrs)s,
                     %(machine_count)s, %(period_hrs)s, %(max_possible_hrs)s,
                     %(planned_offline_hrs)s, %(scheduled_hrs)s,
                     %(reasons)s, %(reason_events)s)
                ON CONFLICT (date) DO UPDATE SET
                    period = EXCLUDED.period,
                    total_events = EXCLUDED.total_events,
                    total_hrs = EXCLUDED.total_hrs,
                    maintenance_hrs = EXCLUDED.maintenance_hrs,
                    toolroom_hrs = EXCLUDED.toolroom_hrs,
                    production_hrs = EXCLUDED.production_hrs,
                    machine_count = EXCLUDED.machine_count,
                    period_hrs = EXCLUDED.period_hrs,
                    max_possible_hrs = EXCLUDED.max_possible_hrs,
                    planned_offline_hrs = EXCLUDED.planned_offline_hrs,
                    scheduled_hrs = EXCLUDED.scheduled_hrs,
                    reasons = EXCLUDED.reasons,
                    reason_events = EXCLUDED.reason_events
            """, row)
        conn.commit()


def get_sfc_daily_snapshots(start_date=None, end_date=None):
    """Saved SFC daily downtime snapshots, oldest first — what the SFC
    Daily Trend / Pareto view will read. Optional start_date/end_date
    (both inclusive, 'YYYY-MM-DD' strings) let a view ask for just this
    week or month instead of pulling the whole history every time.
    reasons/reason_events come back from psycopg2 as plain dicts
    already — JSONB is adapted automatically, no json.loads() needed."""
    query = "SELECT * FROM sfc_daily_snapshots WHERE 1=1"
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

    numeric_fields = [
        'total_hrs', 'maintenance_hrs', 'toolroom_hrs', 'production_hrs',
        'period_hrs', 'max_possible_hrs', 'planned_offline_hrs', 'scheduled_hrs',
    ]
    result = []
    for r in rows:
        row = dict(r)
        for field in numeric_fields:
            if row.get(field) is not None:
                row[field] = float(row[field])
        if row.get('date') is not None:
            row['date'] = row['date'].isoformat()
        if row.get('created_at') is not None:
            row['created_at'] = row['created_at'].isoformat()
        result.append(row)
    return result
