"""
PDF report builder. Takes the dict returned by reconciliation.reconcile()
directly — no loose separate arguments — so the PDF can never show a
number that didn't come from the one shared reconciliation function.

Bug fixed vs the old build_gap_pdf(): the "Downtime Logged Against
Maintenance (SFC)" table used to have an "Agility WO?" column that was
HARDCODED to always print "X None", regardless of whether WOs actually
matched. It never read any real data. Removed — a per-reason-code
coverage check isn't something the tool can honestly compute anyway
(WOs are matched by asset, not by which specific reason code they were
raised against), so a fabricated-looking column is worse than no column.
The real "did anything cover this" answer lives in the Work Orders
Found table further down, which was always correct.
"""
import io
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable, PageBreak
)

from config import BLAME_FAULT_CODES

NAVY = colors.HexColor('#243547')
LIME = colors.HexColor('#95C11F')
RED = colors.HexColor('#CC0000')
GREEN = colors.HexColor('#2E7D32')
LIGHT_GREY = colors.HexColor('#F5F5F5')
MID_GREY = colors.HexColor('#CCCCCC')


def _ps(name, **kw):
    return ParagraphStyle(name, **kw)


def _build_header(story, styles, period):
    title_s, sub_s = styles['title'], styles['sub']
    hdr = Table(
        [[Paragraph('SFC vs Agility — WO Gap Analysis', title_s),
          Paragraph(f'Period: {period}', sub_s)]],
        colWidths=[120 * mm, 65 * mm],
    )
    hdr.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), NAVY),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 6 * mm),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4 * mm),
        ('TOPPADDING', (0, 0), (-1, -1), 5 * mm),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5 * mm),
        ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
    ]))
    story.append(hdr)
    story.append(Spacer(1, 6 * mm))


def _build_headline(story, styles, sfc_summary, wo_count, gap_hrs, gap_pct):
    reasons = sfc_summary.get('reasons', {})
    reason_evts = sfc_summary.get('reason_events', {})
    maint_reasons = {k: v for k, v in reasons.items() if k.upper() in BLAME_FAULT_CODES}
    fault_events = sum(reason_evts.get(k, 0) for k in maint_reasons)

    if gap_pct >= 60:
        band_colour, band_word = RED, 'NOT COVERED'
    elif gap_pct > 0:
        band_colour, band_word = colors.HexColor('#E65100'), 'PARTLY COVERED'
    else:
        band_colour, band_word = GREEN, 'FULLY COVERED'

    plain = (
        f"This period, <b>{sfc_summary['maintenance_hrs']:.2f} hours</b> of press downtime "
        f"({fault_events} fault events) were logged against Maintenance. "
        f"Only <b>{wo_count}</b> of those had a Work Order raised by Maintenance or Electrical "
        f"on a recognised machine."
    )

    headline = Table([
        [Paragraph(band_word, styles['headline_label'])],
        [Spacer(1, 3 * mm)],
        [Paragraph(f"{gap_pct:.0f}%", styles['headline_num'])],
        [Spacer(1, 3 * mm)],
        [Paragraph('of that downtime has NO Work Order to back it up', styles['headline_label'])],
        [Spacer(1, 4 * mm)],
        [Paragraph(plain, styles['headline_text'])],
    ], colWidths=[185 * mm])
    headline.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), band_colour),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('TOPPADDING', (0, 0), (0, 0), 5 * mm),
        ('BOTTOMPADDING', (0, 0), (0, -1), 0),
        ('TOPPADDING', (0, 1), (0, -1), 0),
        ('BOTTOMPADDING', (0, -1), (0, -1), 5 * mm),
        ('LEFTPADDING', (0, 0), (-1, -1), 8 * mm),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8 * mm),
    ]))
    story.append(headline)
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph(
        'What this checks: only downtime SFC recorded as <b>FAULT - PRESS</b> or '
        '<b>FAULT-FEEDER/DECOILER/STR</b> — the two reason codes logged against Maintenance. '
        'Work Orders on machines SFC doesn\u2019t track fault codes for (chillers, compressors, '
        'welders, etc.) are excluded — they can\u2019t count as covering this downtime.',
        styles['body']))
    story.append(Spacer(1, 5 * mm))
    return fault_events


