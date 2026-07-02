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


def is_known_machine(asset_code):
    """True if this Agility asset code maps to an SFC-monitored press."""
    return any(asset_code in codes for codes in SFC_TO_AGILITY.values())
