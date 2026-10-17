"""db.py -- small MySQL helpers for the Blood Bank Management System.

This module owns EVERY database connection. It uses mysql-connector-python
directly (no ORM) so the SQL stays visible and easy to explain.

Configuration is read from environment variables. A local `.env` file is
loaded automatically if present (see `_load_dotenv`). Copy `.env.example`
to `.env` and fill in your credentials.

Environment variables (values are NEVER hard-coded here):
    DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME   -- connection details
    DB_SSL_CA            -- path to a CA certificate (e.g. Aiven ca.pem).
                            When set, the connection uses verified TLS, which
                            hosted MySQL such as Aiven requires.
    DB_SSL_CA_B64        -- base64 of the CA certificate, for hosts such as
                            Vercel that provide env vars but no mounted file.
    DB_SSL_VERIFY_IDENTITY -- optional, "1" to also verify the server hostname.
    DB_SSL_DISABLED      -- optional, "1" to force-disable TLS (local MySQL).

Connection reuse
----------------
Opening a TCP + TLS + MySQL handshake to a remote database is expensive
(measured at ~2.6s per connection from Vercel to Aiven), so this module keeps
ONE connection per thread and reuses it instead of connecting once per query.
Serverless platforms (Vercel Fluid compute) and Gunicorn both call a warm
process many times, so reuse removes the handshake from nearly every request.
Before a cached connection is handed out, `get_connection()` proves it is
still open with an explicit `conn.ping(reconnect=False)` -- a real COM_PING
round trip (never a silent auto-reconnect). When the server has closed the
connection (idle timeout, restart, network drop), the dead object is
discarded and replaced by a fresh handshake with the SAME configuration, so
verified TLS survives recovery. Recovery only happens in `get_connection()`,
i.e. before a transaction starts: a connection lost DURING a transaction
surfaces as an error so the whole transaction fails as a unit, instead of
quietly continuing on a new session with no open transaction. The cache can
be dropped at any time: the next call simply opens a fresh connection.

Locally nothing changes: with DB_SSL_CA unset the module connects exactly as
before. On Vercel, set DB_SSL_CA_B64 (or DB_SSL_CA) to use verified TLS.
"""

import base64
import os
import threading
from contextlib import contextmanager

import mysql.connector
from mysql.connector import Error, InterfaceError


BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv(path=None):
    """Minimal .env loader (no third-party dependency).

    Reads simple KEY=VALUE lines and sets them in os.environ unless the
    variable is already defined. Lines starting with '#' are ignored.
    """
    path = path or os.path.join(BASE_DIR, ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()


def _env_flag(name, default=False):
    """Return True when an env var is set to a common truthy value."""
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _build_db_config():
    """Build the mysql-connector connection options from environment variables.

    TLS is enabled only when DB_SSL_CA (or DB_SSL_CA_B64) is set, so local
    development is unaffected. Set DB_SSL_DISABLED=1 to explicitly turn TLS
    off (for example, a local MySQL server without TLS support).
    """
    config = {
        "host": os.environ.get("DB_HOST", "127.0.0.1"),
        "port": int(os.environ.get("DB_PORT", "3306")),
        "user": os.environ.get("DB_USER", "bloodbank_user"),
        "password": os.environ.get("DB_PASSWORD", ""),
        "database": os.environ.get("DB_NAME", "blood_bank_db"),
        # autocommit keeps each statement self-contained on a long-lived
        # connection, so a reused connection never serves a stale REPEATABLE
        # READ snapshot. Multi-statement atomic work uses start_transaction()
        # (see transaction() below), which works fine with autocommit on.
        "autocommit": True,
        "use_pure": True,
        # Never wait forever on a remote database. connection_timeout bounds
        # the TCP/TLS/auth handshake and read/write_timeout bounds every
        # subsequent read and write, so an unreachable or black-holed Aiven
        # endpoint surfaces as a quick error instead of a hung page.
        "connection_timeout": 10,
        "read_timeout": 30,
        "write_timeout": 30,
    }

    ssl_ca = (os.environ.get("DB_SSL_CA") or "").strip()
    ssl_ca_b64 = (os.environ.get("DB_SSL_CA_B64") or "").strip()
    if ssl_ca_b64:
        ca_path = "/tmp/aiven-ca.pem"
        with open(ca_path, "wb") as ca_file:
            ca_file.write(base64.b64decode(ssl_ca_b64, validate=True))
        ssl_ca = ca_path
    if ssl_ca:
        config["ssl_ca"] = ssl_ca
        config["ssl_verify_cert"] = True
        if _env_flag("DB_SSL_VERIFY_IDENTITY"):
            config["ssl_verify_identity"] = True
    elif _env_flag("DB_SSL_DISABLED"):
        config["ssl_disabled"] = True

    return config


DB_CONFIG = _build_db_config()


# Thread-local cache: Gunicorn threads and concurrent serverless workers must
# never share a single MySQL session (it is not safe to use one connection from
# two threads at once).
_local = threading.local()


def _close_quietly(conn):
    """Close a connection, ignoring errors raised while tearing it down."""
    try:
        conn.close()
    except Error:
        pass


def _forget(conn):
    """Drop `conn` from this thread's cache if (and only if) it is cached."""
    if getattr(_local, "conn", None) is conn:
        _local.conn = None


def _discard_connection(conn):
    """Close and forget a connection found to be unusable.

    Called when a cursor could not even be created, which on a pooled
    connection means the connection itself is suspect. Refuses to close
    while a transaction() block is open on this thread: killing the session
    mid-transaction would silently abandon it, so the error is surfaced
    instead and transaction()'s own cleanup handles the rest.
    """
    if getattr(_local, "tx_depth", 0) > 0:
        return
    _forget(conn)
    _close_quietly(conn)


def _reconnect(conn):
    """Re-establish a discarded connection, or build a brand-new one.

    `conn.reconnect()` re-runs the full handshake (TCP + TLS + auth) with
    the configuration the connection was opened with, so verified TLS is
    preserved. A connector may be left unusable if `reconnect()` itself
    fails, so in that case we fall back to a fresh `connect()`. Either way
    the caller only ever gets a connection that has just completed a
    successful handshake.
    """
    try:
        conn.reconnect()
        return conn
    except Error:
        return mysql.connector.connect(**DB_CONFIG)


def get_connection():
    """Return a live MySQL connection, reusing this thread's warm one.

    The returned connection is OWNED BY THIS MODULE and is reused by later
    calls, so callers must NOT close it.

    Stale-connection recovery: `ping(reconnect=False)` is a real COM_PING
    round trip, so a connection the server has already closed (idle timeout,
    restart, network drop) is detected BEFORE it is used, even though the
    local socket object still looks fine. `reconnect=False` is deliberate:
    it makes ping raise instead of re-establishing the session behind our
    back, which would silently end an open transaction. The dead connection
    is discarded and replaced here, at a safe point (before any transaction
    of this call has started). If the connection dies while a transaction()
    block is open on this thread, this function refuses to reconnect and
    raises instead, so the transaction fails atomically rather than
    continuing on a fresh session with no open transaction.
    """
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.ping(reconnect=False)
            return conn
        except Error:
            # The server has closed this connection; treat it as gone.
            pass
        _forget(conn)
        _close_quietly(conn)
        if getattr(_local, "tx_depth", 0) > 0:
            # Inside an active transaction(): reconnecting now would make
            # the rest of the block run on a DIFFERENT session with no open
            # transaction, breaking atomicity. Fail loudly instead; the
            # server rolls back an unfinished transaction when its session
            # dies, so nothing is half-committed.
            raise InterfaceError(
                "MySQL connection was lost during an active transaction; "
                "aborting instead of silently reconnecting."
            )
        conn = _reconnect(conn)
    else:
        # Cold cache for this thread: open a brand-new connection.
        conn = mysql.connector.connect(**DB_CONFIG)

    _local.conn = conn
    return conn


def close_connection():
    """Close and forget this thread's cached connection, if any.

    Not required during normal request handling (the connection is meant to be
    reused); it is here for tests and clean shutdown.
    """
    conn = getattr(_local, "conn", None)
    if conn is not None:
        _close_quietly(conn)
        _local.conn = None


@contextmanager
def get_cursor(dictionary=True, commit=False):
    """Context manager that yields a cursor and always closes the CURSOR.

    The underlying connection is reused (see ``get_connection``) and is
    therefore NOT closed here. Connections run with autocommit, so single
    statements are durable immediately; ``commit=True`` is kept for call-site
    compatibility and a failed statement always triggers a rollback.

    Cursor creation happens INSIDE the cleanup logic: if it fails (the
    classic symptom of a connection that died between checks), the cached
    connection is dropped, so the next call starts from a clean connection
    instead of tripping over the same broken one.

    Usage:
        with get_cursor() as cur:                 # read-only query
            cur.execute("SELECT ...")

        with get_cursor(commit=True) as cur:      # write operation
            cur.execute("INSERT ...")
    """
    conn = get_connection()
    cur = None
    try:
        cur = conn.cursor(dictionary=dictionary)
        yield cur
        if commit:
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Error:
            pass
        raise
    finally:
        if cur is None:
            _discard_connection(conn)
        else:
            try:
                cur.close()
            except Error:
                pass


def fetch_all(sql, params=None):
    """Run a SELECT and return every row as a list of dicts."""
    with get_cursor() as cur:
        cur.execute(sql, params or ())
        return cur.fetchall()


def fetch_one(sql, params=None):
    """Run a SELECT and return the first row as a dict, or None."""
    with get_cursor() as cur:
        cur.execute(sql, params or ())
        return cur.fetchone()


def execute(sql, params=None):
    """Run a single INSERT/UPDATE/DELETE and return the last inserted id.

    Used for simple single-statement writes. For multi-step, atomic
    operations use `transaction()` instead.
    """
    with get_cursor(commit=True) as cur:
        cur.execute(sql, params or ())
        return cur.lastrowid


@contextmanager
def transaction(dictionary=True):
    """Context manager for a multi-statement, atomic transaction.

    Commits on success and rolls back on ANY exception, so a group of
    statements succeeds or fails together. Works with the autocommit
    connections used by this module: ``start_transaction()`` opens an
    explicit transaction for the duration of the block, which suspends
    autocommit until commit()/rollback() ends it -- so a reused connection
    never leaks a stale REPEATABLE READ snapshot into later statements.
    Use this for the donation and request-fulfilment workflows.

    While the block is open this thread will never transparently reconnect
    (see ``get_connection``): a connection lost mid-transaction surfaces as
    an error and the transaction aborts as a unit, instead of silently
    continuing on a fresh session with no open transaction.

    Cursor creation happens INSIDE the cleanup logic, so a failure there
    closes the cursor (if any was created) and drops the suspect cached
    connection.
    """
    conn = get_connection()
    cur = None
    # Mark this thread as "inside a transaction" before anything else, so
    # get_connection()/_discard_connection() can refuse to reconnect or
    # discard the session while the block is open.
    _local.tx_depth = getattr(_local, "tx_depth", 0) + 1
    try:
        cur = conn.cursor(dictionary=dictionary)
        conn.start_transaction()
        yield cur
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Error:
            pass
        raise
    finally:
        _local.tx_depth -= 1
        if cur is None:
            _discard_connection(conn)
        else:
            try:
                cur.close()
            except Error:
                pass
