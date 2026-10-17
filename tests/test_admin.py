#!/usr/bin/env python3
"""tests/test_admin.py -- tests for the password-protected admin approval workflow.

Covers:
  * unauthenticated access denial (page + approve/reject endpoints)
  * CSRF protection on login, logout, approve and reject
  * admin login with a wrong password and with the configured password
  * brute-force rate limiting
  * session invalidation on logout / forged cookie rejection
  * approving and rejecting pending proposals
  * duplicate / repeated decisions and invalid proposal ids
  * transaction rollback when a decision fails
  * rejected proposals leaving live records untouched
  * missing/weak admin configuration failing safely without leaking secrets

SAFETY
------
Uses a TEST-ONLY admin password set in this process's environment -- never the
real one. It never drops tables and only creates a few clearly-marked pending
blood requests, which it deletes again (with their audit rows) in `finally`.

Usage
-----
    python tests/test_admin.py

Exit codes: 0 = all passed, 1 = failures, 2 = could not connect.

Environment overrides (DB connection) come from .env exactly like test_db.py.
"""

import os
import sys

# Make the project root and this directory importable no matter where the
# script is run from (test_db.py provides the shared test harness).
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, TESTS_DIR)

from werkzeug.security import generate_password_hash  # noqa: E402

# --------------------------------------------------------------------------- #
# Test-only admin configuration. These are NOT the real credentials; they are
# set before app/db are imported so the application sees a configured admin.
# --------------------------------------------------------------------------- #
TEST_ADMIN_PASSWORD = "test-only-admin-password-1234567890"
STRONG_SECRET_KEY = "test-only-session-signing-key-0123456789abcdef"

os.environ["SECRET_KEY"] = STRONG_SECRET_KEY
os.environ["ADMIN_PASSWORD_HASH"] = generate_password_hash(TEST_ADMIN_PASSWORD)
os.environ.pop("ADMIN_PASSWORD", None)
os.environ["ADMIN_LOGIN_MAX_ATTEMPTS"] = "5"
os.environ["ADMIN_LOGIN_WINDOW_SECONDS"] = "300"

import db  # noqa: E402
import app as app_module  # noqa: E402
from app import app  # noqa: E402
from mysql.connector import Error  # noqa: E402
from test_db import Results, csrf_token, post, scalar  # noqa: E402

# A public page that always renders a CSRF-protected form, even when the admin
# area itself is misconfigured or the client is already logged in.
TOKEN_PAGE = "/requests"
PENDING_DATE = "2026-08-01"

# Set in main(): whether Admin_Audit_Log is writable in this environment. When
# it is not (a MySQL user without CREATE/INSERT on the audit table), decisions
# still work and the audit-specific assertions are reported as skipped instead
# of failing.
AUDIT_OK = False
SKIPPED = []


def skip(name, reason):
    SKIPPED.append((name, reason))
    print(f"  [SKIP] {name} ({reason})")


class _Rollback(Exception):
    """Sentinel thrown to force a transaction rollback in probes."""


def audit_log_available():
    """True when a decision row can actually be written to Admin_Audit_Log."""
    app_module.ensure_admin_tables()
    try:
        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO Admin_Audit_Log (Proposal_Type, Proposal_ID, Action) "
                "VALUES ('Blood_Request', 0, 'Approved')",
            )
            raise _Rollback
    except _Rollback:
        return True
    except Error:
        return False


def audit_count(proposal_id, action=None):
    sql = ("SELECT COUNT(*) AS n FROM Admin_Audit_Log "
           "WHERE Proposal_Type='Blood_Request' AND Proposal_ID=%s")
    params = [proposal_id]
    if action is not None:
        sql += " AND Action=%s"
        params.append(action)
    return scalar(sql, tuple(params))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def get_token(client):
    return csrf_token(client, TOKEN_PAGE)


def try_login(client, password):
    """POST the admin login form with a valid session CSRF token."""
    return post(client, "/admin/login", csrf_token=get_token(client), password=password)


