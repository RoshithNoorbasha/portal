"""
storage.py
==========
Single source of truth for all "backend" persistence used by app.py and
restore.py. No database is used - everything lives under ./data as JSON
(for metadata/registries) and CSV (for processed sheet snapshots).

Sections:
  1. Preprocessed-upload registry  (data/preprocessed/registry.json)
  2. User management incl. roles   (data/users.json)
  3. Hash-chained audit log        (data/audit_log.json)

Having ONE registry file for uploads means:
  - app.py always knows what the "current" (latest) file is.
  - restore.py can compare any two calendar dates without re-uploading,
    because every upload's processed sheets are already saved as CSV.
"""

import hashlib
import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import pandas as pd

# ==========================================
# PATHS
# ==========================================
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

PREPROCESS_DIR = DATA_DIR / "preprocessed"
PREPROCESS_DIR.mkdir(exist_ok=True)
PREPROCESS_REGISTRY_FILE = PREPROCESS_DIR / "registry.json"

USERS_FILE = DATA_DIR / "users.json"
AUDIT_FILE = DATA_DIR / "audit_log.json"


# ==========================================
# GENERIC JSON HELPERS
# ==========================================
# Lightweight mtime-based cache: Streamlit re-runs the whole script on
# every widget interaction, and without this, every rerun was re-opening
# and re-parsing registry.json / users.json / audit_log.json several
# times each (this was the main source of the "laggy" UI). We keep a
# tiny in-memory cache keyed by path, and only re-read from disk when the
# file's mtime has actually changed - i.e. someone wrote to it.
_JSON_CACHE = {}


def _read_json(path: Path, default):
    try:
        mtime = path.stat().st_mtime if path.exists() else None
    except OSError:
        mtime = None

    cached = _JSON_CACHE.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = default
    else:
        data = default

    _JSON_CACHE[path] = (mtime, data)
    return data


