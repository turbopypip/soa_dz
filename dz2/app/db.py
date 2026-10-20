import os
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row


@contextmanager
def transaction(write=False):
    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as conn:
        if write:
            # One transaction-scoped lock for business writes, across all app replicas.
            # Deliberately simple for a homework service; reads remain concurrent.
            conn.execute("SELECT pg_advisory_xact_lock(20261020)")
        yield conn
