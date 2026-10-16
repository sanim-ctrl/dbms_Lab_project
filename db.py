import os
import base64
from contextlib import contextmanager

import mysql.connector
from mysql.connector import Error
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
    DB_SSL_VERIFY_IDENTITY -- optional, "1" to also verify the server hostname.
    DB_SSL_DISABLED      -- optional, "1" to force-disable TLS (local MySQL).

Locally nothing changes: with DB_SSL_CA unset the module connects exactly as
before. On Render, point DB_SSL_CA at the mounted Aiven CA certificate.
"""

import os
from contextlib import contextmanager

import mysql.connector
from mysql.connector import Error


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

    TLS is enabled only when DB_SSL_CA points at a CA certificate, so local
    development is unaffected. Set DB_SSL_DISABLED=1 to explicitly turn TLS
    off (for example, a local MySQL server without TLS support).
    """
    config = {
        "host": os.environ.get("DB_HOST", "127.0.0.1"),
        "port": int(os.environ.get("DB_PORT", "3306")),
        "user": os.environ.get("DB_USER", "bloodbank_user"),
        "password": os.environ.get("DB_PASSWORD", ""),
        "database": os.environ.get("DB_NAME", "blood_bank_db"),
        "autocommit": False,
        "use_pure": True,
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


def get_connection():
    """Open a new connection to MySQL. Caller is responsible for closing it."""
    return mysql.connector.connect(**DB_CONFIG)


@contextmanager
def get_cursor(dictionary=True, commit=False):
    """Context manager that yields a cursor and always cleans up.

    - Commits when `commit=True` and no exception happened.
    - Rolls back automatically on any exception.
    - Always closes the cursor and the connection.

    Usage:
        with get_cursor() as cur:                 # read-only query
            cur.execute("SELECT ...")

        with get_cursor(commit=True) as cur:      # write operation
            cur.execute("INSERT ...")
    """
    conn = None
    cur = None
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=dictionary)
        yield cur
        if commit:
            conn.commit()
    except Error:
        if conn is not None:
            conn.rollback()
        raise
    except Exception:
        if conn is not None:
            conn.rollback()
        raise
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()


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
    statements succeeds or fails together. Use this for the donation and
    request-fulfilment workflows.

    Usage:
        with transaction() as cur:
            cur.execute(...)   # step 1
            cur.execute(...)   # step 2
    """
    conn = None
    cur = None
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=dictionary)
        yield cur
        conn.commit()
    except Exception:
        if conn is not None:
            conn.rollback()
        raise
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()
