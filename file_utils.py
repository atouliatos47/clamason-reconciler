"""
Small helper for handling uploaded files safely — saves to a temp path,
yields it, and always cleans up afterward. Centralizes the
"WinError 32: file in use" fix (close any pandas ExcelFile handles
before this runs — see parsers/sfc_monthly_xlsx.py's `with` block) so
every route gets the same safe behavior instead of re-implementing
save/remove by hand.
"""
import os
import tempfile
from contextlib import contextmanager

TMP = tempfile.gettempdir()


@contextmanager
def saved_upload(file_storage, prefix):
    """file_storage: a Flask FileStorage object (request.files.get(...)).
    Saves it to a temp path, yields the path, deletes it afterward —
    even if the caller raises.

    The path is unique per upload (e.g. /tmp/oee_daily_k3j9x2.xls), not a
    fixed '<prefix><ext>': the server can handle several requests at
    once, and two uploads of the same kind arriving together would
    otherwise overwrite — and then delete — each other's file. mkstemp
    opens the file it creates, so that handle is closed straight away
    before the upload is written to it (an open handle is what caused
    the old "WinError 32: file in use" on Windows)."""
    ext = os.path.splitext(file_storage.filename)[1] or '.xlsx'
    fd, path = tempfile.mkstemp(prefix=f'{prefix}_', suffix=ext, dir=TMP)
    os.close(fd)
    file_storage.save(path)
    try:
        yield path
    finally:
        if os.path.exists(path):
            os.remove(path)
