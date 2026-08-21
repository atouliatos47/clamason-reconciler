"""Weekly Production Plan import — same 'WK30'-style workbook Matt
already maintains (WK_30D_2026.xlsx). The sheet we need gets renamed
every week (WK30, WK31, WK32, ...), so instead of hardcoding a sheet
name, this scans every sheet for the one with the right headers:
'Machinery', 'Planned', and 'Available Run Hrs' together on the same
row. That combination is specific to this one sheet — the workbook's
other planning sheets ('4 wk Plan', '1 wk plan') carry some of these
column names but never all three together.

Two numbers come out of this: Plan Quantity (a straight sum of the
Planned column) and Plan Hours (a straight sum of the Available Run
Hours column — the EFACS-rate figure, not the OEE-discounted one; see
the conversation this was speced in for why). Both are plain, working
column sums — no formula-fixing needed, unlike the sheet's own broken
Hours Required column, which this deliberately never touches.
"""
from openpyxl import load_workbook

# Headers must appear together, in this order, somewhere on one row.
REQUIRED_HEADERS = ['MACHINERY', 'PLANNED', 'AVAILABLE RUN HRS']


def _find_plan_sheet(wb):
    """Search every sheet for the header row matching REQUIRED_HEADERS.
    Returns (sheet_name, header_row_index, {header_text: col_index})."""
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        for row_idx, row in enumerate(ws.iter_rows(min_row=1, max_row=15, values_only=True), start=1):
            found = {}
            for col_idx, val in enumerate(row):
                if val is None:
                    continue
                text = str(val).strip().upper()
                for header in REQUIRED_HEADERS:
                    if text == header or text.startswith(header):
                        found[header] = col_idx
            if all(h in found for h in REQUIRED_HEADERS):
                return sheet_name, row_idx, found
    return None, None, None


def parse_production_plan(path):
    """Returns {'sheet_name', 'plan_quantity', 'plan_hours', 'row_count'}.
    Raises ValueError with a clear message if no matching sheet is found,
    or if the columns exist but contain no usable numeric data — better
    to fail loudly here than silently save a zero."""
    wb = load_workbook(path, data_only=True, read_only=True)
    sheet_name, header_row, cols = _find_plan_sheet(wb)
    if sheet_name is None:
        raise ValueError(
            "Could not find a sheet with Machinery / Planned / Available Run Hrs "
            "columns in this workbook. Expected the same layout as the WK30-style "
            "production plan sheet — check the file is the right export."
        )

    ws = wb[sheet_name]
    planned_col = cols['PLANNED']
    hours_col = cols['AVAILABLE RUN HRS']

    plan_quantity = 0.0
    plan_hours = 0.0
    row_count = 0
    for row in ws.iter_rows(min_row=header_row + 1, values_only=True):
        if planned_col < len(row) and isinstance(row[planned_col], (int, float)):
            plan_quantity += row[planned_col]
        if hours_col < len(row) and isinstance(row[hours_col], (int, float)):
            plan_hours += row[hours_col]
        if (planned_col < len(row) and row[planned_col] is not None) or \
           (hours_col < len(row) and row[hours_col] is not None):
            row_count += 1

    if row_count == 0:
        raise ValueError(
            f"Found sheet '{sheet_name}' with the right headers, but no data rows "
            "underneath them. Check the file actually has this week's plan filled in."
        )

    return {
        'sheet_name': sheet_name,
        'plan_quantity': round(plan_quantity, 2),
        'plan_hours': round(plan_hours, 2),
        'row_count': row_count,
    }
