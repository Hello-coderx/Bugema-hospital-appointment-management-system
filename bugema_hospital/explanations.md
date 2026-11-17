# Bugema Hospital System - Database Connection Documentation

## Database Connection Status

**YES, the application is connected to MongoDB.**

### Connection Details
- **Database Type**: MongoDB
- **Connection URI**: `mongodb://127.0.0.1:27017` (localhost)
- **Database Name**: `healthcare-db` (configured in `.env` file)
- **Connection Status**: Active - The app connects on startup and pings the server to verify connection

---

## File Names and Database Connection Code

### 1. `app.py` (Main Application File)
**Location**: Root directory  
**Lines 25-26**: Database configuration
```python
app.config["MONGO_URI"]  = os.environ.get("MONGO_URI",  "mongodb://127.0.0.1:27017")
app.config["DB_NAME"]    = os.environ.get("DB_NAME",    "healthcare-db")
```

**Line 29**: Initializes the MongoDB connection
```python
init_app(app)
```

### 2. `extensions.py` (Database Connection Logic)
**Location**: Root directory  
**Lines 23-26**: `get_db()` function - Returns database connection
```python
def get_db():
    if "db" not in g:
        g.db = _client[current_app.config["DB_NAME"]]
    return g.db
```

**Lines 29-53**: `init_app()` function - Establishes MongoDB connection
```python
def init_app(app):
    global _client
    uri     = app.config["MONGO_URI"]
    db_name = app.config["DB_NAME"]
    try:
        _client = MongoClient(uri, serverSelectionTimeoutMS=3000)
        _client.admin.command("ping")
        print(f"[OK] MongoDB connected — {uri}  database: {db_name}")
    except (ConnectionFailure, ServerSelectionTimeoutError) as e:
        print(
            f"\n[ERROR] Cannot reach MongoDB at {uri}\n"
            "Start MongoDB:  net start MongoDB  (Admin CMD on Windows)\n"
            f"Detail: {e}\n", file=sys.stderr
        )
        sys.exit(1)

    csrf.init_app(app)

    with app.app_context():
        db = _client[db_name]
        db.users.create_index("username", unique=True)
        db.patients.create_index("email",  unique=True)
        db.appointments.create_index([("doctor_id",1),("date",1),("time",1)])
        _seed_admin(db)
```

**Lines 55-68**: `_seed_admin()` function - Creates default admin user
```python
def _seed_admin(db):
    """Create default admin Ahmed/ahmed123 if no admin exists."""
    if db.users.find_one({"role": "admin"}):
        return
    pw = bcrypt.hashpw("ahmed123".encode(), bcrypt.gensalt()).decode()
    db.users.insert_one({
        "full_name":     "Ahmed",
        "username":      "Ahmed",
        "password_hash": pw,
        "role":          "admin",
        "active":        True,
        "created_at":    datetime.utcnow(),
    })
    print("[SEED] Admin created — username: Ahmed  password: ahmed123")
```

**Lines 71-76**: MongoDB proxy class for easy database access
```python
class _MongoProxy:
    @property
    def db(self):
        return get_db()

mongo = _MongoProxy()
```

### 3. `.env` (Environment Configuration)
**Location**: Root directory (gitignored - not visible in IDE)  
**Purpose**: Stores sensitive configuration including database credentials

**Example content** (from `.env.example`):
```
MONGO_URI=mongodb://127.0.0.1:27017
DB_NAME=healthcare-db
SECRET_KEY=change-this-to-a-random-secret-key
WTF_CSRF_SECRET_KEY=change-this-to-another-random-key
```

### 4. `routes.py` (Application Routes)
**Location**: Root directory  
**Usage**: Uses `mongo.db` throughout to access database collections

**Example usage** (Line 70):
```python
user = mongo.db.users.find_one({"username": username, "active": True})
```

---

## Database Collections

The app uses the following MongoDB collections:

1. **users** - System users (admin, doctor, patient)
   - Index: `username` (unique)
   
