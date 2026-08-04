"""
storage1.py
==========
Single source of truth for all "backend" persistence used by app.py and
restore.py. No database is used - everything lives under ./data as JSON
(for metadata/registries) and CSV (for processed sheet snapshots).

Sections:
  1. Preprocessed-upload registry  (data/preprocessed/registry.json)
  2. User management incl. roles   (data/users.json)
  3. Hash-chained audit log        (data/audit_log.json)
  4. Super-admin user-data backup / restore (data/backups)

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
SESSIONS_FILE = DATA_DIR / "sessions.json"
MAINTENANCE_FILE = DATA_DIR / "maintenance.json"

BACKUP_DIR = DATA_DIR / "backups"
BACKUP_DIR.mkdir(exist_ok=True)

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
    except Exception:
        return False


def _release_upload_lock():
    """Release the upload lock"""
    global _UPLOAD_LOCK
    _UPLOAD_LOCK = False
    try:
        if _UPLOAD_LOCK_FILE.exists():
            _UPLOAD_LOCK_FILE.unlink()
    except Exception:
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
# 0. LOGIN SESSIONS (persist across page refresh, valid for the same day)
# ==========================================
# Streamlit's st.session_state is tied to the browser websocket connection -
# it is wiped out on a hard page refresh (F5), forcing the user to log in
# again. To avoid that, we hand back a random session token that the
# frontend keeps in the URL (st.query_params), and validate it against this
# server-side registry on every rerun. A session is valid until local
# midnight of the day it was created - after that it must be re-created via
# a fresh login, even if the token is still present in the URL.
import secrets


def _load_sessions():
    data = _read_json(SESSIONS_FILE, {"sessions": {}})
    if "sessions" not in data:
        data["sessions"] = {}
    return data


def _save_sessions(data):
    _write_json(SESSIONS_FILE, data)


def _end_of_today_iso():
    now = datetime.now()
    end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=0)
    return end_of_day.isoformat()


def create_session(username):
    """Create a new login session token, valid until the end of today."""
    data = _load_sessions()
    token = secrets.token_urlsafe(24)
    data["sessions"][token] = {
        "username": username,
        "created_at": datetime.now().isoformat(),
        "expires_at": _end_of_today_iso(),
    }
    _save_sessions(data)
    return token


def validate_session(token):
    """Return the username for a still-valid session token, else None.
    Expired/unknown sessions are cleaned up as they're encountered."""
    if not token:
        return None
    data = _load_sessions()
    session = data["sessions"].get(token)
    if not session:
        return None
    try:
        expires_at = datetime.fromisoformat(session["expires_at"])
    except Exception:
        expires_at = None
    if expires_at is not None and datetime.now() > expires_at:
        del data["sessions"][token]
        _save_sessions(data)
        return None
    return session.get("username")


def invalidate_session(token):
    """Remove a session token (used on logout)."""
    if not token:
        return
    data = _load_sessions()
    if token in data["sessions"]:
        del data["sessions"][token]
        _save_sessions(data)


def cleanup_expired_sessions():
    """Drop every session token that has already expired."""
    data = _load_sessions()
    now = datetime.now()
    kept = {}
    for token, session in data["sessions"].items():
        try:
            expires_at = datetime.fromisoformat(session["expires_at"])
        except Exception:
            continue
        if now <= expires_at:
            kept[token] = session
    data["sessions"] = kept
    _save_sessions(data)


# ==========================================
# MAINTENANCE MODE (super admin only)
# ==========================================
DEFAULT_MAINTENANCE_MESSAGE = "Dear Sir, we are under maintenance. Please wait."


def get_maintenance_status():
    """Returns dict: {enabled: bool, message: str, enabled_by: str|None, enabled_at: str|None}."""
    data = _read_json(MAINTENANCE_FILE, {
        "enabled": False,
        "message": DEFAULT_MAINTENANCE_MESSAGE,
        "enabled_by": None,
        "enabled_at": None,
    })
    data.setdefault("message", DEFAULT_MAINTENANCE_MESSAGE)
    return data


def set_maintenance_mode(enabled, username, message=None):
    """Super-admin-only: turn maintenance mode on/off, optionally with a custom message."""
    data = {
        "enabled": bool(enabled),
        "message": message or DEFAULT_MAINTENANCE_MESSAGE,
        "enabled_by": username if enabled else None,
        "enabled_at": datetime.now().isoformat() if enabled else None,
    }
    _write_json(MAINTENANCE_FILE, data)
    return data

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

        # STORAGE OPTIMIZATION: the original uploaded .xlsx is intentionally
        # NOT written to disk. Only the processed (preprocessed) CSV sheets
        # are persisted - they're all the app ever needs to render dashboards
        # or history, and skipping the raw workbook avoids storing two
        # copies of the same data. `file_bytes` is only used in-memory here
        # (for hashing/dedup detection) and by the caller for the current
        # request; it is never written to `data/preprocessed/<upload_id>/`.
        ext = Path(original_filename).suffix or ".xlsx"

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
    """The 'current' snapshot is the one with the latest snapshot_date, not
    simply the most recently-uploaded entry - otherwise backfilling an older
    calendar date (Restore & TAT -> Upload Registry) would incorrectly
    become the 'latest' snapshot shown across the app just because it was
    uploaded most recently in wall-clock time."""
    uploads = get_all_uploads()
    if not uploads:
        return None
    return max(
        uploads,
        key=lambda u: (str(u.get("snapshot_date", "")), str(u.get("upload_timestamp", ""))),
    )


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
    """The original uploaded workbook is no longer stored on disk (only the
    processed CSV sheets are, for storage optimization) - this always
    returns None now. Kept only so any older caller doesn't hard-crash;
    use get_processed_dataframes_for_date() / load_sheet_csv() instead."""
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


