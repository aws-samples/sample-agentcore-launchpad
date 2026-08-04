from sqlalchemy.pool import NullPool

from app.core.db import engine


def test_file_sqlite_does_not_use_a_fixed_connection_pool():
    assert engine.url.get_backend_name() == "sqlite"
    assert isinstance(engine.pool, NullPool)
