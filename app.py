"""app.py -- Blood Bank Management System (Flask + MySQL, no ORM).

Run locally with:   python app.py
The application connects directly to MySQL through mysql-connector-python and
writes plain SQL, so every query is visible and easy to explain.

Security notes:
  * Every user-supplied value is passed as a bound parameter (%s), never
    concatenated into the SQL string.
  * Forms are protected with a lightweight session-based CSRF token.
  * Database errors are logged server-side and shown as a friendly message;
    credentials and stack traces are never shown in the browser.
"""

import os
import re
import secrets
import time
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, session, abort,
)
from mysql.connector import Error
from werkzeug.security import check_password_hash

import db


# --------------------------------------------------------------------------- #
# Application setup
# --------------------------------------------------------------------------- #
app = Flask(__name__)
# Session signing key: SECRET_KEY is the documented, required variable;
# FLASK_SECRET_KEY is kept as a legacy fallback so existing deployments that
# only define FLASK_SECRET_KEY keep working exactly as before.
app.config["SECRET_KEY"] = (
    os.environ.get("SECRET_KEY")
    or os.environ.get("FLASK_SECRET_KEY")
    or "dev-insecure-key-change-me"
)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# A permanent cookie gets an explicit lifetime, so an admin session expires on
# its own even if the browser stays open (see ADMIN_SESSION_MINUTES).
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(
    minutes=int(os.environ.get("ADMIN_SESSION_MINUTES", "30"))
)

LOW_STOCK_THRESHOLD = int(os.environ.get("LOW_STOCK_THRESHOLD", "10"))
GENDERS = ("Male", "Female", "Other")
REQUEST_STATUSES = ("Pending", "Approved", "Completed", "Rejected")


# --------------------------------------------------------------------------- #
# CSRF protection (lightweight, session based -- no extra dependency)
# --------------------------------------------------------------------------- #
def generate_csrf_token():
    """Return the session CSRF token, creating it on first use."""
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_hex(32)
    return session["_csrf_token"]


app.jinja_env.globals["csrf_token"] = generate_csrf_token


@app.before_request
def csrf_protect():
    """Reject any POST whose csrf_token does not match the session token."""
    if request.method == "POST":
        expected = session.get("_csrf_token")
        received = request.form.get("csrf_token", "")
        if not expected or not secrets.compare_digest(expected, received):
            abort(400, description="CSRF token validation failed. Please reload the page and try again.")


# --------------------------------------------------------------------------- #
# Small validation helpers
# --------------------------------------------------------------------------- #
class ValidationError(ValueError):
    """Raised when a submitted form value is invalid."""


def as_int(value, field, minimum=None, maximum=None):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValidationError(f"{field} must be a whole number.")
    if minimum is not None and number < minimum:
        raise ValidationError(f"{field} must be at least {minimum}.")
    if maximum is not None and number > maximum:
        raise ValidationError(f"{field} must be at most {maximum}.")
    return number


def as_positive_decimal(value, field):
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError(f"{field} must be a number.")
    if number <= 0:
        raise ValidationError(f"{field} must be greater than zero.")
    if number > 9999:
        raise ValidationError(f"{field} is unreasonably large.")
    return number


def as_date(value, field):
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise ValidationError(f"{field} must be a valid date (YYYY-MM-DD).")


def as_phone(value):
    phone = str(value or "").strip()
    if not re.fullmatch(r"[0-9+\- ]{7,15}", phone):
        raise ValidationError("Phone must be 7-15 characters (digits, +, - or space).")
    return phone


def as_name(value, field, max_len):
    name = str(value or "").strip()
    if not name:
        raise ValidationError(f"{field} is required.")
    if len(name) > max_len:
        raise ValidationError(f"{field} must be at most {max_len} characters.")
    return name


def require_choice(value, allowed, field):
    value = str(value or "").strip()
    if value not in allowed:
        raise ValidationError(f"{field} is not valid.")
    return value


def exists_in(table, pk_col, pk_val):
    row = db.fetch_one(f"SELECT 1 AS ok FROM {table} WHERE {pk_col} = %s", (pk_val,))
    return row is not None


def donor_form_state(form):
    """Rebuild a donor-shaped dict from a submitted form.

    Templates read donor.Name / donor.Age / ... (the database row shape). When a
    validation error occurs we re-render the form, so we map the submitted
    form fields back to that shape to preserve what the user typed.
    """
    return {
        "Name": form.get("name", ""),
        "Age": form.get("age", ""),
        "Gender": form.get("gender", ""),
        "Phone": form.get("phone", ""),
        "Bg_ID": form.get("bg_id", ""),
    }


def hospital_form_state(form):
    """Rebuild a hospital-shaped dict from a submitted form (see above)."""
    return {
        "Name": form.get("name", ""),
        "Address": form.get("address", ""),
        "Phone": form.get("phone", ""),
    }


# --------------------------------------------------------------------------- #
# Info used by many templates (dropdowns populated FROM the database)
# --------------------------------------------------------------------------- #
def blood_groups():
    return db.fetch_all("SELECT Bg_ID, Blood_Type FROM Blood_Group ORDER BY Bg_ID")


