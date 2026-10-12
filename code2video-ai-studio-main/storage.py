"""Permanent storage helpers.

* Database: Postgres (Supabase) when DATABASE_URL is set, otherwise local SQLite (dev / fallback).
* Files: Supabase Storage when SUPABASE_URL + SUPABASE_SERVICE_KEY are set, otherwise local disk only.

The rest of the app keeps using the sqlite-style API (`conn.cursor()`, `?` placeholders,
`row["col"]`), so switching backends needs no query changes.
"""
import os
import re
import sqlite3
import threading
import datetime
import urllib.parse

import requests

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
BUCKET = os.getenv("SUPABASE_BUCKET", "videos").strip() or "videos"

USE_POSTGRES = bool(DATABASE_URL)
USE_REMOTE_FILES = bool(SUPABASE_URL and SUPABASE_KEY)

# --------------------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------------------
_SQLITE_PATH = os.getenv("SQLITE_PATH", "users.db")

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
    import psycopg2.pool

    IntegrityErrors = (psycopg2.IntegrityError, sqlite3.IntegrityError)
    _pool = None
    _pool_lock = threading.Lock()

    def _get_pool():
        global _pool
        with _pool_lock:
            if _pool is None:
                _pool = psycopg2.pool.ThreadedConnectionPool(1, int(os.getenv("DB_POOL_MAX", "8")), DATABASE_URL,
                                                             connect_timeout=10, keepalives=1,
                                                             keepalives_idle=30)
            return _pool

    def _fix_value(v):
        if isinstance(v, (datetime.datetime, datetime.date)):
            return v.strftime("%Y-%m-%d %H:%M:%S") if isinstance(v, datetime.datetime) else v.isoformat()
        return v

    def _translate(sql):
        s = sql.replace("?", "%s")
        s = re.sub(r"INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY", s, flags=re.I)
        s = re.sub(r"ALTER TABLE (\w+) ADD COLUMN (?!IF NOT EXISTS)", r"ALTER TABLE \1 ADD COLUMN IF NOT EXISTS ", s, flags=re.I)
        m = re.match(r"\s*INSERT\s+OR\s+IGNORE\s+INTO", s, flags=re.I)
        if m:
            s = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", s, count=1, flags=re.I).rstrip().rstrip(";")
            s += " ON CONFLICT DO NOTHING"
        return s

    class _Cursor:
        def __init__(self, raw):
            self._c = raw

        def execute(self, sql, params=()):
            self._c.execute(_translate(sql), params)
            return self

        def _row(self, r):
            if r is None:
                return None
            return {k: _fix_value(v) for k, v in r.items()}

        def fetchone(self):
            return self._row(self._c.fetchone())

        def fetchall(self):
            return [self._row(r) for r in self._c.fetchall()]

        @property
        def rowcount(self):
            return self._c.rowcount

    class _Conn:
        def __init__(self):
            self._pool = _get_pool()
            self._raw = self._pool.getconn()
            self._closed = False

        def cursor(self):
            return _Cursor(self._raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor))

        def commit(self):
            self._raw.commit()

        def rollback(self):
            self._raw.rollback()

        def close(self):
            if self._closed:
                return
            self._closed = True
            try:
                self._raw.rollback()  # discard anything uncommitted before the connection is reused
            except Exception:
                pass
            try:
                self._pool.putconn(self._raw)
            except Exception:
                pass

    def get_conn():
        return _Conn()
else:
    IntegrityErrors = (sqlite3.IntegrityError,)

    def get_conn():
        conn = sqlite3.connect(_SQLITE_PATH)
        conn.row_factory = sqlite3.Row
        return conn


# --------------------------------------------------------------------------------------
# File storage (Supabase Storage via its REST API)
# --------------------------------------------------------------------------------------
_headers = {"Authorization": f"Bearer {SUPABASE_KEY}", "apikey": SUPABASE_KEY}
_bucket_ready = False


def _obj_url(name):
    return f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{urllib.parse.quote(name)}"


def _ensure_bucket():
    global _bucket_ready
    if _bucket_ready or not USE_REMOTE_FILES:
        return
    try:
        r = requests.post(f"{SUPABASE_URL}/storage/v1/bucket", headers=_headers,
                          json={"id": BUCKET, "name": BUCKET, "public": False}, timeout=15)
        # 200 = created, 400/409 = already exists
        _bucket_ready = r.status_code in (200, 201, 400, 409)
    except Exception as e:
        print("Storage bucket check failed:", e)


def put_file(name, local_path, content_type="application/octet-stream"):
    """Upload a local file. Returns True on success. Never raises."""
    if not USE_REMOTE_FILES or not os.path.exists(local_path):
        return False
    _ensure_bucket()
    try:
        with open(local_path, "rb") as f:
            r = requests.post(_obj_url(name), data=f, timeout=120,
                              headers={**_headers, "Content-Type": content_type, "x-upsert": "true"})
        if r.status_code not in (200, 201):
            print("Storage upload failed", name, r.status_code, r.text[:200])
            return False
        return True
    except Exception as e:
        print("Storage upload error", name, e)
        return False


def ensure_local(name, local_path):
    """Return local_path if the file is on disk; otherwise pull it from storage first. None if missing."""
    if os.path.exists(local_path):
        return local_path
    if not USE_REMOTE_FILES:
        return None
    try:
        r = requests.get(_obj_url(name), headers=_headers, timeout=120, stream=True)
        if r.status_code != 200:
            return None
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        tmp = local_path + ".part"
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(65536):
                f.write(chunk)
        os.replace(tmp, local_path)
        return local_path
    except Exception as e:
        print("Storage download error", name, e)
        return None


def delete_files(names):
    if not USE_REMOTE_FILES or not names:
        return
    try:
        requests.delete(f"{SUPABASE_URL}/storage/v1/object/{BUCKET}", headers=_headers,
                        json={"prefixes": list(names)}, timeout=30)
    except Exception as e:
        print("Storage delete error", e)
