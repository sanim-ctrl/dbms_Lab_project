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
    python tests/test_db.py            # full suite: verification + behaviour + connection lifecycle
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
import threading
from decimal import Decimal

# Make the project root importable no matter where the script is run from.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

import db  # noqa: E402  (import after sys.path tweak)
import mysql.connector  # noqa: E402
from mysql.connector import Error, InterfaceError  # noqa: E402


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
    for chk in ("chk_donor_age", "chk_donation_units", "chk_request_qty",
                "chk_stock_non_negative", "chk_blood_type"):
        results.check(f"check constraint: {chk}", chk in check_names)

    # A5b Donation.Units_Donated is a positive integer column with default 1.
    units_col = db.fetch_one(
        "SELECT DATA_TYPE, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT "
        "FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = 'Donation' "
        "AND column_name = 'Units_Donated'",
        (db.DB_CONFIG["database"],),
    )
    results.check("Donation.Units_Donated exists", units_col is not None, str(units_col))
    results.check("Donation.Units_Donated is an integer",
                  units_col is not None and units_col["DATA_TYPE"] == "int", str(units_col))
    results.check("Donation.Units_Donated is NOT NULL",
                  units_col is not None and units_col["IS_NULLABLE"] == "NO", str(units_col))
    results.check("Donation.Units_Donated defaults to 1",
                  units_col is not None and str(units_col["COLUMN_DEFAULT"]) == "1", str(units_col))

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
        "SELECT Bg_ID, COALESCE(SUM(Units_Donated),0) AS qty FROM Donation GROUP BY Bg_ID")}
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
        # B4. Donation form: Units Donated increases stock exactly once
        # -------------------------------------------------------------- #
        form_html = client.get("/donations").get_data(as_text=True)
        results.check("donation form has required Units Donated field",
                      'name="units_donated"' in form_html
                      and 'min="1"' in form_html
                      and 'step="1"' in form_html
                      and 'value="1"' in form_html
                      and "required" in form_html)

        def record_units(units, day):
            """Submit a donation and return stock/count values around it."""
            before = scalar("SELECT Total_Qty FROM Blood_Stock WHERE Bg_ID = %s", (bg_id,))
            count_before = scalar(
                "SELECT COUNT(*) AS n FROM Donation WHERE Donor_ID=%s", (d2["Donor_ID"],))
            token = csrf_token(client, "/donations")
            response = post(client, "/donations", csrf_token=token, donor_id=d2["Donor_ID"],
                            bg_id=bg_id, donation_date=f"2026-07-{day:02d}",
                            units_donated=str(units))
            after = scalar("SELECT Total_Qty FROM Blood_Stock WHERE Bg_ID = %s", (bg_id,))
            count_after = scalar(
                "SELECT COUNT(*) AS n FROM Donation WHERE Donor_ID=%s", (d2["Donor_ID"],))
            donation = db.fetch_one(
                "SELECT Donation_ID, Units_Donated FROM Donation "
                "WHERE Donor_ID=%s AND Donation_Date=%s",
                (d2["Donor_ID"], f"2026-07-{day:02d}"),
            )
            if donation:
                created["donations"].append(donation["Donation_ID"])
            return before, after, count_before, count_after, donation, response

        for units, day in ((1, 1), (2, 2), (3, 3)):
            before, after, count_before, count_after, donation, response = record_units(units, day)
            results.check(f"donation of {units} unit(s) redirects (PRG)",
                          response.status_code == 302, f"got {response.status_code}")
            results.check(f"donation of {units} unit(s) increases stock by {units}",
                          after == before + Decimal(units), f"before={before} after={after}")
            results.check(f"donation of {units} unit(s) stores Units_Donated={units}",
                          donation is not None and donation["Units_Donated"] == units, str(donation))
            results.check(f"donation of {units} unit(s) creates exactly one row",
                          count_after == count_before + 1, f"{count_before}->{count_after}")

        # Existing whole-number "qty" submissions keep working (legacy clients).
        legacy_before = scalar("SELECT Total_Qty FROM Blood_Stock WHERE Bg_ID = %s", (bg_id,))
        token = csrf_token(client, "/donations")
        post(client, "/donations", csrf_token=token, donor_id=d2["Donor_ID"],
             bg_id=bg_id, donation_date="2026-07-06", qty="1.00")
        legacy_after = scalar("SELECT Total_Qty FROM Blood_Stock WHERE Bg_ID = %s", (bg_id,))
        legacy = db.fetch_one(
            "SELECT Donation_ID FROM Donation WHERE Donor_ID=%s AND Donation_Date='2026-07-06'",
            (d2["Donor_ID"],))
        if legacy:
            created["donations"].append(legacy["Donation_ID"])
        results.check("legacy qty=1.00 still increases stock by 1",
                      legacy_after == legacy_before + Decimal("1"),
                      f"before={legacy_before} after={legacy_after}")

        # mismatched blood group is rejected and stock unchanged
        other_bg = 5 if bg_id != 5 else 6
        mid = scalar("SELECT Total_Qty FROM Blood_Stock WHERE Bg_ID = %s", (other_bg,))
        token = csrf_token(client, "/donations")
        post(client, "/donations", csrf_token=token, donor_id=d2["Donor_ID"],
             bg_id=other_bg, donation_date="2026-07-04", units_donated="1")
        mid2 = scalar("SELECT Total_Qty FROM Blood_Stock WHERE Bg_ID = %s", (other_bg,))
        results.check("mismatched blood-group donation rejected", mid == mid2,
                      f"other_bg stock changed {mid}->{mid2}")

        # Invalid quantities: zero, negative, decimal, text, blank, missing.
        for label, value in (
            ("zero", "0"),
            ("negative", "-1"),
            ("decimal", "1.5"),
            ("whole-looking decimal", "2.0"),
            ("text", "abc"),
            ("blank", ""),
            ("missing", None),
        ):
            before = scalar("SELECT Total_Qty FROM Blood_Stock WHERE Bg_ID = %s", (bg_id,))
            don_before = scalar("SELECT COUNT(*) AS n FROM Donation")
            data = {"csrf_token": csrf_token(client, "/donations"), "donor_id": d2["Donor_ID"],
                    "bg_id": bg_id, "donation_date": "2026-07-11"}
            if value is not None:
                data["units_donated"] = value
            post(client, "/donations", **data)
            after = scalar("SELECT Total_Qty FROM Blood_Stock WHERE Bg_ID = %s", (bg_id,))
            don_after = scalar("SELECT COUNT(*) AS n FROM Donation")
            results.check(f"invalid units ({label}) rejected: stock unchanged", before == after)
            results.check(f"invalid units ({label}) rejected: no donation row",
                          don_before == don_after, f"{don_before}->{don_after}")

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
                    "INSERT INTO Donation (Donor_ID, Bg_ID, Donation_Date, Units_Donated) VALUES (%s,%s,%s,%s)",
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
# Section C: connection lifecycle -- reuse, stale recovery, atomicity, threads
# --------------------------------------------------------------------------- #
def raw_connection():
    """Open an independent connection, bypassing db's per-thread cache.

    Used to observe server-side truth (what is really committed) and to KILL
    the cached connection when simulating an idle timeout / server restart.
    """
    return mysql.connector.connect(**db.DB_CONFIG)


