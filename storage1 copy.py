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
import time
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
# UPLOAD LOCK - Prevent concurrent uploads
# ==========================================
_UPLOAD_LOCK = False
_UPLOAD_LOCK_FILE = PREPROCESS_DIR / ".upload_lock"


def _acquire_upload_lock():
    """Try to acquire upload lock to prevent concurrent uploads"""
    global _UPLOAD_LOCK
    if _UPLOAD_LOCK:
        return False
    
    try:
        if _UPLOAD_LOCK_FILE.exists():
            lock_time = _UPLOAD_LOCK_FILE.stat().st_mtime
            # If lock is older than 30 seconds, consider it stale
            if time.time() - lock_time > 30:
                _UPLOAD_LOCK_FILE.unlink()
            else:
                return False
        
        _UPLOAD_LOCK_FILE.write_text(str(time.time()))
        _UPLOAD_LOCK = True
        return True
    except:
        return False


def _release_upload_lock():
    """Release the upload lock"""
    global _UPLOAD_LOCK
    _UPLOAD_LOCK = False
    try:
        if _UPLOAD_LOCK_FILE.exists():
            _UPLOAD_LOCK_FILE.unlink()
    except:
        pass

# ==========================================
# GENERIC JSON HELPERS
# ==========================================
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
    _JSON_CACHE[path] = (mtime, data)


def _invalidate_cache():
    """Clear the JSON cache to force fresh reads"""
    global _JSON_CACHE
    _JSON_CACHE = {}

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
    _invalidate_cache()  # Force cache refresh after write


def save_preprocessed_upload(file_bytes, original_filename, processed_dataframes,
                              snapshot_date, uploaded_by):
    """
    Persist a newly uploaded + processed SCADA workbook.
    Ensures only ONE file is stored per snapshot_date.
    """
    # Acquire lock to prevent concurrent uploads
    if not _acquire_upload_lock():
        time.sleep(1)  # Wait a moment and try again
        if not _acquire_upload_lock():
            return None, "Upload in progress, please wait..."
    
    try:
        file_hash = hashlib.md5(file_bytes).hexdigest()
        snapshot_date_str = str(snapshot_date)
        
        # Load existing registry
        registry = _load_registry()
        
        # Check if we already have an upload for this date
        existing_upload = None
        existing_idx = None
        
        for idx, u in enumerate(registry["uploads"]):
            if u.get("snapshot_date") == snapshot_date_str:
                existing_upload = u
                existing_idx = idx
                break
        
        # If exists with same hash, return existing ID
        if existing_upload and existing_upload.get("file_hash") == file_hash:
            return existing_upload["upload_id"], f"File already exists for {snapshot_date_str}"
        
        # Generate unique upload ID
        timestamp = datetime.now()
        upload_id = f"{timestamp.strftime('%Y%m%d_%H%M%S')}_{file_hash[:8]}"
        
        # If we have an existing upload for this date, use the same ID to replace it
        if existing_upload and existing_idx is not None:
            upload_id = existing_upload["upload_id"]
            # Clean up old files
            old_path = PREPROCESS_DIR / upload_id
            import shutil
            if old_path.exists():
                shutil.rmtree(old_path)
        
        # Create upload directory
        upload_path = PREPROCESS_DIR / upload_id
        upload_path.mkdir(parents=True, exist_ok=True)
        
        # Save original file
        ext = Path(original_filename).suffix or ".xlsx"
        with open(upload_path / f"original{ext}", "wb") as f:
            f.write(file_bytes)
        
        # Save processed sheets
        saved_sheets = []
        for sheet_name, df in processed_dataframes.items():
            if df is not None and not df.empty:
                safe_sheet = str(sheet_name).replace("/", "_").replace("\\", "_")
                df.to_csv(upload_path / f"{safe_sheet}.csv", index=False)
                saved_sheets.append(sheet_name)
        
        # Create entry
        entry = {
            "upload_id": upload_id,
            "original_filename": original_filename,
            "original_ext": ext,
            "upload_timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "snapshot_date": snapshot_date_str,
            "file_hash": file_hash,
            "file_size": len(file_bytes),
            "uploaded_by": uploaded_by,
            "saved_sheets": saved_sheets,
        }
        
        # Update registry
        if existing_idx is not None:
            registry["uploads"][existing_idx] = entry
        else:
            registry["uploads"].append(entry)
        
        # Sort by timestamp
        registry["uploads"] = sorted(registry["uploads"], key=lambda x: x["upload_timestamp"])
        _save_registry(registry)
        
        return upload_id, f"File uploaded and saved for {snapshot_date_str}"
    
    finally:
        # Always release lock
        _release_upload_lock()


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
    _invalidate_cache()


_DEFAULT_ADMIN_USERNAME = "super_admin"
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
    if not new_password or len(new_password) < MIN_PASSWORD_LENGTH:
        return False, f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    users = load_users()
    if username not in users:
        return False, "User not found."
    users[username]["password"] = _hash_password(new_password)
    save_users(users)
    return True, "Password reset successfully."


def can_reset_password_for(actor_role, target_role):
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
    entries = get_audit_log()
    for e in entries:
        body = {k: v for k, v in e.items() if k != "entry_hash"}
        if _entry_hash(body) != e.get("entry_hash"):
            return False
    return True


def get_audit_log_for(username, role):
    entries = get_audit_log()
    if role == "admin":
        return entries
    if role == "manager":
        return [e for e in entries if e.get("role") == "engineer"]
    if role == "engineer":
        return [e for e in entries if e.get("username") == username]
    return []