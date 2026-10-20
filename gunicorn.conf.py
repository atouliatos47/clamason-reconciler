# Read automatically by gunicorn when it starts in this folder (Render's
# start command runs gunicorn here). Anything given on the start command
# itself still wins over this file.
#
# timeout: how long one request may take before gunicorn kills the worker.
# The default is 30 seconds. The Open Jobs export is ~31,000 rows; reading
# it is fast now (parsers/xlsx_rows.py), but Render's free server has a
# small share of a CPU, so this leaves plenty of room for big uploads
# instead of cutting them off with an error page.
timeout = 120
