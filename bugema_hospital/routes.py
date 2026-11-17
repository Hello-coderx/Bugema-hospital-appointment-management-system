"""
routes.py — All application routes.
Roles: admin, staff, doctor, patient.
"""
import re
import logging
from datetime import datetime

from flask import (Blueprint, render_template, request, redirect,
                   url_for, session, flash, abort)
from pymongo.errors import PyMongoError, DuplicateKeyError
from bson import ObjectId

from extensions import (
    mongo, hash_password, check_password,
    login_required, role_required, safe_oid,
    validate_email, validate_date, validate_time,
    validate_dob, validate_appt_date, validate_status_change,
    validate_phone, sanitise, log_audit,
)

logger        = logging.getLogger(__name__)
main          = Blueprint("main", __name__)
STATUSES      = ["Pending", "Confirmed", "Completed", "Cancelled"]
ALLOWED_ROLES = ["admin", "staff", "doctor", "patient"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _patient_name(p):
    """Return the patient's display name regardless of which field was stored."""
    if not p:
        return "Unknown"
    return p.get("full_name") or p.get("name") or "Unknown"


def _enrich(appts):
    """Attach patient_name and doctor_name strings to each appointment dict."""
    db = mongo.db
    for a in appts:
        try:
            p = db.patients.find_one({"_id": a.get("patient_id")})
            d = db.doctors.find_one({"_id": a.get("doctor_id")})
            a["patient_name"] = _patient_name(p)
            a["doctor_name"]  = f"Dr. {d['name']}" if d else "Unknown"
        except PyMongoError:
            a.setdefault("patient_name", "Unknown")
            a.setdefault("doctor_name",  "Unknown")


def _admin_stats():
    """Return appointment and user counts for the admin dashboard."""
    db = mongo.db
    try:
        return {
            "total":     db.appointments.count_documents({}),
            "pending":   db.appointments.count_documents({"status": "Pending"}),
            "confirmed": db.appointments.count_documents({"status": "Confirmed"}),
            "completed": db.appointments.count_documents({"status": "Completed"}),
            "cancelled": db.appointments.count_documents({"status": "Cancelled"}),
            "patients":  db.patients.count_documents({}),
            "doctors":   db.doctors.count_documents({"active": True}),
            "users":     db.users.count_documents({}),
            "paid":      db.appointments.count_documents({"payment_status": "paid"}),
            "unpaid":    db.appointments.count_documents({"payment_status": "unpaid"}),
            "revenue":   sum(a.get("payment_amount", 0) for a in db.appointments.find({"payment_status": "paid"})),
        }
    except PyMongoError:
        return {k: 0 for k in
                ["total", "pending", "confirmed", "completed",
                 "cancelled", "patients", "doctors", "users", "paid", "unpaid", "revenue"]}


def _staff_stats():
    """Return appointment counts for the staff dashboard."""
    db    = mongo.db
    today = datetime.utcnow().strftime("%Y-%m-%d")
    try:
        return {
            "total":     db.appointments.count_documents({}),
            "pending":   db.appointments.count_documents({"status": "Pending"}),
            "confirmed": db.appointments.count_documents({"status": "Confirmed"}),
            "today":     db.appointments.count_documents({"date": today}),
        }
    except PyMongoError:
        return {"total": 0, "pending": 0, "confirmed": 0, "today": 0}


def _find_patient_by_user(db):
    """
    Find the patient record linked to the currently logged-in user.
    Tries both string and ObjectId forms of user_id to handle legacy records.
    """
    uid = session.get("user_id", "")
    p   = db.patients.find_one({"user_id": uid})
    if not p:
        try:
            p = db.patients.find_one({"user_id": ObjectId(uid)})
        except Exception:
            pass
    return p


# ── Auth ──────────────────────────────────────────────────────────────────────

@main.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("main.dashboard"))
    return redirect(url_for("main.login"))


@main.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("main.dashboard"))
    error = None
    if request.method == "POST":
        username = sanitise(request.form.get("username", ""), 100)
        password = request.form.get("password", "")
        if not username or not password:
            error = "Please enter both username and password."
        else:
            try:
                # Case-insensitive username match so Ahmed, ahmed, AHMED all work
                user = mongo.db.users.find_one({
                    "username": {"$regex": f"^{re.escape(username)}$", "$options": "i"},
                    "active": True,
                })
                if user and check_password(password, user.get("password_hash", "")):
                    session.clear()
                    session["user_id"]  = str(user["_id"])
                    session["username"] = user.get("username", "")
                    session["role"]     = user.get("role", "")
                    session["name"]     = user.get("full_name", username)
                    session.permanent   = True
                    return redirect(url_for("main.dashboard"))
                error = "Invalid username or password."
            except PyMongoError as e:
                logger.error("login: %s", e)
                error = "Database error. Please try again."
    return render_template("base.html", page="login", error=error)


@main.route("/logout")
def logout():
    session.clear()
    flash("Logged out successfully.", "info")
    return redirect(url_for("main.login"))


@main.route("/register", methods=["GET", "POST"])
def register():
    """Patient self-registration — creates both a user account and a patient profile."""
    if "user_id" in session:
        return redirect(url_for("main.dashboard"))
    error = None
    if request.method == "POST":
        full_name = sanitise(request.form.get("full_name", ""), 100)
        email     = sanitise(request.form.get("email", ""), 150).lower()
        phone     = sanitise(request.form.get("phone", ""), 20)
        username  = sanitise(request.form.get("username", ""), 50)
        password  = request.form.get("password", "")
        confirm   = request.form.get("confirm", "")
        dob       = sanitise(request.form.get("dob", ""), 10)
        gender    = sanitise(request.form.get("gender", ""), 10)
        address   = sanitise(request.form.get("address", ""), 200)

        if not all([full_name, email, phone, username, password, confirm]):
            error = "Full name, email, phone, username and password are all required."
        elif len(full_name) < 2:
            error = "Full name must be at least 2 characters."
        elif not validate_email(email):
            error = "Invalid email address."
        elif not validate_phone(phone):
            error = "Phone must be 7–15 digits (e.g. +256700123456)."
        elif len(username) < 3:
            error = "Username must be at least 3 characters."
        elif password != confirm:
            error = "Passwords do not match."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        else:
            if dob:
                ok, msg = validate_dob(dob)
                if not ok:
                    error = msg
            if not error:
                try:
                    db = mongo.db
                    if db.users.find_one({"username": username}):
                        error = "That username is already taken."
                    elif db.patients.find_one({"email": email}):
                        error = "An account with that email already exists."
                    else:
                        uid = db.users.insert_one({
                            "full_name":     full_name,
                            "username":      username,
                            "password_hash": hash_password(password),
                            "role":          "patient",
                            "active":        True,
                            "created_at":    datetime.utcnow(),
                        }).inserted_id
                        db.patients.insert_one({
                            "full_name":  full_name,
                            "email":      email,
                            "phone":      phone,
                            "dob":        dob,
                            "gender":     gender,
                            "address":    address,
                            "user_id":    str(uid),
                            "created_at": datetime.utcnow(),
                        })
                        flash("Registration successful. Please log in.", "success")
                        return redirect(url_for("main.login"))
                except DuplicateKeyError:
                    error = "That username or email is already taken."
                except PyMongoError as e:
                    logger.error("register: %s", e)
                    error = "Database error. Please try again."
    return render_template("base.html", page="register", error=error)


