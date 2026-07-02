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
    even if the caller raises."""
    ext = os.path.splitext(file_storage.filename)[1] or '.xlsx'
    path = os.path.join(TMP, f'{prefix}{ext}')
    file_storage.save(path)
    try:
        yield path
    finally:
        if os.path.exists(path):
            os.remove(path)