def _build_overview(story, sfc_summary, fault_events, wo_count, gap_hrs, gap_pct):
    mc = sfc_summary.get('machine_count', 0)
    gap_events = max(0, fault_events - wo_count)
    rows = [
        ['Machines monitored (SFC)', str(mc)],
        ['Total SFC downtime events', str(sfc_summary.get('total_events', '—'))],
        ['Total SFC downtime duration', f"{sfc_summary.get('total_hrs', 0):.2f}h"],
        ['Maintenance fault events (SFC)', str(fault_events)],
        ['Maintenance/Electrical WOs raised', str(wo_count)],
        ['Gap — fault events with no WO', str(gap_events)],
        ['Gap — unaccounted hours', f"{gap_hrs:.2f}h  ({gap_pct}%)"],
    ]
    t = Table(rows, colWidths=[120 * mm, 65 * mm])
    t.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('ROWBACKGROUNDS', (0, 0), (-1, -1), [LIGHT_GREY, colors.white]),
        ('GRID', (0, 0), (-1, -1), 0.5, MID_GREY),
        ('LEFTPADDING', (0, 0), (-1, -1), 4 * mm),
        ('TOPPADDING', (0, 0), (-1, -1), 2.5 * mm),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2.5 * mm),
        ('ALIGN', (1, 0), (1, -1), 'CENTER'),
        ('FONTNAME', (1, 0), (1, -1), 'Helvetica-Bold'),
        ('TEXTCOLOR', (1, 0), (1, -1), NAVY),
        ('BACKGROUND', (0, 5), (-1, 6), colors.HexColor('#FDECEA')),
        ('TEXTCOLOR', (1, 5), (1, 6), RED),
        ('FONTNAME', (0, 5), (-1, 6), 'Helvetica-Bold'),
    ]))
    story.append(t)
    story.append(Spacer(1, 5 * mm))


def _build_scheduled_hours(story, styles, sfc_summary):
    story.append(Paragraph('Scheduled vs Actual Hours', styles['section']))
    story.append(HRFlowable(width='100%', thickness=2, color=LIME, spaceAfter=4))
    mc = sfc_summary.get('machine_count', 0)
    phr = sfc_summary.get('period_hrs', 24)
    mph = sfc_summary.get('max_possible_hrs', 0)
    poh = sfc_summary.get('planned_offline_hrs', 0)
    sch = sfc_summary.get('scheduled_hrs', 0)
    actual_run = round(sch - sfc_summary.get('maintenance_hrs', 0) - sfc_summary.get('toolroom_hrs', 0), 2)
    rows = [
        ['Machines monitored', str(mc)],
        ['Period duration', f"{phr:.1f}h"],
        ['Max possible hours', f"{mph:.2f}h  ({mc} machines x {phr:.0f}h)"],
        ['Less: planned offline', f"-{poh:.2f}h"],
        ['Scheduled to run', f"{sch:.2f}h"],
        ['Less: maintenance losses', f"-{sfc_summary.get('maintenance_hrs', 0):.2f}h"],
        ['Less: toolroom losses', f"-{sfc_summary.get('toolroom_hrs', 0):.2f}h"],
        ['Net available to produce', f"{actual_run:.2f}h"],
    ]
    t = Table(rows, colWidths=[120 * mm, 65 * mm])
    t.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('ROWBACKGROUNDS', (0, 0), (-1, -1), [LIGHT_GREY, colors.white]),
        ('GRID', (0, 0), (-1, -1), 0.5, MID_GREY),
        ('LEFTPADDING', (0, 0), (-1, -1), 4 * mm),
        ('TOPPADDING', (0, 0), (-1, -1), 2.5 * mm),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2.5 * mm),
        ('ALIGN', (1, 0), (1, -1), 'CENTER'),
        ('FONTNAME', (1, 0), (1, -1), 'Helvetica-Bold'),
        ('TEXTCOLOR', (1, 0), (1, -1), NAVY),
        ('BACKGROUND', (0, 4), (-1, 4), colors.HexColor('#E8F5E9')),
        ('FONTNAME', (0, 4), (-1, 4), 'Helvetica-Bold'),
        ('BACKGROUND', (0, 7), (-1, 7), colors.HexColor('#E3F2FD')),
        ('FONTNAME', (0, 7), (-1, 7), 'Helvetica-Bold'),
    ]))
    story.append(t)
    story.append(Spacer(1, 5 * mm))