@main.route("/dashboard")
@login_required
def dashboard():
    dest = {
        "admin":   "main.admin_dashboard",
        "staff":   "main.staff_dashboard",
        "doctor":  "main.doctor_dashboard",
        "patient": "main.patient_dashboard",
    }.get(session.get("role"))
    if not dest:
        session.clear()
        flash("Unknown role. Contact the administrator.", "danger")
        return redirect(url_for("main.login"))
    return redirect(url_for(dest))


@main.route("/services")
@login_required
def services():
    return render_template("services.html")


# ── Admin — overview ──────────────────────────────────────────────────────────

@main.route("/admin")
@role_required("admin")
def admin_dashboard():
    try:
        db     = mongo.db
        recent = list(db.appointments.find().sort("created_at", -1).limit(6))
        _enrich(recent)
        return render_template("dashboard_admin.html", section="overview",
            stats=_admin_stats(), recent=recent,
            doctors=list(db.doctors.find().sort("name", 1)),
            users=list(db.users.find().sort("full_name", 1)),
            patients=list(db.patients.find().sort("full_name", 1)),
            statuses=STATUSES)
    except PyMongoError as e:
        logger.error("admin_dashboard: %s", e)
        flash("Could not load dashboard.", "danger")
        return render_template("dashboard_admin.html", section="overview",
            stats=_admin_stats(), recent=[], doctors=[],
            users=[], patients=[], statuses=STATUSES)


# ── Admin — appointments ──────────────────────────────────────────────────────

@main.route("/admin/appointments")
@role_required("admin")
def admin_appointments():
    try:
        db     = mongo.db
        status = sanitise(request.args.get("status", ""), 20)
        search = sanitise(request.args.get("search", ""), 100)
        if status and status not in STATUSES:
            status = ""
        appts = list(
            db.appointments.find({"status": status} if status else {}).sort("date", -1)
        )
        _enrich(appts)
        if search:
            sl    = search.lower()
            appts = [a for a in appts
                     if sl in a["patient_name"].lower() or sl in a["doctor_name"].lower()]
        return render_template("dashboard_admin.html", section="appointments",
            appointments=appts, status_filter=status, search=search,
            stats=_admin_stats(), statuses=STATUSES,
            doctors=list(db.doctors.find({"active": True}).sort("name", 1)),
            patients=list(db.patients.find().sort("full_name", 1)))
    except PyMongoError as e:
        logger.error("admin_appointments: %s", e)
        flash("Could not load appointments.", "danger")
        return redirect(url_for("main.admin_dashboard"))


@main.route("/admin/appointments/add", methods=["POST"])
@role_required("admin")
def admin_add_appointment():
    db     = mongo.db
    pid    = safe_oid(request.form.get("patient_id"))
    did    = safe_oid(request.form.get("doctor_id"))
    date   = sanitise(request.form.get("date", ""), 10)
    time   = sanitise(request.form.get("time", ""), 5)
    reason = sanitise(request.form.get("reason", ""), 500)
    status = sanitise(request.form.get("status", "Pending"), 20)

    ok_date, msg_date = validate_appt_date(date)
    ok_stat, msg_stat = validate_status_change(status, date)

    if not all([pid, did, date, time, reason]):
        flash("All fields are required.", "danger")
    elif not ok_date:
        flash(msg_date, "danger")
    elif not validate_time(time):
        flash("Invalid time format.", "danger")
    elif status not in STATUSES:
        flash("Invalid status.", "danger")
    elif not ok_stat:
        flash(msg_stat, "danger")
    else:
        try:
            clash = db.appointments.find_one({
                "doctor_id": did, "date": date, "time": time,
                "status": {"$nin": ["Cancelled"]},
            })
            if clash:
                flash("That doctor already has an appointment at that date and time.", "danger")
            else:
                db.appointments.insert_one({
                    "patient_id": pid, "doctor_id": did,
                    "date": date, "time": time, "reason": reason, "status": status,
                    "payment_status": "unpaid",
                    "payment_method": None,
                    "payment_amount": 50000,
                    "created_at": datetime.utcnow(), "updated_at": datetime.utcnow(),
                })
                log_audit("created_appointment", {
                    "patient_id": str(pid),
                    "doctor_id": str(did),
                    "date": date,
                    "time": time,
                })
                flash("Appointment booked.", "success")
        except PyMongoError as e:
            logger.error("admin_add_appointment: %s", e)
            flash("Database error.", "danger")
    return redirect(url_for("main.admin_appointments"))


@main.route("/admin/appointments/<appt_id>/edit", methods=["POST"])
@role_required("admin")
def admin_edit_appointment(appt_id):
    oid = safe_oid(appt_id)
    if not oid:
        flash("Invalid appointment ID.", "danger")
        return redirect(url_for("main.admin_appointments"))
    db     = mongo.db
    pid    = safe_oid(request.form.get("patient_id"))
    did    = safe_oid(request.form.get("doctor_id"))
    date   = sanitise(request.form.get("date", ""), 10)
    time   = sanitise(request.form.get("time", ""), 5)
    reason = sanitise(request.form.get("reason", ""), 500)
    status = sanitise(request.form.get("status", ""), 20)

    ok_stat, msg_stat = validate_status_change(status, date)

    if not all([pid, did, date, time, reason, status]):
        flash("All fields are required.", "danger")
    elif not validate_date(date):
        flash("Invalid date format.", "danger")
    elif not validate_time(time):
        flash("Invalid time format.", "danger")
    elif status not in STATUSES:
        flash("Invalid status.", "danger")
    elif not ok_stat:
        flash(msg_stat, "danger")
    else:
        try:
            clash = db.appointments.find_one({
                "_id": {"$ne": oid}, "doctor_id": did, "date": date, "time": time,
                "status": {"$nin": ["Cancelled"]},
            })
            if clash:
                flash("Doctor already booked at that date and time.", "danger")
            else:
                db.appointments.update_one({"_id": oid}, {"$set": {
                    "patient_id": pid, "doctor_id": did,
                    "date": date, "time": time, "reason": reason,
                    "status": status, "updated_at": datetime.utcnow(),
                }})
                flash("Appointment updated.", "success")
        except PyMongoError as e:
            logger.error("admin_edit_appointment: %s", e)
            flash("Database error.", "danger")
    return redirect(url_for("main.admin_appointments"))