def hospitals():
    return db.fetch_all("SELECT Hospital_ID, Name FROM Hospital ORDER BY Name")


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
@app.route("/")
def dashboard():
    # Read every counter in ONE round trip. The database is remote, so each
    # extra statement costs a full network round trip; eight separate COUNT
    # queries were eight round trips for one page.
    stats = db.fetch_one(
        """SELECT
             (SELECT COUNT(*) FROM Donor)                                  AS donors,
             (SELECT COUNT(*) FROM Hospital)                               AS hospitals,
             (SELECT COUNT(*) FROM Donation)                               AS donations,
             (SELECT COALESCE(SUM(Qty),0) FROM Donation)                   AS donated_units,
             (SELECT COUNT(*) FROM Blood_Request WHERE Status='Pending')   AS pending,
             (SELECT COUNT(*) FROM Blood_Request WHERE Status='Approved')  AS approved,
             (SELECT COUNT(*) FROM Blood_Request WHERE Status='Completed') AS completed,
             (SELECT COUNT(*) FROM Blood_Request WHERE Status='Rejected')  AS rejected"""
    ) or {}

    stock = db.fetch_all(
        """SELECT bg.Blood_Type, s.Total_Qty, s.Last_Updated
           FROM Blood_Stock s
           JOIN Blood_Group bg ON s.Bg_ID = bg.Bg_ID
           ORDER BY bg.Bg_ID"""
    )

    recent_donations = db.fetch_all(
        """SELECT dn.Donation_ID, d.Name AS donor, bg.Blood_Type,
                  dn.Donation_Date, dn.Qty
           FROM Donation dn
           JOIN Donor d       ON dn.Donor_ID = d.Donor_ID
           JOIN Blood_Group bg ON dn.Bg_ID = bg.Bg_ID
           ORDER BY dn.Donation_Date DESC, dn.Donation_ID DESC
           LIMIT 5"""
    )

    recent_requests = db.fetch_all(
        """SELECT br.Request_ID, h.Name AS hospital, bg.Blood_Type,
                  br.Qty, br.Status, br.Date
           FROM Blood_Request br
           JOIN Hospital h     ON br.Hospital_ID = h.Hospital_ID
           JOIN Blood_Group bg ON br.Bg_ID = bg.Bg_ID
           ORDER BY br.Request_ID DESC
           LIMIT 5"""
    )

    return render_template(
        "dashboard.html",
        stats=stats,
        stock=stock,
        recent_donations=recent_donations,
        recent_requests=recent_requests,
        low_threshold=LOW_STOCK_THRESHOLD,
    )


# --------------------------------------------------------------------------- #
# Donors
# --------------------------------------------------------------------------- #
@app.route("/donors")
def donors():
    q = request.args.get("q", "").strip()
    bg = request.args.get("bg", "").strip()

    sql = """SELECT d.Donor_ID, d.Name, d.Age, d.Gender, d.Phone,
                    d.Bg_ID, bg.Blood_Type,
                    COUNT(dn.Donation_ID)      AS donation_count,
                    COALESCE(SUM(dn.Qty), 0)   AS total_donated
             FROM Donor d
             JOIN Blood_Group bg ON d.Bg_ID = bg.Bg_ID
             LEFT JOIN Donation dn ON d.Donor_ID = dn.Donor_ID
             WHERE 1 = 1"""
    params = []
    if q:
        sql += " AND (d.Name LIKE %s OR d.Phone LIKE %s)"
        like = f"%{q}%"
        params.extend([like, like])
    if bg:
        sql += " AND d.Bg_ID = %s"
        params.append(bg)
    sql += " GROUP BY d.Donor_ID ORDER BY d.Donor_ID"

    return render_template(
        "donors.html",
        donors=db.fetch_all(sql, params),
        blood_groups=blood_groups(),
        q=q,
        selected_bg=bg,
    )


@app.route("/donors/new", methods=["GET", "POST"])
def donor_new():
    if request.method == "POST":
        try:
            name = as_name(request.form.get("name"), "Name", 100)
            age = as_int(request.form.get("age"), "Age", 18, 65)
            gender = require_choice(request.form.get("gender"), GENDERS, "Gender")
            phone = as_phone(request.form.get("phone"))
            bg_id = as_int(request.form.get("bg_id"), "Blood group")
            if not exists_in("Blood_Group", "Bg_ID", bg_id):
                raise ValidationError("Selected blood group does not exist.")
        except ValidationError as err:
            flash(str(err), "error")
            return render_template("donor_form.html", donor=donor_form_state(request.form), blood_groups=blood_groups())

        try:
            db.execute(
                "INSERT INTO Donor (Name, Age, Gender, Phone, Bg_ID) VALUES (%s, %s, %s, %s, %s)",
                (name, age, gender, phone, bg_id),
            )
            flash(f"Donor '{name}' added successfully.", "success")
            return redirect(url_for("donors"))
        except Error as err:
            if err.errno == 1062:  # duplicate phone
                flash("A donor with this phone number already exists.", "error")
            else:
                flash("Could not save the donor. Please check the values and try again.", "error")
            return render_template("donor_form.html", donor=donor_form_state(request.form), blood_groups=blood_groups())

    return render_template("donor_form.html", donor=None, blood_groups=blood_groups())


@app.route("/donors/<int:donor_id>/edit", methods=["GET", "POST"])
def donor_edit(donor_id):
    donor = db.fetch_one("SELECT * FROM Donor WHERE Donor_ID = %s", (donor_id,))
    if donor is None:
        abort(404)

    if request.method == "POST":
        try:
            name = as_name(request.form.get("name"), "Name", 100)
            age = as_int(request.form.get("age"), "Age", 18, 65)
            gender = require_choice(request.form.get("gender"), GENDERS, "Gender")
            phone = as_phone(request.form.get("phone"))
            bg_id = as_int(request.form.get("bg_id"), "Blood group")
            if not exists_in("Blood_Group", "Bg_ID", bg_id):
                raise ValidationError("Selected blood group does not exist.")
        except ValidationError as err:
            flash(str(err), "error")
            return render_template("donor_form.html", donor=donor_form_state(request.form), blood_groups=blood_groups(), donor_id=donor_id)

        try:
            db.execute(
                """UPDATE Donor SET Name=%s, Age=%s, Gender=%s, Phone=%s, Bg_ID=%s
                   WHERE Donor_ID=%s""",
                (name, age, gender, phone, bg_id, donor_id),
            )
            flash("Donor updated successfully.", "success")
            return redirect(url_for("donors"))
        except Error as err:
            if err.errno == 1062:
                flash("Another donor already uses this phone number.", "error")
            else:
                flash("Could not update the donor.", "error")
            return render_template("donor_form.html", donor=donor_form_state(request.form), blood_groups=blood_groups(), donor_id=donor_id)

    return render_template("donor_form.html", donor=donor, blood_groups=blood_groups(), donor_id=donor_id)


