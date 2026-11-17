"""
app.py — Flask application factory for Bugema University Hospital.
Database : healthcare-db  |  URI : mongodb://127.0.0.1:27017
Start    : python app.py
"""
import os
from datetime import timedelta
from flask import Flask, render_template
from dotenv import load_dotenv
from extensions import init_app, csrf
from routes import main

load_dotenv()


def create_app():
    app = Flask(__name__)

    # ── Configuration ─────────────────────────────────────────────────────────
    app.config["SECRET_KEY"]              = os.environ.get("SECRET_KEY", "bugema-secret-key")
    app.config["WTF_CSRF_SECRET_KEY"]     = os.environ.get("WTF_CSRF_SECRET_KEY", app.config["SECRET_KEY"])
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"]   = os.environ.get("FLASK_ENV") == "production"
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=8)   # auto-logout after 8 h
    app.config["MONGO_URI"]               = os.environ.get("MONGO_URI", "mongodb://127.0.0.1:27017")
    app.config["DB_NAME"]                 = os.environ.get("DB_NAME",   "healthcare-db")

    # ── Email Configuration ─────────────────────────────────────────────────────
    app.config["MAIL_SERVER"]             = os.environ.get("MAIL_SERVER", "smtp.gmail.com")
    app.config["MAIL_PORT"]               = int(os.environ.get("MAIL_PORT", 587))
    app.config["MAIL_USE_TLS"]            = True
    app.config["MAIL_USERNAME"]           = os.environ.get("MAIL_USERNAME", "")
    app.config["MAIL_PASSWORD"]           = os.environ.get("MAIL_PASSWORD", "")
    app.config["MAIL_DEFAULT_SENDER"]     = os.environ.get("MAIL_DEFAULT_SENDER", "noreply@bugema-hospital.com")

    # ── Extensions (MongoDB ping + CSRF setup happens here) ───────────────────
    init_app(app)

    # ── Routes ────────────────────────────────────────────────────────────────
    app.register_blueprint(main)

    # ── Error pages ───────────────────────────────────────────────────────────
    @app.errorhandler(403)
    def forbidden(e):
        return render_template("base.html", page="error", code=403,
            message="You do not have permission to access this page."), 403

    @app.errorhandler(404)
    def not_found(e):
        return render_template("base.html", page="error", code=404,
            message="The page you requested could not be found."), 404

    @app.errorhandler(500)
    def server_error(e):
        return render_template("base.html", page="error", code=500,
            message="An internal server error occurred. Please try again."), 500

    return app


# Module-level instance so gunicorn can find it with `gunicorn app:app`
app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