@main.route("/admin/appointments/<appt_id>/delete", methods=["POST"])
@role_required("admin")
def admin_delete_appointment(appt_id):
    oid = safe_oid(appt_id)
    if not oid:
        flash("Invalid appointment ID.", "danger")
        return redirect(url_for("main.admin_appointments"))
    try:
        appt = mongo.db.appointments.find_one({"_id": oid})
        mongo.db.appointments.delete_one({"_id": oid})
        log_audit("deleted_appointment", {"appointment_id": str(oid), "patient": appt.get("patient_id")})
        flash("Appointment deleted.", "success")
    except PyMongoError as e:
        logger.error("admin_delete_appointment: %s", e)
        flash("Database error.", "danger")
    return redirect(url_for("main.admin_appointments"))


# ── Payment Recording ─────────────────────────────────────────────────────────────

@main.route("/admin/appointments/<appt_id>/payment", methods=["POST"])
@role_required("admin", "staff")
def record_payment(appt_id):
    """Record payment for an appointment (admin and staff only)."""
    oid = safe_oid(appt_id)
    if not oid:
        flash("Invalid appointment ID.", "danger")
        return redirect(url_for("main.admin_appointments"))

    payment_method = sanitise(request.form.get("payment_method", ""), 20)
    amount = request.form.get("amount", "")
    reference = sanitise(request.form.get("reference", ""), 50)

    if not payment_method or not amount:
        flash("Payment method and amount are required.", "danger")
        return redirect(url_for("main.admin_appointments"))

    try:
        amount_int = int(amount)
        if amount_int <= 0:
            flash("Amount must be greater than zero.", "danger")
            return redirect(url_for("main.admin_appointments"))

        appt = mongo.db.appointments.find_one({"_id": oid})
        if not appt:
            flash("Appointment not found.", "danger")
            return redirect(url_for("main.admin_appointments"))

        mongo.db.appointments.update_one(
            {"_id": oid},
            {"$set": {
                "payment_status": "paid",
                "payment_method": payment_method,
                "payment_amount": amount_int,
                "payment_reference": reference,
                "payment_date": datetime.utcnow().strftime("%Y-%m-%d"),
                "payment_recorded_by": session.get("user_id"),
                "updated_at": datetime.utcnow(),
            }}
        )

        log_audit("recorded_payment", {
            "appointment_id": str(oid),
            "amount": amount_int,
            "method": payment_method,
            "reference": reference,
        })

        flash(f"Payment of UGX {amount_int:,} recorded via {payment_method}.", "success")

    except ValueError:
        flash("Invalid amount format.", "danger")
    except PyMongoError as e:
        logger.error("record_payment: %s", e)
        flash("Database error.", "danger")

    dest = "main.admin_appointments" if session.get("role") == "admin" else "main.staff_appointments"
    return redirect(url_for(dest))


@main.route("/staff/appointments/<appt_id>/payment", methods=["POST"])
@role_required("staff")
def staff_record_payment(appt_id):
    """Staff route to record payment - redirects to shared function."""
    return record_payment(appt_id)


# ── Admin — patients ──────────────────────────────────────────────────────────

@main.route("/admin/patients")
@role_required("admin")
def admin_patients():
    try:
        return render_template("dashboard_admin.html", section="patients",
            stats=_admin_stats(),
            patients=list(mongo.db.patients.find().sort("full_name", 1)))
    except PyMongoError as e:
        logger.error("admin_patients: %s", e)
        flash("Could not load patients.", "danger")
        return redirect(url_for("main.admin_dashboard"))


@main.route("/admin/patients/add", methods=["POST"])
@role_required("admin", "staff")
def admin_add_patient():
    """Register a new patient and create their login account. Used by both admin and staff."""
    db        = mongo.db
    full_name = sanitise(request.form.get("full_name", ""), 100)
    email     = sanitise(request.form.get("email", ""), 150).lower()
    phone     = sanitise(request.form.get("phone", ""), 20)
    dob       = sanitise(request.form.get("dob", ""), 10)
    gender    = sanitise(request.form.get("gender", ""), 10)
    address   = sanitise(request.form.get("address", ""), 200)
    username  = sanitise(request.form.get("username", ""), 50)
    password  = request.form.get("password", "")

    error = None
    if not all([full_name, email, phone, username, password]):
        error = "Full name, email, phone, username and password are required."
    elif not validate_email(email):
        error = "Invalid email address."
    elif not validate_phone(phone):
        error = "Phone must be 7–15 digits (e.g. +256700123456)."
    elif len(username) < 3:
        error = "Username must be at least 3 characters."
    elif len(password) < 8:
        error = "Password must be at least 8 characters."
    elif dob:
        ok, msg = validate_dob(dob)
        if not ok:
            error = msg

    if error:
        flash(error, "danger")
    else:
        try:
            if db.users.find_one({"username": username}):
                flash("That username is already taken.", "danger")
            elif db.patients.find_one({"email": email}):
                flash("A patient with that email already exists.", "danger")
            else:
                uid = db.users.insert_one({
                    "full_name":     full_name,
                    "username":      username,
                    "password_hash": hash_password(password),
                    "role":          "patient",
                    "active":        True,
                    "created_at":    datetime.utcnow(),
                }).inserted_id
                db.patients.insert_one({
                    "full_name":  full_name,
                    "email":      email,
                    "phone":      phone,
                    "dob":        dob,
                    "gender":     gender,
                    "address":    address,
                    "user_id":    str(uid),
                    "medical_history": [],
                    "created_at": datetime.utcnow(),
                })
                log_audit("registered_patient", {"patient_name": full_name, "email": email})
                flash(f"Patient '{full_name}' registered. Login: {username}", "success")
        except DuplicateKeyError:
            flash("Username or email already exists.", "danger")
        except PyMongoError as e:
            logger.error("admin_add_patient: %s", e)
            flash("Database error.", "danger")

    dest = "main.admin_patients" if session.get("role") == "admin" else "main.staff_patients"
    return redirect(url_for(dest))