@app.route("/donors/<int:donor_id>/delete", methods=["POST"])
def donor_delete(donor_id):
    try:
        with db.get_cursor(commit=True) as cur:
            cur.execute("DELETE FROM Donor WHERE Donor_ID = %s", (donor_id,))
            if cur.rowcount == 0:
                flash("Donor not found.", "error")
                return redirect(url_for("donors"))
        flash("Donor deleted.", "success")
    except Error as err:
        # 1217 / 1451 = a foreign key still references this row (donation history).
        if err.errno in (1217, 1451):
            flash("This donor has donation records and cannot be deleted (history is preserved).", "error")
        else:
            flash("Could not delete the donor.", "error")
    return redirect(url_for("donors"))


@app.route("/donors/<int:donor_id>/donations")
def donor_donations(donor_id):
    donor = db.fetch_one(
        """SELECT d.*, bg.Blood_Type
           FROM Donor d JOIN Blood_Group bg ON d.Bg_ID = bg.Bg_ID
           WHERE d.Donor_ID = %s""",
        (donor_id,),
    )
    if donor is None:
        abort(404)
    history = db.fetch_all(
        """SELECT dn.Donation_ID, dn.Donation_Date, dn.Qty, bg.Blood_Type
           FROM Donation dn JOIN Blood_Group bg ON dn.Bg_ID = bg.Bg_ID
           WHERE dn.Donor_ID = %s
           ORDER BY dn.Donation_Date DESC, dn.Donation_ID DESC""",
        (donor_id,),
    )
    total = sum((row["Qty"] for row in history), Decimal("0"))
    return render_template("donations.html", donor=donor, history=history, total=total,
                           donors=[], blood_groups=[], today=date.today().isoformat(),
                           filters={"donor_id": str(donor_id)}, readonly=True)


# --------------------------------------------------------------------------- #
# Donations
# --------------------------------------------------------------------------- #
@app.route("/donations", methods=["GET", "POST"])
def donations():
    if request.method == "POST":
        return _create_donation()

    donor_id = request.args.get("donor_id", "").strip()
    bg = request.args.get("bg", "").strip()
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()

    sql = """SELECT dn.Donation_ID, dn.Donation_Date, dn.Qty,
                    d.Donor_ID, d.Name AS donor, bg.Blood_Type
             FROM Donation dn
             JOIN Donor d       ON dn.Donor_ID = d.Donor_ID
             JOIN Blood_Group bg ON dn.Bg_ID = bg.Bg_ID
             WHERE 1 = 1"""
    params = []
    if donor_id:
        sql += " AND dn.Donor_ID = %s"
        params.append(donor_id)
    if bg:
        sql += " AND dn.Bg_ID = %s"
        params.append(bg)
    if date_from:
        sql += " AND dn.Donation_Date >= %s"
        params.append(date_from)
    if date_to:
        sql += " AND dn.Donation_Date <= %s"
        params.append(date_to)
    sql += " ORDER BY dn.Donation_Date DESC, dn.Donation_ID DESC"

    history = db.fetch_all(sql, params)
    total = sum((row["Qty"] for row in history), Decimal("0"))

    donor_list = db.fetch_all(
        """SELECT d.Donor_ID, d.Name, d.Bg_ID, bg.Blood_Type
           FROM Donor d JOIN Blood_Group bg ON d.Bg_ID = bg.Bg_ID
           ORDER BY d.Name"""
    )

    return render_template(
        "donations.html",
        history=history,
        total=total,
        donors=donor_list,
        blood_groups=blood_groups(),
        today=date.today().isoformat(),
        filters={"donor_id": donor_id, "bg": bg, "date_from": date_from, "date_to": date_to},
        readonly=False,
    )


def _create_donation():
    """Handle the donation registration form (POST)."""
    def back():
        return redirect(url_for("donations"))

    try:
        donor_id = as_int(request.form.get("donor_id"), "Donor")
        bg_id = as_int(request.form.get("bg_id"), "Blood group")
        donation_date = as_date(request.form.get("donation_date"), "Donation date")
        qty = as_positive_decimal(request.form.get("qty"), "Quantity")
    except ValidationError as err:
        flash(str(err), "error")
        return back()

    try:
        with db.transaction() as cur:
            # 1) Confirm the donor exists and fetch the REGISTERED blood group.
            cur.execute("SELECT Donor_ID, Bg_ID FROM Donor WHERE Donor_ID = %s", (donor_id,))
            donor = cur.fetchone()
            if donor is None:
                raise ValidationError("Selected donor does not exist.")

            # 2) The donation blood group must match the donor's registered group.
            if donor["Bg_ID"] != bg_id:
                raise ValidationError("The selected blood group does not match the donor's registered blood group.")

            # 3) Insert the donation.
            cur.execute(
                """INSERT INTO Donation (Donor_ID, Bg_ID, Donation_Date, Qty)
                   VALUES (%s, %s, %s, %s)""",
                (donor_id, bg_id, donation_date, qty),
            )

            # 4) Increase stock for that blood group and refresh the timestamp.
            cur.execute(
                """UPDATE Blood_Stock
                   SET Total_Qty = Total_Qty + %s, Last_Updated = NOW()
                   WHERE Bg_ID = %s""",
                (qty, bg_id),
            )
            if cur.rowcount != 1:
                raise ValidationError("No stock record exists for the selected blood group.")
        # 5) transaction() commits both changes together, or rolls back on error.

        flash("Donation recorded and blood stock updated.", "success")
    except ValidationError as err:
        flash(str(err), "error")
    except Error:
        flash("Could not record the donation due to a database error.", "error")
    return back()