def _build_sfc_reasons_table(story, styles, sfc_summary):
    """The table that USED to have the fake 'Agility WO?' column.
    Now just shows what SFC recorded, honestly, with a pointer to the
    real WO table below for coverage — no fabricated check marks."""
    reasons = sfc_summary.get('reasons', {})
    reason_evts = sfc_summary.get('reason_events', {})
    maint_reasons = {k: v for k, v in reasons.items() if k.upper() in BLAME_FAULT_CODES}

    story.append(Paragraph('Downtime Logged Against Maintenance (SFC)', styles['section']))
    story.append(HRFlowable(width='100%', thickness=2, color=LIME, spaceAfter=4))
    if not maint_reasons:
        story.append(Paragraph('No maintenance fault codes found in SFC data.', styles['body']))
        story.append(Spacer(1, 5 * mm))
        return

    header = [Paragraph(t, _ps(f'fh{i}', fontSize=9, textColor=colors.white, fontName='Helvetica-Bold'))
              for i, t in enumerate(['Downtime Reason', 'Events', 'Duration'])]
    rows = [header]
    for reason, hrs in sorted(maint_reasons.items(), key=lambda x: -x[1]):
        evts = reason_evts.get(reason, '—')
        h = int(hrs)
        m = int(round((hrs - h) * 60))
        rows.append([reason, str(evts), f"{h}h {m:02d}m"])

    t = Table(rows, colWidths=[100 * mm, 30 * mm, 55 * mm])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), NAVY),
        ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 1), (-1, -1), 9),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [LIGHT_GREY, colors.white]),
        ('GRID', (0, 0), (-1, -1), 0.5, MID_GREY),
        ('LEFTPADDING', (0, 0), (-1, -1), 3 * mm),
        ('TOPPADDING', (0, 0), (-1, -1), 2.5 * mm),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2.5 * mm),
        ('ALIGN', (1, 0), (2, -1), 'CENTER'),
    ]))
    story.append(t)
    story.append(Spacer(1, 2 * mm))
    story.append(Paragraph(
        'See "Maintenance / Electrical Work Orders Found" below for which WOs actually matched — '
        'coverage isn\u2019t split by reason code here since WOs are matched by asset, not by which '
        'specific fault code triggered them.', styles['footnote']))
    story.append(Spacer(1, 3 * mm))


def _build_wo_table(story, styles, matched_wos):
    story.append(PageBreak())
    story.append(Paragraph('Maintenance / Electrical Work Orders Found', styles['section']))
    story.append(HRFlowable(width='100%', thickness=2, color=LIME, spaceAfter=4))
    if not matched_wos:
        story.append(Paragraph(
            'No Maintenance or Electrical Work Order was raised against this downtime.', styles['body']))
        story.append(Spacer(1, 5 * mm))
        return

    cell_s = _ps('wc', fontSize=8, fontName='Helvetica', leading=11)
    header = [Paragraph(t, _ps(f'wh{i}', fontSize=9, textColor=colors.white, fontName='Helvetica-Bold'))
              for i, t in enumerate(['WO #', 'Asset Code', 'Asset Name', 'Job Type', 'Description', 'Status'])]
    rows = [header]
    for d in matched_wos:
        rows.append([
            Paragraph(d.get('wo', ''), cell_s),
            Paragraph(d.get('asset', ''), cell_s),
            Paragraph(d.get('asset_name', '—') or '—', cell_s),
            Paragraph(d.get('job_type', '—') or '—', cell_s),
            Paragraph(d.get('desc', '—') or '—', cell_s),
            Paragraph(d.get('status', '—') or '—', cell_s),
        ])
    t = Table(rows, colWidths=[18 * mm, 30 * mm, 34 * mm, 32 * mm, 40 * mm, 26 * mm])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), NAVY),
        ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 1), (-1, -1), 9),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [LIGHT_GREY, colors.white]),
        ('GRID', (0, 0), (-1, -1), 0.5, MID_GREY),
        ('LEFTPADDING', (0, 0), (-1, -1), 3 * mm),
        ('TOPPADDING', (0, 0), (-1, -1), 2.5 * mm),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2.5 * mm),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    story.append(t)
    story.append(Spacer(1, 5 * mm))