def hospital_by_phone(conn, phone):
    """Look up a Hospital row by phone from an independent session."""
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT Hospital_ID FROM Hospital WHERE Phone = %s", (phone,))
    row = cur.fetchone()
    cur.close()
    return row


def run_connection_tests(results):
    print("\n== C. Connection lifecycle (reuse, stale recovery, atomicity, threads) ==")

    db.close_connection()  # start from a known state in this thread
    cleanup_hospitals = []
    observer = None
    try:
        # -------------------------------------------------------------- #
        # C1. Connection reuse: many queries, one connection
        # -------------------------------------------------------------- #
        c1 = db.get_connection()
        c2 = db.get_connection()
        results.check("get_connection returns the cached connection", c1 is c2)
        db.fetch_one("SELECT 1 AS ok")
        results.check("queries reuse the cached connection (not closed per call)",
                      db.get_connection() is c1)

        # -------------------------------------------------------------- #
        # C2. Reuse is verified with an explicit ping(reconnect=False)
        # -------------------------------------------------------------- #
        pings = []
        real_ping = c1.ping

        def counting_ping(reconnect=False, *args, **kwargs):
            pings.append(reconnect)
            return real_ping(reconnect=reconnect, *args, **kwargs)

        c1.ping = counting_ping
        db.fetch_one("SELECT 1 AS ok")
        c1.ping = real_ping  # restore before anything else trips on it
        results.check("cached connection is pinged before reuse",
                      len(pings) >= 1, f"pings={pings}")
        results.check("ping passes reconnect=False (never silent reconnect)",
                      all(r is False for r in pings), f"pings={pings}")

        # -------------------------------------------------------------- #
        # C3. Stale connection: the server kills it, the next call recovers
        # -------------------------------------------------------------- #
        victim_id = db.get_connection().connection_id
        killer = raw_connection()
        kcur = killer.cursor()
        kcur.execute(f"KILL {victim_id}")
        kcur.close()
        killer.close()
        row = db.fetch_one("SELECT 1 AS ok")
        results.check("query succeeds after the server killed the connection",
                      row == {"ok": 1}, str(row))
        new_id = db.get_connection().connection_id
        results.check("dead connection replaced by a fresh session",
                      new_id != victim_id, f"old={victim_id} new={new_id}")

        # -------------------------------------------------------------- #
        # C4. A connection lost INSIDE a transaction never silently
        #     reconnects -- the transaction fails as a unit instead
        # -------------------------------------------------------------- #
        victim_id = db.get_connection().connection_id
        killer = raw_connection()
        raised = None
        try:
            with db.transaction() as cur:
                cur.execute("SELECT 1 AS ok")
                kcur = killer.cursor()
                kcur.execute(f"KILL {victim_id}")
                kcur.close()
                # The cached connection is now dead and the transaction is
                # still open: get_connection() must raise, NOT reconnect.
                db.get_connection()
        except Error as err:
            raised = err
        finally:
            killer.close()
        results.check("connection lost mid-transaction raises (no silent reconnect)",
                      raised is not None, "no error raised")
        results.check("connection recovers after the aborted transaction",
                      db.fetch_one("SELECT 1 AS ok") == {"ok": 1})

        # -------------------------------------------------------------- #
        # C5. transaction() + autocommit=True: rollback, commit, and
        #     uncommitted work invisible to other sessions
        # -------------------------------------------------------------- #
        base_phone = "9550000101"
        db.execute(
            "INSERT INTO Hospital (Name, Address, Phone) VALUES (%s, %s, %s)",
            ("TESTX ConnC", "Connection Test Road", base_phone),
        )
        row = db.fetch_one("SELECT Hospital_ID FROM Hospital WHERE Phone = %s", (base_phone,))
        results.check("C5 setup: temp hospital row created", row is not None)
        if row:
            hid = row["Hospital_ID"]
            cleanup_hospitals.append(hid)
            observer = raw_connection()

            # rollback: an exception inside the block discards every statement
            rolled_back = False
            try:
                with db.transaction() as cur:
                    cur.execute("UPDATE Hospital SET Phone = %s WHERE Hospital_ID = %s",
                                ("9550000102", hid))
                    results.check("uncommitted change invisible to other sessions",
                                  hospital_by_phone(observer, "9550000102") is None)
                    raise RuntimeError("force rollback")
            except RuntimeError:
                rolled_back = True
            results.check("exception inside transaction() propagated", rolled_back)
            results.check("rollback discarded the UPDATE (server-side truth)",
                          hospital_by_phone(observer, base_phone) is not None
                          and hospital_by_phone(observer, "9550000102") is None)

            # commit: statements become visible to other sessions on exit
            with db.transaction() as cur:
                cur.execute("UPDATE Hospital SET Phone = %s WHERE Hospital_ID = %s",
                            ("9550000103", hid))
            results.check("commit made the UPDATE visible to other sessions",
                          hospital_by_phone(observer, "9550000103") is not None)

            # the reused connection is transaction-free afterwards: a plain
            # autocommit write is durable without any explicit commit
            db.execute("UPDATE Hospital SET Phone = %s WHERE Hospital_ID = %s",
                       ("9550000104", hid))
            results.check("autocommit write durable without an explicit commit",
                          hospital_by_phone(observer, "9550000104") is not None)

        # -------------------------------------------------------------- #
        # C6. Concurrent threads: one connection per thread, all commit
        # -------------------------------------------------------------- #
        workers = 5
        cids = {}
        errors = []

        def worker(idx):
            try:
                with db.transaction() as cur:
                    cur.execute(
                        "INSERT INTO Hospital (Name, Address, Phone) VALUES (%s, %s, %s)",
                        (f"TESTX Thread{idx}", "Thread Test Road", f"955000030{idx}"),
                    )
                    cur.execute("SELECT CONNECTION_ID() AS cid")
                    cids[idx] = cur.fetchone()["cid"]
            except Exception as err:  # reported through results below
                errors.append((idx, err))
            finally:
                db.close_connection()  # threads must not leak pooled connections

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        results.check("all worker threads finished without error", not errors, str(errors))
        ids = [cids.get(i) for i in range(workers)]
        results.check("every worker thread got a connection", all(ids), str(ids))
        results.check("each thread used its OWN connection (no sharing)",
                      len(set(ids)) == workers, f"connection ids: {ids}")
        stored = db.fetch_all("SELECT Phone FROM Hospital WHERE Name LIKE 'TESTX Thread%'")
        results.check("every thread's transaction committed",
                      {r["Phone"] for r in stored} == {f"955000030{i}" for i in range(workers)},
                      str(stored))

        # -------------------------------------------------------------- #
        # C7. A failed cursor creation drops the bad cached connection
        # -------------------------------------------------------------- #
        conn = db.get_connection()
        dead_id = conn.connection_id
        real_cursor = conn.cursor

        def broken_cursor(*args, **kwargs):
            raise InterfaceError("simulated cursor-creation failure")

        conn.cursor = broken_cursor
        raised = None
        try:
            db.fetch_one("SELECT 1 AS ok")
        except InterfaceError as err:
            raised = err
        conn.cursor = real_cursor  # restore (attribute only; object is discarded)
        results.check("cursor-creation failure surfaces to the caller",
                      raised is not None, "no error raised")
        results.check("bad cached connection was dropped and replaced",
                      db.get_connection().connection_id != dead_id,
                      f"old={dead_id}")

    finally:
        # ---------------------------------------------------------- #
        # Cleanup: remove exactly what we created, never touch the rest
        # ---------------------------------------------------------- #
        if observer is not None:
            try:
                observer.close()
            except Error:
                pass
        try:
            db.execute("DELETE FROM Hospital WHERE Name LIKE 'TESTX Thread%'")
        except Error:
            pass
        for hid in cleanup_hospitals:
            try:
                db.execute("DELETE FROM Hospital WHERE Hospital_ID = %s", (hid,))
            except Error:
                pass
        db.close_connection()
        print("\n  (cleanup: connection-test rows removed, connections closed)")


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
        run_connection_tests(results)

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
