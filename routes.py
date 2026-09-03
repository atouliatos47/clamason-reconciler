"""
Flask routes. Deliberately thin — every route just wires an upload
through to the modules that actually do the work. If you're tempted to
add filtering or gap-calculation logic inside a route function, it
belongs in reconciliation.py instead, so every route (and the future
dashboard) stays consistent by construction.
"""
import re

from flask import Blueprint, request, jsonify, send_file, send_from_directory

from file_utils import saved_upload
from parsers.sfc_monthly_xlsx import parse_monthly_summary_xlsx
from parsers.downtime_parser import parse_downtime_file
from parsers.oee_parser import parse_oee_file, aggregate_oee, apply_efacs_scrap_correction

# Agility plant asset codes: numeric, zero-padded, at most 6 digits.
PLANT_ASSET_CODE = re.compile(r'^\d{1,6}$')
from parsers.mtbf_parser import parse_mtbf_file, summarise_mtbf
from parsers.wo_parser import (
    parse_wo_file, parse_wo_file_all_types, parse_toolroom_wo_file,
)
from parsers.due_date_performance_parser import (
    parse_due_date_performance, summarise_due_date_performance,
)
from parsers.efacs_scrap_parser import parse_efacs_scrap_file
from reconciliation import reconcile
from report_pdf import build_gap_pdf
from daily import compute_daily_summary
from daily_trend import (
    weekly_rollup, monthly_rollup,
    sfc_daily_rollup, sfc_weekly_rollup, sfc_monthly_rollup,
    oee_daily_rollup, oee_weekly_rollup, oee_monthly_rollup,
    attach_production_plan,
)
from parsers.sfc_daily_downtime_pdf import parse_daily_downtime_pdf
from parsers.production_plan_xlsx import parse_production_plan
import db

bp = Blueprint('routes', __name__)


def _parse_oee_uploads():
    """Parse the single monthly SFC OEE .xls upload, if one was provided.

    Returns None when none was provided — OEE is optional, so a check
    run without it behaves exactly as it did before.

    SFC also produces a 'Monthly UK OEE By Machine Tabular' export
    covering a calendar month directly, sidestepping the old
    Sunday-Sunday weekly-file boundary problem entirely. Its Sub Totals
    rows use the exact same column layout as the weekly export, so
    parse_oee_file() needs no changes — only the number of files
    expected here.

    aggregate_oee() still takes a list of "week" record-lists so it can
    sum raw hours/parts before computing percentages (see oee_parser.py
    for why that order matters); a single monthly file is simply passed
    as a one-item list.
    """
    oee_file = request.files.get('oee_monthly')
    if not oee_file or not oee_file.filename:
        return None

    try:
        with saved_upload(oee_file, 'oee_monthly') as path:
            records, date_range = parse_oee_file(path)
    except Exception as exc:
        # xlrd's own message for a modern workbook is 'Excel xlsx file;
        # not supported', which tells the user nothing about what they
        # should have picked. The SFC OEE export is legacy .xls — an
        # easy field to drop the wrong file into when the others on the
        # page take .xlsx.
        raise ValueError(
            f"Couldn't read '{oee_file.filename}' as an SFC monthly OEE export. "
            "It must be the 'Monthly UK OEE By Machine Tabular' file, "
            f"which SFC produces as legacy .xls. ({exc})"
        )
    if not records:
        raise ValueError(
            f"No OEE data found in '{oee_file.filename}' — expected the SFC "
            "'Monthly UK OEE By Machine Tabular' .xls export"
        )

    result = aggregate_oee([records])
    result['week_ranges'] = [{'file': oee_file.filename, 'range': date_range}]
    return result