# --------------------------------------------------------------------------- #
# Blood stock
# --------------------------------------------------------------------------- #
@app.route("/stock")
def stock():
    rows = db.fetch_all(
        """SELECT bg.Blood_Type, s.Total_Qty, s.Last_Updated
           FROM Blood_Stock s
           JOIN Blood_Group bg ON s.Bg_ID = bg.Bg_ID
           ORDER BY bg.Bg_ID"""
    )
    total = sum((row["Total_Qty"] for row in rows), Decimal("0"))
    return render_template("stock.html", stock=rows, total=total, low_threshold=LOW_STOCK_THRESHOLD)


# --------------------------------------------------------------------------- #
# Hospitals
# --------------------------------------------------------------------------- #
@app.route("/hospitals")
def hospitals_list():
    q = request.args.get("q", "").strip()
    sql = """SELECT h.Hospital_ID, h.Name, h.Address, h.Phone,
                    COUNT(br.Request_ID) AS request_count
             FROM Hospital h
             LEFT JOIN Blood_Request br ON h.Hospital_ID = br.Hospital_ID
             WHERE 1 = 1"""
    params = []
    if q:
        sql += " AND (h.Name LIKE %s OR h.Phone LIKE %s)"
        like = f"%{q}%"
        params.extend([like, like])
    sql += " GROUP BY h.Hospital_ID ORDER BY h.Hospital_ID"
    return render_template("hospitals.html", hospitals=db.fetch_all(sql, params), q=q)


@app.route("/hospitals/new", methods=["GET", "POST"])
def hospital_new():
    if request.method == "POST":
        try:
            name = as_name(request.form.get("name"), "Name", 150)
            address = str(request.form.get("address") or "").strip()
            phone = as_phone(request.form.get("phone"))
        except ValidationError as err:
            flash(str(err), "error")
            return render_template("hospital_form.html", hospital=hospital_form_state(request.form), hospital_id=None)

        try:
            db.execute(
                "INSERT INTO Hospital (Name, Address, Phone) VALUES (%s, %s, %s)",
                (name, address or None, phone),
            )
            flash(f"Hospital '{name}' added successfully.", "success")
            return redirect(url_for("hospitals_list"))
        except Error as err:
            if err.errno == 1062:
                flash("A hospital with this phone number already exists.", "error")
            else:
                flash("Could not save the hospital.", "error")
            return render_template("hospital_form.html", hospital=hospital_form_state(request.form), hospital_id=None)

    return render_template("hospital_form.html", hospital=None, hospital_id=None)


@app.route("/hospitals/<int:hospital_id>/edit", methods=["GET", "POST"])
def hospital_edit(hospital_id):
    hospital = db.fetch_one("SELECT * FROM Hospital WHERE Hospital_ID = %s", (hospital_id,))
    if hospital is None:
        abort(404)

    if request.method == "POST":
        try:
            name = as_name(request.form.get("name"), "Name", 150)
            address = str(request.form.get("address") or "").strip()
            phone = as_phone(request.form.get("phone"))
        except ValidationError as err:
            flash(str(err), "error")
            return render_template("hospital_form.html", hospital=hospital_form_state(request.form), hospital_id=hospital_id)

        try:
            db.execute(
                "UPDATE Hospital SET Name=%s, Address=%s, Phone=%s WHERE Hospital_ID=%s",
                (name, address or None, phone, hospital_id),
            )
            flash("Hospital updated successfully.", "success")
            return redirect(url_for("hospitals_list"))
        except Error as err:
            if err.errno == 1062:
                flash("Another hospital already uses this phone number.", "error")
            else:
                flash("Could not update the hospital.", "error")
            return render_template("hospital_form.html", hospital=hospital_form_state(request.form), hospital_id=hospital_id)

    return render_template("hospital_form.html", hospital=hospital, hospital_id=hospital_id)


@app.route("/hospitals/<int:hospital_id>/delete", methods=["POST"])
def hospital_delete(hospital_id):
    try:
        with db.get_cursor(commit=True) as cur:
            cur.execute("DELETE FROM Hospital WHERE Hospital_ID = %s", (hospital_id,))
            if cur.rowcount == 0:
                flash("Hospital not found.", "error")
                return redirect(url_for("hospitals_list"))
        flash("Hospital deleted.", "success")
    except Error as err:
        # 1217 / 1451 = a foreign key still references this row (request history).
        if err.errno in (1217, 1451):
            flash("This hospital has request records and cannot be deleted (history is preserved).", "error")
        else:
            flash("Could not delete the hospital.", "error")
    return redirect(url_for("hospitals_list"))


@app.route("/hospitals/<int:hospital_id>/requests")
def hospital_requests(hospital_id):
    hospital = db.fetch_one("SELECT * FROM Hospital WHERE Hospital_ID = %s", (hospital_id,))
    if hospital is None:
        abort(404)
    history = db.fetch_all(
        """SELECT br.Request_ID, bg.Blood_Type, br.Date, br.Qty, br.Status
           FROM Blood_Request br
           JOIN Blood_Group bg ON br.Bg_ID = bg.Bg_ID
           WHERE br.Hospital_ID = %s
           ORDER BY br.Request_ID DESC""",
        (hospital_id,),
    )
    return render_template("requests.html", hospital=hospital, requests=history)


