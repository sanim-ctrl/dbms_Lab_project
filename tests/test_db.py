#!/usr/bin/env python3
"""tests/test_db.py -- database + application tests for the Blood Bank system.

Runs against the MySQL server configured in `.env` (or environment overrides).

SAFETY
------
This script never drops or truncates tables. It only:
  * reads the schema and sample data (verification), and
  * creates a few clearly-marked temporary rows to exercise CRUD / transactions,
    then deletes exactly those rows again and restores every Blood_Stock
    quantity to its original value (including Last_Updated).

Usage
-----
    python tests/test_db.py            # full suite: verification + behaviour
    python tests/test_db.py --check    # read-only: connection, tables, FKs, data

Exit codes
----------
    0 = all tests passed
    1 = one or more tests failed
    2 = could not connect / database not set up

Environment overrides (take priority over .env), useful for testing a
non-default instance:
    DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME
"""

import argparse
import os
import re
import sys
from decimal import Decimal

# Make the project root importable no matter where the script is run from.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

import db  # noqa: E402  (import after sys.path tweak)
from mysql.connector import Error  # noqa: E402


# --------------------------------------------------------------------------- #
# Tiny test result tracker
# --------------------------------------------------------------------------- #
class Results:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.failures = []

    def ok(self, name):
        self.passed += 1
        print(f"  [PASS] {name}")

    def fail(self, name, detail=""):
        self.failed += 1
        self.failures.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")

    def check(self, name, condition, detail=""):
        if condition:
            self.ok(name)
        else:
            self.fail(name, detail)
        return bool(condition)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def csrf_token(client, path):
    """Fetch a page and return its CSRF token (session-backed)."""
    html = client.get(path).get_data(as_text=True)
    match = CSRF_RE.search(html)
    if not match:
        raise RuntimeError(f"no CSRF token found on {path}")
    return match.group(1)


def post(client, path, **data):
    """POST to the app without following redirects (Post/Redirect/Get)."""
    return client.post(path, data=data, follow_redirects=False)


def scalar(sql, params=None):
    row = db.fetch_one(sql, params)
    if row is None:
        return None
    return next(iter(row.values()))


def table_engine(table):
    return scalar(
        "SELECT ENGINE FROM information_schema.tables "
        "WHERE table_schema = %s AND table_name = %s",
        (db.DB_CONFIG["database"], table),
    )


# --------------------------------------------------------------------------- #
# Section A: read-only verification of schema + sample data
# --------------------------------------------------------------------------- #
EXPECTED_TABLES = [
    "Blood_Group", "Donor", "Donation", "Hospital", "Blood_Request", "Blood_Stock",
]
EXPECTED_FKS = {
    "fk_donor_bg", "fk_donation_donor", "fk_donation_bg",
    "fk_request_hospital", "fk_request_bg", "fk_stock_bg",
}


