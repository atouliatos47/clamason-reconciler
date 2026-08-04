"""
Config and lookup tables for the reconciler.

Everything here is data, not logic — pulled out of server.py so the
"what counts as X" rules live in one place you can scan without wading
through parser code. If a reason code or job type needs to be added or
reclassified, this is the only file that should need to change.
"""

# --- SFC downtime reason codes -------------------------------------------

# Broader set: anything that's ever been coded as a maintenance-caused
# downtime reason in SFC, including lubrication.
MAINTENANCE_CODES = {
    'FAULT - PRESS',
    'FAULT-FEEDER/DECOILER/STR',
    'FAULT-SHAKER/CONVEYOR',
    'FAULT-FEEDER/DECOILER/STRAIGHTENER',
    'LUBRICATION/OIL REFILL',
}

# Narrower set: only these two reasons are actually logged against
# Maintenance/Electrician on the shop floor, so this is the scope used
# for the "is this fairly attributed to Maintenance?" gap report.
# Lubrication and the other MAINTENANCE_CODES entries are deliberately
# excluded from this specific check.
BLAME_FAULT_CODES = {'FAULT - PRESS', 'FAULT-FEEDER/DECOILER/STR'}

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
}

TOOLROOM_CODES = {
    'TOOL FAILURE - PRODUCTION',
    'TOOL FAILURE - SET UP',
    'TOOL UNAVAILABLE',
    'WASTE IN THE TOOL',
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
