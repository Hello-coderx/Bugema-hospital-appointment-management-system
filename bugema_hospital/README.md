# Bugema University Hospital — Appointment Management System

Academic project | Database II / NoSQL | Python Flask + MongoDB

---

## Requirements
- Python 3.10+
- MongoDB Community Server (local, NOT Atlas)

---

## Setup — Windows / VS Code

### 1. Create and activate a virtual environment
```bash
cd bugema_hospital
python -m venv venv
venv\Scripts\activate
```

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

### 3. Configure environment
Copy `.env.example` to `.env` and edit the values:
```
MONGO_URI=mongodb://127.0.0.1:27017
DB_NAME=bugema_hospital
SECRET_KEY=your-random-secret
WTF_CSRF_SECRET_KEY=another-random-secret
```

### 4. Start MongoDB Community Server
Open an Administrator Command Prompt and run:
```
net start MongoDB
```
Or start it from Windows Services (services.msc → MongoDB).

### 5. Run the application
```bash
python app.py
```
Open http://127.0.0.1:5000 in your browser.

---

## First-time setup — create the admin account
Navigate to http://127.0.0.1:5000/setup-admin  
This route is only available when no admin user exists yet.

---

## Roles
| Role | Access |
|------|--------|
| Administrator | Full access — users, doctors, patients, all appointments |
| Receptionist/Staff | Register patients, create/edit appointments |
| Doctor | View and update status of own appointments only |
| Patient/Student | View and cancel own appointments only |

---

## Collections in MongoDB (bugema_hospital)
- `users` — login credentials and roles
- `patients` — patient demographics
- `doctors` — doctor profiles and department
- `appointments` — all appointment records with references to patients and doctors

---

## Notes
This is an academic local-only setup and is not production-ready.
Do not deploy without adding HTTPS, environment hardening, and a proper secret manager.