def _parse_mtbf_upload():
    """Agility MTBF export, or None if not uploaded.

    Summarised twice on purpose. This export has no craft column and
    lists presses, plant and TOOLS together — on June 2026, tools are
    3,279h of the 3,469h total, so an unfiltered 'maintenance MTTR'
    from this file is really a toolroom figure inflated roughly
    thirteen-fold. Splitting it here means the number that reaches a
    slide has a stated scope.
    """
    mtbf_file = request.files.get('agility_mtbf')
    if not mtbf_file or not mtbf_file.filename:
        return None

    with saved_upload(mtbf_file, 'agility_mtbf') as path:
        records, breakdown_range = parse_mtbf_file(path)

    if not records:
        raise ValueError(
            f"No asset rows found in '{mtbf_file.filename}' — expected the "
            "Agility 'Mean Time Between Failure' export"
        )

    # Agility plant asset codes are numeric and at most 6 digits
    # (00014, 00141, 12833). The length bound matters: a bare .isdigit()
    # also matches part numbers like 1301250031, which are tools. On
    # June 2026 that one difference moves the 'plant' MTTR from 1.41h to
    # 19.86h, because three long-numeric tool rows carry 800+ hours
    # between them.
    plant = [r for r in records if PLANT_ASSET_CODE.match(r['asset'])]
    tools = [r for r in records if not PLANT_ASSET_CODE.match(r['asset'])]

    return {
        'breakdown_range': breakdown_range,
        'all': summarise_mtbf(records),
        'plant': summarise_mtbf(plant),
        'tools': summarise_mtbf(tools),
        'assets': records,
    }


def _parse_efacs_scrap_upload():
    """EFACS 'Cost of Scrap' export, or None if not uploaded.

    Optional, same as OEE and MTBF — a check run without it behaves
    exactly as before (fleet quality stays SFC-sourced). See
    oee_parser.apply_efacs_scrap_correction for why this file exists:
    SFC's own scrap tracking is badly under-populated next to EFACS's.
    """
    efacs_file = request.files.get('efacs_scrap')
    if not efacs_file or not efacs_file.filename:
        return None

    with saved_upload(efacs_file, 'efacs_scrap') as path:
        return parse_efacs_scrap_file(path)


def _parse_ppm_completion_upload():
    """Agility 'Due Date Performance' export (AG3-205 run with different
    settings than the Down Time Analysis export elsewhere in this app —
    same report code, confirmed from a real export's filename, not the
    same report), or None if not uploaded.

    This is the board's real 'TPM Schedule Completion' methodology,
    ported from the old clamason-oee-dashboard project — see
    due_date_performance_parser.py for the full story, including why an
    earlier rougher calculation in this reconciler (eventual completion
    from the Selective Work Orders file, no due dates involved) read
    99% against a board figure nowhere close to that.

    Optional, same NULL-on-absence pattern as everything else here — a
    check run without it just doesn't show a TPM Completion figure,
    rather than falling back to the old calculation now known to be
    misleading.
    """
    dd_file = request.files.get('due_date_performance')
    if not dd_file or not dd_file.filename:
        return None

    with saved_upload(dd_file, 'due_date_performance') as path:
        records = parse_due_date_performance(path)
    return summarise_due_date_performance(records)