# --------------------------------------------------------------------------- #
# Blood requests
# --------------------------------------------------------------------------- #
@app.route("/requests", methods=["GET", "POST"])
def requests_view():
    if request.method == "POST":
        return _create_request()

    hospital_id = request.args.get("hospital_id", "").strip()
    bg = request.args.get("bg", "").strip()
    status = request.args.get("status", "").strip()
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()

    sql = """SELECT br.Request_ID, br.Date, br.Qty, br.Status,
                    h.Hospital_ID, h.Name AS hospital, bg.Blood_Type
             FROM Blood_Request br
             JOIN Hospital h     ON br.Hospital_ID = h.Hospital_ID
             JOIN Blood_Group bg ON br.Bg_ID = bg.Bg_ID
             WHERE 1 = 1"""
    params = []
    if hospital_id:
        sql += " AND br.Hospital_ID = %s"
        params.append(hospital_id)
    if bg:
        sql += " AND br.Bg_ID = %s"
        params.append(bg)
    if status:
        sql += " AND br.Status = %s"
        params.append(status)
    if date_from:
        sql += " AND br.Date >= %s"
        params.append(date_from)
    if date_to:
        sql += " AND br.Date <= %s"
        params.append(date_to)
    sql += " ORDER BY br.Request_ID DESC"

    return render_template(
        "requests.html",
        requests=db.fetch_all(sql, params),
        hospitals=hospitals(),
        blood_groups=blood_groups(),
        statuses=REQUEST_STATUSES,
        today=date.today().isoformat(),
        filters={"hospital_id": hospital_id, "bg": bg, "status": status,
                 "date_from": date_from, "date_to": date_to},
        hospital=None,
    )


def _create_request():
    try:
        hospital_id = as_int(request.form.get("hospital_id"), "Hospital")
        bg_id = as_int(request.form.get("bg_id"), "Blood group")
        req_date = as_date(request.form.get("date"), "Request date")
        qty = as_positive_decimal(request.form.get("qty"), "Quantity")
        if not exists_in("Hospital", "Hospital_ID", hospital_id):
            raise ValidationError("Selected hospital does not exist.")
        if not exists_in("Blood_Group", "Bg_ID", bg_id):
            raise ValidationError("Selected blood group does not exist.")
    except ValidationError as err:
        flash(str(err), "error")
        return redirect(url_for("requests_view"))

    try:
        # A new request starts as Pending. NO stock is deducted at this stage.
        db.execute(
            """INSERT INTO Blood_Request (Hospital_ID, Bg_ID, Date, Qty, Status)
               VALUES (%s, %s, %s, %s, 'Pending')""",
            (hospital_id, bg_id, req_date, qty),
        )
        flash("Blood request created with status Pending.", "success")
    except Error:
        flash("Could not create the request.", "error")
    return redirect(url_for("requests_view"))


def _set_status(request_id, new_status, from_statuses):
    """Guarded status update: only changes the row if its status is allowed."""
    with db.get_cursor(commit=True) as cur:
        cur.execute(
            f"UPDATE Blood_Request SET Status = %s WHERE Request_ID = %s AND Status IN ({','.join(['%s'] * len(from_statuses))})",
            (new_status, request_id, *from_statuses),
        )
        return cur.rowcount


@app.route("/requests/<int:request_id>/approve", methods=["POST"])
def request_approve(request_id):
    try:
        changed = _set_status(request_id, "Approved", ("Pending",))
        flash("Request approved." if changed else "Only Pending requests can be approved.", "success" if changed else "error")
    except Error:
        flash("Could not approve the request.", "error")
    return redirect(url_for("requests_view"))


@app.route("/requests/<int:request_id>/reject", methods=["POST"])
def request_reject(request_id):
    try:
        changed = _set_status(request_id, "Rejected", ("Pending",))
        flash("Request rejected." if changed else "Only Pending requests can be rejected.", "success" if changed else "error")
    except Error:
        flash("Could not reject the request.", "error")
    return redirect(url_for("requests_view"))


@app.route("/requests/<int:request_id>/fulfill", methods=["POST"])
def request_fulfill(request_id):
    """Fulfil a Pending/Approved request atomically.

    Locks the stock row, checks availability, deducts stock and marks the
    request Completed in a SINGLE transaction. The request row is also locked
    so the same request cannot be fulfilled twice.
    """
    try:
        with db.transaction() as cur:
            # 1) Lock and re-read the request (prevents double fulfilment).
            cur.execute(
                "SELECT Request_ID, Bg_ID, Qty, Status FROM Blood_Request WHERE Request_ID = %s FOR UPDATE",
                (request_id,),
            )
            req = cur.fetchone()
            if req is None:
                raise ValidationError("Request not found.")
            if req["Status"] not in ("Pending", "Approved"):
                raise ValidationError(f"This request is already '{req['Status']}' and cannot be fulfilled again.")

            qty = req["Qty"]
            bg_id = req["Bg_ID"]

            # 2) Lock the stock row for that blood group.
            cur.execute("SELECT Total_Qty FROM Blood_Stock WHERE Bg_ID = %s FOR UPDATE", (bg_id,))
            stock = cur.fetchone()
            if stock is None:
                raise ValidationError("No stock record found for this blood group.")

            # 3) Verify sufficient stock (never go negative).
            if stock["Total_Qty"] < qty:
                raise ValidationError(
                    f"Insufficient stock: {stock['Total_Qty']} units available, {qty} units requested."
                )

            # 4) Deduct stock and refresh the timestamp.
            cur.execute(
                """UPDATE Blood_Stock
                   SET Total_Qty = Total_Qty - %s, Last_Updated = NOW()
                   WHERE Bg_ID = %s""",
                (qty, bg_id),
            )

            # 5) Mark the request Completed.
            cur.execute("UPDATE Blood_Request SET Status = 'Completed' WHERE Request_ID = %s", (request_id,))

        flash("Request fulfilled and stock deducted.", "success")
    except ValidationError as err:
        flash(str(err), "error")
    except Error:
        flash("Could not fulfil the request due to a database error.", "error")
    return redirect(url_for("requests_view"))


