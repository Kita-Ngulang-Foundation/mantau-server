"""Task-owned serialization for repositories sharing one SQLite connection."""
from functools import wraps
from inspect import iscoroutinefunction


def serialized_repository(cls):
    for name, method in list(vars(cls).items()):
        if not iscoroutinefunction(method):
            continue

        def wrap(fn):
            @wraps(fn)
            async def operation(self, *args, **kwargs):
                async with self._db.serialized():
                    return await fn(self, *args, **kwargs)
            return operation

        setattr(cls, name, wrap(method))
    return cls


class TransactionConnection:
    """Repository commits belong to an enclosing atomic operation, when present."""
    def __init__(self, db):
        self.db = db

    def __getattr__(self, name):
        return getattr(self.db._conn, name)

    async def execute(self, sql, parameters=()):
        if self.db.in_atomic and sql.strip().upper().startswith('BEGIN'):
            return await self.db._conn.execute('SELECT 1')
        return await self.db._conn.execute(sql, parameters)

    async def commit(self):
        if not self.db.in_atomic:
            await self.db._conn.commit()

    async def rollback(self):
        if self.db.in_atomic:
            self.db._atomic_failed = True
        await self.db._conn.rollback()
