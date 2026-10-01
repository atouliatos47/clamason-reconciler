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
    parse_wo_file_for_toolroom_gap,
)
from parsers.due_date_performance_parser import (
    parse_due_date_performance, summarise_due_date_performance,
    summarise_by_employee, detect_report_period,
)
from parsers.efacs_scrap_parser import parse_efacs_scrap_file
from reconciliation import reconcile, enrich_and_filter, compute_toolroom_gap, compute_toolroom_machine_breakdown
from report_pdf import build_gap_pdf, build_personnel_pdf, build_personnel_yearly_pdf
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
    toolroom_gap_wo_data = []
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
            # Third pass, same file: Toolmaker craft AND job-type
            # restricted, for the new Toolroom SFC-vs-Agility gap. Not
            # the same list as toolroom_records above (that one is
            # every job type, for the backlog card) or wo_data above
            # (that one is Maintenance/Electrician craft) — see
            # parse_wo_file_for_toolroom_gap's own docstring.
            toolroom_gap_wo_data, _ = parse_wo_file_for_toolroom_gap(path)
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

    # Toolroom's own SFC-vs-Agility gap, same shape as the Maintenance
    # one reconcile() produces below but scoped to Scott's confirmed
    # 3 tool-reason codes and Toolmaker-craft WOs. Computed here rather
    # than inside reconcile() itself — see that module's docstring on
    # why a shared function with a department switch is the wrong
    # shape for this.
    matched_toolroom_wos, _ = enrich_and_filter(downtime_data, toolroom_gap_wo_data, asset_lookup)
    toolroom_gap = compute_toolroom_gap(sfc_summary, matched_toolroom_wos)
    toolroom_machine_breakdown = compute_toolroom_machine_breakdown(sfc_summary, matched_toolroom_wos)

    extras = {
        'oee': oee_result,
        'efacs_scrap': efacs_scrap,
        'mtbf': _parse_mtbf_upload(),
        'toolroom_wos': toolroom_wos,
        'ppm_completion': _parse_ppm_completion_upload(),
        'toolroom_gap': toolroom_gap,
        'toolroom_machine_breakdown': toolroom_machine_breakdown,
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


@bp.route('/production-sfc-daily')
def production_sfc_daily_view():
    return send_from_directory('public', 'production-sfc-daily.html')


@bp.route('/production-sfc-trend')
def production_sfc_trend_view():
    return send_from_directory('public', 'production-sfc-trend.html')


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


@bp.route('/api/save-production-sfc-daily', methods=['POST'])
def save_production_sfc_daily():
    """Production's own copy of /api/save-sfc-daily — same PDF, same
    parser, but writes to production_sfc_daily_snapshots instead of
    sfc_daily_snapshots. Confirmed with Andreas: Production's saved
    history must stay fully independent, even for the same date, so
    this never touches the Maintenance table and vice versa."""
    try:
        date = request.form.get('date', '').strip()
        if not date:
            return jsonify({'error': 'date is required (YYYY-MM-DD)'})

        pdf_file = request.files.get('sfc_daily_pdf')
        if not pdf_file:
            return jsonify({'error': 'SFC Daily Downtime Summary PDF is required'})

        with saved_upload(pdf_file, 'sfc_daily_pdf') as path:
            summary = parse_daily_downtime_pdf(path)

        db.save_production_sfc_daily_snapshot(summary, date)
        return jsonify({'saved': True, 'date': date, 'production_hrs': summary['production_hrs']})
    except ValueError as e:
        return jsonify({'error': str(e)})
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()})


@bp.route('/api/production-sfc-daily-trend')
def production_sfc_daily_trend():
    """Production's own copy of /api/sfc-daily-trend — reads
    production_sfc_daily_snapshots instead of sfc_daily_snapshots.
    Same optional ?start=&end= window, same rollup functions (those
    just take a list of snapshot dicts, so no need for Production-
    specific rollup logic — only the saved rows themselves need to
    stay apart)."""
    try:
        start = request.args.get('start') or None
        end = request.args.get('end') or None
        snapshots = db.get_production_sfc_daily_snapshots(start_date=start, end_date=end)
        return jsonify({
            'daily': sfc_daily_rollup(snapshots),
            'weekly': sfc_weekly_rollup(snapshots),
            'monthly': sfc_monthly_rollup(snapshots),
        })
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