# --------------------------------------------------------------------------- #
# Database reports
# --------------------------------------------------------------------------- #
@app.route("/reports")
def reports():
    donors_by_group = db.fetch_all(
        """SELECT bg.Blood_Type, COUNT(d.Donor_ID) AS donor_count
           FROM Blood_Group bg
           LEFT JOIN Donor d ON bg.Bg_ID = d.Bg_ID
           GROUP BY bg.Bg_ID, bg.Blood_Type
           ORDER BY bg.Bg_ID"""
    )

    donations_by_donor = db.fetch_all(
        """SELECT d.Name AS donor, bg.Blood_Type,
                  COUNT(dn.Donation_ID) AS donations,
                  COALESCE(SUM(dn.Qty), 0) AS total_units
           FROM Donor d
           JOIN Blood_Group bg ON d.Bg_ID = bg.Bg_ID
           LEFT JOIN Donation dn ON d.Donor_ID = dn.Donor_ID
           GROUP BY d.Donor_ID, d.Name, bg.Blood_Type
           HAVING donations > 0
           ORDER BY total_units DESC, d.Name"""
    )

    stock_summary = db.fetch_all(
        """SELECT bg.Blood_Type, s.Total_Qty, s.Last_Updated
           FROM Blood_Stock s
           JOIN Blood_Group bg ON s.Bg_ID = bg.Bg_ID
           ORDER BY s.Total_Qty DESC"""
    )

    pending_by_hospital = db.fetch_all(
        """SELECT h.Name AS hospital, COUNT(br.Request_ID) AS pending_requests,
                  COALESCE(SUM(br.Qty), 0) AS pending_units
           FROM Hospital h
           LEFT JOIN Blood_Request br
                  ON h.Hospital_ID = br.Hospital_ID AND br.Status = 'Pending'
           GROUP BY h.Hospital_ID, h.Name
           ORDER BY pending_requests DESC, h.Name"""
    )

    requests_by_status = db.fetch_all(
        """SELECT Status, COUNT(*) AS request_count
           FROM Blood_Request
           GROUP BY Status
           ORDER BY request_count DESC"""
    )

    monthly_donations = db.fetch_all(
        """SELECT DATE_FORMAT(Donation_Date, '%Y-%m') AS month,
                  COUNT(*) AS donations,
                  SUM(Qty) AS total_units
           FROM Donation
           GROUP BY DATE_FORMAT(Donation_Date, '%Y-%m')
           ORDER BY month"""
    )

    low_stock = db.fetch_all(
        """SELECT bg.Blood_Type, s.Total_Qty
           FROM Blood_Stock s
           JOIN Blood_Group bg ON s.Bg_ID = bg.Bg_ID
           WHERE s.Total_Qty < %s
           ORDER BY s.Total_Qty ASC""",
        (LOW_STOCK_THRESHOLD,),
    )

    most_requested = db.fetch_all(
        """SELECT bg.Blood_Type,
                  COUNT(br.Request_ID) AS times_requested,
                  COALESCE(SUM(br.Qty), 0) AS total_requested
           FROM Blood_Group bg
           LEFT JOIN Blood_Request br ON bg.Bg_ID = br.Bg_ID
           GROUP BY bg.Bg_ID, bg.Blood_Type
           ORDER BY total_requested DESC, bg.Blood_Type"""
    )

    return render_template(
        "reports.html",
        donors_by_group=donors_by_group,
        donations_by_donor=donations_by_donor,
        stock_summary=stock_summary,
        pending_by_hospital=pending_by_hospital,
        requests_by_status=requests_by_status,
        monthly_donations=monthly_donations,
        low_stock=low_stock,
        most_requested=most_requested,
        low_threshold=LOW_STOCK_THRESHOLD,
    )


# --------------------------------------------------------------------------- #
# Admin approval workflow
# ---------------------------------------------------------------------------
# One shared password, read from the environment on every request (never from
# source code, templates or JavaScript):
#   ADMIN_PASSWORD_HASH -- a Werkzeug password hash (recommended), generated
#                          locally with scripts/generate_admin_hash.py
#   ADMIN_PASSWORD      -- plain-text fallback when hashing is impractical
# If neither is set, admin access is denied with a configuration error.
#
# The pending changes shown on the admin page are the application's existing
# proposal workflow: Blood_Request rows created with Status='Pending'. They are
# already stored separately from approved/live data (Approved / Completed /
# Rejected rows and Blood_Stock), so approving or rejecting them here never
# rewrites historical records. Every decision is written to Admin_Audit_Log
# inside the same transaction as the status change.
# --------------------------------------------------------------------------- #
ADMIN_SESSION_FLAG = "admin_logged_in"

# Brute-force protection: failed logins are counted per client address inside
# a sliding window. In-memory is deliberate (no extra dependency); it resets on
# process restart, which is acceptable for a shared-password admin door.
LOGIN_WINDOW_SECONDS = int(os.environ.get("ADMIN_LOGIN_WINDOW_SECONDS", "300"))
LOGIN_MAX_ATTEMPTS = int(os.environ.get("ADMIN_LOGIN_MAX_ATTEMPTS", "5"))

_login_attempts = {}  # client address -> [monotonic timestamps of failures]


def _failed_logins(ip):
    """Prune and return the recent failed-login timestamps for `ip`."""
    now = time.monotonic()
    recent = [t for t in _login_attempts.get(ip, []) if now - t < LOGIN_WINDOW_SECONDS]
    if recent:
        _login_attempts[ip] = recent
    else:
        _login_attempts.pop(ip, None)
    return recent