def get_processed_dataframes_for_date(snapshot_date):
    """
    Load every already-preprocessed sheet that was saved for a given calendar
    date, straight from the CSV cache (no re-parsing of the original workbook
    needed). Powers the header "calendar" selector on the main dashboard so
    users can jump between previously uploaded snapshots.
    Returns (dict[sheet_name -> DataFrame] or None, registry entry or None).
    """
    entry = get_upload_for_date(snapshot_date)
    if not entry:
        return None, None
    dfs = {}
    for sheet_name in entry.get("saved_sheets", []):
        df = load_sheet_csv(entry["upload_id"], sheet_name)
        if df is not None:
            dfs[sheet_name] = df
    return (dfs or None), entry


def verify_upload_integrity(upload_id):
    """
    Confirm that a registry entry's underlying files actually exist on disk
    (original workbook + every saved sheet CSV). Redeploys/volume resets can
    leave a "ghost" registry entry with no backing files, which otherwise
    fails silently deep inside pandas. Returns (is_ok: bool, message: str).
    """
    entry = get_upload_by_id(upload_id)
    if not entry:
        return False, "Not found in registry"

    upload_path = PREPROCESS_DIR / upload_id

    missing_sheets = []
    for sheet_name in entry.get("saved_sheets", []):
        safe_sheet = str(sheet_name).replace("/", "_").replace("\\", "_")
        if not (upload_path / f"{safe_sheet}.csv").exists():
            missing_sheets.append(str(sheet_name))

    if missing_sheets:
        return False, f"Missing sheet CSV(s): {', '.join(missing_sheets)}"

    return True, "OK"


def get_upload_registry_report():
    """Every registry entry plus an on-disk integrity flag, for a health check UI."""
    report = []
    for entry in get_all_uploads():
        ok, msg = verify_upload_integrity(entry["upload_id"])
        report.append({**entry, "integrity_ok": ok, "integrity_message": msg})
    return report


def delete_upload(upload_id):
    """Admin-only: permanently delete one snapshot's saved CSVs and remove
    it from the registry. Returns (success: bool, message: str)."""
    registry = _load_registry()
    match_idx = None
    for idx, u in enumerate(registry["uploads"]):
        if u.get("upload_id") == upload_id:
            match_idx = idx
            break

    if match_idx is None:
        return False, "Snapshot not found in registry."

    entry = registry["uploads"][match_idx]
    upload_path = PREPROCESS_DIR / upload_id
    try:
        if upload_path.exists():
            import shutil
            shutil.rmtree(upload_path, ignore_errors=True)
    except Exception as exc:
        return False, f"Could not delete files on disk: {exc}"

    del registry["uploads"][match_idx]
    _save_registry(registry)
    return True, f"Deleted snapshot for {entry.get('snapshot_date', upload_id)}."


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

# Public alias so app.py can identify the super-admin account without
# reaching into a "private" module attribute.
SUPER_ADMIN_USERNAME = _DEFAULT_ADMIN_USERNAME


def is_super_admin(username):
    """True only for the single reserved super-admin account. User-data
    backup/restore is intentionally restricted to this identity rather than
    to the whole 'admin' role, since regular admins can be created/removed
    by the super admin and shouldn't be able to overwrite login data."""
    return username == SUPER_ADMIN_USERNAME


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
# 4. SUPER-ADMIN USER-DATA BACKUP / RESTORE
# ==========================================
# app.py's login/user records (data/users.json) are lost whenever the app
# is redeployed/updated without a persistent volume. These helpers let the
# super admin export the current login data and, later, restore ONLY the
# accounts that are missing (existing accounts are never touched/overwritten)
# so nobody's current password/role gets silently clobbered by an old backup.
def export_users_backup():
    """Return raw JSON bytes of the current users.json for download."""
    users = load_users()
    payload = {
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "user_count": len(users),
        "users": users,
    }
    data = json.dumps(payload, indent=2, default=str).encode("utf-8")

    # Also keep a dated copy server-side as a safety net.
    try:
        snap_path = BACKUP_DIR / f"users_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(snap_path, "wb") as f:
            f.write(data)
    except Exception:
        pass

    return data


