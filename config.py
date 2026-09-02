"""
Config and lookup tables for the reconciler.

Everything here is data, not logic — pulled out of server.py so the
"what counts as X" rules live in one place you can scan without wading
through parser code. If a reason code or job type needs to be added or
reclassified, this is the only file that should need to change.
"""

# --- SFC downtime reason codes -------------------------------------------

# SFC switched to a prefixed naming scheme (MA-/TR-/QA-) from September
# 2026 — e.g. 'FAULT - PRESS' became 'MA-FAULT PRESS'. Both old and new
# spellings are kept in every set below, on purpose: a month reprocessed
# from an older upload still needs the old spelling to categorise
# correctly, and there's no reliable way to tell "this file predates the
# rename" from the data alone. QA-prefixed codes (QA-QUALITY FAIL,
# QA-WAITING QUALITY PASS OFF...) are new categories, not renames of
# anything that existed before — they're not in any set here yet, so
# they land in the 'production' residual bucket until/unless a
# dedicated quality category gets built.
#
# 'MA-FAULT-FEEDER/DECOILER/' is exactly what SFC's own export gives —
# it cuts off right at the slash, same as the old 'FAULT-FEEDER/
# DECOILER/STR' short form did. Not confirmed whether that's genuinely
# the full code or a display truncation; if downtime under this reason
# stops showing up as Maintenance-owned after the SFC switch, check the
# exact string in a fresh export first.
#
# Broader set: anything that's ever been coded as a maintenance-caused
# downtime reason in SFC, including lubrication.
MAINTENANCE_CODES = {
    'FAULT - PRESS',
    'FAULT-FEEDER/DECOILER/STR',
    'FAULT-SHAKER/CONVEYOR',
    'FAULT-FEEDER/DECOILER/STRAIGHTENER',
    'LUBRICATION/OIL REFILL',
    'MA-FAULT PRESS',
    'MA-FAULT-FEEDER/DECOILER/',
    'MA-FAULT SHAKER/CONVEYOR',
}

# Narrower set: only these two reasons are actually logged against
# Maintenance/Electrician on the shop floor, so this is the scope used
# for the "is this fairly attributed to Maintenance?" gap report.
# Lubrication and the other MAINTENANCE_CODES entries are deliberately
# excluded from this specific check.
BLAME_FAULT_CODES = {'FAULT - PRESS', 'FAULT-FEEDER/DECOILER/STR', 'MA-FAULT PRESS', 'MA-FAULT-FEEDER/DECOILER/'}

# Departmental attribution for the board review — every code that is a
# FAULT on plant maintenance is responsible for.
#
# Deliberately NOT the same set as BLAME_FAULT_CODES. That one drives the
# SFC-vs-Agility gap figure and is scoped to the two codes that can
# actually be matched against a press-fault work order; widening it would
# move a number already presented to the board. This set answers a
# different question — "which department owns this downtime" — and
# FAULT-SHAKER/CONVEYOR plainly belongs to maintenance by that test.
#
# On June 2026 the difference is 122.1h vs 126.7h. The 126.7h figure is
# what the board slide mockup already shows, so this brings the code into
# line with what was presented rather than the other way round.
#
# LUBRICATION/OIL REFILL is excluded on purpose. It's maintenance-ish
# work but it isn't a fault, and the incoming SFC code list doesn't give
# it an 'MA -' prefix — it's an operator topping up oil, not a breakdown.
# Folding 17.3h of it into a fault figure would overstate breakdowns.
FAULT_CODES = {
    'FAULT - PRESS',
    'FAULT-FEEDER/DECOILER/STR',
    'FAULT-FEEDER/DECOILER/STRAIGHTENER',
    'FAULT-SHAKER/CONVEYOR',
    'MA-FAULT PRESS',
    'MA-FAULT-FEEDER/DECOILER/',
    'MA-FAULT SHAKER/CONVEYOR',
}

TOOLROOM_CODES = {
    'TOOL FAILURE - PRODUCTION',
    'TOOL FAILURE - SET UP',
    'TOOL UNAVAILABLE',
    'WASTE IN THE TOOL',
    'TR-TOOL UNAVAILABLE',
    'TR-TOOL FAILURE',
}

PLANNED_CODES = {'PLANNED OFFLINE', 'NO PRODUCTION PLANNED'}

# --- Agility work order rules ----------------------------------------------

# Job types that count as "maintenance work" when raised by a
# Maintenance/Electrician-craft resource.
MAINTENANCE_JOB_TYPES = [
    'breakdown repair',
    'routine minor service',
    'planned service & maintenance',
    'corrective maintenance',
]

# Only these crafts count as "Maintenance" for the gap report.
# Everything else (Toolmaker/Toolroom, Production, Quality, etc.) is a
# different area and must not appear in a Maintenance downtime report.
MAINTENANCE_CRAFTS = ('maintenance', 'electrician')

# Toolroom craft, for counting the toolroom's own work orders.
#
# Kept separate from MAINTENANCE_CRAFTS rather than added to it: the
# board slide had a Toolroom card labelled 'Agility (tool WOs)' that was
# actually showing the MAINTENANCE count, because that was the only WO
# figure the reconciler produced. Widening MAINTENANCE_CRAFTS would have
# fixed the label by breaking the gap report, which is scoped to
# maintenance work on purpose.
TOOLROOM_CRAFTS = ('toolmaker',)

