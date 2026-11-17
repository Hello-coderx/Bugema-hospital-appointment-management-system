"""
extensions.py — MongoDB connection, validation helpers, auth decorators.
Database : healthcare-db
URI      : mongodb://127.0.0.1:27017
"""
import re
import sys
import bcrypt
import logging
from datetime import datetime
from functools import wraps

from pymongo import MongoClient
from pymongo.errors import (ConnectionFailure, ServerSelectionTimeoutError,
                             PyMongoError, DuplicateKeyError)
from flask_wtf.csrf import CSRFProtect
from flask import g, session, redirect, url_for, flash, abort, current_app
from bson import ObjectId
from bson.errors import InvalidId

logger = logging.getLogger(__name__)
csrf   = CSRFProtect()
client = None   # MongoClient instance, set by init_app()


# ── Database connection ───────────────────────────────────────────────────────

def get_db():
    """Return the database for the current request (cached in Flask's g)."""
    if "db" not in g:
        g.db = client[current_app.config["DB_NAME"]]
    return g.db


def init_app(app):
    """Connect to MongoDB, set up CSRF, create indexes, seed admin."""
    global client
    uri  = app.config["MONGO_URI"]
    name = app.config["DB_NAME"]

    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=3000)
        client.admin.command("ping")          # test the connection immediately
        print(f"[OK] MongoDB connected  →  {uri}  /  {name}")
    except (ConnectionFailure, ServerSelectionTimeoutError) as e:
        print(
            f"[ERROR] Cannot connect to MongoDB at {uri}\n"
            "  Make sure MongoDB is running:  net start MongoDB\n"
            f"  Detail: {e}", file=sys.stderr)
        sys.exit(1)

    csrf.init_app(app)

    with app.app_context():
        db = client[name]
        try:
            # Unique indexes prevent duplicate usernames and emails
            db.users.create_index("username", unique=True)
            db.patients.create_index("email", unique=True, sparse=True)
            db.appointments.create_index([("doctor_id", 1), ("date", 1), ("time", 1)])
        except PyMongoError:
            pass   # indexes already exist — safe to ignore
        _seed_admin(db)


def _seed_admin(db):
    """Insert the default admin account on the very first run."""
    try:
        if db.users.find_one({"role": "admin"}):
            return   # admin already exists, nothing to do
        pw = bcrypt.hashpw(b"ahmed123", bcrypt.gensalt()).decode()
        db.users.insert_one({
            "full_name":     "Ahmed",
            "username":      "Ahmed",
            "password_hash": pw,
            "role":          "admin",
            "active":        True,
            "created_at":    datetime.utcnow(),
        })
        print("[SEED] Admin created  →  username: Ahmed  |  password: ahmed123")
    except DuplicateKeyError:
        pass


# Proxy object so routes write mongo.db.collection instead of calling get_db()
class _MongoProxy:
    @property
    def db(self):
        return get_db()

mongo = _MongoProxy()


# ── Password helpers ──────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    """Hash a plain-text password with bcrypt."""
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()

def check_password(plain: str, hashed: str) -> bool:
    """Return True if plain matches the stored bcrypt hash."""
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


# ── Input validation ──────────────────────────────────────────────────────────

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DATE_RE  = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE  = re.compile(r"^\d{2}:\d{2}$")


def validate_email(v: str) -> bool:
    return bool(EMAIL_RE.match(v))


def validate_date(v: str) -> bool:
    """Check that v is a valid YYYY-MM-DD string."""
    if not DATE_RE.match(v):
        return False
    try:
        datetime.strptime(v, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def validate_dob(v: str):
    """
    Date of birth rules:
    - Must be a valid date.
    - Must be strictly in the past (not today, not future).
    - Person must be at least 1 year old.
    - Person cannot be older than 120 years.
    Returns (True, "") on success or (False, "error message") on failure.
    """
    if not validate_date(v):
        return False, "Invalid date of birth format."
    dob   = datetime.strptime(v, "%Y-%m-%d").date()
    today = datetime.utcnow().date()
    if dob >= today:
        return False, "Date of birth cannot be today or in the future."
    age = (today - dob).days / 365.25
    if age < 1:
        return False, "Date of birth must be at least 1 year in the past."
    if age > 120:
        return False, "That date of birth is unrealistically far in the past."
    return True, ""


def validate_appt_date(v: str):
    """
    Appointment date rules:
    - Must be a valid date.
    - Must be today or in the future (no past bookings allowed).
    Returns (True, "") or (False, "error message").
    """
    if not validate_date(v):
        return False, "Invalid date format. Use YYYY-MM-DD."
    appt  = datetime.strptime(v, "%Y-%m-%d").date()
    today = datetime.utcnow().date()
    if appt < today:
        return False, "Appointment date cannot be in the past."
    return True, ""


def validate_status_change(new_status: str, appt_date_str: str):
    """
    Status rule: 'Completed' is only valid when the appointment date
    is today or already passed. You cannot complete a future appointment.
    Returns (True, "") or (False, "error message").
    """
    if new_status != "Completed":
        return True, ""
    if not validate_date(appt_date_str):
        return True, ""   # date format error is caught by validate_appt_date
    appt  = datetime.strptime(appt_date_str, "%Y-%m-%d").date()
    today = datetime.utcnow().date()
    if appt > today:
        return False, "Cannot mark an appointment as Completed before it has taken place."
    return True, ""


def validate_time(v: str) -> bool:
    """Check that v matches HH:MM format."""
    return bool(TIME_RE.match(v)) if v else False


def validate_phone(v: str) -> bool:
    """Accept 7–15 digits, optional leading +, ignoring spaces/dashes/brackets."""
    clean = re.sub(r"[\s\-\(\)]", "", v)
    return bool(re.match(r"^\+?\d{7,15}$", clean))


def sanitise(v: str, max_len: int = 300) -> str:
    """Strip whitespace and truncate to max_len characters."""
    return v.strip()[:max_len] if v else ""


# ── ObjectId helper ───────────────────────────────────────────────────────────

def safe_oid(v):
    """Convert string to ObjectId. Returns None if v is invalid."""
    try:
        return ObjectId(v)
    except (InvalidId, TypeError):
        return None


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _current_user():
    """Return session user dict, or None if not logged in."""
    if "user_id" not in session:
        return None
    return {
        "id":       session["user_id"],
        "username": session["username"],
        "role":     session["role"],
        "name":     session["name"],
    }


def login_required(f):
    """Decorator — redirect to login if no active session."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _current_user():
            flash("Please log in to continue.", "warning")
            return redirect(url_for("main.login"))
        return f(*args, **kwargs)
    return decorated


def role_required(*roles):
    """Decorator — abort 403 if the user's role is not in the allowed list."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            u = _current_user()
            if not u:
                flash("Please log in to continue.", "warning")
                return redirect(url_for("main.login"))
            if u["role"] not in roles:
                abort(403)
            return f(*args, **kwargs)
        return decorated
    return decorator


# ── Audit Logging ────────────────────────────────────────────────────────────────

def log_audit(action, details=None):
    """
    Log an audit trail entry for tracking system actions.
    Records: action, user, timestamp, details, IP address.
    """
    try:
        from flask import request
        db = get_db()
        db.audit_logs.insert_one({
            "action": action,
            "user_id": session.get("user_id"),
            "username": session.get("username"),
            "role": session.get("role"),
            "timestamp": datetime.utcnow(),
            "details": details or {},
            "ip_address": request.remote_addr if request else None,
        })
    except Exception as e:
        logger.error("Audit log failed: %s", e)