def run_verification(results):
    print("\n== A. Schema & sample-data verification (read-only) ==")

    # A1 all six tables exist
    rows = db.fetch_all(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
        (db.DB_CONFIG["database"],),
    )
    present = {r[next(iter(r))] for r in rows}
    for table in EXPECTED_TABLES:
        results.check(f"table exists: {table}", table in present)

    if not all(t in present for t in EXPECTED_TABLES):
        return False  # stop; nothing else can be verified

    # A2 all tables use InnoDB
    for table in EXPECTED_TABLES:
        results.check(f"InnoDB engine: {table}", table_engine(table) == "InnoDB")

    # A3 foreign keys (by name) exist
    fk_rows = db.fetch_all(
        "SELECT CONSTRAINT_NAME AS name FROM information_schema.table_constraints "
        "WHERE table_schema = %s AND constraint_type = 'FOREIGN KEY'",
        (db.DB_CONFIG["database"],),
    )
    fk_names = {r["name"] for r in fk_rows}
    for fk in EXPECTED_FKS:
        results.check(f"foreign key: {fk}", fk in fk_names)
    results.check("foreign key count >= 6", len(fk_names) >= 6, f"found {len(fk_names)}")

    # A4 unique constraints enforced
    for uq in ("uq_blood_group_type", "uq_donor_phone", "uq_hospital_phone", "uq_stock_bg"):
        present_uq = db.fetch_one(
            "SELECT 1 AS ok FROM information_schema.table_constraints "
            "WHERE table_schema = %s AND constraint_type = 'UNIQUE' "
            "AND constraint_name = %s",
            (db.DB_CONFIG["database"], uq),
        )
        results.check(f"unique constraint: {uq}", present_uq is not None)

    # A5 CHECK constraints enforced
    check_rows = db.fetch_all(
        "SELECT CONSTRAINT_NAME AS name FROM information_schema.table_constraints "
        "WHERE table_schema = %s AND constraint_type = 'CHECK'",
        (db.DB_CONFIG["database"],),
    )
    check_names = {r["name"] for r in check_rows}
    for chk in ("chk_donor_age", "chk_donation_qty", "chk_request_qty",
                "chk_stock_non_negative", "chk_blood_type"):
        results.check(f"check constraint: {chk}", chk in check_names)

    # A6 sample data present (minimums from the brief)
    results.check("8 blood groups", scalar("SELECT COUNT(*) AS n FROM Blood_Group") == 8)
    results.check(">= 15 donors", scalar("SELECT COUNT(*) AS n FROM Donor") >= 15)
    results.check(">= 10 donations", scalar("SELECT COUNT(*) AS n FROM Donation") >= 10)
    results.check(">= 5 hospitals", scalar("SELECT COUNT(*) AS n FROM Hospital") >= 5)
    results.check(">= 8 blood requests", scalar("SELECT COUNT(*) AS n FROM Blood_Request") >= 8)
    results.check("8 stock rows (one per group)", scalar("SELECT COUNT(*) AS n FROM Blood_Stock") == 8)

    # A7 no negative stock, no orphan stock
    results.check("no negative stock", scalar("SELECT COUNT(*) AS n FROM Blood_Stock WHERE Total_Qty < 0") == 0)

    # A8 stock reconciliation: opening + donated - completed == stored
    #    Opening balances are the documented seed baseline (schema-independent
    #    check: stored must equal opening + donations - completed).
    opening = {1: 20, 2: 15, 3: 18, 4: 10, 5: 8, 6: 5, 7: 25, 8: 7}
    donated = {r["Bg_ID"]: r["qty"] for r in db.fetch_all(
        "SELECT Bg_ID, COALESCE(SUM(Qty),0) AS qty FROM Donation GROUP BY Bg_ID")}
    completed = {r["Bg_ID"]: r["qty"] for r in db.fetch_all(
        "SELECT Bg_ID, COALESCE(SUM(Qty),0) AS qty FROM Blood_Request "
        "WHERE Status = 'Completed' GROUP BY Bg_ID")}
    stored = {r["Bg_ID"]: r["Total_Qty"] for r in db.fetch_all(
        "SELECT Bg_ID, Total_Qty FROM Blood_Stock")}
    for bg_id, base in opening.items():
        if bg_id not in stored:
            continue
        expected = Decimal(base) + donated.get(bg_id, Decimal(0)) - completed.get(bg_id, Decimal(0))
        results.check(
            f"stock reconciles for Bg_ID {bg_id}",
            stored[bg_id] == expected,
            f"stored={stored[bg_id]} expected={expected}",
        )
    return True


