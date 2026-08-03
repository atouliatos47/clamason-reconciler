"""
Shared reconciliation logic — the ONE place that decides "does this
Agility WO count as covering an SFC fault event". Every route and the
dashboard call into this, instead of each re-implementing the same
filtering. This is deliberate: both bugs found this session (the
known_machine filter being computed but never applied, and the craft
filter missing from one of two routes) existed because the same rule
was written out twice in two different places and only fixed in one.
With one shared function, that class of bug can't happen again.
"""
from config import BREAKDOWN_JOB_TYPE, is_known_machine


def enrich_and_filter(downtime_data, wo_data, asset_lookup):
    """Take raw Down Time Analysis rows + raw Selective WO rows, and
    return only the rows that genuinely count as Maintenance/Electrician
    breakdown work on an SFC-monitored press.

    Two filters, both required:
      1. wo_map match  — the WO must be a Maintenance/Electrician-craft
                          job (parse_wo_file already restricts to that
                          craft + those job types).
      2. known_machine  — the WO's asset must map to a machine SFC
                          actually tracks fault codes for. A real
                          Breakdown Repair on a chiller/compressor/
                          welder is still real Maintenance work, but it
                          can never be "covering" a FAULT-PRESS event,
                          because SFC doesn't track fault codes for
                          anything outside SFC_TO_AGILITY.
    """
    enriched, wo_map = enrich_all(downtime_data, wo_data, asset_lookup)
    matched = [d for d in enriched if d['wo'] in wo_map]
    matched = [d for d in matched if d['known_machine']]
    return matched, wo_map


def enrich_all(downtime_data, wo_data, asset_lookup):
    """Every Down Time Analysis row, tagged with its WO's job type,
    status, craft and known_machine flag — with NO filtering applied.

    Split out of enrich_and_filter so repair times can be computed on
    the craft-matched set BEFORE the known_machine filter narrows it.
    The gap figure needs both filters; MTTR does not, and forcing them
    to share one filtered list would mean either a wrong gap or a wrong
    MTTR."""
    wo_map = {w['jobNo']: w for w in wo_data}

    enriched = []
    for d in downtime_data:
        d = dict(d)  # don't mutate the caller's rows
        wo = wo_map.get(d['wo'])
        if wo:
            d['job_type'] = wo['jobType']
            d['status'] = wo['status']
            d['desc'] = wo['desc']
            d['craft'] = wo.get('craft', '')
        d['asset_name'] = asset_lookup.get(d['asset'], '')
        d['known_machine'] = is_known_machine(d['asset'])
        enriched.append(d)

    return enriched, wo_map


def compute_repair_times(rows):
    """Mean MTTA / MTTR / MDT across Breakdown Repair rows, from the
    timestamps parsers.downtime_parser derives. Same three durations
    daily.py reports, so the monthly and daily views can never disagree
    about what MTTR means.

    Restricted to Breakdown Repair on purpose. The monthly WO parser
    admits four job types (breakdown repair, routine minor service,
    planned service & maintenance, corrective maintenance) — a PPM is
    scheduled work, not a repair after a failure, and averaging those in
    would drag MTTR toward whatever the PPM schedule looks like rather
    than how long a breakdown takes to fix.

    Counts are reported alongside the means because a mean over two
    jobs and a mean over eighty are not the same claim, and the weekly
    rollups need the denominator to weight by."""
    breakdowns = [
        r for r in rows
        if (r.get('job_type') or '').strip().lower() == BREAKDOWN_JOB_TYPE
    ]

    def mean_of(key):
        vals = [r[key] for r in breakdowns if r.get(key) is not None]
        return (round(sum(vals) / len(vals), 2) if vals else None), len(vals)

    mtta, n_mtta = mean_of('mtta_hrs')
    mttr, n_mttr = mean_of('mttr_hrs')
    mdt, n_mdt = mean_of('mdt_hrs')

    return {
        'mtta_hrs': mtta,
        'mttr_hrs': mttr,
        'mdt_hrs': mdt,
        'mttr_jobs': n_mttr,
        # Breakdown WOs that matched a DTA row but had no usable
        # Started/Finished pair — blank cell, or Finished before
        # Started. Surfaced rather than hidden so a thin month is
        # visible instead of just producing a confident-looking mean.
        'mttr_no_duration': len(breakdowns) - n_mttr,
        'breakdown_count': len(breakdowns),
    }