def _write_json(path: Path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    # Update the cache immediately so the writer's own next read (in the
    # same rerun) doesn't need to hit disk again.
    _JSON_CACHE[path] = (mtime, data)


# ==========================================
# 1. PREPROCESSED UPLOAD REGISTRY
# ==========================================
def _load_registry():
    data = _read_json(PREPROCESS_REGISTRY_FILE, {"uploads": []})
    if "uploads" not in data:
        data["uploads"] = []
    return data


def _save_registry(data):
    _write_json(PREPROCESS_REGISTRY_FILE, data)


def save_preprocessed_upload(file_bytes, original_filename, processed_dataframes,
                              snapshot_date, uploaded_by):
    """
    Persist a newly uploaded + processed SCADA workbook.

    - Saves the raw excel bytes (so it can be re-downloaded / re-processed later)
    - Saves every processed sheet as a CSV (so restore.py never needs to
      re-parse the excel to compare two dates)
    - Adds ONE entry to the single registry.json

    snapshot_date: 'YYYY-MM-DD' string - the calendar date this upload
                   represents, used for day-wise / range comparisons.
    Returns the new upload_id.
    """
    file_hash = hashlib.md5(file_bytes).hexdigest()
    timestamp = datetime.now()
    upload_id = f"{timestamp.strftime('%Y%m%d_%H%M%S')}_{file_hash[:8]}"

    upload_path = PREPROCESS_DIR / upload_id
    upload_path.mkdir(parents=True, exist_ok=True)

    ext = Path(original_filename).suffix or ".xlsx"
    with open(upload_path / f"original{ext}", "wb") as f:
        f.write(file_bytes)

    saved_sheets = []
    for sheet_name, df in processed_dataframes.items():
        if df is not None and not df.empty:
            safe_sheet = str(sheet_name).replace("/", "_").replace("\\", "_")
            df.to_csv(upload_path / f"{safe_sheet}.csv", index=False)
            saved_sheets.append(sheet_name)

    entry = {
        "upload_id": upload_id,
        "original_filename": original_filename,
        "original_ext": ext,
        "upload_timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        "snapshot_date": str(snapshot_date),
        "file_hash": file_hash,
        "file_size": len(file_bytes),
        "uploaded_by": uploaded_by,
        "saved_sheets": saved_sheets,
    }

    registry = _load_registry()
    # Replace any earlier upload for the exact same file+date (avoid duplicates)
    registry["uploads"] = [
        u for u in registry["uploads"]
        if not (u.get("file_hash") == file_hash and u.get("snapshot_date") == entry["snapshot_date"])
    ]
    registry["uploads"].append(entry)
    registry["uploads"] = sorted(registry["uploads"], key=lambda x: x["upload_timestamp"])
    _save_registry(registry)
    return upload_id


def get_all_uploads():
    return _load_registry().get("uploads", [])


def get_latest_upload():
    uploads = get_all_uploads()
    return uploads[-1] if uploads else None


def get_upload_by_id(upload_id):
    for u in get_all_uploads():
        if u["upload_id"] == upload_id:
            return u
    return None


def get_upload_for_date(snapshot_date):
    """Most recent upload registered under a given calendar date (YYYY-MM-DD)."""
    matches = [u for u in get_all_uploads() if u.get("snapshot_date") == str(snapshot_date)]
    if not matches:
        return None
    return sorted(matches, key=lambda x: x["upload_timestamp"])[-1]


@lru_cache(maxsize=32)
def _read_bytes_cached(path_str, mtime):
    with open(path_str, "rb") as f:
        return f.read()


def load_original_bytes(upload_id):
    """Uploads are immutable once written, so once a given (path, mtime)
    pair has been read from disk it's safe to keep serving it from memory
    - this avoids re-reading the (often large) raw excel bytes from disk
    on every Streamlit rerun."""
    entry = get_upload_by_id(upload_id)
    if not entry:
        return None
    path = PREPROCESS_DIR / upload_id / f"original{entry.get('original_ext', '.xlsx')}"
    if path.exists():
        return _read_bytes_cached(str(path), path.stat().st_mtime)
    return None


@lru_cache(maxsize=256)
def _read_csv_cached(path_str, mtime):
    return pd.read_csv(path_str)


def load_sheet_csv(upload_id, sheet_name):
    safe_sheet = str(sheet_name).replace("/", "_").replace("\\", "_")
    path = PREPROCESS_DIR / upload_id / f"{safe_sheet}.csv"
    if path.exists():
        # Return a copy so callers mutating the frame don't corrupt the
        # cached original used by other callers/reruns.
        return _read_csv_cached(str(path), path.stat().st_mtime).copy()
    return None


def get_available_snapshot_dates():
    """Sorted list of distinct calendar dates that have at least one saved upload."""
    return sorted({u["snapshot_date"] for u in get_all_uploads() if u.get("snapshot_date")})


# ==========================================
# 2. USER MANAGEMENT (roles: admin / manager / engineer)
# ==========================================
ALL_PLOTS = ["P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9", "P10"]
ROLES = ["engineer", "manager", "admin"]


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def load_users():
    return _read_json(USERS_FILE, {})


def save_users(users):
    _write_json(USERS_FILE, users)


_DEFAULT_ADMIN_USERNAME = "super_admin"
# Only the salted SHA-256 hash of this password is ever persisted to disk
# (see _hash_password below / users.json) - the plaintext never gets
# written anywhere and only exists transiently at first-run time.
_DEFAULT_ADMIN_PASSWORD = "super_admin@scada@56433"


def init_default_users():
    """Create a single default admin the very first time the app runs."""
    users = load_users()
    if not users:
        users = {
            _DEFAULT_ADMIN_USERNAME: {
                "password": _hash_password(_DEFAULT_ADMIN_PASSWORD),
                "role": "admin",
                "full_name": "System Administrator",
                "assigned_plots": ALL_PLOTS.copy(),
                "created_at": datetime.now().isoformat(),
                "created_by": "system",
            }
        }
        save_users(users)
    return users


def authenticate_user(username, password):
    users = load_users()
    user = users.get(username)
    if user and user.get("password") == _hash_password(password):
        return user
    return None


# ---- role capability helpers ----
def creatable_roles(current_role):
    """Which roles the current role is allowed to create."""
    if current_role == "admin":
        return ["engineer", "manager", "admin"]
    if current_role == "manager":
        return ["engineer"]
    return []


def can_manage_users(role):
    return role in ("admin", "manager")


def can_delete_users(role):
    return role == "admin"


def can_assign_plots(role):
    return role in ("admin", "manager")


def can_change_role(role):
    return role == "admin"


def can_view_audit_log(role):
    """Every authenticated role can see *some* slice of the audit log now
    - see get_audit_log_for() for the actual scoping rules."""
    return role in ("admin", "manager", "engineer")


def can_view_full_audit_log(role):
    return role == "admin"


# ---- CRUD ----
def create_user(username, password, role, full_name, assigned_plots, created_by):
    users = load_users()
    if not username or not password:
        return False, "Username and password are required."
    if username in users:
        return False, "Username already exists."
    if role not in ROLES:
        return False, "Invalid role."

    users[username] = {
        "password": _hash_password(password),
        "role": role,
        "full_name": full_name.strip() if full_name else username,
        "assigned_plots": assigned_plots or [],
        "created_at": datetime.now().isoformat(),
        "created_by": created_by,
    }
    save_users(users)
    return True, f"User '{username}' created."


def delete_user(username):
    users = load_users()
    if username in users:
        del users[username]
        save_users(users)
        return True
    return False


def update_user_plots(username, plots):
    users = load_users()
    if username in users:
        users[username]["assigned_plots"] = plots
        save_users(users)
        return True
    return False


def update_user_profile(username, full_name=None, new_password=None):
    users = load_users()
    if username not in users:
        return False
    if full_name is not None:
        users[username]["full_name"] = full_name.strip() or username
    if new_password:
        users[username]["password"] = _hash_password(new_password)
    save_users(users)
    return True


MIN_PASSWORD_LENGTH = 6


def reset_password(username, new_password):
    """Reset a user's password - used both for self-service 'change my
    password' and for an admin/manager resetting a password on behalf of
    someone who's locked themselves out."""
    if not new_password or len(new_password) < MIN_PASSWORD_LENGTH:
        return False, f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    users = load_users()
    if username not in users:
        return False, "User not found."
    users[username]["password"] = _hash_password(new_password)
    save_users(users)
    return True, "Password reset successfully."


def can_reset_password_for(actor_role, target_role):
    """Who is allowed to reset whose password.
    - admin can reset anyone's password (including other admins)
    - manager can only reset engineer passwords
    - engineer cannot reset anyone else's password
    """
    if actor_role == "admin":
        return True
    if actor_role == "manager":
        return target_role == "engineer"
    return False


# ==========================================
# 3. HASH-CHAINED AUDIT LOG
# ==========================================
def _load_audit():
    data = _read_json(AUDIT_FILE, {"entries": []})
    if "entries" not in data:
        data["entries"] = []
    return data


def _save_audit(data):
    _write_json(AUDIT_FILE, data)


def _entry_hash(body: dict) -> str:
    payload = json.dumps(body, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def log_audit_event(username, role, event_type, details=None):
    """
    event_type examples: 'login', 'logout', 'download_report',
    'user_created', 'user_deleted', 'plots_assigned', 'file_uploaded'

    Every entry stores the hash of the previous entry plus its own hash,
    so the log forms a tamper-evident chain (like a mini blockchain) -
    if any historical entry is edited, verify_audit_chain() will fail.
    """
    data = _load_audit()
    entries = data["entries"]
    prev_hash = entries[-1]["entry_hash"] if entries else "0"

    body = {
        "username": username,
        "role": role,
        "event_type": event_type,
        "details": details or {},
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "prev_hash": prev_hash,
    }
    body["entry_hash"] = _entry_hash(body)

    entries.append(body)
    data["entries"] = entries
    _save_audit(data)
    return body


def get_audit_log():
    return _load_audit().get("entries", [])


def verify_audit_chain():
    """True if no entry in the audit log has been tampered with."""
    entries = get_audit_log()
    for e in entries:
        body = {k: v for k, v in e.items() if k != "entry_hash"}
        if _entry_hash(body) != e.get("entry_hash"):
            return False
    return True


def get_audit_log_for(username, role):
    """Role-scoped view of the audit log:
      - admin:    every event, from every user
      - manager:  events logged by/about engineers only
      - engineer: only their own events
    """
    entries = get_audit_log()
    if role == "admin":
        return entries
    if role == "manager":
        return [e for e in entries if e.get("role") == "engineer"]
    if role == "engineer":
        return [e for e in entries if e.get("username") == username]
    return []
