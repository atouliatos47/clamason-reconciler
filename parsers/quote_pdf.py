"""
Reads a supplier's quote PDF and picks out the details for the PO Approvals
"Add a quote" form: supplier, quote reference, quote date and the total
excluding VAT.

Every supplier lays their quote out differently, so this only looks for
the common ways these details are written ("Quote reference FS32264",
"Date 22/10/2026", "Total Cost (excl. VAT) £652.00"). Anything it can't
find with confidence is left out rather than guessed — an empty box is
easy to fill in, a wrong one is easy to miss. The person always checks
the boxes before saving.

A scanned quote (a picture of paper) has no text inside it to read;
extract_quote_fields() reports that with text_found = False.

First written for the Filtermist quote FS32264 (Nov 2026).
"""
import re
from collections import Counter
from datetime import date

import pdfplumber

# Only the first few pages: the details are always near the top, and the
# rest is usually terms and conditions.
MAX_PAGES = 4

_MONTHS = {m: i for i, m in enumerate(
    ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'], start=1)}

# Email providers that say nothing about which company sent the quote.
_GENERIC_EMAIL = {'gmail', 'googlemail', 'outlook', 'hotmail', 'live', 'yahoo',
                  'icloud', 'btinternet', 'aol', 'msn', 'clamason'}

# Words that often sit in front of a company name on the same line but
# aren't part of it ("Registered Office Smith Engineering Ltd").
_NOT_A_NAME = {'registered', 'office', 'copyright', 'from', 'to', 'dear', 'quotation',
               'quote', 'invoice', 'thank', 'thanks', 'please', 'for', 'company',
               'by', 'regards', 'kind', 'sincerely', 'yours', 'supplier', 'customer'}

# A money amount like 652.00 or 1,250.50 (pence always present on totals).
_MONEY = r'([\d]{1,3}(?:,\d{3})*(?:\.\d{2})|\d+\.\d{2})'


def read_pdf_text(path):
    """All the text in the first MAX_PAGES pages, or '' if there is none.
    `path` can be a file's path or the file itself held in memory
    (io.BytesIO) — pdfplumber reads either."""
    with pdfplumber.open(path) as pdf:
        parts = [(page.extract_text() or '') for page in pdf.pages[:MAX_PAGES]]
    return '\n'.join(parts)


# --- each detail ---------------------------------------------------------------

def find_quote_ref(text):
    """'Quote reference FS32264', 'Quotation No: Q-1234', 'Our Ref: AB/123'.
    The reference must contain a digit, so words like 'Validity' are never
    taken by mistake."""
    patterns = [
        r'\bquot(?:e|ation)\s*(?:ref(?:erence)?|no\.?|number|#)\s*[:.#]?\s*([A-Z0-9][A-Z0-9/\-_.]{2,30})',
        r'\bour\s*ref(?:erence)?\s*[:.]?\s*([A-Z0-9][A-Z0-9/\-_.]{2,30})',
        r'^\s*quot(?:e|ation)\s*[:#]\s*([A-Z0-9][A-Z0-9/\-_.]{2,30})',
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.I | re.M):
            ref = match.group(1).rstrip('.-_/')
            if re.search(r'\d', ref):
                return ref
    return None


def _make_date(day, month, year):
    year = int(year)
    if year < 100:
        year += 2000
    try:
        return date(year, int(month), int(day))
    except ValueError:
        return None


def find_quote_date(text):
    """The date written next to the word 'Date' — '22/10/2026', '22.10.26',
    '5th November 2026', '05 Nov 2026'. UK order (day first). Dates
    elsewhere in the text (like 'surcharge from 22.06.2026') are ignored."""
    numeric = re.search(
        r'\bdate[d]?\b[^\n\d]{0,25}(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})\b', text, re.I)
    if numeric:
        found = _make_date(*numeric.groups())
        if found:
            return found
    written = re.search(
        r'\bdate[d]?\b[^\n\d]{0,25}(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})', text, re.I)
    if written:
        month = _MONTHS.get(written.group(2)[:3].lower())
        if month:
            return _make_date(written.group(1), month, written.group(3))
    return None


def find_net_total(text):
    """The total EXCLUDING VAT, as a string like '652.00'.

    Looks first for lines that say so ('Total (excl. VAT)', 'Net Total',
    'Sub Total'). A plain 'Total' line is only used when the quote shows
    no VAT amount at all — otherwise 'Total' is usually the price WITH VAT.
    The amount taken is the last one on the line, where totals sit.
    """
    lines = text.split('\n')
    net_labels = [
        r'total[^\n]*?\b(?:excl?(?:uding)?|ex)\b\.?\s*vat',
        r'\bnett?\s*total\b|\btotal\s*nett?\b',
        r'\bsub[\s-]?total\b',
        r'\bgoods\s*total\b',
    ]
    for label in net_labels:
        for line in lines:
            if re.search(label, line, re.I):
                amounts = re.findall(_MONEY, line)
                if amounts:
                    return amounts[-1].replace(',', '')
    shows_vat_amount = any(
        re.search(r'\bvat\b', line, re.I) and re.search(_MONEY, line)
        and not re.search(r'excl?|ex\b|exclud', line, re.I)
        for line in lines)
    if not shows_vat_amount:
        for line in lines:
            if re.match(r'\s*(?:grand\s*)?total\b', line, re.I):
                amounts = re.findall(_MONEY, line)
                if amounts:
                    return amounts[-1].replace(',', '')
    return None


def find_supplier(text):
    """The company that sent the quote.

    1. A company name ending in Limited / Ltd / PLC / LLP that isn't
       Clamason — the one written most often (usually in the footer).
       The name must be on one line: the pattern allows spaces and tabs
       between words but not line breaks, which would join a person's
       name on the line above onto the company name.
    2. Otherwise, the company part of an email address
       (diana@filtermist.com -> Filtermist).
    """
    names = []
    for match in re.finditer(
            r"\b([A-Z][A-Za-z0-9&'.\-]*(?:[ \t]+(?:&[ \t]+)?[A-Z][A-Za-z0-9&'.\-]*){0,4})[ \t]+(Limited|Ltd\.?|PLC|LLP)\b",
            text):
        words = match.group(1).split()
        # "Registered Office Smith Engineering Ltd" -> "Smith Engineering Ltd"
        while len(words) > 1 and words[0].lower() in _NOT_A_NAME:
            words.pop(0)
        name = f"{' '.join(words)} {match.group(2)}".strip()
        if 'clamason' not in name.lower():
            names.append(name)
    if names:
        return Counter(names).most_common(1)[0][0]
    for domain in re.findall(r'[\w.+-]+@([\w-]+)\.[A-Za-z.]{2,}', text):
        if domain.lower() not in _GENERIC_EMAIL:
            return domain.replace('-', ' ').title()
    return None


# --- all together ----------------------------------------------------------------

def extract_quote_fields(path):
    """Everything this file can find in one quote PDF (a path, or the file
    in memory as io.BytesIO).

    Returns {'text_found': bool, 'fields': {...}} where fields only holds
    the details that were found: supplier, quote_ref, received_date
    ('2026-10-22') and amount ('652.00').
    """
    text = read_pdf_text(path)
    if len(text.strip()) < 20:
        return {'text_found': False, 'fields': {}}
    found = {
        'supplier': find_supplier(text),
        'quote_ref': find_quote_ref(text),
        'received_date': find_quote_date(text),
        'amount': find_net_total(text),
    }
    if found['received_date']:
        found['received_date'] = found['received_date'].isoformat()
    return {'text_found': True, 'fields': {k: v for k, v in found.items() if v}}
