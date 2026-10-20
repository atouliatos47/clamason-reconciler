"""
Fast, low-memory reading of the first sheet of an .xlsx file into plain
rows of values — the same rows pandas.read_excel(header=None) gives, as
far as the work-order block walk is concerned.

Why it exists: Agility's Selective Work Orders export for the Open Jobs
check (every status, due 01/01/2025 onwards) is ~31,000 rows and 26 MB
of sheet XML with 100,000+ merged cells. pandas/openpyxl took 7.6 s on a
fast PC to read it, and far longer on Render's free server — long enough
to hit the server's time limit (20 Oct 2026: "Unexpected token '<'"). This
streams the sheet XML instead and only builds the values, which reads the
same file in under 2 s.

It reads values only: text (inline or shared strings), numbers, booleans,
and numbers stored with a date format turned into datetimes (that's how
Agility stores the Due Date). No formulas, styles or merges — nothing the
block walk uses.
"""
import re
import zipfile
from datetime import datetime, timedelta
import xml.etree.ElementTree as ET

_NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
_REL = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'

# Excel's built-in number formats that show a date or time.
_BUILTIN_DATE_FORMATS = set(range(14, 23)) | set(range(27, 37)) | set(range(45, 48)) | set(range(50, 59))


def _looks_like_date(code):
    """A custom number format like '[$-010809]dd/mm/yyyy' shows a date.
    Quoted text, [colour/locale] blocks and escaped characters don't count."""
    code = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', '', code or '').lower()
    return bool(re.search(r'[dmyhs]', code))


def _date_styles(z):
    """Indexes of the cell styles (the 's' attribute) that show dates."""
    try:
        styles = ET.fromstring(z.read('xl/styles.xml'))
    except KeyError:
        return set()
    custom = {}
    num_fmts = styles.find(f'{_NS}numFmts')
    if num_fmts is not None:
        for f in num_fmts:
            custom[int(f.get('numFmtId'))] = f.get('formatCode')
    out = set()
    xfs = styles.find(f'{_NS}cellXfs')
    if xfs is not None:
        for i, xf in enumerate(xfs):
            fid = int(xf.get('numFmtId') or 0)
            if fid in _BUILTIN_DATE_FORMATS or (fid in custom and _looks_like_date(custom[fid])):
                out.add(i)
    return out


def _first_sheet(z):
    """Path of the workbook's first sheet inside the zip, and whether the
    workbook counts dates from 1904 (old Mac Excel) instead of 1900."""
    wb = ET.fromstring(z.read('xl/workbook.xml'))
    pr = wb.find(f'{_NS}workbookPr')
    date1904 = pr is not None and (pr.get('date1904') or '').lower() in ('1', 'true')
    sheet = wb.find(f'{_NS}sheets/{_NS}sheet')
    rid = sheet.get(f'{_REL}id') if sheet is not None else None
    try:
        rels = ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
        for rel in rels:
            if rel.get('Id') == rid:
                target = rel.get('Target')
                return (target.lstrip('/') if target.startswith('/') else 'xl/' + target), date1904
    except KeyError:
        pass
    return 'xl/worksheets/sheet1.xml', date1904


def _shared_strings(z):
    try:
        root = ET.fromstring(z.read('xl/sharedStrings.xml'))
    except KeyError:
        return []
    return [''.join(t.text or '' for t in si.iter(f'{_NS}t')) for si in root.findall(f'{_NS}si')]


def _col_index(ref):
    """'K21' -> 10 (zero-based column)."""
    n = 0
    for ch in ref:
        if 'A' <= ch <= 'Z':
            n = n * 26 + ord(ch) - 64
        else:
            break
    return n - 1


def _to_datetime(serial, date1904):
    base = datetime(1904, 1, 1) if date1904 else datetime(1899, 12, 30)
    # Rounded to the second, like openpyxl, so 19:17 doesn't come out as 19:16:59.999.
    return base + timedelta(seconds=round(float(serial) * 86400))


def read_rows(filepath):
    """Every row of the first sheet as a list of values (None for an empty
    cell), all rows padded to the same width. Rows missing from the file are
    returned as empty rows, so blank separator rows stay where they were."""
    with zipfile.ZipFile(filepath) as z:
        sheet_path, date1904 = _first_sheet(z)
        date_styles = _date_styles(z)
        shared = _shared_strings(z)
        rows, width = [], 0
        T_TAG, C_TAG, V_TAG, IS_TAG, ROW_TAG = (f'{_NS}t', f'{_NS}c', f'{_NS}v', f'{_NS}is', f'{_NS}row')
        with z.open(sheet_path) as fh:
            for _, el in ET.iterparse(fh, events=('end',)):
                if el.tag != ROW_TAG:
                    continue
                r = el.get('r')
                row_no = int(r) if r else len(rows) + 1
                while len(rows) < row_no - 1:
                    rows.append([])
                values = {}
                col = -1
                for c in el.iter(C_TAG):
                    ref = c.get('r')
                    col = _col_index(ref) if ref else col + 1
                    t = c.get('t')
                    if t == 'inlineStr':
                        is_el = c.find(IS_TAG)
                        val = ''.join(x.text or '' for x in is_el.iter(T_TAG)) if is_el is not None else ''
                    else:
                        v = c.find(V_TAG)
                        raw = v.text if v is not None else None
                        if raw is None:
                            val = None
                        elif t == 's':
                            val = shared[int(raw)]
                        elif t in ('str', 'e'):
                            val = raw
                        elif t == 'b':
                            val = raw == '1'
                        else:
                            num = float(raw)
                            s = c.get('s')
                            if s is not None and int(s) in date_styles:
                                val = _to_datetime(num, date1904)
                            else:
                                val = int(num) if num.is_integer() else num
                    if val is not None:
                        values[col] = val
                row = [None] * (max(values) + 1 if values else 0)
                for k, val in values.items():
                    row[k] = val
                width = max(width, len(row))
                rows.append(row)
                el.clear()
    for row in rows:
        if len(row) < width:
            row.extend([None] * (width - len(row)))
    return rows