def _parse_uploads():
    """Shared upload-handling for both routes below. Returns
    (sfc_summary, downtime_data, wo_data, asset_lookup, wo_provided, extras)
    or raises ValueError with a user-facing message.

    `extras` carries the optional OEE and MTBF results. They're kept
    separate from the reconciliation inputs because neither feeds the
    SFC-vs-Agility gap calculation — they're additional context for the
    board review, and a missing one must never change the gap figure.

    EVERY upload here is optional, SFC Monthly Downtime Summary and
    Agility Down Time Analysis included. A check with just one file
    still runs — it just can't compute whatever that file alone doesn't
    cover (no SFC file means no gap %; see reconciliation.compute_gap).
    The only thing this function still refuses is a request with
    nothing in it at all, since there'd be nothing to reconcile.
    """
    ds_file = request.files.get('daily_summary')
    dt_file = request.files.get('agility_downtime')
    wo_file = request.files.get('agility_wo')

    if not any(f.filename for f in request.files.values()):
        raise ValueError('Upload at least one file to run a check')

    sfc_summary = {}
    if ds_file and ds_file.filename:
        with saved_upload(ds_file, 'sfc_summary') as path:
            sfc_summary = parse_monthly_summary_xlsx(path)

    downtime_data = []
    if dt_file and dt_file.filename:
        with saved_upload(dt_file, 'agility_downtime') as path:
            downtime_data = parse_downtime_file(path)

    asset_lookup = {}
    wo_data = []
    wo_provided = bool(wo_file)
    toolroom_wos = None
    if wo_file:
        with saved_upload(wo_file, 'agility_wo') as path:
            wo_data, asset_lookup = parse_wo_file(path)
            # Same file, second pass, Toolmaker craft. The board review's
            # Toolroom card previously showed the MAINTENANCE work-order
            # count under a 'tool WOs' label, because that was the only
            # WO figure the reconciler produced. Kept as its own pass so
            # nothing about the maintenance path or the gap figure moves.
            #
            # Second correction, same card: the gauge itself moved from a
            # raw 'WOs raised this month' count to the 'open' backlog
            # figure below, to match the board's own Toolroom slide
            # ('Tools awaiting repair / maintenance', target <25) instead
            # of a number with no board-approved target to read against.
            toolroom_records = parse_toolroom_wo_file(path)
        toolroom_wos = {
            'total': len(toolroom_records),
            'completed': sum(1 for r in toolroom_records
                             if r['status'].strip().lower() == 'completed'),
            # Cancelled jobs are counted in 'total' — the card says WOs
            # RAISED, and a cancelled WO was still raised. Reported
            # separately so the note can say so rather than leaving the
            # reader to assume every one was worked.
            'cancelled': sum(1 for r in toolroom_records
                             if r['status'].strip().lower() == 'cancelled'),
        }
        # 'open' is everything left over — Open, Scheduled, Accepted Job,
        # and whatever else Agility's status field produces — rather than
        # an explicit allow-list. Same residual-bucket reasoning as the
        # reason-code categorisation elsewhere: a new status string should
        # land here and stay visible, not silently vanish from the count.
        #
        # This is the board's "Tools awaiting repair / maintenance" figure
        # (target <25). One caveat worth knowing if the number looks low:
        # it's scoped to WOs whose Start Date falls inside the uploaded
        # file's period, same as 'total' above, so it reads as "still open
        # from what was raised this period" rather than a true live
        # backlog that would also carry in older unfinished jobs.
        toolroom_wos['open'] = (
            toolroom_wos['total'] - toolroom_wos['completed'] - toolroom_wos['cancelled']
        )

    efacs_scrap = _parse_efacs_scrap_upload()
    oee_result = _parse_oee_uploads()
    if efacs_scrap:
        apply_efacs_scrap_correction(oee_result, efacs_scrap['total_quantity'])

    extras = {
        'oee': oee_result,
        'efacs_scrap': efacs_scrap,
        'mtbf': _parse_mtbf_upload(),
        'toolroom_wos': toolroom_wos,
        'ppm_completion': _parse_ppm_completion_upload(),
    }

    return sfc_summary, downtime_data, wo_data, asset_lookup, wo_provided, extras


@bp.route('/')
def index():
    return send_from_directory('public', 'index.html')


@bp.route('/dashboard')
def dashboard():
    return send_from_directory('public', 'dashboard.html')


@bp.route('/teep')
def teep_page():
    return send_from_directory('public', 'teep.html')


@bp.route('/daily')
def daily_view():
    return send_from_directory('public', 'daily.html')


@bp.route('/daily-trend')
def daily_trend_view():
    return send_from_directory('public', 'daily-trend.html')


@bp.route('/api/daily-trend')
def daily_trend():
    """Saved daily_snapshots plus weekly/monthly rollups, for the Daily
    Trend view. Read-only, same split as /api/trend vs /api/save-run:
    this route never writes, /api/save-daily never reads."""
    try:
        snapshots = db.get_daily_snapshots()
        return jsonify({
            'daily': snapshots,
            'weekly': weekly_rollup(snapshots),
            'monthly': monthly_rollup(snapshots),
        })
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()})


@bp.route('/api/daily-check', methods=['POST'])
def daily_check():
    """Daily View — whole-site Maintenance/Electrician WO check, replacing
    Maintenance Daily's own calculation. Only needs Selective Work Orders;
    Down Time Analysis is optional — if provided, MTTR is calculated for
    real (see compute_daily_summary), matched by WO number against the
    already craft-filtered breakdown list, so nothing outside
    Maintenance/Electrician can leak into it."""
    try:
        wo_file = request.files.get('agility_wo')
        if not wo_file:
            return jsonify({'error': 'Selective Work Orders xlsx is required'})

        with saved_upload(wo_file, 'daily_wo') as path:
            wo_data, asset_lookup = parse_wo_file_all_types(path)

        for w in wo_data:
            w['assetName'] = asset_lookup.get(w['asset'], '')

        downtime_data = None
        dt_file = request.files.get('agility_downtime')
        if dt_file:
            with saved_upload(dt_file, 'daily_downtime') as path:
                downtime_data = parse_downtime_file(path)

        summary = compute_daily_summary(wo_data, downtime_data)
        return jsonify(summary)
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()})


