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
    (matched on period_label, the user-typed identifier) overwrites the
    previous row rather than creating a duplicate.

    Used to key on sfc_summary's own 'period' string when present,
    falling back to period_label only when SFC data was missing. That
    broke the moment a month went from no-SFC to has-SFC between two
    saves: the conflict key changed underneath it, so the second save
    landed as a new row instead of overwriting the first. period_label
    is what actually stays stable across every save of the same month,
    so it's now the only thing this keys on, regardless of which files
    happen to be attached this time.
    """
    sfc = result.get('sfc_summary') or {}
    row = {
        # Always period_label, never sfc.get('period'). SFC's own period
        # string used to be preferred here when present — but a month can
        # go from "no SFC file yet" to "SFC file arrived" between two
        # saves of what's genuinely the same period, and SFC's string
        # isn't guaranteed to stay identical between those two uploads.
        # That's not a hypothetical: it's exactly what happened the first
        # time this shipped — a partial July save keyed on the typed
        # label, a later complete one keyed on SFC's own date-range
        # string, and two rows where one was meant, because the conflict
        # key changed out from under it. period_label is what the person
        # actually chose to call this period; it's the only thing
        # guaranteed to stay stable across every save of the same month
        # regardless of which files happen to be attached this time.
        'period': period_label,
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
        'oee_total_avail_hrs': fleet.get('total_avail_hrs'),
        'oee_utilization_pct': fleet.get('utilization_pct'),
        'oee_teep_pct': fleet.get('teep_pct'),
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
        'oee_intended_hours': fleet.get('intended_hours'),
        'oee_utilization_vs_intended_pct': fleet.get('utilization_vs_intended_pct'),
        'oee_teep_vs_intended_pct': fleet.get('teep_vs_intended_pct'),
        'oee_intended_configured_count': fleet.get('intended_configured_count'),
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

    tg = result.get('toolroom_gap') or {}
    row.update({
        'toolroom_sfc_hrs': tg.get('toolroom_sfc_hrs'),
        'agility_toolroom_hrs': tg.get('agility_toolroom_hrs'),
        'toolroom_gap_hrs': tg.get('toolroom_gap_hrs'),
        'toolroom_gap_pct': tg.get('toolroom_gap_pct'),
        'toolroom_gap_wo_count': tg.get('toolroom_wo_count'),
        'toolroom_machine_breakdown': json.dumps(result.get('toolroom_machine_breakdown', [])),
    })

    ppm = result.get('ppm_completion') or {}
    row.update({
        'ppm_total': ppm.get('total'),
        'ppm_completed': ppm.get('completed'),
        'ppm_completion_pct': ppm.get('pct'),
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
                     oee_net_avail_hrs, oee_total_avail_hrs, oee_utilization_pct, oee_teep_pct,
                     oee_total_parts, oee_scrap_parts, oee_per_machine,
                     mtbf_days, mtbf_assets, mtbf_asset_count, mtbf_mttr_hrs,
                     mtbf_wait_hrs, mtbf_jobs, mtbf_downtime_hrs, mtbf_scopes,
                     toolroom_wo_count, toolroom_wo_completed, toolroom_wo_cancelled,
                     toolroom_wo_open, ppm_total, ppm_completed, ppm_completion_pct,
                     oee_quality_source, efacs_scrap_qty, efacs_scrap_cost,
                     oee_intended_hours, oee_utilization_vs_intended_pct,
                     oee_teep_vs_intended_pct, oee_intended_configured_count,
                     toolroom_sfc_hrs, agility_toolroom_hrs, toolroom_gap_hrs,
                     toolroom_gap_pct, toolroom_gap_wo_count, toolroom_machine_breakdown)
                VALUES
                    (%(period)s, %(period_label)s, %(machine_count)s, %(total_hrs)s, %(total_events)s,
                     %(maintenance_hrs)s, %(toolroom_hrs)s, %(agility_maintenance_hrs)s,
                     %(gap_hrs)s, %(gap_pct)s, %(wo_count)s, %(machine_breakdown)s,
                     %(mtta_hrs_press)s, %(mttr_hrs_press)s, %(mdt_hrs_press)s, %(mttr_jobs_press)s,
                     %(mtta_hrs_all)s, %(mttr_hrs_all)s, %(mdt_hrs_all)s, %(mttr_jobs_all)s,
                     %(oee_pct)s, %(oee_availability_pct)s, %(oee_performance_pct)s, %(oee_performance_pct_raw)s,
                     %(oee_quality_pct)s, %(oee_week_count)s, %(oee_machine_count)s, %(oee_run_hrs)s,
                     %(oee_net_avail_hrs)s, %(oee_total_avail_hrs)s, %(oee_utilization_pct)s, %(oee_teep_pct)s,
                     %(oee_total_parts)s, %(oee_scrap_parts)s, %(oee_per_machine)s,
                     %(mtbf_days)s, %(mtbf_assets)s, %(mtbf_asset_count)s, %(mtbf_mttr_hrs)s,
                     %(mtbf_wait_hrs)s, %(mtbf_jobs)s, %(mtbf_downtime_hrs)s, %(mtbf_scopes)s,
                     %(toolroom_wo_count)s, %(toolroom_wo_completed)s, %(toolroom_wo_cancelled)s,
                     %(toolroom_wo_open)s, %(ppm_total)s, %(ppm_completed)s, %(ppm_completion_pct)s,
                     %(oee_quality_source)s, %(efacs_scrap_qty)s, %(efacs_scrap_cost)s,
                     %(oee_intended_hours)s, %(oee_utilization_vs_intended_pct)s,
                     %(oee_teep_vs_intended_pct)s, %(oee_intended_configured_count)s,
                     %(toolroom_sfc_hrs)s, %(agility_toolroom_hrs)s, %(toolroom_gap_hrs)s,
                     %(toolroom_gap_pct)s, %(toolroom_gap_wo_count)s, %(toolroom_machine_breakdown)s)
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
                    oee_total_avail_hrs = EXCLUDED.oee_total_avail_hrs,
                    oee_utilization_pct = EXCLUDED.oee_utilization_pct,
                    oee_teep_pct = EXCLUDED.oee_teep_pct,
                    oee_intended_hours = EXCLUDED.oee_intended_hours,
                    oee_utilization_vs_intended_pct = EXCLUDED.oee_utilization_vs_intended_pct,
                    oee_teep_vs_intended_pct = EXCLUDED.oee_teep_vs_intended_pct,
                    oee_intended_configured_count = EXCLUDED.oee_intended_configured_count,
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
                    ppm_total = EXCLUDED.ppm_total,
                    ppm_completed = EXCLUDED.ppm_completed,
                    ppm_completion_pct = EXCLUDED.ppm_completion_pct,
                    oee_quality_source = EXCLUDED.oee_quality_source,
                    efacs_scrap_qty = EXCLUDED.efacs_scrap_qty,
                    efacs_scrap_cost = EXCLUDED.efacs_scrap_cost,
                    toolroom_sfc_hrs = EXCLUDED.toolroom_sfc_hrs,
                    agility_toolroom_hrs = EXCLUDED.agility_toolroom_hrs,
                    toolroom_gap_hrs = EXCLUDED.toolroom_gap_hrs,
                    toolroom_gap_pct = EXCLUDED.toolroom_gap_pct,
                    toolroom_gap_wo_count = EXCLUDED.toolroom_gap_wo_count,
                    toolroom_machine_breakdown = EXCLUDED.toolroom_machine_breakdown
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
        'oee_net_avail_hrs', 'oee_total_avail_hrs', 'oee_utilization_pct',
        'oee_teep_pct', 'oee_total_parts', 'oee_scrap_parts',
        'mtbf_days', 'mtbf_mttr_hrs', 'mtbf_wait_hrs', 'mtbf_downtime_hrs',
        # Same gap as above, just added later than the rest — these two
        # existed before the pattern above was caught, so they'd been
        # missing this conversion the whole time rather than being a new
        # omission.
        'efacs_scrap_qty', 'efacs_scrap_cost',
        'oee_intended_hours', 'oee_utilization_vs_intended_pct',
        'oee_teep_vs_intended_pct', 'ppm_completion_pct',
        'toolroom_sfc_hrs', 'agility_toolroom_hrs', 'toolroom_gap_hrs', 'toolroom_gap_pct',
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


def save_oee_daily_snapshot(oee_result, date_range, date):
    """Store one day's Daily UK OEE By Machine Tabular result.
    oee_result is the dict returned by parsers.oee_parser.aggregate_oee()
    (has 'fleet' and 'per_machine'); date_range is the report's own
    period string from parse_oee_file()'s second return value; date is a
    'YYYY-MM-DD' string (or a date object) chosen at save time — same as
    every other daily snapshot in this app, never derived from the
    report's own period. Re-saving the same date overwrites the previous
    row rather than creating a duplicate.

    Stores fleet's raw hours/parts fields, not its percentages — see the
    schema comment on oee_daily_snapshots for why a percentage can't be
    the thing a weekly/monthly rollup sums."""
    fleet = oee_result.get('fleet') or {}
    row = {
        'date': date,
        'period': date_range,
        'machine_count': fleet.get('machine_count'),
        'total_avail_hrs': fleet.get('total_avail_hrs'),
        'planned_down_hrs': fleet.get('planned_down_hrs'),
        'net_avail_hrs': fleet.get('net_avail_hrs'),
        'unplanned_down_hrs': fleet.get('unplanned_down_hrs'),
        'run_time_hrs': fleet.get('run_time_hrs'),
        'total_parts': fleet.get('total_parts'),
        'ideal_parts': fleet.get('ideal_parts'),
        'scrap_parts': fleet.get('scrap_parts'),
        'per_machine': json.dumps(oee_result.get('per_machine', [])),
    }

    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO oee_daily_snapshots
                    (date, period, machine_count, total_avail_hrs,
                     planned_down_hrs, net_avail_hrs, unplanned_down_hrs,
                     run_time_hrs, total_parts, ideal_parts, scrap_parts,
                     per_machine)
                VALUES
                    (%(date)s, %(period)s, %(machine_count)s, %(total_avail_hrs)s,
                     %(planned_down_hrs)s, %(net_avail_hrs)s, %(unplanned_down_hrs)s,
                     %(run_time_hrs)s, %(total_parts)s, %(ideal_parts)s, %(scrap_parts)s,
                     %(per_machine)s)
                ON CONFLICT (date) DO UPDATE SET
                    period = EXCLUDED.period,
                    machine_count = EXCLUDED.machine_count,
                    total_avail_hrs = EXCLUDED.total_avail_hrs,
                    planned_down_hrs = EXCLUDED.planned_down_hrs,
                    net_avail_hrs = EXCLUDED.net_avail_hrs,
                    unplanned_down_hrs = EXCLUDED.unplanned_down_hrs,
                    run_time_hrs = EXCLUDED.run_time_hrs,
                    total_parts = EXCLUDED.total_parts,
                    ideal_parts = EXCLUDED.ideal_parts,
                    scrap_parts = EXCLUDED.scrap_parts,
                    per_machine = EXCLUDED.per_machine
            """, row)
        conn.commit()