@main.route("/admin/patients/<patient_id>/edit", methods=["POST"])
@role_required("admin")
def admin_edit_patient(patient_id):
    oid = safe_oid(patient_id)
    if not oid:
        flash("Invalid patient ID.", "danger")
        return redirect(url_for("main.admin_patients"))
    db        = mongo.db
    full_name = sanitise(request.form.get("full_name", ""), 100)
    email     = sanitise(request.form.get("email", ""), 150).lower()
    phone     = sanitise(request.form.get("phone", ""), 20)
    dob       = sanitise(request.form.get("dob", ""), 10)
    gender    = sanitise(request.form.get("gender", ""), 10)
    address   = sanitise(request.form.get("address", ""), 200)

    if not all([full_name, email, phone]):
        flash("Full name, email and phone are required.", "danger")
        return redirect(url_for("main.admin_patients"))
    if not validate_email(email):
        flash("Invalid email address.", "danger")
        return redirect(url_for("main.admin_patients"))
    if dob:
        ok, msg = validate_dob(dob)
        if not ok:
            flash(msg, "danger")
            return redirect(url_for("main.admin_patients"))
    try:
        patient = db.patients.find_one({"_id": oid})
        if not patient:
            flash("Patient not found.", "danger")
            return redirect(url_for("main.admin_patients"))
        if email != patient.get("email", "") and db.patients.find_one(
                {"email": email, "_id": {"$ne": oid}}):
            flash("That email is already used by another patient.", "danger")
            return redirect(url_for("main.admin_patients"))
        db.patients.update_one({"_id": oid}, {"$set": {
            "full_name": full_name, "email": email, "phone": phone,
            "dob": dob, "gender": gender, "address": address,
        }})
        flash(f"Patient '{full_name}' updated.", "success")
    except PyMongoError as e:
        logger.error("admin_edit_patient: %s", e)
        flash("Database error.", "danger")
    return redirect(url_for("main.admin_patients"))


@main.route("/admin/patients/<patient_id>/delete", methods=["POST"])
@role_required("admin")
def admin_delete_patient(patient_id):
    oid = safe_oid(patient_id)
    if not oid:
        flash("Invalid patient ID.", "danger")
        return redirect(url_for("main.admin_patients"))
    try:
        p = mongo.db.patients.find_one({"_id": oid})
        if not p:
            flash("Patient not found.", "warning")
            return redirect(url_for("main.admin_patients"))
        # Delete the patient's appointments too
        mongo.db.appointments.delete_many({"patient_id": oid})
        mongo.db.patients.delete_one({"_id": oid})
        log_audit("deleted_patient", {"patient_id": str(oid), "patient_name": _patient_name(p)})
        flash(f"Patient '{_patient_name(p)}' and their appointments deleted.", "success")
    except PyMongoError as e:
        logger.error("admin_delete_patient: %s", e)
        flash("Database error.", "danger")
    return redirect(url_for("main.admin_patients"))


# ── Medical History ───────────────────────────────────────────────────────────────

@main.route("/admin/patients/<patient_id>/medical-history", methods=["POST"])
@role_required("admin", "staff")
def add_medical_history(patient_id):
    """Add medical history entry for a patient."""
    oid = safe_oid(patient_id)
    if not oid:
        flash("Invalid patient ID.", "danger")
        return redirect(url_for("main.admin_patients"))

    diagnosis = sanitise(request.form.get("diagnosis", ""), 300)
    treatment = sanitise(request.form.get("treatment", ""), 300)
    doctor = sanitise(request.form.get("doctor", ""), 100)
    notes = sanitise(request.form.get("notes", ""), 500)

    if not all([diagnosis, treatment, doctor]):
        flash("Diagnosis, treatment, and doctor are required.", "danger")
        return redirect(url_for("main.admin_patients"))

    try:
        mongo.db.patients.update_one(
            {"_id": oid},
            {"$push": {
                "medical_history": {
                    "date": datetime.utcnow().strftime("%Y-%m-%d"),
                    "diagnosis": diagnosis,
                    "treatment": treatment,
                    "doctor": doctor,
                    "notes": notes,
                    "added_by": session.get("user_id"),
                }
            }}
        )
        log_audit("added_medical_history", {
            "patient_id": str(oid),
            "diagnosis": diagnosis,
            "doctor": doctor,
        })
        flash("Medical history added.", "success")
    except PyMongoError as e:
        logger.error("add_medical_history: %s", e)
        flash("Database error.", "danger")

    dest = "main.admin_patients" if session.get("role") == "admin" else "main.staff_patients"
    return redirect(url_for(dest))


# ── Admin — doctors ───────────────────────────────────────────────────────────

@main.route("/admin/doctors")
@role_required("admin")
def admin_doctors():
    try:
        return render_template("dashboard_admin.html", section="doctors",
            stats=_admin_stats(),
            doctors=list(mongo.db.doctors.find().sort("name", 1)))
    except PyMongoError as e:
        logger.error("admin_doctors: %s", e)
        flash("Could not load doctors.", "danger")
        return redirect(url_for("main.admin_dashboard"))


@main.route("/admin/doctors/add", methods=["POST"])
@role_required("admin")
def admin_add_doctor():
    name  = sanitise(request.form.get("name", ""), 100)
    dept  = sanitise(request.form.get("department", ""), 100)
    uname = sanitise(request.form.get("username", ""), 50)
    if not name or not dept:
        flash("Name and department are required.", "danger")
    else:
        try:
            mongo.db.doctors.insert_one({
                "name": name, "department": dept,
                "username": uname, "active": True,
                "created_at": datetime.utcnow(),
            })
            flash(f"Dr. {name} added.", "success")
        except PyMongoError as e:
            logger.error("admin_add_doctor: %s", e)
            flash("Database error.", "danger")
    return redirect(url_for("main.admin_doctors"))


@main.route("/admin/doctors/<doc_id>/delete", methods=["POST"])
@role_required("admin")
def admin_delete_doctor(doc_id):
    oid = safe_oid(doc_id)
    if not oid:
        flash("Invalid doctor ID.", "danger")
        return redirect(url_for("main.admin_doctors"))
    try:
        mongo.db.doctors.delete_one({"_id": oid})
        flash("Doctor removed.", "success")
    except PyMongoError as e:
        logger.error("admin_delete_doctor: %s", e)
        flash("Database error.", "danger")
    return redirect(url_for("main.admin_doctors"))


# ── Admin — users ─────────────────────────────────────────────────────────────

@main.route("/admin/users")
@role_required("admin")
def admin_users():
    try:
        return render_template("dashboard_admin.html", section="users",
            stats=_admin_stats(),
            users=list(mongo.db.users.find().sort("full_name", 1)))
    except PyMongoError as e:
        logger.error("admin_users: %s", e)
        flash("Could not load users.", "danger")
        return redirect(url_for("main.admin_dashboard"))