@bp.route('/api/save-daily', methods=['POST'])
def save_daily():
    """Deliberately separate from /api/daily-check, same reasoning as
    /api/save-run: running the check itself never auto-saves, so a day
    you're still reviewing doesn't silently land in daily_snapshots.

    Recomputes from the uploaded file(s) rather than trusting whatever
    JSON the browser already holds, so the saved row can never drift
    from what a fresh /api/daily-check would produce. Down Time
    Analysis is handled exactly the same way here as in /api/daily-check
    (optional, real MTTR if provided) — deliberately kept identical so
    the saved MTTR can never silently disagree with the MTTR on screen."""
    try:
        date = request.form.get('date', '').strip()
        if not date:
            return jsonify({'error': 'date is required (YYYY-MM-DD)'})

        wo_file = request.files.get('agility_wo')
        if not wo_file:
            return jsonify({'error': 'Selective Work Orders xlsx is required'})

        with saved_upload(wo_file, 'daily_wo') as path:
            wo_data, asset_lookup = parse_wo_file_all_types(path)

        for w in wo_data:
            w['assetName'] = asset_lookup.get(w['asset'], '')

        downtime_data = None
        dt_file = request.files.get('agility_downtime')
        if dt_file:
            with saved_upload(dt_file, 'daily_downtime') as path:
                downtime_data = parse_downtime_file(path)

        summary = compute_daily_summary(wo_data, downtime_data)
        db.save_daily_snapshot(summary, date)
        return jsonify({'saved': True, 'date': date, 'total_wos': summary['total_wos']})
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()})


@bp.route('/sfc-daily')
def sfc_daily_view():
    return send_from_directory('public', 'sfc-daily.html')


@bp.route('/sfc-daily-trend')
def sfc_daily_trend_view():
    return send_from_directory('public', 'sfc-daily-trend.html')


@bp.route('/api/sfc-daily-trend')
def sfc_daily_trend():
    """Saved sfc_daily_snapshots plus weekly/monthly rollups, for the SFC
    Daily Trend view. Read-only — the mirror of /api/daily-trend, and the
    first consumer db.get_sfc_daily_snapshots() has ever had.

    Optional ?start=YYYY-MM-DD&end=YYYY-MM-DD narrows the window; both are
    inclusive and either can be given on its own. Left off, it returns the
    whole saved history.

    Note the rollups are built from whatever the date filter returned, NOT
    from the full history — so a filtered week's Pareto is that week's
    Pareto, not the all-time one re-labelled."""
    try:
        start = request.args.get('start') or None
        end = request.args.get('end') or None
        snapshots = db.get_sfc_daily_snapshots(start_date=start, end_date=end)
        return jsonify({
            'daily': sfc_daily_rollup(snapshots),
            'weekly': sfc_weekly_rollup(snapshots),
            'monthly': sfc_monthly_rollup(snapshots),
        })
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()})


@bp.route('/api/sfc-daily-check', methods=['POST'])
def sfc_daily_check():
    """SFC Daily Downtime Summary PDF check — entirely separate from the
    WO-based Daily Check above: own upload, own page, own table
    (sfc_daily_snapshots). Never auto-saves; /api/save-sfc-daily below
    is the only route that writes."""
    try:
        pdf_file = request.files.get('sfc_daily_pdf')
        if not pdf_file:
            return jsonify({'error': 'SFC Daily Downtime Summary PDF is required'})

        with saved_upload(pdf_file, 'sfc_daily_pdf') as path:
            summary = parse_daily_downtime_pdf(path)

        return jsonify(summary)
    except ValueError as e:
        return jsonify({'error': str(e)})
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()})


