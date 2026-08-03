import logging
import os

from flask import Flask

import db
from routes import bp

app = Flask(__name__, static_folder='public', static_url_path='')
app.register_blueprint(bp)

log = logging.getLogger(__name__)


def _apply_schema():
    """Run schema.sql at startup so the database always matches the code.

    This used to be a manual step — init_db.py, run from a laptop with
    DATABASE_URL set. That's one step too many to remember: Render's free
    tier has no shell, so the migration and the deploy happen in different
    places, and forgetting it produces a deploy that looks completely
    healthy right up until someone presses Save and gets
    'column "mtta_hrs_press" does not exist'.

    schema.sql is written to be safe to run on every boot — every
    statement is CREATE TABLE IF NOT EXISTS, CREATE UNIQUE INDEX IF NOT
    EXISTS, or ALTER TABLE ADD COLUMN IF NOT EXISTS. Nothing drops,
    nothing rewrites, existing rows are untouched.

    A failure here is logged rather than raised. If the schema can't be
    applied the app is still worth starting: every read-only page keeps
    working, and a running app showing one clear error beats a service
    that won't boot at all. The error goes to the Render logs with the
    exception text so the cause is visible.
    """
    if not os.environ.get('DATABASE_URL'):
        log.warning('DATABASE_URL not set — skipping schema setup. '
                    'Saving and trend history will not work.')
        return
    try:
        db.init_schema()
        log.info('Database schema applied.')
    except Exception as exc:
        log.error('Could not apply database schema: %s', exc, exc_info=True)


_apply_schema()


if __name__ == '__main__':
    app.run(port=3017, debug=False)