def admin_decision(client, proposal_id, action):
    """POST an approve/reject decision as an authenticated admin."""
    return post(client, f"/admin/proposals/{proposal_id}/{action}", csrf_token=get_token(client))


def make_pending_request(client, hospital_id, bg_id, qty, when):
    """Create a Pending blood request through the existing public route."""
    post(client, "/requests", csrf_token=get_token(client), hospital_id=hospital_id,
         bg_id=bg_id, date=when, qty=qty)
    return db.fetch_one(
        "SELECT Request_ID, Status FROM Blood_Request "
        "WHERE Hospital_ID=%s AND Date=%s ORDER BY Request_ID DESC LIMIT 1",
        (hospital_id, when),
    )


# --------------------------------------------------------------------------- #
# Test groups
# --------------------------------------------------------------------------- #
def run_configuration_tests(results):
    """Missing/broken admin configuration must fail safely (no secrets shown)."""
    print("\n== A. Configuration safety ==")
    client = app.test_client()
    saved_hash = os.environ.pop("ADMIN_PASSWORD_HASH")
    saved_plain = os.environ.pop("ADMIN_PASSWORD", None)
    try:
        r = client.get("/admin/login")
        html = r.get_data(as_text=True)
        results.check("missing ADMIN_PASSWORD(_HASH) -> 503", r.status_code == 503,
                      f"got {r.status_code}")
        results.check("config error mentions the variable name",
                      "ADMIN_PASSWORD_HASH" in html)
        results.check("config error does not leak a password/hash",
                      TEST_ADMIN_PASSWORD not in html and saved_hash not in html)

        r = try_login(client, TEST_ADMIN_PASSWORD)
        results.check("login with missing config -> 503", r.status_code == 503,
                      f"got {r.status_code}")
        results.check("no admin session created without config",
                      client.get("/admin").status_code == 503)
    finally:
        os.environ["ADMIN_PASSWORD_HASH"] = saved_hash
        if saved_plain is not None:
            os.environ["ADMIN_PASSWORD"] = saved_plain

    # Missing SECRET_KEY is refused even when the password is configured.
    saved_secret = os.environ.pop("SECRET_KEY", None)
    try:
        r = client.get("/admin/login")
        html = r.get_data(as_text=True)
        results.check("missing SECRET_KEY -> 503", r.status_code == 503, f"got {r.status_code}")
        results.check("secret error mentions SECRET_KEY", "SECRET_KEY" in html)
    finally:
        if saved_secret is not None:
            os.environ["SECRET_KEY"] = saved_secret

    os.environ["SECRET_KEY"] = "too-short"
    try:
        r = client.get("/admin")
        results.check("weak SECRET_KEY -> 503", r.status_code == 503, f"got {r.status_code}")
    finally:
        os.environ["SECRET_KEY"] = STRONG_SECRET_KEY

    results.check("restored configuration is valid",
                  app_module.admin_config_problem() is None)

    # Plain-text fallback (ADMIN_PASSWORD) when no hash is configured.
    saved_hash = os.environ.pop("ADMIN_PASSWORD_HASH")
    os.environ["ADMIN_PASSWORD"] = TEST_ADMIN_PASSWORD
    try:
        r = try_login(app.test_client(), TEST_ADMIN_PASSWORD)
        results.check("ADMIN_PASSWORD fallback authenticates", r.status_code == 302,
                      f"got {r.status_code}")
        r = try_login(app.test_client(), "the-wrong-password")
        results.check("ADMIN_PASSWORD fallback rejects a wrong password",
                      r.status_code == 401, f"got {r.status_code}")
    finally:
        os.environ["ADMIN_PASSWORD_HASH"] = saved_hash
        os.environ.pop("ADMIN_PASSWORD", None)

    # A malformed hash must deny access safely (never a 500, never a leak).
    saved_hash = os.environ["ADMIN_PASSWORD_HASH"]
    os.environ["ADMIN_PASSWORD_HASH"] = "this-is-not-a-valid-password-hash"
    try:
        r = try_login(app.test_client(), TEST_ADMIN_PASSWORD)
        results.check("malformed ADMIN_PASSWORD_HASH denies safely (401)",
                      r.status_code == 401, f"got {r.status_code}")
        results.check("malformed hash is not echoed to the browser",
                      "this-is-not-a-valid-password-hash" not in r.get_data(as_text=True))
    finally:
        os.environ["ADMIN_PASSWORD_HASH"] = saved_hash

    app_module.reset_login_attempts()
    results.check("configuration fully restored",
                  app_module.admin_config_problem() is None)


