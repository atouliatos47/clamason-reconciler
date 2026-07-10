"""
Flask routes. Deliberately thin — every route just wires an upload
through to the modules that actually do the work. If you're tempted to
add filtering or gap-calculation logic inside a route function, it
belongs in reconciliation.py instead, so every route (and the future
dashboard) stays consistent by construction.
"""
from flask import Blueprint, request, jsonify, send_file, send_from_directory

from file_utils import saved_upload
from parsers.sfc_monthly_xlsx import parse_monthly_summary_xlsx
from parsers.downtime_parser import parse_downtime_file
from parsers.wo_parser import parse_wo_file, parse_wo_file_all_types
from reconciliation import reconcile
from report_pdf import build_gap_pdf
from daily import compute_daily_summary
from daily_trend import weekly_rollup, monthly_rollup
from parsers.sfc_daily_downtime_pdf import parse_daily_downtime_pdf
import db

bp = Blueprint('routes', __name__)


def _parse_uploads():
    """Shared upload-handling for both routes below. Returns
    (sfc_summary, downtime_data, wo_data, asset_lookup, wo_provided)
    or raises ValueError with a user-facing message."""
    ds_file = request.files.get('daily_summary')
    dt_file = request.files.get('agility_downtime')
    wo_file = request.files.get('agility_wo')

    if not ds_file or not dt_file:
        raise ValueError('Need the SFC Monthly Downtime Summary xlsx and the Agility Down Time Analysis xlsx')

    with saved_upload(ds_file, 'sfc_summary') as path:
        sfc_summary = parse_monthly_summary_xlsx(path)

    with saved_upload(dt_file, 'agility_downtime') as path:
        downtime_data = parse_downtime_file(path)

    asset_lookup = {}
    wo_data = []
    wo_provided = bool(wo_file)
    if wo_file:
        with saved_upload(wo_file, 'agility_wo') as path:
            wo_data, asset_lookup = parse_wo_file(path)

    return sfc_summary, downtime_data, wo_data, asset_lookup, wo_provided


@bp.route('/')
def index():
    return send_from_directory('public', 'index.html')


@bp.route('/dashboard')
def dashboard():
    return send_from_directory('public', 'dashboard.html')


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
        sfc_summary, downtime_data, wo_data, asset_lookup, wo_provided = _parse_uploads()
        result = reconcile(sfc_summary, downtime_data, wo_data, asset_lookup, wo_file_provided=wo_provided)
        return jsonify(result)
    except ValueError as e:
        return jsonify({'error': str(e)})
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()})


@bp.route('/api/generate-report', methods=['POST'])
def generate_report():
    try:
        sfc_summary, downtime_data, wo_data, asset_lookup, wo_provided = _parse_uploads()
        result = reconcile(sfc_summary, downtime_data, wo_data, asset_lookup, wo_file_provided=wo_provided)
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

        sfc_summary, downtime_data, wo_data, asset_lookup, wo_provided = _parse_uploads()
        result = reconcile(sfc_summary, downtime_data, wo_data, asset_lookup, wo_file_provided=wo_provided)
        db.save_run(result, period_label=period_label)
        return jsonify({'saved': True, 'period_label': period_label, 'gap_pct': result['gap_pct']})
    except ValueError as e:
        return jsonify({'error': str(e)})
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()})
