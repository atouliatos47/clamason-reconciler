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

# Several requests at once. Opening a tab makes ~5 requests (the page,
# its scripts, the reminder badges, the page's data); gunicorn's default
# is ONE sync worker, so they all queued behind each other — and behind
# anyone else's upload. 'gthread' gives the one worker process 4 threads,
# so they're answered side by side. Threads rather than extra worker
# processes: they share one copy of the app in memory (Render's free
# server has 512 MB) and one pool of database connections (db.py), and
# the reminder cache (reminders.py) is shared and lock-protected.
# Uploads are thread-safe too: each gets its own temp file (file_utils.py).
worker_class = 'gthread'
workers = 1
threads = 4