# --- SFC machine <-> Agility asset code mapping ----------------------------
# Only machines in this dict can ever be reconciled — a WO on an asset
# code NOT in here (a chiller, compressor, welder, etc.) can never be
# treated as "covering" an SFC fault event, because SFC doesn't track
# fault codes for anything outside this list.
SFC_TO_AGILITY = {
    'Bihler':               ['000074'],
    'Bruderer 1':           ['00016'],
    'Bruderer 2':           ['00031', '00046'],
    'Bruderer 3':           ['00032', '00047'],
    'Bruderer 60T ISI73':   ['00043'],
    'Chin Fong 110 ISI1':   ['00009'],
    'Chin Fong 110 ISI74':  ['00044'],
    'Finzer Line 17':       ['00375'],
    'Finzer Line 18':       ['00378'],
    'Finzer Line 19':       ['00379'],
    'Finzer Line 20':       ['00383'],
    'Heenan 1':             ['000038'],
    'Heenan 2':             ['000040'],
    'Heenan 3':             ['000048'],
    'HME 20T A ISI23':      ['00025'],
    'HME 20T C ISI22':      ['00024'],
    'Kaiser 50T 1':         ['00029'],
    'Kaiser 50T 2':         ['00030'],
    'Rockwell 1':           ['00231'],
}

# Sheets/machines in SFC exports that aren't real production assets and
# should never be counted toward machine_count, max_possible_hrs, etc.
NON_MACHINE_SHEETS = {'spare sfc box'}


# --- Daily View job-type buckets --------------------------------------------
# Confirmed against Agility's actual Job Type dropdown (screenshots) and real
# WO descriptions (the "Normal Maintenance" investigation). Deliberately a
# SEPARATE, more complete set of lists from MAINTENANCE_JOB_TYPES above —
# that one was built narrowly for the monthly board reconciliation. The
# Daily View needs to bucket every job type Maintenance/Electrician actually
# raises, not just the ones the board report cares about.
BREAKDOWN_JOB_TYPE = 'breakdown repair'

PLANNED_JOB_TYPES_DAILY = {
    'planned service & maintenance',
    'tool preventative maintenance',
    'routine minor service',
}

PROJECT_JOB_TYPES_DAILY = {
    'modification',
    'continuous improvement',
    'efficiency improvement',
}

# Deliberately excluded from every bucket above — confirmed to be used
# inconsistently for both planned and reactive work in practice, so it
# can't be trusted either way until logging discipline improves (see the
# toolbox-talk email). Falls into the "Other" bucket instead.
# 'normal maintenance'


def is_known_machine(asset_code):
    """True if this Agility asset code maps to an SFC-monitored press."""
    return any(asset_code in codes for codes in SFC_TO_AGILITY.values())


# --- Intended shift pattern, for TEEP against a realistic baseline --------
# TEEP's textbook definition measures against every calendar hour that
# exists — see oee_parser.py — which treats every machine as if it could
# plausibly run 24/7. Bihler was never going to hit that, and comparing
# it to that baseline every month buries a genuinely useful number under
# one that was never achievable to begin with.
#
# This is the one piece no report can give you. SFC and Agility both
# report what WAS scheduled and what DID run, after the fact — neither
# has any concept of what a machine is INTENDED to run. That's a roster
# decision, not shop-floor data, so it has to be entered here rather
# than parsed from an export.
#
# Seeded from July 2026's actual Net Available hours (SFC's own "what
# got scheduled" figure), converted to an hours/week rate. That's a real
# starting point rather than a guess, but it's still one month's data —
# July had no unusual shutdown week, so it should be close for most
# machines, but it's worth walking through this list and correcting
# anything that doesn't reflect the real, ongoing intended pattern (a
# machine deliberately kept at reduced hours, or one due to go to extra
# shifts, wouldn't show up right from July alone).
#
# Heenan 1 has no entry to seed from — it didn't appear in July's OEE
# export at all despite being a real press in SFC_TO_AGILITY. Left at
# 0.0, which reads as "not yet configured" on the TEEP page, until a
# real figure goes in.
#
# Whole fleet in hours/week rather than a shift-count-and-days model —
# simpler to fill in for anything that doesn't cleanly fit "N shifts,
# M days" (a machine run for part of a shift, for instance), and every
# figure here is just "roughly how many hours a week should this
# machine be running," which is the only thing actually needed for the
# oee_parser.py calculation this feeds.
SHIFT_HOURS_PER_WEEK = {
    'Bihler':               10.0,
    'Bruderer 1':           86.5,
    'Bruderer 2':           98.2,
    'Bruderer 3':           85.0,
    'Bruderer 60T ISI73':   49.0,
    'Chin Fong 110 ISI1':   37.2,
    'Chin Fong 110 ISI74':  22.9,
    'Finzer Line 17':       75.9,
    'Finzer Line 18':       0.0,
    'Finzer Line 19':       21.9,
    'Finzer Line 20':       36.9,
    'HME 20T A ISI23':      12.9,
    'HME 20T C ISI22':      0.0,
    'Heenan 1':             0.0,   # no July OEE data — needs your input
    'Heenan 2':             39.5,
    'Heenan 3':             29.4,
    'Kaiser 50T 1':         73.0,
    'Kaiser 50T 2':         62.6,
    'Rockwell 1':           16.8,
}
