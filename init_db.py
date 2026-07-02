"""
Run this ONCE after setting the DATABASE_URL environment variable, to
create the monthly_runs table in your Neon database.

Usage (Windows cmd):
    set DATABASE_URL=postgresql://your-connection-string-here
    python init_db.py

After running, you can verify it worked by checking the "Tables" tab
in the Neon dashboard, or running SELECT * FROM monthly_runs; in
Neon's SQL editor — you should see an empty table with the right columns.
"""
import db

if __name__ == '__main__':
    print('Creating monthly_runs table (safe to run more than once)...')
    db.init_schema()
    print('Done. Check the Neon dashboard "Tables" tab to confirm monthly_runs now exists.')