2. **patients** - Patient profiles
   - Index: `email` (unique)
   
3. **doctors** - Doctor profiles
   - No specific index
   
4. **appointments** - Appointment records
   - Index: `doctor_id`, `date`, `time` (compound)

---

## How to Disconnect from MongoDB

### Option 1: Stop MongoDB Service (Windows)
```cmd
net stop MongoDB
```
This stops the MongoDB service. The app will fail to start if MongoDB is not running.

### Option 2: Change Connection URI in `.env`
Edit your `.env` file and change:
```
MONGO_URI=mongodb://127.0.0.1:27017
```
To an invalid or non-existent URI. The app will show an error on startup.

### Option 3: Comment Out Connection Code
In `extensions.py`, comment out lines 34-36:
```python
# _client = MongoClient(uri, serverSelectionTimeoutMS=3000)
# _client.admin.command("ping")
# print(f"[OK] MongoDB connected — {uri}  database: {db_name}")
```

---

## How to Reconnect to MongoDB

### Option 1: Start MongoDB Service (Windows)
```cmd
net start MongoDB
```
Then restart the Flask application.

### Option 2: Restore Connection URI in `.env`
Edit your `.env` file and restore:
```
MONGO_URI=mongodb://127.0.0.1:27017
DB_NAME=healthcare-db
```

### Option 3: Uncomment Connection Code
In `extensions.py`, uncomment lines 34-36 if you commented them out.

---

## Connection Troubleshooting

### Error: "Cannot reach MongoDB"
**Cause**: MongoDB service is not running  
**Solution**: 
```cmd
net start MongoDB
```
(Requires Administrator privileges)

### Error: "Connection refused"
**Cause**: MongoDB is not installed or wrong port  
**Solution**: 
- Install MongoDB if not installed
- Check if MongoDB is running on port 27017
- Update `MONGO_URI` in `.env` if using different port

### Error: "Authentication failed"
**Cause**: MongoDB requires authentication  
**Solution**: Update `MONGO_URI` in `.env` with credentials:
```
MONGO_URI=mongodb://username:password@127.0.0.1:27017
```

---

## Database Connection Flow

1. **App starts** → `app.py` runs
2. **`create_app()` called** → Line 15
3. **`init_app(app)` called** → Line 29
4. **MongoDB connection established** → `extensions.py` line 34
5. **Connection verified** → `extensions.py` line 35 (ping command)
6. **Indexes created** → `extensions.py` lines 49-51
7. **Admin seeded** → `extensions.py` line 52 (if no admin exists)
8. **App ready** → Database accessible via `mongo.db`

---

## Testing Connection

To verify MongoDB is running:
```cmd
# Windows
net start MongoDB

# Or check if service is running
sc query MongoDB
```

To test connection manually (Python):
```python
from pymongo import MongoClient
client = MongoClient("mongodb://127.0.0.1:27017")
client.admin.command("ping")
print("Connected!")
```

---

## Important Notes

- **Data Persistence**: All data (users, patients, appointments, doctors) is stored in MongoDB and persists between app restarts
- **Default Admin**: Username `Ahmed`, Password `ahmed123` (created automatically on first run)
- **Connection Timeout**: 3 seconds (configured in `extensions.py` line 34)
- **No Connection Pooling**: Uses single client instance for simplicity
- **Local Database**: Uses localhost MongoDB - not suitable for production without security hardening

---

## File Structure Summary

```
bugema_hospital/
├── app.py              # Main Flask app, DB config (lines 25-26, 29)
├── extensions.py       # MongoDB connection logic (lines 23-76)
├── routes.py           # Uses mongo.db for all DB operations
├── .env                # DB credentials (MONGO_URI, DB_NAME)
├── .env.example        # Example .env file
├── requirements.txt    # Python dependencies (includes pymongo)
├── templates/          # HTML templates
└── static/
    └── css/
        └── style.css   # Styling
```