@bp.route('/api/save-sfc-daily', methods=['POST'])
def save_sfc_daily():
    """Deliberately separate from /api/sfc-daily-check, same reasoning
    as /api/save-daily: never auto-saves, and recomputes from the
    uploaded file rather than trusting whatever the browser already
    has, so the saved row can never drift from a fresh check."""
    try:
        date = request.form.get('date', '').strip()
        if not date:
            return jsonify({'error': 'date is required (YYYY-MM-DD)'})

        pdf_file = request.files.get('sfc_daily_pdf')
        if not pdf_file:
            return jsonify({'error': 'SFC Daily Downtime Summary PDF is required'})

        with saved_upload(pdf_file, 'sfc_daily_pdf') as path:
            summary = parse_daily_downtime_pdf(path)

        db.save_sfc_daily_snapshot(summary, date)
        return jsonify({'saved': True, 'date': date, 'maintenance_hrs': summary['maintenance_hrs']})
    except ValueError as e:
        return jsonify({'error': str(e)})
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()})


@bp.route('/daily-oee')
def daily_oee_view():
    return send_from_directory('public', 'daily-oee.html')


@bp.route('/daily-oee-trend')
def daily_oee_trend_view():
    return send_from_directory('public', 'daily-oee-trend.html')


@bp.route('/api/daily-oee-trend')
def daily_oee_trend():
    """Saved oee_daily_snapshots plus weekly/monthly rollups, for the
    Daily OEE Trend view. Read-only — same shape as /api/sfc-daily-trend.

    Optional ?start=YYYY-MM-DD&end=YYYY-MM-DD narrows the window; both
    inclusive, either can stand alone. Left off, returns the whole saved
    history.

    Per-machine detail lives directly on each daily/weekly/monthly
    bucket now (see daily_trend.py's _oee_finalize) — each machine's
    figures summed across every day in that bucket, then computed as
    one ratio, same principle as every other OEE number in this app.
    No separate 'raw' field needed any more; carrying every snapshot's
    full per-machine breakdown a second time here would only grow
    unbounded as more days get saved, duplicating data the buckets
    already provide more efficiently."""
    try:
        start = request.args.get('start') or None
        end = request.args.get('end') or None
        snapshots = db.get_oee_daily_snapshots(start_date=start, end_date=end)
        weekly = oee_weekly_rollup(snapshots)
        attach_production_plan(weekly, db.get_production_plan_weeks())
        return jsonify({
            'daily': oee_daily_rollup(snapshots),
            'weekly': weekly,
            'monthly': oee_monthly_rollup(snapshots),
        })
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()})


@bp.route('/api/daily-oee-check', methods=['POST'])
def daily_oee_check():
    """Daily UK OEE By Machine Tabular check — entirely separate from
    the monthly OEE upload on Monthly Check: own upload, own page, own
    table (oee_daily_snapshots). Never auto-saves; /api/save-daily-oee
    below is the only route that writes.

    Reuses parse_oee_file() and aggregate_oee() completely unchanged —
    a daily export has the identical column layout the weekly/monthly
    ones already use (same COL_* positions in oee_parser.py), just a
    24-hour period instead of a longer one, so nothing about the parser
    itself needed to know this is a new upload type."""
    try:
        oee_file = request.files.get('oee_daily')
        if not oee_file:
            return jsonify({'error': 'Daily UK OEE By Machine Tabular file is required'})

        with saved_upload(oee_file, 'oee_daily') as path:
            records, date_range = parse_oee_file(path)
        if not records:
            return jsonify({'error': f"No OEE data found in '{oee_file.filename}'"})

        result = aggregate_oee([records])
        result['fleet']['period'] = date_range
        return jsonify(result)
    except ValueError as e:
        return jsonify({'error': str(e)})
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()})


@bp.route('/api/save-daily-oee', methods=['POST'])
def save_daily_oee():
    """Deliberately separate from /api/daily-oee-check, same reasoning
    as /api/save-sfc-daily: never auto-saves, and recomputes from the
    uploaded file rather than trusting whatever the browser already
    has, so the saved row can never drift from a fresh check."""
    try:
        date = request.form.get('date', '').strip()
        if not date:
            return jsonify({'error': 'date is required (YYYY-MM-DD)'})

        oee_file = request.files.get('oee_daily')
        if not oee_file:
            return jsonify({'error': 'Daily UK OEE By Machine Tabular file is required'})

        with saved_upload(oee_file, 'oee_daily') as path:
            records, date_range = parse_oee_file(path)
        if not records:
            return jsonify({'error': f"No OEE data found in '{oee_file.filename}'"})

        result = aggregate_oee([records])
        db.save_oee_daily_snapshot(result, date_range, date)
        return jsonify({'saved': True, 'date': date, 'oee_pct': result['fleet']['oee_pct']})
    except ValueError as e:
        return jsonify({'error': str(e)})
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()})