def _build_recommendations(story, styles, gap_hrs, gap_pct):
    story.append(Paragraph('Recommendations', styles['section']))
    story.append(HRFlowable(width='100%', thickness=2, color=LIME, spaceAfter=4))
    recs = [
        '1.  A WO must be raised in Agility at the point of every fault — before or immediately '
        'after attending the machine.',
        f'2.  {gap_hrs:.2f}h ({gap_pct}%) of maintenance-attributable SFC downtime has no matching '
        'breakdown WO in Agility. Retrospective WOs should be raised to maintain accurate MTBF/MTTR data.',
        '3.  Run this check every period to catch gaps before they accumulate.',
        '4.  Confirm with the team: was this downtime actually attended by Maintenance/Electrical '
        'without logging a WO, or was it something else that SFC mis-coded as a press/feeder fault?',
    ]
    for r in recs:
        story.append(Paragraph(r, styles['body']))
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph(
        'Generated by Clamason Maintenance Reconciler  |  Prepared by Andreas Touliatos', styles['footer']))


def build_gap_pdf(result):
    """result is the dict returned by reconciliation.reconcile()."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=15 * mm,
    )

    styles = {
        'title': _ps('t', fontSize=15, textColor=colors.white, fontName='Helvetica-Bold'),
        'sub': _ps('s', fontSize=8, textColor=LIME, fontName='Helvetica'),
        'section': _ps('sc', fontSize=11, textColor=NAVY, fontName='Helvetica-Bold', spaceBefore=8, spaceAfter=3),
        'body': _ps('b', fontSize=9, textColor=colors.HexColor('#333333'), fontName='Helvetica', leading=13),
        'footnote': _ps('fn', fontSize=7.5, textColor=colors.HexColor('#777777'), fontName='Helvetica-Oblique', leading=10),
        'footer': _ps('f', fontSize=7, textColor=colors.HexColor('#999999'), fontName='Helvetica', alignment=TA_CENTER),
        'headline_num': _ps('hn', fontSize=34, textColor=colors.white, fontName='Helvetica-Bold', alignment=TA_CENTER, leading=40),
        'headline_label': _ps('hl', fontSize=10, textColor=colors.white, fontName='Helvetica-Bold', alignment=TA_CENTER),
        'headline_text': _ps('ht', fontSize=11, textColor=colors.white, fontName='Helvetica', alignment=TA_CENTER, leading=15),
    }

    sfc_summary = result['sfc_summary']
    matched_wos = result['matched_wos']
    wo_count = result['wo_count']
    gap_hrs = result['gap_hrs']
    gap_pct = result['gap_pct']

    story = []
    _build_header(story, styles, sfc_summary.get('period', 'N/A'))
    fault_events = _build_headline(story, styles, sfc_summary, wo_count, gap_hrs, gap_pct)
    _build_overview(story, sfc_summary, fault_events, wo_count, gap_hrs, gap_pct)
    _build_scheduled_hours(story, styles, sfc_summary)
    _build_sfc_reasons_table(story, styles, sfc_summary)
    _build_wo_table(story, styles, matched_wos)
    _build_recommendations(story, styles, gap_hrs, gap_pct)

    doc.build(story)
    buf.seek(0)
    return buf