def run_access_tests(results, client):
    """Unauthenticated users and CSRF must not be able to see or change data."""
    print("\n== B. Access control & CSRF ==")

    r = client.get("/admin")
    results.check("unauthenticated GET /admin redirects to login",
                  r.status_code == 302 and r.headers.get("Location", "").endswith("/admin/login"),
                  f"got {r.status_code} {r.headers.get('Location')}")

    token = get_token(client)
    r = post(client, "/admin/proposals/1/approve", csrf_token=token)
    results.check("unauthenticated approve -> 401", r.status_code == 401, f"got {r.status_code}")
    r = post(client, "/admin/proposals/1/reject", csrf_token=token)
    results.check("unauthenticated reject -> 401", r.status_code == 401, f"got {r.status_code}")

    # CSRF: POSTs without the session token are refused before any handler runs.
    r = client.post("/admin/login", data={"password": TEST_ADMIN_PASSWORD})
    results.check("login POST without CSRF token -> 400", r.status_code == 400, f"got {r.status_code}")
    r = client.post("/admin/logout", data={})
    results.check("logout POST without CSRF token -> 400", r.status_code == 400, f"got {r.status_code}")
    r = client.post("/admin/proposals/1/approve", data={})
    results.check("approve POST without CSRF token -> 400", r.status_code == 400, f"got {r.status_code}")

    results.check("no admin session was created by the failed requests",
                  client.get("/admin").status_code == 302)


def run_login_tests(results, client):
    """Wrong password denied; correct password authenticates; rate limiting works."""
    print("\n== C. Admin login ==")

    results.check("not authenticated yet", client.get("/admin").status_code == 302)

    r = try_login(client, "definitely-the-wrong-password")
    results.check("wrong password -> 401", r.status_code == 401, f"got {r.status_code}")
    results.check("wrong password: no admin session",
                  client.get("/admin").status_code == 302)
    html = r.get_data(as_text=True)
    results.check("failed login page leaks neither password nor hash",
                  TEST_ADMIN_PASSWORD not in html
                  and os.environ["ADMIN_PASSWORD_HASH"] not in html)

    app_module.reset_login_attempts()

    r = try_login(client, TEST_ADMIN_PASSWORD)
    results.check("correct password -> redirect", r.status_code == 302, f"got {r.status_code}")

    r = client.get("/admin")
    results.check("authenticated GET /admin -> 200", r.status_code == 200, f"got {r.status_code}")
    html = r.get_data(as_text=True)
    results.check("admin page never exposes password or hash",
                  TEST_ADMIN_PASSWORD not in html
                  and os.environ["ADMIN_PASSWORD_HASH"] not in html)
    cookie = client.get_cookie("session")
    cookie_value = cookie.value if cookie else ""
    results.check("session cookie contains neither password nor hash",
                  TEST_ADMIN_PASSWORD not in cookie_value
                  and os.environ["ADMIN_PASSWORD_HASH"] not in cookie_value)

    # A forged plaintext session cookie must not be accepted (the real session
    # is signed with SECRET_KEY; this value is not a valid signature).
    forged = app.test_client()
    forged.set_cookie("session", "eyJhZG1pbl9sb2dnZWRfaW4iOnRydWV9")
    results.check("forged session cookie does not grant admin",
                  forged.get("/admin").status_code == 302,
                  f"got {forged.get('/admin').status_code}")

    # State-changing endpoints are POST-only.
    r = client.get("/admin/proposals/1/approve")
    results.check("GET on approve endpoint -> 405", r.status_code == 405, f"got {r.status_code}")
    r = client.get("/admin/logout")
    results.check("GET on logout endpoint -> 405", r.status_code == 405, f"got {r.status_code}")

    # Brute-force protection (fresh client so no pre-existing session).
    app_module.reset_login_attempts()
    limited = app.test_client()
    for _ in range(app_module.LOGIN_MAX_ATTEMPTS):
        r = try_login(limited, "wrong-again")
    results.check("all failed attempts within the limit were refused",
                  r.status_code == 401, f"got {r.status_code}")
    r = try_login(limited, TEST_ADMIN_PASSWORD)
    results.check("rate limit blocks even a correct password -> 429",
                  r.status_code == 429, f"got {r.status_code}")
    results.check("rate-limited client has no session",
                  limited.get("/admin").status_code == 302)
    app_module.reset_login_attempts()
    results.check("rate limiter reset restores login",
                  try_login(limited, TEST_ADMIN_PASSWORD).status_code == 302)