def _login_is_limited(ip):
    return len(_failed_logins(ip)) >= LOGIN_MAX_ATTEMPTS


def _record_failed_login(ip):
    _login_attempts.setdefault(ip, []).append(time.monotonic())


def reset_login_attempts():
    """Forget recorded failures (called after a successful login; also used by tests)."""
    _login_attempts.clear()


def admin_secret_key_ok():
    """True when a strong, private SECRET_KEY is configured.

    The signing key must exist, be reasonably long, and not be the well-known
    development placeholder. The value itself is never logged or displayed.
    """
    key = (os.environ.get("SECRET_KEY") or "").strip()
    return len(key) >= 32 and key != "dev-insecure-key-change-me"


def admin_password_hash():
    value = (os.environ.get("ADMIN_PASSWORD_HASH") or "").strip()
    return value or None


def admin_password_plain():
    value = (os.environ.get("ADMIN_PASSWORD") or "").strip()
    return value or None


def admin_config_problem():
    """Return a secret-free description of missing admin configuration, or None."""
    problems = []
    if not admin_secret_key_ok():
        problems.append(
            "SECRET_KEY is missing or too weak (set a random string of at least 32 characters)."
        )
    if not admin_password_hash() and not admin_password_plain():
        problems.append(
            "ADMIN_PASSWORD_HASH (recommended) or ADMIN_PASSWORD is not set."
        )
    return " ".join(problems) or None


def admin_password_matches(candidate):
    """Constant-time verification of the shared admin password."""
    pw_hash = admin_password_hash()
    if pw_hash:
        try:
            return check_password_hash(pw_hash, candidate)
        except Exception:
            # Malformed hash in the environment: treat as a failed check and
            # log only the fact (never the value).
            app.logger.error("ADMIN_PASSWORD_HASH is not a valid password hash.")
            return False
    plain = admin_password_plain()
    if plain is not None:
        return secrets.compare_digest(plain.encode("utf-8"), candidate.encode("utf-8"))
    return False


def _admin_config_error():
    """Render the safe configuration error page (503) for missing admin setup."""
    return render_template(
        "error.html",
        message="Admin access is not configured on this server: " + str(admin_config_problem()),
    ), 503


def require_admin(view):
    """Protect a route: valid configuration + authenticated admin session.

    Configuration and authentication are both decided SERVER-SIDE from the
    signed session cookie; no form field, query string, cookie value or client
    script can grant admin access.
    """

    @wraps(view)
    def wrapped(*args, **kwargs):
        if admin_config_problem():
            return _admin_config_error()
        if not session.get(ADMIN_SESSION_FLAG):
            if request.method == "GET":
                return redirect(url_for("admin_login"))
            # State-changing endpoints fail loudly instead of redirecting.
            abort(401, description="Admin authentication required.")
        return view(*args, **kwargs)

    return wrapped


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if admin_config_problem():
        return _admin_config_error()
    if session.get(ADMIN_SESSION_FLAG):
        return redirect(url_for("admin_pending"))

    if request.method == "POST":
        ip = request.remote_addr or "unknown"
        if _login_is_limited(ip):
            return render_template(
                "error.html",
                message="Too many failed admin login attempts. Please wait a few minutes and try again.",
            ), 429

        password = request.form.get("password", "")
        if admin_password_matches(password):
            reset_login_attempts()
            # Drop anything carried in the pre-login session, then start a
            # fresh, permanent (time-limited) session for the admin.
            session.clear()
            session.permanent = True
            session[ADMIN_SESSION_FLAG] = True
            try:
                ensure_admin_tables()
            except Error:
                app.logger.warning("Could not prepare admin audit tables.")
            flash("Admin login successful.", "success")
            return redirect(url_for("admin_pending"))

        _record_failed_login(ip)
        # Log the event and the client address only -- never the password.
        app.logger.warning("Failed admin login attempt from %s.", ip)
        flash("Incorrect admin password.", "error")
        return render_template("admin_login.html"), 401

    return render_template("admin_login.html")


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    # CSRF is enforced for this POST by the global before_request hook.
    session.clear()
    flash("Admin session ended.", "success")
    return redirect(url_for("dashboard"))


@app.route("/admin")
@require_admin
def admin_pending():
    """List pending changes (proposed blood requests) awaiting a decision."""
    pending = db.fetch_all(
        """SELECT br.Request_ID, br.Date, br.Qty, br.Status,
                  h.Name AS hospital, bg.Blood_Type
           FROM Blood_Request br
           JOIN Hospital h      ON br.Hospital_ID = h.Hospital_ID
           JOIN Blood_Group bg  ON br.Bg_ID = bg.Bg_ID
           WHERE br.Status = 'Pending'
           ORDER BY br.Request_ID"""
    )

    decisions = []
    try:
        decisions = db.fetch_all(
            """SELECT Audit_ID, Proposal_Type, Proposal_ID, Action, Detail, Decided_At
               FROM Admin_Audit_Log
               ORDER BY Audit_ID DESC
               LIMIT 10"""
        )
    except Error:
        # Audit table not created yet (e.g. no DDL privilege): approvals still work.
        app.logger.warning("Admin_Audit_Log is not readable; hiding the audit list.")

    return render_template("admin_pending.html", pending=pending, decisions=decisions)


_admin_tables_ready = None  # None = unknown, True = ensured, False = cannot create