@bp.route('/production-plan')
def production_plan_view():
    return send_from_directory('public', 'production-plan.html')


@bp.route('/tpm-schedule')
def tpm_schedule_view():
    return send_from_directory('public', 'tpm-schedule.html')


@bp.route('/splash')
def splash_view():
    return send_from_directory('public', 'splash.html')


@bp.route('/api/production-plan-check', methods=['POST'])
def production_plan_check():
    """Weekly Production Plan check — same shape as /api/daily-oee-check:
    parses and returns a preview, never saves. /api/save-production-plan
    below is the only route that writes.

    Deliberately does not touch the workbook's own Hours Required
    column (broken — wrong lookup table, see the WK30 investigation)
    or the OEE-adjusted OEE HRS REQ'D column. Sums Planned and
    Available Run Hrs directly, which is what was actually asked for
    and what parse_production_plan() computes."""
    try:
        plan_file = request.files.get('production_plan')
        if not plan_file:
            return jsonify({'error': 'Production Plan file is required'})

        with saved_upload(plan_file, 'production_plan') as path:
            result = parse_production_plan(path)
        result['filename'] = plan_file.filename
        return jsonify(result)
    except ValueError as e:
        return jsonify({'error': str(e)})
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()})


@bp.route('/api/save-production-plan', methods=['POST'])
def save_production_plan():
    """Deliberately separate from /api/production-plan-check, same
    reasoning as /api/save-daily-oee: never auto-saves, and recomputes
    from the uploaded file rather than trusting whatever the browser
    already has, so the saved row can never drift from a fresh check.

    Whatever date is picked gets snapped to that week's Monday before
    saving — same convention _week_key() in daily_trend.py already
    uses for every other weekly bucket in this app. Picking Wednesday
    of the intended week still saves correctly."""
    try:
        from datetime import date as date_cls, timedelta
        week_start_raw = request.form.get('week_start', '').strip()
        if not week_start_raw:
            return jsonify({'error': 'week_start is required (YYYY-MM-DD, any day in the plan week)'})
        picked = date_cls.fromisoformat(week_start_raw)
        week_start = (picked - timedelta(days=picked.weekday())).isoformat()

        plan_file = request.files.get('production_plan')
        if not plan_file:
            return jsonify({'error': 'Production Plan file is required'})

        with saved_upload(plan_file, 'production_plan') as path:
            result = parse_production_plan(path)
        db.save_production_plan_week(result, week_start, plan_file.filename)
        return jsonify({
            'saved': True, 'week_start': week_start,
            'plan_quantity': result['plan_quantity'], 'plan_hours': result['plan_hours'],
        })
    except ValueError as e:
        return jsonify({'error': str(e)})
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()})


@bp.route('/board-review')
def board_review_view():
    return send_from_directory('public', 'board-review.html')


@bp.route('/production')
def production_view():
    return send_from_directory('public', 'production.html')


@bp.route('/production-notes')
def production_notes_view():
    return send_from_directory('public', 'production-notes.html')


@bp.route('/toolroom')
def toolroom_view():
    return send_from_directory('public', 'toolroom.html')


@bp.route('/toolroom-notes')
def toolroom_notes_view():
    return send_from_directory('public', 'toolroom-notes.html')


@bp.route('/toolroom-sfc-vs-agility')
def toolroom_sfc_vs_agility_view():
    return send_from_directory('public', 'toolroom-sfc-vs-agility.html')


@bp.route('/maintenance')
def maintenance_view():
    return send_from_directory('public', 'maintenance.html')


@bp.route('/maintenance-notes')
def maintenance_notes_view():
    return send_from_directory('public', 'maintenance-notes.html')


