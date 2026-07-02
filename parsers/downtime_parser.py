"""
Parser for the Agility "Down Time Analysis" (AG3-205) export.

Each row is a WO with a 6-digit job number, an asset code, and a
Down Time column like '02h25m' or '01d03h16m'. Returns raw rows only
— no craft/machine filtering here. That filtering belongs in
reconciliation.py, not in the parser, so the parser stays a pure
"read this file format" function.
"""
import re
import pandas as pd

from time_utils import dhm_to_hours


def parse_downtime_file(filepath):
    df = pd.read_excel(filepath, header=None)
    records = []
    for _, row in df.iterrows():
        vals = list(row)
        v0 = str(vals[0]).strip() if vals[0] is not None else ''
        if not re.match(r'^\d{6}$', v0):
            continue
        asset = str(vals[1]).strip() if vals[1] else ''
        downtime_str = str(vals[9]).strip() if len(vals) > 9 and vals[9] else ''
        records.append({
            'wo': v0,
            'asset': asset,
            'downtime_hrs': dhm_to_hours(downtime_str),
            'downtime_raw': downtime_str,
            'job_type': '',
            'status': '',
            'desc': '',
            'asset_name': '',
        })
    return records
