"""SQLite transactions used by short-lived library and catalogue operations."""
import sqlite3


class ClosingConnection(sqlite3.Connection):
    """Commit/rollback and close on context exit (sqlite3 alone leaves it open)."""

    def __exit__(self, *exc):
        try:
            return super().__exit__(*exc)
        finally:
            self.close()