def compute_gap(sfc_summary, matched_wos):
    """Fleet-wide gap: SFC maintenance hours vs Agility hours actually
    matched to a real press-fault-relevant breakdown WO."""
    agility_hrs = round(sum(d['downtime_hrs'] for d in matched_wos), 2)
    sfc_hrs = sfc_summary['maintenance_hrs']
    gap_hrs = round(sfc_hrs - agility_hrs, 2)
    gap_pct = round((gap_hrs / sfc_hrs * 100) if sfc_hrs > 0 else 0, 1)
    return {
        'agility_maintenance_hrs': agility_hrs,
        'gap_hrs': gap_hrs,
        'gap_pct': gap_pct,
        'wo_count': len(matched_wos),
    }


def compute_machine_breakdown(sfc_summary, matched_wos):
    """Per-machine table: fault hours/events (from SFC) vs matched WO
    count/hours (from Agility), for the dashboard chart and trend view.
    Sorted by fault hours descending, same order as the chart we built."""
    from config import SFC_TO_AGILITY

    by_machine_sfc = sfc_summary.get('by_machine', {})

    wo_by_asset_count = {}
    wo_by_asset_hrs = {}
    for d in matched_wos:
        a = d['asset']
        wo_by_asset_count[a] = wo_by_asset_count.get(a, 0) + 1
        wo_by_asset_hrs[a] = round(wo_by_asset_hrs.get(a, 0) + d['downtime_hrs'], 2)

    rows = []
    for machine, asset_codes in SFC_TO_AGILITY.items():
        sfc = by_machine_sfc.get(machine, {'fault_hrs': 0, 'fault_events': 0})
        wo_count = sum(wo_by_asset_count.get(a, 0) for a in asset_codes)
        wo_hrs = round(sum(wo_by_asset_hrs.get(a, 0) for a in asset_codes), 2)
        if sfc['fault_hrs'] == 0 and sfc['fault_events'] == 0 and wo_count == 0:
            continue  # nothing happened on this machine this period — skip from the table
        rows.append({
            'machine': machine,
            'fault_hrs': round(sfc['fault_hrs'], 2),
            'fault_events': sfc['fault_events'],
            'wo_count': wo_count,
            'wo_hrs': wo_hrs,
        })

    rows.sort(key=lambda r: -r['fault_hrs'])
    return rows


def reconcile(sfc_summary, downtime_data, wo_data, asset_lookup, wo_file_provided=True):
    """Top-level entry point — everything a route or the dashboard
    needs from one call. This is the only function routes.py should
    ever call for reconciliation; it should never re-derive any of
    this filtering itself."""
    warning = None
    if not wo_file_provided:
        matched_wos = []
        warning = ('No Selective Work Orders file uploaded — craft cannot be verified, '
                   'so Agility hours are 0 rather than an unfiltered (and misleading) total. '
                   'Upload Selective Work Orders for an accurate gap figure.')
    else:
        matched_wos, _ = enrich_and_filter(downtime_data, wo_data, asset_lookup)

    gap = compute_gap(sfc_summary, matched_wos)
    machine_breakdown = compute_machine_breakdown(sfc_summary, matched_wos)

    # Two MTTR figures, deliberately. They answer different questions and
    # on June 2026 data they differ by about 65%, so publishing one
    # without saying which is a real risk of a wrong board number:
    #
    #   repair_times_press — SFC-monitored presses only. Describes the
    #     same asset set as the gap figure, so MTTR and coverage % are
    #     talking about the same machines.
    #   repair_times_all — every Maintenance/Electrician breakdown,
    #     including compressors, chillers and other plant. Describes
    #     what the team actually maintains.
    #
    # Neither is "correct" in the abstract; it depends what the number is
    # being used to claim. Both are returned so the choice is made where
    # it's visible, not silently inside this function.
    if wo_file_provided:
        all_enriched, wo_map = enrich_all(downtime_data, wo_data, asset_lookup)
        craft_matched = [d for d in all_enriched if d['wo'] in wo_map]
    else:
        craft_matched = []

    repair_times_all = compute_repair_times(craft_matched)
    repair_times_press = compute_repair_times(matched_wos)

    return {
        'sfc_summary': sfc_summary,
        'matched_wos': matched_wos,
        'machine_breakdown': machine_breakdown,
        'warning': warning,
        'repair_times_press': repair_times_press,
        'repair_times_all': repair_times_all,
        **gap,
    }