@bp.route('/maintenance-personnel')
def maintenance_personnel_view():
    return send_from_directory('public', 'maintenance-personnel.html')


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


@bp.route('/board-review-trend')
def board_review_trend_view():
    """Month-over-month progress across all three departments — same
    /api/trend data the Maintenance-only Trend Dashboard (/dashboard)
    already reads, just not duplicating it: that page is Maintenance's
    own SFC-vs-Agility deep dive, this one is the board-level view it
    doesn't cover (Production/Toolroom/Maintenance downtime side by
    side, plus the three headline KPIs and reliability trend in one
    place) — see board-review-trend.html's own top comment."""
    return send_from_directory('public', 'board-review-trend.html')


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


def _parse_personnel_uploads():
    """Shared by /api/personnel-check and /api/save-personnel: pools
    however many Agility Due Date Performance exports were uploaded
    under the 'due_date_files' field (today that's a Maintenance-craft
    file and an Electrician-craft file, but nothing here assumes
    exactly two), groups the result by employee, and determines which
    month it all belongs to by reading each file's own "From Completion
    Date / To Completion Date" header (detect_report_period) rather
    than asking the person to pick a month by hand.

    That used to be a month picker on the upload form, defaulting to
    "last calendar month" relative to today. It looked right in the
    ordinary case and was silently wrong the moment that assumption
    didn't hold — Andreas hit this for real, uploading a July export
    once "last month" had already rolled over to August, and it saved
    as August with nothing to show the mistake. Reading the month out
    of the file itself removes the guess entirely: whatever month the
    export says it covers is the month it gets saved under, uploaded
    on time, late, or backfilled in any order.

    Raises ValueError (never silently picks one) if the uploaded files
    disagree on what month they cover — that means either two
    different months got selected together by mistake, or one file
    is not a normal single-month export.

    Deliberately re-parses the files every time rather than being
    handed a previously-computed result — see /api/save-personnel's
    docstring for why that matters."""
    files = request.files.getlist('due_date_files')
    if not files:
        raise ValueError('At least one Due Date Performance export is required')

    all_records = []
    periods_seen = {}  # period -> {'label': ..., 'filenames': [...]}
    for f in files:
        if not f or not f.filename:
            continue
        with saved_upload(f, 'due_date_performance_personnel') as path:
            all_records.extend(parse_due_date_performance(path))
            info = detect_report_period(path)
        periods_seen.setdefault(info['period'], {'label': info['period_label'], 'filenames': []})
        periods_seen[info['period']]['filenames'].append(f.filename)

    if not periods_seen:
        raise ValueError('At least one Due Date Performance export is required')

    if len(periods_seen) > 1:
        parts = [f"{data['label']} ({', '.join(data['filenames'])})"
                 for period, data in sorted(periods_seen.items())]
        raise ValueError(
            "These exports cover different months — " + '; '.join(parts) +
            ". Upload one month's files together, not several months at once."
        )

    period, period_data = next(iter(periods_seen.items()))
    employees = summarise_by_employee(all_records)
    employees.sort(key=lambda e: e['employee'])
    return employees, period, period_data['label']


@bp.route('/api/personnel-check', methods=['POST'])
def personnel_check():
    """Personnel PPM performance — same shape as /api/production-plan-check:
    parses and returns a preview, never saves. /api/save-personnel below
    is the only route that writes. period/period_label come back too,
    read from the file itself, so the page can show which month this
    is BEFORE saving rather than after."""
    try:
        employees, period, period_label = _parse_personnel_uploads()
        return jsonify({'employees': employees, 'period': period, 'period_label': period_label})
    except ValueError as e:
        return jsonify({'error': str(e)})
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()})