# --------------------------------------------------------------------------- #
# Section B: behavioural tests (CRUD, transactions, workflow, errors)
# --------------------------------------------------------------------------- #
def run_behaviour(results):
    from app import app  # imported here so --check mode never needs it

    print("\n== B. Behavioural tests (create temp rows, then clean up) ==")

    client = app.test_client()

    marker = "TESTX"  # helps identify temporary rows if cleanup ever fails
    created = {"donors": [], "hospitals": [], "requests": [], "donations": []}
    initial_stock = {
        r["Bg_ID"]: (r["Total_Qty"], r["Last_Updated"])
        for r in db.fetch_all("SELECT Bg_ID, Total_Qty, Last_Updated FROM Blood_Stock")
    }

    # Unique phone numbers that will not collide with seed/fictional data.
    donor_phone = "9550000001"
    donor2_phone = "9550000002"
    hospital_phone = "9550000009"

    try:
        # -------------------------------------------------------------- #
        # B1. CSRF protection
        # -------------------------------------------------------------- #
        r = client.post("/donors/new", data={"name": "No CSRF", "age": 30,
                                             "gender": "Male", "phone": "9550000099",
                                             "bg_id": 1})
        results.check("CSRF: POST without token rejected (400)", r.status_code == 400,
                      f"got {r.status_code}")

        # -------------------------------------------------------------- #
        # B2. Donor CRUD
        # -------------------------------------------------------------- #
        token = csrf_token(client, "/donors/new")
        r = post(client, "/donors/new", csrf_token=token, name=f"{marker} Donor",
                 age=30, gender="Male", phone=donor_phone, bg_id=3)
        results.check("donor create returns redirect", r.status_code == 302, f"got {r.status_code}")
        d1 = db.fetch_one("SELECT Donor_ID, Name, Bg_ID FROM Donor WHERE Phone = %s", (donor_phone,))
        results.check("donor create persisted", d1 is not None)
        if d1:
            created["donors"].append(d1["Donor_ID"])

        # validation: bad age must be rejected AND typed name preserved
        token = csrf_token(client, "/donors/new")
        r = post(client, "/donors/new", csrf_token=token, name=f"{marker} Preserve",
                 age=12, gender="Male", phone="9550000088", bg_id=1)
        html = r.get_data(as_text=True)
        results.check("invalid age rejected (no insert)",
                      scalar("SELECT COUNT(*) AS n FROM Donor WHERE Phone='9550000088'") == 0)
        results.check("invalid form keeps typed value (bug fix)",
                      "value=\"TESTX Preserve\"" in html)

        # update
        if d1:
            token = csrf_token(client, f"/donors/{d1['Donor_ID']}/edit")
            r = post(client, f"/donors/{d1['Donor_ID']}/edit", csrf_token=token,
                     name=f"{marker} Donor Edited", age=31, gender="Male",
                     phone=donor_phone, bg_id=3)
            row = db.fetch_one("SELECT Name, Age FROM Donor WHERE Donor_ID = %s", (d1["Donor_ID"],))
            results.check("donor update persisted",
                          row is not None and row["Name"] == f"{marker} Donor Edited"
                          and row["Age"] == 31, str(row))

        # a second donor to exercise donation + RESTRICT delete
        token = csrf_token(client, "/donors/new")
        r = post(client, "/donors/new", csrf_token=token, name=f"{marker} Donor2",
                 age=28, gender="Female", phone=donor2_phone, bg_id=1)
        d2 = db.fetch_one("SELECT Donor_ID, Bg_ID FROM Donor WHERE Phone = %s", (donor2_phone,))
        if d2:
            created["donors"].append(d2["Donor_ID"])
        results.check("second donor created", d2 is not None)

        if not d2:
            print("  (skipping donation/request tests -- prerequisites failed)")
            return

        bg_id = d2["Bg_ID"]

        # -------------------------------------------------------------- #
        # B3. Hospital CRUD
        # -------------------------------------------------------------- #
        token = csrf_token(client, "/hospitals/new")
        r = post(client, "/hospitals/new", csrf_token=token, name=f"{marker} Hospital",
                 address="Test Road", phone=hospital_phone)
        h1 = db.fetch_one("SELECT Hospital_ID FROM Hospital WHERE Phone = %s", (hospital_phone,))
        results.check("hospital create persisted", h1 is not None)
        if h1:
            created["hospitals"].append(h1["Hospital_ID"])

        # -------------------------------------------------------------- #
        # B4. Donation increases stock (exactly once)
        # -------------------------------------------------------------- #
        before = scalar("SELECT Total_Qty FROM Blood_Stock WHERE Bg_ID = %s", (bg_id,))
        token = csrf_token(client, "/donations")
        post(client, "/donations", csrf_token=token, donor_id=d2["Donor_ID"],
             bg_id=bg_id, donation_date="2026-07-01", qty="2.00")
        after = scalar("SELECT Total_Qty FROM Blood_Stock WHERE Bg_ID = %s", (bg_id,))
        results.check("donation increases stock by qty", after == before + Decimal("2.00"),
                      f"before={before} after={after}")
        don = db.fetch_one(
            "SELECT Donation_ID FROM Donation WHERE Donor_ID=%s AND Donation_Date='2026-07-01'",
            (d2["Donor_ID"],))
        if don:
            created["donations"].append(don["Donation_ID"])
        results.check("donation row persisted", don is not None)

        # mismatched blood group is rejected and stock unchanged
        other_bg = 5 if bg_id != 5 else 6
        mid = scalar("SELECT Total_Qty FROM Blood_Stock WHERE Bg_ID = %s", (other_bg,))
        token = csrf_token(client, "/donations")
        post(client, "/donations", csrf_token=token, donor_id=d2["Donor_ID"],
             bg_id=other_bg, donation_date="2026-07-02", qty="1.00")
        mid2 = scalar("SELECT Total_Qty FROM Blood_Stock WHERE Bg_ID = %s", (other_bg,))
        results.check("mismatched blood-group donation rejected", mid == mid2,
                      f"other_bg stock changed {mid}->{mid2}")

        # invalid (zero) quantity rejected
        zero_before = scalar("SELECT Total_Qty FROM Blood_Stock WHERE Bg_ID = %s", (bg_id,))
        token = csrf_token(client, "/donations")
        post(client, "/donations", csrf_token=token, donor_id=d2["Donor_ID"],
             bg_id=bg_id, donation_date="2026-07-02", qty="0")
        zero_after = scalar("SELECT Total_Qty FROM Blood_Stock WHERE Bg_ID = %s", (bg_id,))
        results.check("zero quantity rejected", zero_before == zero_after)

        # -------------------------------------------------------------- #
        # B5. Request workflow: create/approve change nothing; fulfil deducts
        # -------------------------------------------------------------- #
        stock_before = scalar("SELECT Total_Qty FROM Blood_Stock WHERE Bg_ID = %s", (bg_id,))
        token = csrf_token(client, "/requests")
        post(client, "/requests", csrf_token=token, hospital_id=h1["Hospital_ID"],
             bg_id=bg_id, date="2026-07-03", qty="1.00")
        req = db.fetch_one(
            "SELECT Request_ID, Status FROM Blood_Request WHERE Hospital_ID=%s "
            "ORDER BY Request_ID DESC LIMIT 1", (h1["Hospital_ID"],))
        results.check("request created as Pending",
                      req is not None and req["Status"] == "Pending", str(req))
        if req:
            created["requests"].append(req["Request_ID"])
        results.check("create request does not change stock",
                      scalar("SELECT Total_Qty FROM Blood_Stock WHERE Bg_ID=%s", (bg_id,)) == stock_before)

        token = csrf_token(client, "/requests")
        post(client, f"/requests/{req['Request_ID']}/approve", csrf_token=token)
        results.check("request approved",
                      scalar("SELECT Status FROM Blood_Request WHERE Request_ID=%s",
                             (req["Request_ID"],)) == "Approved")
        results.check("approve does not change stock",
                      scalar("SELECT Total_Qty FROM Blood_Stock WHERE Bg_ID=%s", (bg_id,)) == stock_before)

        token = csrf_token(client, "/requests")
        post(client, f"/requests/{req['Request_ID']}/fulfill", csrf_token=token)
        results.check("request completed after fulfil",
                      scalar("SELECT Status FROM Blood_Request WHERE Request_ID=%s",
                             (req["Request_ID"],)) == "Completed")
        after_fulfil = scalar("SELECT Total_Qty FROM Blood_Stock WHERE Bg_ID=%s", (bg_id,))
        results.check("fulfil deducts stock once",
                      after_fulfil == stock_before - Decimal("1.00"),
                      f"{stock_before} -> {after_fulfil}")

        # B6. duplicate fulfilment prevention
        token = csrf_token(client, "/requests")
        post(client, f"/requests/{req['Request_ID']}/fulfill", csrf_token=token)
        results.check("second fulfil does not deduct again",
                      scalar("SELECT Total_Qty FROM Blood_Stock WHERE Bg_ID=%s", (bg_id,)) == after_fulfil)

        # B7. oversized request cannot overdraw
        # qty=5000 passes app validation (<= 9999) and fits DECIMAL(6,2) but is
        # far larger than the available stock, so fulfilment must fail.
        token = csrf_token(client, "/requests")
        post(client, "/requests", csrf_token=token, hospital_id=h1["Hospital_ID"],
             bg_id=bg_id, date="2026-07-04", qty="5000")
        big = db.fetch_one(
            "SELECT Request_ID FROM Blood_Request "
            "WHERE Hospital_ID=%s AND Date='2026-07-04' ORDER BY Request_ID DESC LIMIT 1",
            (h1["Hospital_ID"],))
        results.check("oversized request created as Pending",
                      big is not None and scalar(
                          "SELECT Status FROM Blood_Request WHERE Request_ID=%s",
                          (big["Request_ID"],)) == "Pending")
        if big:
            created["requests"].append(big["Request_ID"])
            token = csrf_token(client, "/requests")
            post(client, f"/requests/{big['Request_ID']}/fulfill", csrf_token=token)
            results.check("oversized request stays Pending (insufficient stock)",
                          scalar("SELECT Status FROM Blood_Request WHERE Request_ID=%s",
                                 (big["Request_ID"],)) == "Pending")
            results.check("stock unchanged after failed fulfil",
                          scalar("SELECT Total_Qty FROM Blood_Stock WHERE Bg_ID=%s",
                                 (bg_id,)) == after_fulfil)
        results.check("stock never negative",
                      scalar("SELECT COUNT(*) AS n FROM Blood_Stock WHERE Total_Qty < 0") == 0)

        # -------------------------------------------------------------- #
        # B8. RESTRICT delete: donor with donations (errno 1217/1451)
        # -------------------------------------------------------------- #
        try:
            with db.get_cursor(commit=True) as cur:
                cur.execute("DELETE FROM Donor WHERE Donor_ID = %s", (d2["Donor_ID"],))
            results.fail("RESTRICT delete via SQL raises", "no error raised")
        except Error as err:
            results.check("RESTRICT delete errno in (1217,1451)", err.errno in (1217, 1451),
                          f"errno={err.errno}")
            results.check("donor with donations still present",
                          scalar("SELECT COUNT(*) AS n FROM Donor WHERE Donor_ID=%s",
                                 (d2["Donor_ID"],)) == 1)

        # via the web route, the friendly message must appear and the row stays
        token = csrf_token(client, "/donors")
        r = post(client, f"/donors/{d2['Donor_ID']}/delete", csrf_token=token)
        results.check("delete-with-history route is guarded (302)",
                      r.status_code == 302, f"got {r.status_code}")
        results.check("guarded donor not deleted",
                      scalar("SELECT COUNT(*) AS n FROM Donor WHERE Donor_ID=%s", (d2["Donor_ID"],)) == 1)

        # hospital with request history is guarded too
        token = csrf_token(client, "/hospitals")
        post(client, f"/hospitals/{h1['Hospital_ID']}/delete", csrf_token=token)
        results.check("hospital with requests not deleted",
                      scalar("SELECT COUNT(*) AS n FROM Hospital WHERE Hospital_ID=%s",
                             (h1["Hospital_ID"],)) == 1)

        # -------------------------------------------------------------- #
        # B9. Transaction rollback (multi-step)
        # -------------------------------------------------------------- #
        don_before = scalar("SELECT COUNT(*) AS n FROM Donation WHERE Donor_ID=%s", (d2["Donor_ID"],))
        stock_x = scalar("SELECT Total_Qty FROM Blood_Stock WHERE Bg_ID=%s", (bg_id,))
        raised = False
        try:
            with db.transaction() as cur:
                cur.execute(
                    "INSERT INTO Donation (Donor_ID, Bg_ID, Donation_Date, Qty) VALUES (%s,%s,%s,%s)",
                    (d2["Donor_ID"], bg_id, "2026-07-05", 1))
                cur.execute("UPDATE Blood_Stock SET Total_Qty = Total_Qty + 1 WHERE Bg_ID=%s", (bg_id,))
                # force a failure: duplicate blood group violates a UNIQUE key
                cur.execute("INSERT INTO Blood_Group (Blood_Type) VALUES ('A+')")
        except Error:
            raised = True
        don_after = scalar("SELECT COUNT(*) AS n FROM Donation WHERE Donor_ID=%s", (d2["Donor_ID"],))
        stock_y = scalar("SELECT Total_Qty FROM Blood_Stock WHERE Bg_ID=%s", (bg_id,))
        results.check("transaction rolled back on failure", raised)
        results.check("rolled-back donation not persisted", don_before == don_after)
        results.check("rolled-back stock change not persisted", stock_x == stock_y)

        # -------------------------------------------------------------- #
        # B10. Donor delete succeeds for a donor WITHOUT donations
        # -------------------------------------------------------------- #
        if d1:
            token = csrf_token(client, "/donors")
            post(client, f"/donors/{d1['Donor_ID']}/delete", csrf_token=token)
            deleted = scalar("SELECT COUNT(*) AS n FROM Donor WHERE Donor_ID=%s", (d1["Donor_ID"],)) == 0
            results.check("donor without donations deleted", deleted)
            if deleted:
                created["donors"].remove(d1["Donor_ID"])

    finally:
        # ---------------------------------------------------------- #
        # Cleanup: remove exactly what we created, restore stock
        # ---------------------------------------------------------- #
        for rid in created["requests"]:
            db.execute("DELETE FROM Blood_Request WHERE Request_ID = %s", (rid,))
        for did in created["donations"]:
            db.execute("DELETE FROM Donation WHERE Donation_ID = %s", (did,))
        for did in created["donors"]:
            try:
                db.execute("DELETE FROM Donor WHERE Donor_ID = %s", (did,))
            except Error:
                pass
        for hid in created["hospitals"]:
            try:
                db.execute("DELETE FROM Hospital WHERE Hospital_ID = %s", (hid,))
            except Error:
                pass
        for bg_id, (qty, ts) in initial_stock.items():
            db.execute("UPDATE Blood_Stock SET Total_Qty = %s, Last_Updated = %s WHERE Bg_ID = %s",
                       (qty, ts, bg_id))
        print("\n  (cleanup: temporary rows removed, stock restored)")


# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(description="Blood Bank database tests")
    parser.add_argument("--check", action="store_true",
                        help="read-only: verify connection, tables, FKs and sample data only")
    args = parser.parse_args()

    host = db.DB_CONFIG["host"]
    port = db.DB_CONFIG["port"]
    user = db.DB_CONFIG["user"]
    name = db.DB_CONFIG["database"]
    print(f"Connecting to MySQL at {host}:{port} as '{user}', database '{name}' ...")

    try:
        version = scalar("SELECT VERSION() AS v")
    except Error as err:
        print(f"\n[ERROR] Could not connect to MySQL: {err}")
        print("        Update DB_HOST/DB_PORT/DB_USER/DB_PASSWORD/DB_NAME in .env "
              "and make sure the database and user exist.")
        return 2
    print(f"Connected. Server version: {version}\n")

    # Database present?
    if scalar("SELECT COUNT(*) AS n FROM information_schema.schemata WHERE schema_name = %s",
              (name,)) == 0:
        print(f"[ERROR] Database '{name}' does not exist. Import schema.sql and seed.sql first.")
        return 2

    results = Results()
    verified = run_verification(results)

    if not args.check and verified:
        run_behaviour(results)

    print("\n== Summary ==")
    print(f"  passed: {results.passed}")
    print(f"  failed: {results.failed}")
    if results.failures:
        print("  failures:")
        for name_, detail in results.failures:
            print(f"    - {name_} {detail}")
    return 0 if results.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