@bp.route('/maintenance-sfc-vs-agility')
def maintenance_sfc_vs_agility_view():
    return send_from_directory('public', 'maintenance-sfc-vs-agility.html')


# Allowlist rather than accepting any string — keeps department_notes
# clean (no typo'd department names quietly creating their own row)
# and stops the API being used to write notes against something that
# isn't actually one of the three pages that read them back.
_VALID_DEPARTMENTS = {'production', 'toolroom', 'maintenance'}


@bp.route('/api/department-notes/<department>', methods=['GET'])
def get_department_notes_route(department):
    if department not in _VALID_DEPARTMENTS:
        return jsonify({'error': f"Unknown department '{department}'"}), 404
    try:
        result = db.get_department_notes(department) or {'notes': '', 'updated_by': None, 'updated_at': None}
        result['actions'] = db.get_department_actions(department)
        return jsonify(result)
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()})


@bp.route('/api/department-notes/<department>', methods=['POST'])
def save_department_notes_route(department):
    if department not in _VALID_DEPARTMENTS:
        return jsonify({'error': f"Unknown department '{department}'"}), 404
    try:
        data = request.get_json(force=True, silent=True) or {}
        notes = data.get('notes', '')
        updated_by = (data.get('updated_by') or '').strip() or None
        actions = data.get('actions', [])
        db.save_department_notes(department, notes, updated_by)
        db.save_department_actions(department, actions)
        return jsonify({'saved': True})
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()})


@bp.route('/api/trend')
def trend():
    """All saved monthly runs, for the trend dashboard. Read-only —
    never touched by the reconciliation routes above, only by
    /api/save-run writing and this route reading."""
    try:
        runs = db.get_all_runs()
        return jsonify({'runs': runs})
    except Exception as e:
        return jsonify({'error': str(e)})


@bp.route('/api/maintenance-check', methods=['POST'])
def maintenance_check():
    try:
        sfc_summary, downtime_data, wo_data, asset_lookup, wo_provided, extras = _parse_uploads()
        result = reconcile(sfc_summary, downtime_data, wo_data, asset_lookup, wo_file_provided=wo_provided)
        result.update(extras)
        return jsonify(result)
    except ValueError as e:
        return jsonify({'error': str(e)})
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()})


@bp.route('/api/generate-report', methods=['POST'])
def generate_report():
    try:
        sfc_summary, downtime_data, wo_data, asset_lookup, wo_provided, extras = _parse_uploads()
        # The PDF's entire content is the SFC-vs-Agility gap narrative —
        # there's no meaningful partial version of it. Say so plainly
        # rather than handing report_pdf.py an empty sfc_summary it was
        # never written to expect.
        if not sfc_summary:
            return jsonify({'error': 'PDF report needs the SFC Monthly Downtime Summary file — '
                                      'that one hasn\'t been uploaded yet. Everything else '
                                      '(the on-page results, saving to the trend dashboard) '
                                      'works fine without it.'})
        result = reconcile(sfc_summary, downtime_data, wo_data, asset_lookup, wo_file_provided=wo_provided)
        result.update(extras)
        pdf_buf = build_gap_pdf(result)
        return send_file(
            pdf_buf, mimetype='application/pdf', as_attachment=True,
            download_name='Maintenance_WO_Gap_Report.pdf',
        )
    except ValueError as e:
        return jsonify({'error': str(e)})
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()})


@bp.route('/api/save-run', methods=['POST'])
def save_run():
    """Deliberately separate from /api/maintenance-check. This is the
    ONLY route that writes to the database — running the check itself
    never auto-saves, so numbers you're still verifying don't silently
    end up in the trend history."""
    try:
        period_label = request.form.get('period_label', '').strip()
        if not period_label:
            return jsonify({'error': 'period_label is required (e.g. "June 2026")'})

        sfc_summary, downtime_data, wo_data, asset_lookup, wo_provided, extras = _parse_uploads()
        result = reconcile(sfc_summary, downtime_data, wo_data, asset_lookup, wo_file_provided=wo_provided)
        result.update(extras)
        db.save_run(result, period_label=period_label)
        return jsonify({'saved': True, 'period_label': period_label, 'gap_pct': result['gap_pct']})
    except ValueError as e:
        return jsonify({'error': str(e)})
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()})