def ensure_admin_tables():
    """Create the audit table once per process, when the DB user is allowed to.

    Degrades gracefully: if the MySQL user lacks the CREATE privilege (or the
    table is otherwise unavailable), approving and rejecting still work and the
    audit entry is skipped. Run admin_schema.sql once with an admin account to
    enable auditing (it is ADDITIVE only -- it never touches existing data).
    """
    global _admin_tables_ready
    if _admin_tables_ready is not None:
        return
    try:
        with db.get_cursor(commit=True) as cur:
            cur.execute(
                """CREATE TABLE IF NOT EXISTS Admin_Audit_Log (
                       Audit_ID      INT          NOT NULL AUTO_INCREMENT,
                       Proposal_Type VARCHAR(30)  NOT NULL,
                       Proposal_ID   INT          NOT NULL,
                       Action        ENUM('Approved','Rejected') NOT NULL,
                       Detail        VARCHAR(255) NULL,
                       Decided_At    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
                       PRIMARY KEY (Audit_ID),
                       KEY idx_audit_proposal (Proposal_Type, Proposal_ID)
                   ) ENGINE=InnoDB"""
            )
        _admin_tables_ready = True
    except Error:
        _admin_tables_ready = False
        app.logger.warning(
            "Admin_Audit_Log could not be created automatically (no DDL privilege?); "
            "run admin_schema.sql with an admin account to enable the audit log."
        )


def _record_audit(cur, proposal_id, action, detail):
    """Write one audit row using the transaction's own cursor."""
    cur.execute(
        """INSERT INTO Admin_Audit_Log (Proposal_Type, Proposal_ID, Action, Detail)
           VALUES (%s, %s, %s, %s)""",
        ("Blood_Request", proposal_id, action, detail),
    )


def _admin_decide(proposal_id, action):
    """Apply an admin decision to one pending proposal, atomically.

    A single transaction: lock the proposal row, re-check its status, update it
    and record the audit entry. Any failure rolls the WHOLE decision back, so
    the proposal stays Pending and can be retried; repeat submissions cannot
    double-apply because the row is locked and re-validated against 'Pending'.
    Stock is never touched by approval or rejection.
    """
    try:
        ensure_admin_tables()
        with db.transaction() as cur:
            # 1) Lock the proposal row: two admins clicking at once serialize here.
            cur.execute(
                "SELECT Request_ID, Status FROM Blood_Request WHERE Request_ID = %s FOR UPDATE",
                (proposal_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise ValidationError("Proposal not found.")
            if row["Status"] != "Pending":
                raise ValidationError(
                    f"Proposal #{proposal_id} has already been processed (status: {row['Status']})."
                )

            # 2) Human-readable detail for the audit log (read-only lookup).
            cur.execute(
                """SELECT h.Name AS hospital, bg.Blood_Type, br.Qty
                   FROM Blood_Request br
                   JOIN Hospital h      ON br.Hospital_ID = h.Hospital_ID
                   JOIN Blood_Group bg  ON br.Bg_ID = bg.Bg_ID
                   WHERE br.Request_ID = %s""",
                (proposal_id,),
            )
            info = cur.fetchone() or {}

            # 3) Guarded update: only Pending rows may change, only once.
            cur.execute(
                "UPDATE Blood_Request SET Status = %s WHERE Request_ID = %s AND Status = 'Pending'",
                (action, proposal_id),
            )
            if cur.rowcount != 1:
                raise ValidationError("Proposal was already processed; nothing changed.")

            # 4) Audit entry in the SAME transaction (rolls back with it).
            detail = None
            if info:
                detail = (
                    f"{info.get('hospital')} - {info.get('Qty')} units of {info.get('Blood_Type')}"
                )[:255]
            try:
                _record_audit(cur, proposal_id, action, detail)
            except Error as err:
                # 1146 = table missing, 1142 = INSERT denied: the audit log is
                # best-effort, so the decision still commits in those cases.
                if err.errno not in (1142, 1146):
                    raise
                app.logger.warning("Admin audit entry skipped (errno %s).", err.errno)

        verb = "approved" if action == "Approved" else "rejected"
        flash(f"Proposal #{proposal_id} {verb}.", "success")
    except ValidationError as err:
        flash(str(err), "error")
    except Error:
        # Includes a failure of the audit insert: the transaction rolled back,
        # so the proposal is untouched and no partial change is visible.
        app.logger.error("Admin %s of proposal %s failed; rolled back.", action, proposal_id)
        flash("Could not process the decision due to a database error. No changes were applied.", "error")
    return redirect(url_for("admin_pending"))


@app.route("/admin/proposals/<int:proposal_id>/approve", methods=["POST"])
@require_admin
def admin_proposal_approve(proposal_id):
    return _admin_decide(proposal_id, "Approved")


@app.route("/admin/proposals/<int:proposal_id>/reject", methods=["POST"])
@require_admin
def admin_proposal_reject(proposal_id):
    return _admin_decide(proposal_id, "Rejected")


# --------------------------------------------------------------------------- #
# Error handlers
# --------------------------------------------------------------------------- #
@app.errorhandler(Error)
def handle_db_error(err):
    app.logger.error("Database error: %s", err)
    return render_template("error.html",
                           message="A database error occurred. Please check that MySQL is running and configured correctly."), 500


@app.errorhandler(400)
def handle_bad_request(err):
    return render_template("error.html", message=err.description or "Bad request."), 400


@app.errorhandler(401)
def handle_unauthorized(err):
    return render_template("error.html", message=err.description or "Authentication required."), 401


@app.errorhandler(429)
def handle_rate_limited(err):
    return render_template("error.html", message=err.description or "Too many attempts. Please try again later."), 429


@app.errorhandler(404)
def handle_not_found(err):
    return render_template("error.html", message="The page you requested was not found."), 404


if __name__ == "__main__":
    # Local development server only. In production Render runs the app through
    # Gunicorn (see the Procfile), which imports `app` and binds to $PORT.
    # These defaults keep the local Ubuntu setup unchanged (127.0.0.1:5000).
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "1").strip().lower() in ("1", "true", "yes", "on")
    app.run(host=host, port=port, debug=debug)
