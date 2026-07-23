"""Database layer — SQLite locally, Postgres (Neon/Supabase/any) when DATABASE_URL is set.

Both engines are accessed through the same tiny wrapper so server.py can keep
using  c.execute(sql, params).fetchone() / .fetchall()  with '?' placeholders
and dict-like rows.
"""
import os, sqlite3, ssl
from urllib.parse import urlparse, unquote, parse_qs

DATABASE_URL = (
    os.environ.get('DATABASE_URL')
    or os.environ.get('POSTGRES_URL')
    or ''
).strip()

IS_PG = DATABASE_URL.startswith(('postgres://', 'postgresql://'))

SQLITE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'timetracker.db')


def db_configured():
    """True when a persistent database is available (Postgres in the cloud,
    or SQLite when running locally outside Vercel)."""
    if IS_PG:
        return True
    return not os.environ.get('VERCEL')  # SQLite is fine locally, not on Vercel


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class Db:
    def __init__(self):
        if IS_PG:
            self.conn = _pg_connect()
        else:
            self.conn = sqlite3.connect(SQLITE_PATH)

    def execute(self, sql, params=()):
        if IS_PG:
            cur = self.conn.cursor()
            cur.execute(sql.replace('?', '%s'), tuple(params))
        else:
            cur = self.conn.execute(sql, tuple(params))
        rows = []
        if cur.description:
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return _Result(rows)

    def commit(self):
        self.conn.commit()

    def rollback(self):
        try:
            self.conn.rollback()
        except Exception:
            pass

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


def _pg_connect():
    import pg8000.dbapi as pgdb
    u = urlparse(DATABASE_URL)
    q = parse_qs(u.query or '')
    sslmode = (q.get('sslmode') or ['require'])[0]
    ctx = None
    if sslmode != 'disable':
        ctx = ssl.create_default_context()
    return pgdb.connect(
        user=unquote(u.username or 'postgres'),
        password=unquote(u.password or ''),
        host=u.hostname,
        port=u.port or 5432,
        database=(u.path or '').lstrip('/') or 'postgres',
        ssl_context=ctx,
        timeout=15,
    )