def get_oee_daily_snapshots(start_date=None, end_date=None):
    """Saved OEE daily snapshots, oldest first — what the Daily OEE Trend
    view reads. Optional start_date/end_date (both inclusive, 'YYYY-MM-DD'
    strings). per_machine comes back from psycopg2 as a plain list
    already — JSONB is adapted automatically, no json.loads() needed."""
    query = "SELECT * FROM oee_daily_snapshots WHERE 1=1"
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
        'total_avail_hrs', 'planned_down_hrs', 'net_avail_hrs',
        'unplanned_down_hrs', 'run_time_hrs', 'total_parts',
        'ideal_parts', 'scrap_parts',
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


def save_production_plan_week(plan_result, week_start, source_filename):
    """Store one week's Production Plan import. plan_result is the dict
    returned by parsers.production_plan_xlsx.parse_production_plan();
    week_start is a Monday date ('YYYY-MM-DD' or date object) chosen at
    save time — same convention as every other snapshot in this app,
    never derived from the workbook's own sheet name (that's exactly
    what broke the "which week is this" question for the sheet itself
    on multiple occasions). Re-saving the same week overwrites rather
    than duplicating."""
    row = {
        'week_start': week_start,
        'source_filename': source_filename,
        'sheet_name': plan_result.get('sheet_name'),
        'plan_quantity': plan_result.get('plan_quantity'),
        'plan_hours': plan_result.get('plan_hours'),
        'row_count': plan_result.get('row_count'),
    }
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO production_plan_weekly
                    (week_start, source_filename, sheet_name, plan_quantity, plan_hours, row_count)
                VALUES
                    (%(week_start)s, %(source_filename)s, %(sheet_name)s, %(plan_quantity)s,
                     %(plan_hours)s, %(row_count)s)
                ON CONFLICT (week_start) DO UPDATE SET
                    source_filename = EXCLUDED.source_filename,
                    sheet_name = EXCLUDED.sheet_name,
                    plan_quantity = EXCLUDED.plan_quantity,
                    plan_hours = EXCLUDED.plan_hours,
                    row_count = EXCLUDED.row_count
            """, row)
        conn.commit()


def get_production_plan_weeks(start_date=None, end_date=None):
    """Saved weekly production plans, oldest first. Optional
    start_date/end_date filter on week_start (both inclusive,
    'YYYY-MM-DD' strings)."""
    query = "SELECT * FROM production_plan_weekly WHERE 1=1"
    params = {}
    if start_date:
        query += " AND week_start >= %(start_date)s"
        params['start_date'] = start_date
    if end_date:
        query += " AND week_start <= %(end_date)s"
        params['end_date'] = end_date
    query += " ORDER BY week_start ASC"

    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

    result = []
    for r in rows:
        row = dict(r)
        for field in ('plan_quantity', 'plan_hours'):
            if row.get(field) is not None:
                row[field] = float(row[field])
        if row.get('week_start') is not None:
            row['week_start'] = row['week_start'].isoformat()
        if row.get('created_at') is not None:
            row['created_at'] = row['created_at'].isoformat()
        result.append(row)
    return result


def get_department_notes(department):
    """Returns {'notes': str, 'updated_by': str, 'updated_at': iso string}
    or None if nothing's been saved for this department yet."""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT notes, updated_by, updated_at FROM department_notes WHERE department = %(department)s",
                {'department': department}
            )
            row = cur.fetchone()
    if not row:
        return None
    result = dict(row)
    if result.get('updated_at') is not None:
        result['updated_at'] = result['updated_at'].isoformat()
    return result