def import_users_backup(backup_bytes):
    """
    Restore missing users from a previously exported backup file.
    Only usernames that do NOT already exist in the current users.json are
    added back - existing users (their current passwords/roles/plots) are
    left completely untouched.
    Returns (success: bool, message: str, restored_usernames: list[str]).
    """
    try:
        payload = json.loads(backup_bytes.decode("utf-8"))
    except Exception:
        return False, "Invalid backup file - could not parse JSON.", []

    if isinstance(payload, dict) and "users" in payload and isinstance(payload["users"], dict):
        backup_users = payload["users"]
    elif isinstance(payload, dict):
        # Allow restoring from a raw users.json (no wrapper) too.
        backup_users = payload
    else:
        return False, "Invalid backup file - unexpected format.", []

    current_users = load_users()
    restored = []
    for username, user_data in backup_users.items():
        if username not in current_users and isinstance(user_data, dict):
            current_users[username] = user_data
            restored.append(username)

    if restored:
        save_users(current_users)
        return True, f"Restored {len(restored)} missing user(s): {', '.join(restored)}", restored
    return True, "No missing users found - backup matched current data. Nothing was changed.", []


def list_user_backups():
    """Return server-side backup snapshots ordered from newest to oldest."""
    backups = []
    backup_root = BACKUP_DIR.resolve()

    for path in sorted(BACKUP_DIR.glob("users_backup_*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            resolved_path = path.resolve()
            if resolved_path.parent != backup_root:
                continue

            stat_info = path.stat()
            user_count = "?"
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    if isinstance(payload.get("user_count"), int):
                        user_count = payload["user_count"]
                    elif isinstance(payload.get("users"), dict):
                        user_count = len(payload["users"])
            except Exception:
                pass

            backups.append({
                "filename": path.name,
                "created": datetime.fromtimestamp(stat_info.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "size_kb": round(stat_info.st_size / 1024, 1),
                "user_count": user_count,
            })
        except Exception:
            continue

    return backups


def get_user_backup_bytes(filename):
    """Return the raw bytes for a saved backup snapshot."""
    backup_root = BACKUP_DIR.resolve()
    path = (backup_root / filename).resolve()
    if path.parent != backup_root or not path.exists() or not path.is_file():
        return None
    return path.read_bytes()


def delete_user_backup(filename):
    """Delete a single saved backup snapshot."""
    backup_root = BACKUP_DIR.resolve()
    path = (backup_root / filename).resolve()
    if path.parent != backup_root or not path.exists() or not path.is_file():
        return False, "Backup snapshot not found."

    try:
        path.unlink()
        return True, f"Deleted backup snapshot {path.name}."
    except Exception as exc:
        return False, f"Could not delete backup snapshot: {exc}"


def delete_all_user_backups():
    """Delete every server-side saved backup snapshot."""
    deleted = 0
    for path in BACKUP_DIR.glob("users_backup_*.json"):
        try:
            if path.is_file():
                path.unlink()
                deleted += 1
        except Exception:
            continue

    if deleted == 0:
        return True, "No saved backups were found."
    return True, f"Deleted {deleted} saved backup snapshot(s)."


def reset_application_data():
    """Remove uploaded SCADA data and audit history without touching user accounts."""
    details = {
        "uploads_deleted": 0,
        "audit_log_cleared": False,
    }

    try:
        if PREPROCESS_DIR.exists():
            for entry in PREPROCESS_DIR.iterdir():
                if entry.name == PREPROCESS_REGISTRY_FILE.name:
                    continue
                if entry.is_dir():
                    import shutil
                    shutil.rmtree(entry, ignore_errors=True)
                    details["uploads_deleted"] += 1
                elif entry.is_file():
                    try:
                        entry.unlink()
                    except Exception:
                        pass

        _save_registry({"uploads": []})
        _release_upload_lock()
        _save_audit({"entries": []})
        details["audit_log_cleared"] = True
        return True, "Application data reset successfully.", details
    except Exception as exc:
        return False, f"Application reset failed: {exc}", details


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


def delete_audit_entries(entry_hashes):
    """
    Super-admin-only: permanently remove one or more audit log entries
    (identified by their entry_hash) belonging to ANY user, then rebuild
    the hash chain for everything that remains so verify_audit_chain()
    still reports the log as intact.
    Returns (success: bool, message: str, deleted_count: int).
    """
    if not entry_hashes:
        return False, "No entries selected.", 0

    to_delete = set(entry_hashes)
    data = _load_audit()
    entries = data.get("entries", [])

    kept = [e for e in entries if e.get("entry_hash") not in to_delete]
    deleted_count = len(entries) - len(kept)

    if deleted_count == 0:
        return False, "None of the selected entries were found.", 0

    # Rebuild the prev_hash / entry_hash chain over the surviving entries so
    # the chain stays internally consistent (each entry's hash still
    # depends on the previous surviving entry, not the deleted ones).
    rebuilt = []
    prev_hash = "0"
    for e in kept:
        body = {k: v for k, v in e.items() if k != "entry_hash"}
        body["prev_hash"] = prev_hash
        body["entry_hash"] = _entry_hash(body)
        rebuilt.append(body)
        prev_hash = body["entry_hash"]

    data["entries"] = rebuilt
    _save_audit(data)
    return True, f"Deleted {deleted_count} audit log entr{'y' if deleted_count == 1 else 'ies'}.", deleted_count