def run_decision_tests(results, client, proposal_ids):
    """Approve, reject, duplicate decisions, invalid ids and live-data safety."""
    print("\n== D. Approve / reject pending changes ==")

    pid_approve = proposal_ids["approve"]
    pid_reject = proposal_ids["reject"]
    pid_rollback = proposal_ids["rollback"]

    # --- the pending page shows the proposals, with all required detail ---
    html = client.get("/admin").get_data(as_text=True)
    results.check("pending page lists the new proposals",
                  f"#{pid_approve}" in html and f"#{pid_reject}" in html)
    results.check("pending page shows record type / action / details / date",
                  "Blood Request" in html and "Approve blood request" in html
                  and "units of" in html and PENDING_DATE in html)

    # --- approve ---
    r = admin_decision(client, pid_approve, "approve")
    results.check("approve redirects (PRG)", r.status_code == 302, f"got {r.status_code}")
    results.check("proposal approved in database",
                  scalar("SELECT Status FROM Blood_Request WHERE Request_ID=%s",
                         (pid_approve,)) == "Approved")
    if AUDIT_OK:
        results.check("approval recorded in audit log",
                      audit_count(pid_approve, "Approved") == 1)
    else:
        skip("approval recorded in audit log", "audit table unavailable in this environment")

    # --- repeated decision on an approved proposal: no double processing ---
    r = admin_decision(client, pid_approve, "approve")
    results.check("repeated approve still redirects (no 500)", r.status_code == 302,
                  f"got {r.status_code}")
    if AUDIT_OK:
        results.check("repeated approve does not create a second audit row",
                      audit_count(pid_approve) == 1)
    else:
        skip("repeated approve does not create a second audit row", "audit table unavailable")
    results.check("repeated approve leaves status Approved",
                  scalar("SELECT Status FROM Blood_Request WHERE Request_ID=%s",
                         (pid_approve,)) == "Approved")
    html = client.get("/admin").get_data(as_text=True)
    results.check("already-decided message shown",
                  "already been processed" in html)

    # --- reject, and prove live/historical data is untouched ---
    stock_before = {r["Bg_ID"]: str(r["Total_Qty"]) for r in
                    db.fetch_all("SELECT Bg_ID, Total_Qty FROM Blood_Stock")}
    donors_before = scalar("SELECT COUNT(*) AS n FROM Donor")
    hospitals_before = scalar("SELECT COUNT(*) AS n FROM Hospital")
    donations_before = scalar("SELECT COUNT(*) AS n FROM Donation")
    statuses_before = {r["Request_ID"]: r["Status"] for r in
                       db.fetch_all("SELECT Request_ID, Status FROM Blood_Request")}

    r = admin_decision(client, pid_reject, "reject")
    results.check("reject redirects (PRG)", r.status_code == 302, f"got {r.status_code}")
    results.check("proposal rejected in database",
                  scalar("SELECT Status FROM Blood_Request WHERE Request_ID=%s",
                         (pid_reject,)) == "Rejected")
    if AUDIT_OK:
        results.check("rejection recorded in audit log", audit_count(pid_reject, "Rejected") == 1)
    else:
        skip("rejection recorded in audit log", "audit table unavailable")

    stock_after = {r["Bg_ID"]: str(r["Total_Qty"]) for r in
                   db.fetch_all("SELECT Bg_ID, Total_Qty FROM Blood_Stock")}
    statuses_after = {r["Request_ID"]: r["Status"] for r in
                      db.fetch_all("SELECT Request_ID, Status FROM Blood_Request")}
    changed = {rid: (before, statuses_after.get(rid, "MISSING"))
               for rid, before in statuses_before.items()
               if statuses_after.get(rid, "MISSING") != before}
    results.check("rejection leaves blood stock untouched", stock_before == stock_after)
    results.check("rejection leaves donors/hospitals/donations untouched",
                  (donors_before, hospitals_before, donations_before)
                  == (scalar("SELECT COUNT(*) AS n FROM Donor"),
                      scalar("SELECT COUNT(*) AS n FROM Hospital"),
                      scalar("SELECT COUNT(*) AS n FROM Donation")))
    results.check("rejection changes only the rejected proposal row",
                  set(changed) == {pid_reject}, f"unexpected changes: {changed}")

    # --- decision on an already-rejected proposal is refused ---
    r = admin_decision(client, pid_reject, "approve")
    results.check("approve after reject is refused (still 302, no change)",
                  r.status_code == 302
                  and scalar("SELECT Status FROM Blood_Request WHERE Request_ID=%s",
                             (pid_reject,)) == "Rejected")
    if AUDIT_OK:
        results.check("refused decision adds no audit row", audit_count(pid_reject) == 1)
    else:
        skip("refused decision adds no audit row", "audit table unavailable")

    # --- invalid proposal data ---
    r = admin_decision(client, 990000001, "approve")
    results.check("nonexistent proposal is handled gracefully (302)", r.status_code == 302,
                  f"got {r.status_code}")
    if AUDIT_OK:
        results.check("nonexistent proposal creates no audit row",
                      audit_count(990000001) == 0)
    else:
        skip("nonexistent proposal creates no audit row", "audit table unavailable")
    html = client.get("/admin").get_data(as_text=True)
    results.check("nonexistent proposal shows a 'not found' message", "not found" in html.lower())

    r = client.post("/admin/proposals/not-a-number/approve",
                    data={"csrf_token": get_token(client)})
    results.check("non-numeric proposal id -> 404", r.status_code == 404, f"got {r.status_code}")

    # --- transaction rollback when the decision cannot be completed ---
    original = app_module._record_audit

    def failing_audit(*_args, **_kwargs):
        raise Error("simulated audit failure")

    app_module._record_audit = failing_audit
    try:
        r = admin_decision(client, pid_rollback, "approve")
    finally:
        app_module._record_audit = original

    results.check("failed decision still responds (no 500)", r.status_code == 302,
                  f"got {r.status_code}")
    results.check("transaction rolled back: proposal still Pending",
                  scalar("SELECT Status FROM Blood_Request WHERE Request_ID=%s",
                         (pid_rollback,)) == "Pending")
    if AUDIT_OK:
        results.check("transaction rolled back: no partial audit row",
                      audit_count(pid_rollback) == 0)
    else:
        skip("transaction rolled back: no partial audit row", "audit table unavailable")
    html = client.get("/admin").get_data(as_text=True)
    results.check("rollback surfaces a friendly error", "No changes were applied" in html)

    # Retry after the failure succeeds and is recorded exactly once.
    r = admin_decision(client, pid_rollback, "reject")
    results.check("retry after rollback succeeds",
                  scalar("SELECT Status FROM Blood_Request WHERE Request_ID=%s",
                         (pid_rollback,)) == "Rejected")
    if AUDIT_OK:
        results.check("retry records exactly one audit row", audit_count(pid_rollback) == 1)
    else:
        skip("retry records exactly one audit row", "audit table unavailable")