@bp.route('/api/save-personnel', methods=['POST'])
def save_personnel():
    """Deliberately separate from /api/personnel-check, same reasoning
    as every other save-* route here: never auto-saves, and recomputes
    from the uploaded files rather than trusting whatever the browser
    already has, so the saved rows can never drift from a fresh check.

    No 'period' form field any more — see _parse_personnel_uploads for
    why that was the wrong thing to be trusting in the first place.
    period and period_label now come from the same file-reading
    _parse_personnel_uploads does for the check above, so a save can
    never land under a different month than what the check just showed."""
    try:
        employees, period, period_label = _parse_personnel_uploads()
        db.save_personnel_run(employees, period, period_label)

        return jsonify({
            'saved': True,
            'period': period,
            'period_label': period_label,
            'employees': employees,
        })
    except ValueError as e:
        return jsonify({'error': str(e)})
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()})


@bp.route('/api/personnel-pdf')
def personnel_pdf():
    """Printable PPM performance PDF for one engineer. Reads whatever's
    already saved (no file upload here); /api/save-personnel is what
    puts data in reach of this route in the first place. Three shapes,
    picked by which optional query param is present - mutually
    exclusive, ?year takes priority if both are somehow sent:

    - (neither) full trend history, headlined on the latest month -
      the original behaviour, unchanged for anyone already using this
      link (e.g. the "Printable PPM Performance Reports" list).
    - ?year=2026 an annual review: one aggregated headline for the
      year plus that year's own Monthly Trend rows. 404s with a clear
      message if nothing was saved for that year.
    - ?month=2026-06 the report AS IT STOOD at the end of that month -
      trend history trimmed to everything up to and including it, so
      the headline is that month rather than whatever's most recent
      now. Requires that exact month to have been saved (no nearest-
      month guessing, which could silently hand back the wrong
      period); errors otherwise.
    """
    try:
        employee = request.args.get('employee', '').strip()
        if not employee:
            return jsonify({'error': 'employee is required'})

        year = request.args.get('year', '').strip()
        month = request.args.get('month', '').strip()

        months = db.get_personnel_trend(employee)
        if not months:
            return jsonify({'error': f'No saved Personnel PPM data for {employee.title()} yet'})

        safe_name = re.sub(r'[^A-Za-z0-9]+', '_', employee.title()).strip('_')

        if year:
            if not re.match(r'^\d{4}$', year):
                return jsonify({'error': "year must be 'YYYY'"})
            year_months = [m for m in months if m['period'].startswith(year)]
            if not year_months:
                return jsonify({'error': f'No saved Personnel PPM data for {employee.title()} in {year}'})
            pdf_buf = build_personnel_yearly_pdf(employee.title(), year, year_months)
            download_name = f'{safe_name}_PPM_Performance_{year}.pdf'
        elif month:
            if not re.match(r'^\d{4}-\d{2}$', month):
                return jsonify({'error': "month must be 'YYYY-MM'"})
            if not any(m['period'] == month for m in months):
                available = ', '.join(m['period_label'] for m in months)
                return jsonify({'error': f'No saved data for {employee.title()} in {month}. Saved months: {available}'})
            months_to_date = [m for m in months if m['period'] <= month]
            pdf_buf = build_personnel_pdf(employee.title(), months_to_date)
            download_name = f'{safe_name}_PPM_Performance_{month}.pdf'
        else:
            pdf_buf = build_personnel_pdf(employee.title(), months)
            download_name = f'{safe_name}_PPM_Performance.pdf'

        # as_attachment=False (not the True every other PDF route here
        # uses): opens inline in the browser's own PDF viewer first,
        # rather than forcing an immediate download. Andreas asked for
        # this specifically for Personnel reports - view first, save
        # only if you decide to, using the browser's own Save/Download
        # control inside its PDF viewer. download_name still sets what
        # that save defaults to, so choosing to save still gets the
        # right filename.
        return send_file(
            pdf_buf, mimetype='application/pdf', as_attachment=False,
            download_name=download_name,
        )
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()})


@bp.route('/api/personnel-trend')
def personnel_trend():
    """All saved personnel-months, for the Personnel page's trend
    chart. Read-only — never touched by the routes above, only by
    /api/save-personnel writing and this route reading. Optional
    ?employee= scopes to one engineer (unused by the page today, which
    charts everyone at once, but kept for the PDF route to reuse)."""
    try:
        employee = request.args.get('employee')
        rows = db.get_personnel_trend(employee)
        return jsonify({'rows': rows})
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
