"""
Parser for the EFACS E/8 "Cost of Scrap" .xls export.

One row per works-order/part/quantity line, sorted by works order number,
with a single 'Total :' row at the end of the data rather than a running
subtotal per works order. Legacy CDFV2 .xls, but — unlike the SFC weekly/
monthly OEE export — pandas' default engine reads it cleanly with no
sector-size warning, so this doesn't need xlrd called directly.

WHY THIS FILE EXISTS
---------------------
SFC's own scrap tracking (the scrap_parts column on the OEE Sub Totals
row, see oee_parser.py) is badly under-populated: June 2026 SFC logged
1,777 scrap parts against EFACS's real 6,955, and July 2026 SFC logged
125 against EFACS's real 4,995 — roughly a 40x gap. EFACS is the
system of record for scrap (it's driven by the works-order booking
process, not a shop-floor sensor count), so its total is the one to
trust for the OEE Quality pillar.

WHAT THIS DOESN'T GIVE YOU
----------------------------
No machine or press column — EFACS keys this report by works order and
part, not by asset. That means the correction this feeds
(oee_parser.apply_efacs_scrap_correction) can only be applied at fleet
level; per-machine quality/OEE has no EFACS-sourced equivalent and stays
exactly as SFC reported it.
"""
import pandas as pd


def parse_efacs_scrap_file(filepath):
    """Returns a dict: total_quantity, total_cost, row_count,
    works_order_count, period (start/end strings from the report footer,
    or '' if not found).

    Raises ValueError if the expected header row isn't where this export
    always puts it — better than silently summing the wrong columns.
    """
    df = pd.read_excel(filepath, header=None)

    header_row = None
    for i in range(min(5, len(df))):
        if str(df.iat[i, 0]).strip() == 'Works order':
            header_row = i
            break
    if header_row is None:
        raise ValueError(
            "Couldn't find the 'Works order' header row — this doesn't "
            "look like an EFACS Cost of Scrap export."
        )

    # Data runs from just after the header down to (not including) the
    # 'Total :' row EFACS prints once, after the last works order — not
    # a per-works-order subtotal repeated throughout. Summing past it
    # double-counts the total against itself.
    end_row = len(df)
    for i in range(header_row + 1, len(df)):
        if str(df.iat[i, 0]).strip() == 'Total :':
            end_row = i
            break

    data = df.iloc[header_row + 1:end_row].copy()
    data.columns = ['works_order', 'part', 'revision', 'quantity', 'uom',
                     'actual_time', 'operator_value', 'work_centre_value',
                     'material_cost', 'total_cost'][:df.shape[1]]

    quantity = pd.to_numeric(data['quantity'], errors='coerce')
    cost = pd.to_numeric(data['total_cost'], errors='coerce')

    period = ''
    for i in range(end_row, min(end_row + 10, len(df))):
        cell = str(df.iat[i, 0]).strip()
        if cell.startswith('Earliest start date'):
            start = str(df.iat[i, 2]).strip()
            # 'Latest start date' is the next row down in every export
            # seen so far — not searched for by label, since a blank
            # 'To' cell on this row makes that harder to find reliably
            # than just reading the row directly below it.
            end = str(df.iat[i + 1, 2]).strip() if i + 1 < len(df) else ''
            period = f'{start} to {end}'
            break

    return {
        'total_quantity': int(quantity.sum()) if quantity.notna().any() else 0,
        'total_cost': round(float(cost.sum()), 2) if cost.notna().any() else 0.0,
        'row_count': len(data),
        'works_order_count': data['works_order'].nunique(),
        'period': period,
    }