def save_department_notes(department, notes, updated_by):
    """Upserts on department — one row per department, always. updated_by
    is whatever name was typed in the box, not a real user account (this
    app has no login), so it's context, not an audit trail."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO department_notes (department, notes, updated_by)
                VALUES (%(department)s, %(notes)s, %(updated_by)s)
                ON CONFLICT (department) DO UPDATE SET
                    notes = EXCLUDED.notes,
                    updated_by = EXCLUDED.updated_by,
                    updated_at = now()
            """, {'department': department, 'notes': notes, 'updated_by': updated_by})
        conn.commit()


def get_department_actions(department):
    """Returns a list of {'id', 'action_text', 'target_date', 'done'}
    for this department, in the order they were last saved in.
    target_date comes back as an ISO date string ('2026-09-12') or
    None — never a raw date object, so routes.py can jsonify it
    without extra conversion."""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, action_text, target_date, done FROM department_actions "
                "WHERE department = %(department)s ORDER BY sort_order",
                {'department': department}
            )
            rows = cur.fetchall()
    result = []
    for row in rows:
        r = dict(row)
        if r.get('target_date') is not None:
            r['target_date'] = r['target_date'].isoformat()
        result.append(r)
    return result


def save_department_actions(department, actions):
    """Replaces this department's whole action list in one go: deletes
    every existing row for it, then re-inserts `actions` in the order
    given. Simpler than diffing against what's already stored, and the
    list is short enough (a handful of items) that this is cheap. Each
    item in `actions` is a dict with 'action_text' (required),
    'target_date' (ISO date string or None/missing), and 'done'
    (bool, defaults False). Rows with no action_text are skipped, so
    an empty row left in the UI doesn't get saved as blank."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM department_actions WHERE department = %(department)s",
                {'department': department}
            )
            for i, action in enumerate(actions):
                text = (action.get('action_text') or '').strip()
                if not text:
                    continue
                cur.execute("""
                    INSERT INTO department_actions
                        (department, action_text, target_date, done, sort_order)
                    VALUES (%(department)s, %(action_text)s, %(target_date)s, %(done)s, %(sort_order)s)
                """, {
                    'department': department,
                    'action_text': text,
                    'target_date': action.get('target_date') or None,
                    'done': bool(action.get('done')),
                    'sort_order': i,
                })
        conn.commit()