def run_logout_tests(results, client):
    """Logout clears the admin session; protected routes are closed again."""
    print("\n== E. Logout & session invalidation ==")
    r = post(client, "/admin/logout", csrf_token=get_token(client))
    results.check("logout redirects", r.status_code == 302, f"got {r.status_code}")
    r = client.get("/admin")
    results.check("admin page denied after logout",
                  r.status_code == 302 and "/admin/login" in r.headers.get("Location", ""),
                  f"got {r.status_code} {r.headers.get('Location')}")
    r = post(client, "/admin/proposals/1/approve", csrf_token=get_token(client))
    results.check("approve denied after logout -> 401", r.status_code == 401, f"got {r.status_code}")


# --------------------------------------------------------------------------- #
def main():
    try:
        version = scalar("SELECT VERSION() AS v")
    except Error as err:
        print(f"\n[ERROR] Could not connect to MySQL: {err}")
        print("        Check the DB_* values in .env and that the database is running.")
        return 2
    print(f"Connected to MySQL. Server version: {version}")

    global AUDIT_OK
    app_module.ensure_admin_tables()
    AUDIT_OK = audit_log_available()
    print("Audit log (Admin_Audit_Log): "
          + ("writable -- audit assertions enabled" if AUDIT_OK
             else "not writable here -- audit assertions will be skipped"))

    hospital = db.fetch_one("SELECT Hospital_ID FROM Hospital ORDER BY Hospital_ID LIMIT 1")
    blood_group = db.fetch_one("SELECT Bg_ID FROM Blood_Group ORDER BY Bg_ID LIMIT 1")
    if hospital is None or blood_group is None:
        print("[ERROR] Need at least one Hospital and one Blood_Group row (import seed.sql first).")
        return 2

    results = Results()
    client = app.test_client()

    proposal_ids = {}
    created_ids = []
    try:
        run_configuration_tests(results)
        run_access_tests(results, client)
        run_login_tests(results, client)

        # Create pending proposals through the existing (public) route, so the
        # admin workflow is exercised end-to-end.
        for key, qty in (("approve", "1.00"), ("reject", "2.00"), ("rollback", "3.00")):
            row = make_pending_request(client, hospital["Hospital_ID"],
                                       blood_group["Bg_ID"], qty, PENDING_DATE)
            if row:
                proposal_ids[key] = row["Request_ID"]
                created_ids.append(row["Request_ID"])
        results.check("three pending proposals created via the existing route",
                      len(proposal_ids) == 3, str(proposal_ids))

        if len(proposal_ids) == 3:
            run_decision_tests(results, client, proposal_ids)
        run_logout_tests(results, client)
    finally:
        for rid in created_ids:
            try:
                if AUDIT_OK:
                    db.execute("DELETE FROM Admin_Audit_Log WHERE Proposal_Type='Blood_Request' "
                               "AND Proposal_ID = %s", (rid,))
                db.execute("DELETE FROM Blood_Request WHERE Request_ID = %s", (rid,))
            except Error:
                pass
        app_module.reset_login_attempts()
        db.close_connection()
        print("\n  (cleanup: temporary proposals and audit rows removed)")

    print("\n== Summary ==")
    print(f"  passed: {results.passed}")
    print(f"  failed: {results.failed}")
    if SKIPPED:
        print(f"  skipped: {len(SKIPPED)} (audit table not writable in this environment)")
    if results.failures:
        print("  failures:")
        for name, detail in results.failures:
            print(f"    - {name} {detail}")
    return 0 if results.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
