from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3


class Vault:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.session() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS volumes (id TEXT PRIMARY KEY, manifest TEXT NOT NULL, evidence TEXT NOT NULL)')
            conn.execute('CREATE TABLE IF NOT EXISTS slips (id TEXT PRIMARY KEY REFERENCES volumes(id), digest TEXT NOT NULL, body TEXT NOT NULL)')
            conn.execute('CREATE TABLE IF NOT EXISTS commitments (run TEXT NOT NULL REFERENCES volumes(id), pool TEXT NOT NULL, '
                         'gross INTEGER NOT NULL, net INTEGER NOT NULL, PRIMARY KEY (run, pool))')
            conn.execute('CREATE TABLE IF NOT EXISTS releases (run TEXT NOT NULL REFERENCES volumes(id), entry TEXT NOT NULL, '
                         "line INTEGER NOT NULL, received_year TEXT, target_run TEXT, target TEXT NOT NULL, org TEXT, year TEXT, "
                         'pool TEXT, pages INTEGER NOT NULL, reason TEXT, PRIMARY KEY (run, entry))')
            conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS release_target_once ON releases(target) WHERE reason IS NULL')

    @contextmanager
    def session(self):
        with closing(sqlite3.connect(self.path,timeout=10)) as conn:
            conn.execute('PRAGMA foreign_keys=ON')
            with conn:
                yield conn

    @contextmanager
    def writing(self):
        """Serialize writers so concurrent imprints cannot oversubscribe one yearly pool."""
        with closing(sqlite3.connect(self.path,timeout=30)) as conn:
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('PRAGMA busy_timeout=30000')
            conn.execute('BEGIN IMMEDIATE')
            try:
                yield conn
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()
