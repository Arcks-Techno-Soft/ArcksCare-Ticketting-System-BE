"""SQLAlchemy engine, session factory, and Base declarative class."""
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool

from .config import get_settings

settings = get_settings()

_is_sqlite = settings.database_url.startswith("sqlite")
# Supabase exposes two poolers:
#   * port 5432 = session mode — one client connection per Postgres backend,
#     capped at ~15 clients per project. Easy to exhaust with restarts.
#   * port 6543 = transaction mode — pgbouncer-style multiplexing, supports
#     hundreds of clients but does NOT support server-side prepared
#     statements. psycopg's "prepare_threshold=None" disables them.
# Either pooler can also be opted into via "?pgbouncer=true" on the URL.
_url_lower = settings.database_url.lower()
_is_tx_pooler = (
    ":6543" in _url_lower or "pgbouncer=true" in _url_lower
)

if _is_sqlite:
    connect_args = {"check_same_thread": False}
    engine_kwargs = {}
else:
    # Postgres (incl. Supabase): enable TCP keepalives so dead sockets are
    # detected quickly, and recycle connections every 5 min so we never reuse
    # one that the server / pooler has silently dropped.
    connect_args = {
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 3,
    }
    if _is_tx_pooler:
        # Transaction-mode pooler rebinds connections between statements, so
        # server-side prepared-statement caches are invalid. psycopg respects
        # prepare_threshold=None as "never prepare".
        connect_args["prepare_threshold"] = None
        # Recommended pattern when going through pgbouncer-style poolers:
        # let the pooler handle pooling. NullPool opens a fresh connection
        # per checkout and closes it on return — cheap because the real
        # work happens between our app and pgbouncer, which already keeps
        # a large internal pool. This avoids client-side QueuePool exhaustion
        # under bursts of concurrent requests (the detail page can fan out
        # 7+ parallel calls).
        engine_kwargs = {"poolclass": NullPool}
    else:
        # Direct Postgres (session-mode pooler or self-hosted): keep a small
        # client-side pool. These knobs are env-tunable.
        engine_kwargs = {
            "pool_size": settings.db_pool_size,
            "max_overflow": settings.db_max_overflow,
            "pool_recycle": settings.db_pool_recycle_seconds,
        }

engine = create_engine(
    settings.database_url,
    connect_args=connect_args,
    pool_pre_ping=True,
    future=True,
    **engine_kwargs,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)


class Base(DeclarativeBase):
    """Base class for all ORM models."""


def get_db():
    """FastAPI dependency that yields a DB session and closes it after the request.

    Rolling back on exception is critical: if the handler raised mid-transaction
    (e.g. a network drop on Postgres), a bare close() would leave the connection
    in an ACTIVE transaction state and the next pool_pre_ping would fail with
    "can't change 'autocommit' now: connection in transaction status ACTIVE".
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