@main.route("/admin/users/add", methods=["POST"])
@role_required("admin")
def admin_add_user():
    db        = mongo.db
    full_name = sanitise(request.form.get("full_name", ""), 100)
    username  = sanitise(request.form.get("username", ""), 50)
    password  = request.form.get("password", "")
    role      = sanitise(request.form.get("role", ""), 20)

    if not all([full_name, username, password, role]):
        flash("All fields are required.", "danger")
    elif len(username) < 3:
        flash("Username must be at least 3 characters.", "danger")
    elif role not in ALLOWED_ROLES:
        flash("Invalid role selected.", "danger")
    elif len(password) < 8:
        flash("Password must be at least 8 characters.", "danger")
    else:
        try:
            db.users.insert_one({
                "full_name":     full_name,
                "username":      username,
                "password_hash": hash_password(password),
                "role":          role,
                "active":        True,
                "created_at":    datetime.utcnow(),
            })
            flash(f"User '{full_name}' created.", "success")
        except DuplicateKeyError:
            flash("That username is already taken.", "danger")
        except PyMongoError as e:
            logger.error("admin_add_user: %s", e)
            flash("Database error.", "danger")
    # If the form was submitted from the Staff page, go back there
    next_page = request.form.get("next", "")
    if next_page == "staff":
        return redirect(url_for("main.admin_staff"))
    return redirect(url_for("main.admin_users"))


@main.route("/admin/users/<user_id>/toggle", methods=["POST"])
@role_required("admin")
def admin_toggle_user(user_id):
    oid = safe_oid(user_id)
    if not oid:
        flash("Invalid user ID.", "danger")
        return redirect(url_for("main.admin_users"))
    if user_id == session.get("user_id"):
        flash("You cannot deactivate your own account.", "warning")
        return redirect(url_for("main.admin_users"))
    try:
        user = mongo.db.users.find_one({"_id": oid})
        if user:
            mongo.db.users.update_one(
                {"_id": oid},
                {"$set": {"active": not user.get("active", True)}},
            )
            flash("User status updated.", "success")
    except PyMongoError as e:
        logger.error("admin_toggle_user: %s", e)
        flash("Database error.", "danger")
    return redirect(url_for("main.admin_users"))


@main.route("/admin/users/<user_id>/delete", methods=["POST"])
@role_required("admin")
def admin_delete_user(user_id):
    oid = safe_oid(user_id)
    if not oid:
        flash("Invalid user ID.", "danger")
        return redirect(url_for("main.admin_users"))
    if user_id == session.get("user_id"):
        flash("You cannot delete your own account.", "warning")
        return redirect(url_for("main.admin_users"))
    try:
        user = mongo.db.users.find_one({"_id": oid})
        mongo.db.users.delete_one({"_id": oid})
        log_audit("deleted_user", {"user_id": str(oid), "username": user.get("username")})
        flash("User deleted.", "success")
    except PyMongoError as e:
        logger.error("admin_delete_user: %s", e)
        flash("Database error.", "danger")
    return redirect(url_for("main.admin_users"))


@main.route("/admin/users/<user_id>/reset-password", methods=["POST"])
@role_required("admin")
def admin_reset_password(user_id):
    """Admin can reset any user's password directly."""
    oid = safe_oid(user_id)
    if not oid:
        flash("Invalid user ID.", "danger")
        return redirect(url_for("main.admin_users"))
    if user_id == session.get("user_id"):
        flash("You cannot reset your own password here. Use Change Password.", "warning")
        return redirect(url_for("main.admin_users"))

    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not new_password or not confirm_password:
        flash("Password fields are required.", "danger")
        return redirect(url_for("main.admin_users"))
    if len(new_password) < 8:
        flash("Password must be at least 8 characters.", "danger")
        return redirect(url_for("main.admin_users"))
    if new_password != confirm_password:
        flash("Passwords do not match.", "danger")
        return redirect(url_for("main.admin_users"))

    try:
        user = mongo.db.users.find_one({"_id": oid})
        if not user:
            flash("User not found.", "danger")
            return redirect(url_for("main.admin_users"))

        mongo.db.users.update_one(
            {"_id": oid},
            {"$set": {"password_hash": hash_password(new_password)}}
        )
        log_audit("reset_password", {"username": user.get("username"), "reset_by": session.get("username")})
        flash(f"Password reset for {user.get('username')}.", "success")
    except PyMongoError as e:
        logger.error("admin_reset_password: %s", e)
        flash("Database error.", "danger")

    return redirect(url_for("main.admin_users"))


# ── Password Change Approval System ─────────────────────────────────────────────

@main.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    """Request a password change (requires admin approval)."""
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not all([current_password, new_password, confirm_password]):
            error = "All fields are required."
        elif len(new_password) < 8:
            error = "New password must be at least 8 characters."
        elif new_password != confirm_password:
            error = "Passwords do not match."
        else:
            try:
                user = mongo.db.users.find_one({"_id": ObjectId(session.get("user_id"))})
                if not check_password(current_password, user.get("password_hash", "")):
                    error = "Current password is incorrect."
                else:
                    # Store pending password change request
                    mongo.db.password_requests.insert_one({
                        "user_id": ObjectId(session.get("user_id")),
                        "username": session.get("username"),
                        "new_password_hash": hash_password(new_password),
                        "status": "pending",
                        "requested_at": datetime.utcnow(),
                    })
                    log_audit("requested_password_change", {"username": session.get("username")})
                    flash("Password change request submitted. Awaiting admin approval.", "success")
                    return redirect(url_for("main.dashboard"))
            except PyMongoError as e:
                logger.error("change_password: %s", e)
                error = "Database error."

        return render_template("base.html", page="change_password", error=error)

    return render_template("base.html", page="change_password", error=None)


@main.route("/admin/password-requests")
@role_required("admin")
def admin_password_requests():
    """View and manage pending password change requests."""
    try:
        requests = list(mongo.db.password_requests.find({"status": "pending"})
                        .sort("requested_at", -1))
        return render_template("dashboard_admin.html", section="password_requests",
            stats=_admin_stats(),
            password_requests=requests)
    except PyMongoError as e:
        logger.error("admin_password_requests: %s", e)
        flash("Could not load password requests.", "danger")
        return redirect(url_for("main.admin_dashboard"))


