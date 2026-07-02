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
from parsers.wo_parser import parse_wo_file
from reconciliation import reconcile
from report_pdf import build_gap_pdf
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
