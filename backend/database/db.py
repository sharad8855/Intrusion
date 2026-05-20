"""Database engine + session factory."""
from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.core.config import CONFIG
from backend.database.models import Base

_url = CONFIG.get_path("system.database_url", "sqlite:///./intrusion.db")

# check_same_thread=False: camera worker threads share the SQLite engine.
_connect_args = {"check_same_thread": False} if _url.startswith("sqlite") else {}
engine = create_engine(_url, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db() -> None:
    Base.metadata.create_all(engine)
    _migrate_sqlite()


def _migrate_sqlite() -> None:
    """
    Add columns introduced after an existing database was first created.

    SQLAlchemy's create_all() only creates missing *tables*, not missing
    *columns* — so an old intrusion.db keeps working without being wiped.
    """
    if not _url.startswith("sqlite"):
        return
    wanted = {"cameras": {
        "url": "VARCHAR(400)",
        "port": "VARCHAR(10)",
        "latitude": "FLOAT",
        "longitude": "FLOAT",
    }}
    with engine.begin() as conn:
        for table, cols in wanted.items():
            existing = {row[1] for row in
                        conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            for col, decl in cols.items():
                if col not in existing:
                    conn.exec_driver_sql(
                        f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                    print(f"[db] migrated: added {table}.{col}")

        # ── Backfill the new `url` column for cameras created before the
        #    schema change (their address still lives in rtsp_url / ip). ──
        cam_cols = {row[1] for row in
                    conn.exec_driver_sql("PRAGMA table_info(cameras)")}
        if "url" in cam_cols and "rtsp_url" in cam_cols:
            res = conn.exec_driver_sql(
                "UPDATE cameras SET url = TRIM(rtsp_url) "
                "WHERE (url IS NULL OR TRIM(url) = '') "
                "AND rtsp_url IS NOT NULL AND TRIM(rtsp_url) <> ''")
            if res.rowcount:
                print(f"[db] migrated: filled url from rtsp_url for "
                      f"{res.rowcount} camera(s)")
        if "url" in cam_cols and "ip" in cam_cols:
            res = conn.exec_driver_sql(
                "UPDATE cameras SET url = TRIM(ip) "
                "WHERE (url IS NULL OR TRIM(url) = '') "
                "AND ip IS NOT NULL AND TRIM(ip) <> ''")
            if res.rowcount:
                print(f"[db] migrated: filled url from ip for "
                      f"{res.rowcount} camera(s)")


@contextmanager
def session_scope():
    """Transactional session context manager."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