@main.route("/admin/password-requests/<request_id>/approve", methods=["POST"])
@role_required("admin")
def approve_password_request(request_id):
    """Approve a password change request."""
    oid = safe_oid(request_id)
    if not oid:
        flash("Invalid request ID.", "danger")
        return redirect(url_for("main.admin_password_requests"))

    try:
        req = mongo.db.password_requests.find_one({"_id": oid})
        if not req or req.get("status") != "pending":
            flash("Request not found or already processed.", "warning")
            return redirect(url_for("main.admin_password_requests"))

        # Update user's password
        mongo.db.users.update_one(
            {"_id": req["user_id"]},
            {"$set": {"password_hash": req["new_password_hash"]}}
        )

        # Mark request as approved
        mongo.db.password_requests.update_one(
            {"_id": oid},
            {"$set": {
                "status": "approved",
                "approved_by": session.get("user_id"),
                "approved_at": datetime.utcnow(),
            }}
        )

        log_audit("approved_password_change", {
            "username": req.get("username"),
            "approved_by":	session.get("username")
        })
        flash(f"Password change approved for {req.get('username')}.", "success")

    except PyMongoError as e:
        logger.error("approve_password_request: %s", e)
        flash("Database error.", "danger")

    return redirect(url_for("main.admin_password_requests"))


@main.route("/admin/password-requests/<request_id>/reject", methods=["POST"])
@role_required("admin")
def reject_password_request(request_id):
    """Reject a password change request."""
    oid = safe_oid(request_id)
    if not oid:
        flash("Invalid request ID.", "danger")
        return redirect(url_for("main.admin_password_requests"))

    try:
        req = mongo.db.password_requests.find_one({"_id": oid})
        if not req or req.get("status") != "pending":
            flash("Request not found or already processed.", "warning")
            return redirect(url_for("main.admin_password_requests"))

        # Mark request as rejected
        mongo.db.password_requests.update_one(
            {"_id": oid},
            {"$set": {
                "status": "rejected",
                "rejected_by": session.get("user_id"),
                "rejected_at": datetime.utcnow(),
            }}
        )

        log_audit("rejected_password_change", {
            "username": req.get("username"),
            "rejected_by": session.get("username")
        })
        flash(f"Password change rejected for {req.get('username')}.", "success")

    except PyMongoError as e:
        logger.error("reject_password_request: %s", e)
        flash("Database error.", "danger")

    return redirect(url_for("main.admin_password_requests"))


# ── Admin — staff ─────────────────────────────────────────────────────────────

@main.route("/admin/staff")
@role_required("admin")
def admin_staff():
    try:
        return render_template("dashboard_admin.html", section="staff",
            stats=_admin_stats(),
            staff_list=list(mongo.db.users.find({"role": "staff"}).sort("full_name", 1)))
    except PyMongoError as e:
        logger.error("admin_staff: %s", e)
        flash("Could not load staff.", "danger")
        return redirect(url_for("main.admin_dashboard"))


# ── Staff — dashboard ─────────────────────────────────────────────────────────

@main.route("/staff")
@role_required("staff")
def staff_dashboard():
    try:
        db     = mongo.db
        recent = list(db.appointments.find().sort("created_at", -1).limit(8))
        _enrich(recent)
        return render_template("dashboard_staff.html", section="overview",
            stats=_staff_stats(), recent=recent,
            doctors=list(db.doctors.find({"active": True}).sort("name", 1)),
            patients=list(db.patients.find().sort("full_name", 1)))
    except PyMongoError as e:
        logger.error("staff_dashboard: %s", e)
        flash("Could not load dashboard.", "danger")
        return render_template("dashboard_staff.html", section="overview",
            stats=_staff_stats(), recent=[], doctors=[], patients=[])


# ── Staff — appointments ──────────────────────────────────────────────────────

@main.route("/staff/appointments")
@role_required("staff")
def staff_appointments():
    try:
        db     = mongo.db
        status = sanitise(request.args.get("status", ""), 20)
        if status and status not in STATUSES:
            status = ""
        appts = list(
            db.appointments.find({"status": status} if status else {}).sort("date", -1)
        )
        _enrich(appts)
        return render_template("dashboard_staff.html", section="appointments",
            appointments=appts, status_filter=status,
            stats=_staff_stats(), statuses=STATUSES,
            doctors=list(db.doctors.find({"active": True}).sort("name", 1)),
            patients=list(db.patients.find().sort("full_name", 1)))
    except PyMongoError as e:
        logger.error("staff_appointments: %s", e)
        flash("Could not load appointments.", "danger")
        return redirect(url_for("main.staff_dashboard"))


@main.route("/staff/appointments/add", methods=["POST"])
@role_required("staff")
def staff_add_appointment():
    db     = mongo.db
    pid    = safe_oid(request.form.get("patient_id"))
    did    = safe_oid(request.form.get("doctor_id"))
    date   = sanitise(request.form.get("date", ""), 10)
    time   = sanitise(request.form.get("time", ""), 5)
    reason = sanitise(request.form.get("reason", ""), 500)
    status = sanitise(request.form.get("status", "Pending"), 20)

    ok_date, msg_date = validate_appt_date(date)
    ok_stat, msg_stat = validate_status_change(status, date)

    if not all([pid, did, date, time, reason]):
        flash("All fields are required.", "danger")
    elif not ok_date:
        flash(msg_date, "danger")
    elif not validate_time(time):
        flash("Invalid time format.", "danger")
    elif status not in STATUSES:
        flash("Invalid status.", "danger")
    elif not ok_stat:
        flash(msg_stat, "danger")
    else:
        try:
            clash = db.appointments.find_one({
                "doctor_id": did, "date": date, "time": time,
                "status": {"$nin": ["Cancelled"]},
            })
            if clash:
                flash("That doctor is already booked at that date and time.", "danger")
            else:
                db.appointments.insert_one({
                    "patient_id": pid, "doctor_id": did,
                    "date": date, "time": time, "reason": reason, "status": status,
                    "created_at": datetime.utcnow(), "updated_at": datetime.utcnow(),
                })
                flash("Appointment booked.", "success")
        except PyMongoError as e:
            logger.error("staff_add_appointment: %s", e)
            flash("Database error.", "danger")
    return redirect(url_for("main.staff_appointments"))


@main.route("/staff/appointments/<appt_id>/edit", methods=["POST"])
@role_required("staff")
def staff_edit_appointment(appt_id):
    oid = safe_oid(appt_id)
    if not oid:
        flash("Invalid appointment ID.", "danger")
        return redirect(url_for("main.staff_appointments"))
    db     = mongo.db
    pid    = safe_oid(request.form.get("patient_id"))
    did    = safe_oid(request.form.get("doctor_id"))
    date   = sanitise(request.form.get("date", ""), 10)
    time   = sanitise(request.form.get("time", ""), 5)
    reason = sanitise(request.form.get("reason", ""), 500)
    status = sanitise(request.form.get("status", ""), 20)

    ok_stat, msg_stat = validate_status_change(status, date)

    if not all([pid, did, date, time, reason, status]):
        flash("All fields are required.", "danger")
    elif not validate_date(date):
        flash("Invalid date format.", "danger")
    elif not validate_time(time):
        flash("Invalid time format.", "danger")
    elif status not in STATUSES:
        flash("Invalid status.", "danger")
    elif not ok_stat:
        flash(msg_stat, "danger")
    else:
        try:
            clash = db.appointments.find_one({
                "_id": {"$ne": oid}, "doctor_id": did, "date": date, "time": time,
                "status": {"$nin": ["Cancelled"]},
            })
            if clash:
                flash("Doctor already booked at that date and time.", "danger")
            else:
                db.appointments.update_one({"_id": oid}, {"$set": {
                    "patient_id": pid, "doctor_id": did,
                    "date": date, "time": time, "reason": reason,
                    "status": status, "updated_at": datetime.utcnow(),
                }})
                flash("Appointment updated.", "success")
        except PyMongoError as e:
            logger.error("staff_edit_appointment: %s", e)
            flash("Database error.", "danger")
    return redirect(url_for("main.staff_appointments"))


# ── Staff — patients ──────────────────────────────────────────────────────────

@main.route("/staff/patients")
@role_required("staff")
def staff_patients():
    try:
        db = mongo.db
        return render_template("dashboard_staff.html", section="patients",
            stats=_staff_stats(),
            patients=list(db.patients.find().sort("full_name", 1)),
            doctors=list(db.doctors.find({"active": True}).sort("name", 1)),
            recent=[])
    except PyMongoError as e:
        logger.error("staff_patients: %s", e)
        flash("Could not load patients.", "danger")
        return redirect(url_for("main.staff_dashboard"))


@main.route("/staff/patients/add", methods=["GET", "POST"])
@role_required("staff")
def staff_add_patient():
    db    = mongo.db
    error = None
    if request.method == "POST":
        full_name = sanitise(request.form.get("full_name", ""), 100)
        email     = sanitise(request.form.get("email", ""), 150).lower()
        phone     = sanitise(request.form.get("phone", ""), 20)
        dob       = sanitise(request.form.get("dob", ""), 10)
        gender    = sanitise(request.form.get("gender", ""), 10)
        address   = sanitise(request.form.get("address", ""), 200)
        username  = sanitise(request.form.get("username", ""), 50)
        password  = request.form.get("password", "")

        if not all([full_name, email, phone, username, password]):
            error = "Full name, email, phone, username and password are required."
        elif not validate_email(email):
            error = "Invalid email address."
        elif not validate_phone(phone):
            error = "Phone must be 7–15 digits (e.g. +256700123456)."
        elif len(username) < 3:
            error = "Username must be at least 3 characters."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        elif dob:
            ok, msg = validate_dob(dob)
            if not ok:
                error = msg

        if not error:
            try:
                if db.users.find_one({"username": username}):
                    error = "That username is already taken."
                elif db.patients.find_one({"email": email}):
                    error = "A patient with that email already exists."
                else:
                    uid = db.users.insert_one({
                        "full_name":     full_name,
                        "username":      username,
                        "password_hash": hash_password(password),
                        "role":          "patient",
                        "active":        True,
                        "created_at":    datetime.utcnow(),
                    }).inserted_id
                    db.patients.insert_one({
                        "full_name":  full_name,
                        "email":      email,
                        "phone":      phone,
                        "dob":        dob,
                        "gender":     gender,
                        "address":    address,
                        "user_id":    str(uid),
                        "created_at": datetime.utcnow(),
                    })
                    flash(f"Patient '{full_name}' registered. Login: {username}", "success")
                    return redirect(url_for("main.staff_patients"))
            except DuplicateKeyError:
                error = "Username or email already exists."
            except PyMongoError as e:
                logger.error("staff_add_patient: %s", e)
                error = "Database error. Please try again."

    try:
        return render_template("dashboard_staff.html", section="add_patient",
            error=error, stats=_staff_stats(),
            doctors=list(db.doctors.find({"active": True}).sort("name", 1)),
            patients=list(db.patients.find().sort("full_name", 1)),
            recent=[])
    except PyMongoError as e:
        logger.error("staff_add_patient render: %s", e)
        flash("Could not load form.", "danger")
        return redirect(url_for("main.staff_dashboard"))


# ── Doctor ────────────────────────────────────────────────────────────────────

@main.route("/doctor")
@role_required("doctor")
def doctor_dashboard():
    try:
        db     = mongo.db
        doctor = db.doctors.find_one({"username": session.get("username")})
        appts  = []
        stats  = {"total": 0, "pending": 0, "confirmed": 0, "completed": 0, "today": 0}
        if doctor:
            appts = list(db.appointments.find(
                {"doctor_id": doctor["_id"]}).sort("date", -1))
            _enrich(appts)
            today = datetime.utcnow().strftime("%Y-%m-%d")
            stats = {
                "total":     len(appts),
                "pending":   sum(1 for a in appts if a.get("status") == "Pending"),
                "confirmed": sum(1 for a in appts if a.get("status") == "Confirmed"),
                "completed": sum(1 for a in appts if a.get("status") == "Completed"),
                "today":     sum(1 for a in appts if a.get("date") == today),
            }
        return render_template("dashboard_doctor.html", section="overview",
            doctor=doctor, appointments=appts[:10], stats=stats)
    except PyMongoError as e:
        logger.error("doctor_dashboard: %s", e)
        flash("Could not load dashboard.", "danger")
        return render_template("dashboard_doctor.html", section="overview",
            doctor=None, appointments=[], stats={})


@main.route("/doctor/appointments")
@role_required("doctor")
def doctor_appointments():
    try:
        db     = mongo.db
        doctor = db.doctors.find_one({"username": session.get("username")})
        if not doctor:
            flash("Your doctor profile is not linked. Contact the administrator.", "warning")
            return redirect(url_for("main.doctor_dashboard"))
        status = sanitise(request.args.get("status", ""), 20)
        if status and status not in STATUSES:
            status = ""
        query = {"doctor_id": doctor["_id"]}
        if status:
            query["status"] = status
        appts = list(db.appointments.find(query).sort("date", -1))
        _enrich(appts)
        return render_template("dashboard_doctor.html", section="appointments",
            doctor=doctor, appointments=appts, status_filter=status, stats={})
    except PyMongoError as e:
        logger.error("doctor_appointments: %s", e)
        flash("Could not load schedule.", "danger")
        return redirect(url_for("main.doctor_dashboard"))


@main.route("/doctor/appointments/<appt_id>/update", methods=["POST"])
@role_required("doctor")
def doctor_update_status(appt_id):
    oid = safe_oid(appt_id)
    if not oid:
        flash("Invalid appointment ID.", "danger")
        return redirect(url_for("main.doctor_appointments"))
    new_status = sanitise(request.form.get("status", ""), 20)
    if new_status not in ["Confirmed", "Completed", "Cancelled"]:
        flash("Invalid status value.", "danger")
        return redirect(url_for("main.doctor_appointments"))
    try:
        db     = mongo.db
        doctor = db.doctors.find_one({"username": session.get("username")})
        if not doctor:
            abort(403)
        appt = db.appointments.find_one({"_id": oid, "doctor_id": doctor["_id"]})
        if not appt:
            flash("Appointment not found.", "danger")
            return redirect(url_for("main.doctor_appointments"))
        if appt.get("status") in ("Completed", "Cancelled"):
            flash("This appointment is already finalised.", "warning")
            return redirect(url_for("main.doctor_appointments"))
        # Cannot mark Completed if the appointment date hasn't happened yet
        ok, msg = validate_status_change(new_status, appt.get("date", ""))
        if not ok:
            flash(msg, "danger")
            return redirect(url_for("main.doctor_appointments"))
        db.appointments.update_one(
            {"_id": oid},
            {"$set": {"status": new_status, "updated_at": datetime.utcnow()}},
        )
        flash(f"Appointment marked as {new_status}.", "success")
    except PyMongoError as e:
        logger.error("doctor_update_status: %s", e)
        flash("Database error.", "danger")
    return redirect(url_for("main.doctor_appointments"))


# ── Patient ───────────────────────────────────────────────────────────────────

@main.route("/patient")
@role_required("patient")
def patient_dashboard():
    try:
        db      = mongo.db
        patient = _find_patient_by_user(db)
        appts   = []
        if patient:
            appts = list(db.appointments.find(
                {"patient_id": patient["_id"]}).sort("date", -1))
            for a in appts:
                d = db.doctors.find_one({"_id": a.get("doctor_id")})
                a["doctor_name"] = f"Dr. {d['name']}" if d else "Unknown"
        doctors = list(db.doctors.find({"active": True}).sort("name", 1))
        return render_template("dashboard_patient.html",
            section="overview", patient=patient,
            appointments=appts, doctors=doctors)
    except PyMongoError as e:
        logger.error("patient_dashboard: %s", e)
        flash("Could not load dashboard.", "danger")
        return render_template("dashboard_patient.html",
            section="overview", patient=None, appointments=[], doctors=[])


@main.route("/patient/appointments")
@role_required("patient")
def patient_appointments():
    try:
        db      = mongo.db
        patient = _find_patient_by_user(db)
        if not patient:
            flash("Patient profile not found. Please contact the reception.", "warning")
            return redirect(url_for("main.patient_dashboard"))
        appts = list(db.appointments.find(
            {"patient_id": patient["_id"]}).sort("date", -1))
        for a in appts:
            d = db.doctors.find_one({"_id": a.get("doctor_id")})
            a["doctor_name"] = f"Dr. {d['name']}" if d else "Unknown"
        return render_template("dashboard_patient.html",
            section="appointments", patient=patient, appointments=appts)
    except PyMongoError as e:
        logger.error("patient_appointments: %s", e)
        flash("Could not load appointments.", "danger")
        return redirect(url_for("main.patient_dashboard"))


@main.route("/patient/appointments/add", methods=["POST"])
@role_required("patient")
def patient_add_appointment():
    db      = mongo.db
    patient = _find_patient_by_user(db)

    # Safety net: auto-create a minimal profile if none exists
    if not patient:
        try:
            user = db.users.find_one({"_id": safe_oid(session.get("user_id"))})
            if not user:
                flash("Your account was not found. Please log in again.", "danger")
                return redirect(url_for("main.patient_dashboard"))
            new_id  = db.patients.insert_one({
                "full_name":  user.get("full_name", session.get("name", "")),
                "email":      "",
                "phone":      "",
                "user_id":    session["user_id"],
                "created_at": datetime.utcnow(),
            }).inserted_id
            patient = db.patients.find_one({"_id": new_id})
        except PyMongoError as e:
            logger.error("patient profile auto-create: %s", e)
            flash("Could not create patient profile. Contact reception.", "danger")
            return redirect(url_for("main.patient_dashboard"))

    did    = safe_oid(request.form.get("doctor_id"))
    date   = sanitise(request.form.get("date", ""), 10)
    time   = sanitise(request.form.get("time", ""), 5)
    reason = sanitise(request.form.get("reason", ""), 500)

    ok_date, msg_date = validate_appt_date(date)

    if not all([did, date, time, reason]):
        flash("All fields are required.", "danger")
    elif not ok_date:
        flash(msg_date, "danger")
    elif not validate_time(time):
        flash("Invalid time format.", "danger")
    else:
        try:
            clash = db.appointments.find_one({
                "doctor_id": did, "date": date, "time": time,
                "status": {"$nin": ["Cancelled"]},
            })
            if clash:
                flash("That doctor is not available at that date and time.", "danger")
            else:
                db.appointments.insert_one({
                    "patient_id": patient["_id"],
                    "doctor_id":  did,
                    "date":       date,
                    "time":       time,
                    "reason":     reason,
                    "status":     "Pending",
                    "created_at": datetime.utcnow(),
                    "updated_at": datetime.utcnow(),
                })
                flash("Appointment requested successfully. Status: Pending.", "success")
        except PyMongoError as e:
            logger.error("patient_add_appointment: %s", e)
            flash("Database error.", "danger")
    return redirect(url_for("main.patient_dashboard"))


@main.route("/patient/appointments/<appt_id>/cancel", methods=["POST"])
@role_required("patient")
def patient_cancel(appt_id):
    oid = safe_oid(appt_id)
    if not oid:
        flash("Invalid appointment ID.", "danger")
        return redirect(url_for("main.patient_appointments"))
    try:
        db      = mongo.db
        patient = _find_patient_by_user(db)
        if not patient:
            flash("Patient profile not found.", "danger")
            return redirect(url_for("main.patient_dashboard"))
        appt = db.appointments.find_one({"_id": oid, "patient_id": patient["_id"]})
        if not appt:
            abort(403)
        if appt.get("status") in ("Completed", "Cancelled"):
            flash("This appointment cannot be cancelled.", "warning")
            return redirect(url_for("main.patient_appointments"))
        db.appointments.update_one(
            {"_id": oid},
            {"$set": {"status": "Cancelled", "updated_at": datetime.utcnow()}},
        )
        flash("Appointment cancelled.", "info")
    except PyMongoError as e:
        logger.error("patient_cancel: %s", e)
        flash("Database error.", "danger")
    return redirect(url_for("main.patient_appointments"))
