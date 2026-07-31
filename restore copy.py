"""
restore.py
==========
Restore & TAT Analysis Module for PV SCADA Analytics.
Optimized with caching and session management for fast response.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
import hashlib
import re

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import storage1

# ==========================================
# CONFIGURATION
# ==========================================
WORKING_HOURS_START = 6
WORKING_HOURS_END = 18
WORKING_HOURS_PER_DAY = WORKING_HOURS_END - WORKING_HOURS_START
WORKING_CURRENT_THRESHOLD = 0.5
DEFAULT_TOTAL_ACTIVE_STRINGS = 19

DATA_DIR = Path("data")
HISTORY_FILE = DATA_DIR / "string_history.json"
DATA_DIR.mkdir(exist_ok=True)

INVERTER_ID_COLS = [
    "Inverter ID", "Inverter_ID", "Inverter", "ID",
    "Device Name", "String Inverter", "Inverters",
]

# Active string overrides matching app.py
ACTIVE_STRING_OVERRIDES = {
    "P2": {"IB1": 18, "IB3": 17, "IB4": 18, "IB5": 18},
    "P6": {"IB1": 18, "IB2": 18, "IB3": 18, "IB5": 18, "IB6": 18, "IB7": 18},
}

# Cache TTL in seconds
CACHE_TTL = 300  # 5 minutes
CACHE_LONG_TTL = 3600  # 1 hour


def sorted_filter_options(series):
    """Return non-null unique values sorted safely for Streamlit filters."""
    values = [value for value in series.dropna().unique()]

    def natural_key(value):
        text = str(value).strip()
        return tuple(
            (0, int(part)) if part.isdigit() else (1, part.casefold())
            for part in re.split(r"(\d+)", text)
        )

    return sorted(values, key=natural_key)


# ==========================================
# ROLE HELPERS
# ==========================================
def can_upload(user_role: str) -> bool:
    return str(user_role).strip().lower() == "admin"


# ==========================================
# STRING HISTORY MANAGEMENT (per-string TAT tracking)
# ==========================================
@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_string_history_cached():
    """Cached version of load_string_history for better performance."""
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            data.setdefault("strings", {})
            data.setdefault("last_updated", None)
            return data
        except Exception:
            return {"strings": {}, "last_updated": None}
    return {"strings": {}, "last_updated": None}


def load_string_history():
    """Load string history with caching."""
    return load_string_history_cached()


def save_string_history(history):
    """Save string history and invalidate cache."""
    history.setdefault("strings", {})
    history["last_updated"] = datetime.now().isoformat()
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)
    # Clear cache after save
    load_string_history_cached.clear()


def init_history():
    if not HISTORY_FILE.exists():
        save_string_history({"strings": {}, "last_updated": None})


def get_inverter_column(df: pd.DataFrame):
    df_columns_lower_map = {str(c).strip().lower(): c for c in df.columns}
    for col in INVERTER_ID_COLS:
        if col in df.columns:
            return col
        lower_col = col.strip().lower()
        if lower_col in df_columns_lower_map:
            return df_columns_lower_map[lower_col]
    return None


def get_pv_current_columns(df: pd.DataFrame):
    pv_cols = [c for c in df.columns if str(c).strip().startswith("PV-I")]

    def sort_key(name):
        try:
            return int(str(name).replace("PV-I", "").strip())
        except Exception:
            return 9999

    return sorted(pv_cols, key=sort_key)


def get_total_active_strings(plot, block):
    """Get total active strings for a plot/block combination."""
    plot_key = str(plot).strip().upper() if plot else ""
    block_key = str(block).strip().upper() if block else ""
    if plot_key in ACTIVE_STRING_OVERRIDES and block_key in ACTIVE_STRING_OVERRIDES[plot_key]:
        return ACTIVE_STRING_OVERRIDES[plot_key][block_key]
    return DEFAULT_TOTAL_ACTIVE_STRINGS


def update_string_history(df, date_str):
    """Update per-string status history with a given day's data."""
    if df is None or df.empty:
        return

    history = load_string_history()
    inverter_col = get_inverter_column(df)
    if not inverter_col:
        return

    pv_current_cols = get_pv_current_columns(df)
    if not pv_current_cols:
        return

    # Get total active strings per inverter
    df["Total_Active"] = df.apply(
        lambda row: get_total_active_strings(row.get("Plot"), row.get("Block")), axis=1
    )

    for _, row in df.iterrows():
        inverter_id = str(row[inverter_col])
        total_active = int(row.get("Total_Active", DEFAULT_TOTAL_ACTIVE_STRINGS))
        
        history["strings"].setdefault(inverter_id, {})
        history["strings"][inverter_id].setdefault("_metadata", {
            "plot": str(row.get("Plot", "")),
            "block": str(row.get("Block", "")),
            "sacu": str(row.get("SACU", "")),
            "total_active": total_active,
        })

        # Track all PV-I columns, but mark beyond total_active as "open"
        for i, col in enumerate(pv_current_cols, 1):
            string_id = f"PV-I{i}"
            current_value = pd.to_numeric(row.get(col), errors="coerce")
            
            # Determine status: working, failed, or open (no data)
            if i > total_active:
                status = "open"
            elif pd.isna(current_value):
                status = "open"
            elif current_value > WORKING_CURRENT_THRESHOLD:
                status = "working"
            else:
                status = "failed"

            if string_id not in history["strings"][inverter_id]:
                history["strings"][inverter_id][string_id] = {
                    "status_history": [],
                    "current_status": "unknown",
                    "last_change": None,
                }

            status_history = history["strings"][inverter_id][string_id]["status_history"]
            last_record = status_history[-1] if status_history else None

            should_add = (
                last_record is None
                or last_record.get("status") != status
                or last_record.get("date") != date_str
            )

            if should_add:
                status_history.append({
                    "date": date_str,
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "status": status,
                    "value": float(current_value) if pd.notna(current_value) else 0,
                })
                history["strings"][inverter_id][string_id]["current_status"] = status
                history["strings"][inverter_id][string_id]["last_change"] = datetime.now().isoformat()

    save_string_history(history)


# ==========================================
# SNAPSHOT ACCESS (delegates to storage.py)
# ==========================================
@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def get_available_snapshot_dates_cached():
    """Cached version of get_available_snapshot_dates."""
    return storage1.get_available_snapshot_dates()


def get_available_snapshot_dates():
    """Get available snapshot dates with caching."""
    return get_available_snapshot_dates_cached()


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_snapshot_sheet_cached(snapshot_date, sheet_name):
    """Cached version of load_snapshot_sheet."""
    entry = storage1.get_upload_for_date(snapshot_date)
    if not entry:
        return None
    return storage1.load_sheet_csv(entry["upload_id"], sheet_name)


def load_snapshot_sheet(snapshot_date, sheet_name):
    """Load snapshot sheet with caching."""
    return load_snapshot_sheet_cached(snapshot_date, sheet_name)


def get_snapshots_in_range(from_date, to_date):
    """Get all snapshot dates in the given range."""
    available = get_available_snapshot_dates()
    from_str, to_str = str(from_date), str(to_date)
    return [d for d in available if from_str <= d <= to_str]


# ==========================================
# STRING HISTORY MATRIX VIEW
# ==========================================
@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def build_all_strings_matrix_cached(inverter_id, date_start_str=None, date_end_str=None):
    """Cached version of build_all_strings_matrix."""
    # Convert date strings back to date objects
    date_start = datetime.strptime(date_start_str, "%Y-%m-%d").date() if date_start_str else None
    date_end = datetime.strptime(date_end_str, "%Y-%m-%d").date() if date_end_str else None
    
    history = load_string_history()
    strings = history.get("strings", {})
    
    if inverter_id not in strings:
        return pd.DataFrame()
    
    # Get total active strings from metadata
    total_active = strings[inverter_id].get("_metadata", {}).get("total_active", DEFAULT_TOTAL_ACTIVE_STRINGS)
    string_ids = [f"PV-I{i}" for i in range(1, total_active + 1)]
    
    # Also include any additional strings that might exist
    for s in strings[inverter_id].keys():
        if not s.startswith("_") and s not in string_ids:
            string_ids.append(s)
    string_ids = sorted(string_ids)
    
    # Build matrix
    matrix_data = {}
    all_dates = set()
    
    for string_id in string_ids:
        if string_id not in strings[inverter_id]:
            continue
        status_history = strings[inverter_id][string_id].get("status_history", [])
        
        # Filter by date range
        if date_start or date_end:
            filtered = []
            for record in status_history:
                try:
                    record_date = datetime.strptime(record.get("date", ""), "%Y-%m-%d").date()
                    if date_start and record_date < date_start:
                        continue
                    if date_end and record_date > date_end:
                        continue
                    filtered.append(record)
                except Exception:
                    continue
            status_history = filtered
        
        for record in status_history:
            date_str = record.get("date", "")
            status = record.get("status", "unknown")
            if date_str not in matrix_data:
                matrix_data[date_str] = {}
            matrix_data[date_str][string_id] = status
            all_dates.add(date_str)
    
    if not matrix_data:
        return pd.DataFrame()
    
    # Create DataFrame
    dates_sorted = sorted(all_dates)
    rows = []
    for date_str in dates_sorted:
        row_data = {"Date": date_str}
        for string_id in string_ids:
            row_data[string_id] = matrix_data.get(date_str, {}).get(string_id, "unknown")
        rows.append(row_data)
    
    return pd.DataFrame(rows)


def build_all_strings_matrix(inverter_id, date_start=None, date_end=None):
    """Build a matrix of daily status for all strings of an inverter."""
    date_start_str = date_start.strftime("%Y-%m-%d") if date_start else None
    date_end_str = date_end.strftime("%Y-%m-%d") if date_end else None
    return build_all_strings_matrix_cached(inverter_id, date_start_str, date_end_str)


def display_string_history_matrix(history, current_df):
    """Display the string history as a matrix with color coding."""
    st.subheader("📊 String History Matrix")
    st.caption("Daily status for each string across all inverters. Green = Working, Red = Failed, Gray = Open/NA")
    
    if not history.get("strings"):
        st.info("No string history available. Please upload SCADA data first.")
        return
    
    # Get all inverters with their metadata - cache this
    inverter_data = []
    for inv_id, inv_data in history["strings"].items():
        if inv_id.startswith("_"):
            continue
        metadata = inv_data.get("_metadata", {})
        inverter_data.append({
            "inverter": inv_id,
            "plot": metadata.get("plot", ""),
            "block": metadata.get("block", ""),
            "sacu": metadata.get("sacu", ""),
        })
    
    if not inverter_data:
        st.info("No inverters found in history.")
        return
    
    df_inverters = pd.DataFrame(inverter_data)
    
    # Filters
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        plots = ["All"] + sorted_filter_options(df_inverters["plot"])
        selected_plot = st.selectbox("Filter by Plot", plots, key="matrix_plot")
    
    filtered_inverters = df_inverters.copy()
    if selected_plot != "All":
        filtered_inverters = filtered_inverters[filtered_inverters["plot"] == selected_plot]
    
    with col2:
        blocks = ["All"] + sorted_filter_options(filtered_inverters["block"])
        selected_block = st.selectbox("Filter by Block", blocks, key="matrix_block")
    
    if selected_block != "All":
        filtered_inverters = filtered_inverters[filtered_inverters["block"] == selected_block]
    
    with col3:
        sacus = ["All"] + sorted_filter_options(filtered_inverters["sacu"])
        selected_sacu = st.selectbox("Filter by SACU", sacus, key="matrix_sacu")
    
    if selected_sacu != "All":
        filtered_inverters = filtered_inverters[filtered_inverters["sacu"] == selected_sacu]
    
    with col4:
        inverters = ["All"] + sorted_filter_options(filtered_inverters["inverter"])
        selected_inverter = st.selectbox("Select Inverter", inverters, key="matrix_inverter")
    
    # Date range
    col5, col6 = st.columns(2)
    with col5:
        available_dates = get_available_snapshot_dates()
        if available_dates:
            min_date = datetime.strptime(available_dates[0], "%Y-%m-%d").date()
            max_date = datetime.strptime(available_dates[-1], "%Y-%m-%d").date()
            date_range = st.date_input(
                "Date Range",
                value=(min_date, max_date),
                min_value=min_date,
                max_value=max_date,
                key="matrix_date_range"
            )
        else:
            date_range = (None, None)
    with col6:
        show_unknown = st.checkbox("Show Unknown Status", value=True, key="matrix_show_unknown")
        show_open = st.checkbox("Show Open/NA Status", value=True, key="matrix_show_open")
    
    # Parse date range
    date_start, date_end = None, None
    if isinstance(date_range, (list, tuple)) and len(date_range) == 2:
        date_start, date_end = date_range
    
    # Get list of inverters to display
    if selected_inverter != "All":
        display_inverters = [selected_inverter]
    else:
        # Limit display to avoid too many matrices
        display_inverters = filtered_inverters["inverter"].tolist()
        if len(display_inverters) > 10:
            st.warning(f"Showing first 10 of {len(display_inverters)} inverters. Please use filters to narrow down.")
            display_inverters = display_inverters[:10]
    
    if not display_inverters:
        st.info("No inverters match the selected filters.")
        return
    
    # Build and display matrix for each inverter
    for inverter_id in display_inverters:
        with st.spinner(f"Loading data for {inverter_id}..."):
            df_matrix = build_all_strings_matrix(inverter_id, date_start, date_end)
        
        if df_matrix.empty:
            st.warning(f"No history data available for inverter {inverter_id}")
            continue
        
        # Sort by date
        df_matrix = df_matrix.sort_values("Date")
        
        # Get metadata
        metadata = history["strings"].get(inverter_id, {}).get("_metadata", {})
        
        # Show inverter header
        st.markdown(f"### 🔌 Inverter: {inverter_id}")
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Plot", metadata.get("plot", "N/A"))
        with col2:
            st.metric("Block", metadata.get("block", "N/A"))
        with col3:
            st.metric("SACU", metadata.get("sacu", "N/A"))
        with col4:
            st.metric("Total Active Strings", metadata.get("total_active", DEFAULT_TOTAL_ACTIVE_STRINGS))
        
        # Prepare display data
        display_df = df_matrix.copy()
        display_df["Date"] = pd.to_datetime(display_df["Date"]).dt.strftime("%Y-%m-%d")
        
        # Color mapping function
        def color_status(val):
            if val == "working":
                return "background-color: #10b981; color: white; text-align: center; font-weight: bold;"
            elif val == "failed":
                return "background-color: #ef4444; color: white; text-align: center; font-weight: bold;"
            elif val == "open":
                return "background-color: #94a3b8; color: white; text-align: center;"
            else:
                return "background-color: #64748b; color: white; text-align: center;"
        
        # Apply styling
        styled_df = display_df.style.map(color_status)
        
        # Show the matrix
        st.dataframe(
            styled_df, 
            use_container_width=True,
            height=300,
            column_config={
                "Date": st.column_config.Column("Date", width="small"),
                **{col: st.column_config.Column(col, width="small") for col in display_df.columns if col != "Date"}
            }
        )
        
        # Summary stats for this inverter
        date_cols = [col for col in display_df.columns if col != "Date"]
        total_cells = len(display_df) * len(date_cols)
        if total_cells > 0:
            working_cells = sum((display_df[col] == "working").sum() for col in date_cols)
            failed_cells = sum((display_df[col] == "failed").sum() for col in date_cols)
            open_cells = sum((display_df[col] == "open").sum() for col in date_cols)
            unknown_cells = total_cells - working_cells - failed_cells - open_cells
            
            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("Total Cells", total_cells)
            c2.metric("✅ Working", f"{working_cells} ({working_cells/total_cells*100:.1f}%)")
            c3.metric("❌ Failed", f"{failed_cells} ({failed_cells/total_cells*100:.1f}%)")
            c4.metric("⚪ Open/NA", f"{open_cells} ({open_cells/total_cells*100:.1f}%)")
            c5.metric("❓ Unknown", f"{unknown_cells} ({unknown_cells/total_cells*100:.1f}%)")
        
        st.markdown("---")


# ==========================================
# TAT & RESTORE CALCULATIONS
# ==========================================
@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def calculate_failure_to_restore_tat_cached(history_hash, inverter_id, string_id, date_start_str=None, date_end_str=None):
    """Cached version of calculate_failure_to_restore_tat."""
    # Convert date strings back to date objects
    date_start = datetime.strptime(date_start_str, "%Y-%m-%d").date() if date_start_str else None
    date_end = datetime.strptime(date_end_str, "%Y-%m-%d").date() if date_end_str else None
    
    # We need to reload history since we can't pass the full object
    history = load_string_history()
    strings = history.get("strings", {})
    
    if inverter_id not in strings or string_id not in strings[inverter_id]:
        return []

    status_history = strings[inverter_id][string_id].get("status_history", [])
    if len(status_history) < 2:
        return []

    events = []
    last_failure_time = None

    for record in status_history:
        status = record.get("status", "")
        if status == "open" or status == "unknown":
            continue
            
        date = record.get("date", "")
        time_str = record.get("time", "00:00:00")

        try:
            event_time = datetime.strptime(f"{date} {time_str}", "%Y-%m-%d %H:%M:%S")
        except Exception:
            try:
                event_time = datetime.strptime(f"{date} 00:00:00", "%Y-%m-%d")
            except Exception:
                continue

        if date_start and event_time.date() < date_start:
            continue
        if date_end and event_time.date() > date_end:
            continue

        if status == "failed" and last_failure_time is None:
            last_failure_time = event_time
        elif status == "working" and last_failure_time is not None:
            restore_time = event_time
            total_minutes = 0
            current_time = last_failure_time
            while current_time < restore_time:
                if WORKING_HOURS_START <= current_time.hour < WORKING_HOURS_END:
                    total_minutes += 60
                current_time += timedelta(minutes=60)

            events.append({
                "failure_date": last_failure_time.strftime("%Y-%m-%d %H:%M:%S"),
                "restore_date": restore_time.strftime("%Y-%m-%d %H:%M:%S"),
                "tat_working_hours": round(total_minutes / 60, 2),
                "tat_actual_hours": round((restore_time - last_failure_time).total_seconds() / 3600, 2),
                "status": "restored",
            })
            last_failure_time = None

    if last_failure_time is not None:
        events.append({
            "failure_date": last_failure_time.strftime("%Y-%m-%d %H:%M:%S"),
            "restore_date": "Not restored yet",
            "tat_working_hours": "Ongoing",
            "tat_actual_hours": "Ongoing",
            "status": "ongoing_failure",
        })

    return events


def calculate_failure_to_restore_tat(history, inverter_id, string_id, date_start=None, date_end=None):
    """Calculate failure -> restore TAT events with caching."""
    # Create a hash of the history for cache key
    history_hash = hashlib.md5(json.dumps(history, sort_keys=True).encode()).hexdigest()
    date_start_str = date_start.strftime("%Y-%m-%d") if date_start else None
    date_end_str = date_end.strftime("%Y-%m-%d") if date_end else None
    return calculate_failure_to_restore_tat_cached(history_hash, inverter_id, string_id, date_start_str, date_end_str)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def calculate_current_failure_hours_cached(history_hash, inverter_id, string_id, date_start_str=None, date_end_str=None):
    """Cached version of calculate_current_failure_hours."""
    date_start = datetime.strptime(date_start_str, "%Y-%m-%d").date() if date_start_str else None
    date_end = datetime.strptime(date_end_str, "%Y-%m-%d").date() if date_end_str else None
    
    history = load_string_history()
    strings = history.get("strings", {})
    
    if inverter_id not in strings or string_id not in strings[inverter_id]:
        return 0
    
    status_history = strings[inverter_id][string_id].get("status_history", [])
    if len(status_history) < 2:
        return 0
    
    failure_hours = 0
    is_failing = False
    failure_start = None
    
    for record in status_history:
        status = record.get("status", "")
        if status == "open" or status == "unknown":
            continue
            
        date = record.get("date", "")
        time_str = record.get("time", "00:00:00")
        
        try:
            event_time = datetime.strptime(f"{date} {time_str}", "%Y-%m-%d %H:%M:%S")
        except Exception:
            try:
                event_time = datetime.strptime(f"{date} 00:00:00", "%Y-%m-%d")
            except Exception:
                continue
        
        if date_start and event_time.date() < date_start:
            continue
        if date_end and event_time.date() > date_end:
            continue
        
        if status == "failed" and not is_failing:
            is_failing = True
            failure_start = event_time
        elif status == "working" and is_failing:
            if failure_start:
                current_time = failure_start
                while current_time < event_time:
                    if WORKING_HOURS_START <= current_time.hour < WORKING_HOURS_END:
                        failure_hours += 1
                    current_time += timedelta(hours=1)
            is_failing = False
            failure_start = None
    
    if is_failing and failure_start:
        end_time = datetime.now()
        if date_end:
            try:
                end_time = datetime.combine(date_end, datetime.max.time())
            except Exception:
                pass
        current_time = failure_start
        while current_time < end_time:
            if WORKING_HOURS_START <= current_time.hour < WORKING_HOURS_END:
                failure_hours += 1
            current_time += timedelta(hours=1)
    
    return failure_hours


def calculate_current_failure_hours(history, inverter_id, string_id, date_start=None, date_end=None):
    """Calculate current failure hours with caching."""
    history_hash = hashlib.md5(json.dumps(history, sort_keys=True).encode()).hexdigest()
    date_start_str = date_start.strftime("%Y-%m-%d") if date_start else None
    date_end_str = date_end.strftime("%Y-%m-%d") if date_end else None
    return calculate_current_failure_hours_cached(history_hash, inverter_id, string_id, date_start_str, date_end_str)


# ==========================================
# CALENDAR (FROM DATE -> TO DATE) COMPARISON
# ==========================================
@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def compare_two_snapshots_by_date_cached(old_date, new_date, sheet_name="Sheet1"):
    """Cached version of compare_two_snapshots_by_date."""
    df_old = load_snapshot_sheet(old_date, sheet_name)
    df_new = load_snapshot_sheet(new_date, sheet_name)

    if df_old is None or df_new is None:
        return None, f"No saved snapshot data found for '{sheet_name}' on {old_date} and/or {new_date}."

    inverter_col = get_inverter_column(df_old) or get_inverter_column(df_new)
    if not inverter_col:
        return None, "Could not detect an inverter identifier column in the snapshots."

    id_cols = ["Plot", "Block", inverter_col]
    pv_cols = get_pv_current_columns(df_old)
    required_cols = id_cols + pv_cols + ["Failed String Count", "Total Active Strings", "Working String Count"]

    for name, df in [("Baseline", df_old), ("Latest", df_new)]:
        missing = [c for c in required_cols if c not in df.columns]
        if missing:
            return None, f"{name} snapshot ({old_date if name=='Baseline' else new_date}) is missing columns: {missing}"

    op_hours = WORKING_HOURS_PER_DAY

    df_old_prep = df_old[required_cols].copy()
    df_new_prep = df_new[required_cols].copy()

    rename_cols = pv_cols + ["Failed String Count", "Total Active Strings", "Working String Count"]
    for col in rename_cols:
        df_old_prep.rename(columns={col: f"{col}_old"}, inplace=True)
        df_new_prep.rename(columns={col: f"{col}_new"}, inplace=True)

    merged_df = pd.merge(df_old_prep, df_new_prep, on=id_cols, how="outer")

    for suffix in ["_old", "_new"]:
        for col in pv_cols:
            merged_df[f"{col}{suffix}"] = pd.to_numeric(merged_df[f"{col}{suffix}"], errors="coerce").fillna(0)
        for base in ["Failed String Count", "Working String Count", "Total Active Strings"]:
            merged_df[f"{base}{suffix}"] = pd.to_numeric(merged_df[f"{base}{suffix}"], errors="coerce").fillna(0).astype(int)

    baseline_pv_values = []
    for col in pv_cols:
        col_series = pd.to_numeric(df_old[col], errors="coerce")
        baseline_pv_values.extend(col_series[col_series > WORKING_CURRENT_THRESHOLD].dropna().tolist())
    baseline_avg_working_current = round(pd.Series(baseline_pv_values).mean(), 2) if baseline_pv_values else 10.0

    results = []
    for _, row in merged_df.iterrows():
        plot = row.get("Plot", "")
        block = row.get("Block", "")
        total_active = get_total_active_strings(plot, block)
        
        data = {
            "Plot": row["Plot"],
            "Block": row["Block"],
            inverter_col: row[inverter_col],
            "Failed_to_Working": 0,
            "Working_to_Failed": 0,
            "Current_Failure_Hours": 0,
            "Restoration_TAT_Hours": 0,
        }
        
        for i in range(1, total_active + 1):
            pv_col_name = f"PV-I{i}"
            if pv_col_name not in pv_cols:
                continue

            pv_old = row.get(f"{pv_col_name}_old", 0)
            pv_new = row.get(f"{pv_col_name}_new", 0)
            
            old_valid = pd.notna(pv_old) and pv_old > 0
            new_valid = pd.notna(pv_new) and pv_new > 0

            is_working_old = old_valid and pv_old > WORKING_CURRENT_THRESHOLD
            is_working_new = new_valid and pv_new > WORKING_CURRENT_THRESHOLD
            
            if old_valid and new_valid:
                if not is_working_old and is_working_new:
                    data["Failed_to_Working"] += 1
                    data["Restoration_TAT_Hours"] += op_hours
                elif is_working_old and not is_working_new:
                    data["Working_to_Failed"] += 1
                    data["Current_Failure_Hours"] += op_hours
                elif not is_working_old and not is_working_new:
                    data["Current_Failure_Hours"] += op_hours
            elif old_valid and not new_valid:
                data["Working_to_Failed"] += 1
                data["Current_Failure_Hours"] += op_hours

        results.append(data)

    df_history = pd.DataFrame(results)

    merge_back_cols = ["Plot", "Block", inverter_col,
                        "Failed String Count_old", "Failed String Count_new",
                        "Total Active Strings_old", "Total Active Strings_new",
                        "Working String Count_old", "Working String Count_new"]
    df_history = pd.merge(df_history, merged_df[merge_back_cols], on=["Plot", "Block", inverter_col], how="left")

    df_history["Lost_Current_Old"] = df_history["Failed String Count_old"] * baseline_avg_working_current
    df_history["Lost_Current_New"] = df_history["Failed String Count_new"] * baseline_avg_working_current
    df_history["Change_Lost_Current"] = df_history["Lost_Current_New"] - df_history["Lost_Current_Old"]

    return {
        "df_history": df_history,
        "baseline_avg_working_current": baseline_avg_working_current,
        "operational_hours": op_hours,
    }, None


def compare_two_snapshots_by_date(old_date, new_date, sheet_name="Sheet1"):
    """Compare two snapshots with caching."""
    return compare_two_snapshots_by_date_cached(old_date, new_date, sheet_name)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def build_range_trend_data_cached(from_date, to_date, sheet_name="Sheet1"):
    """Cached version of build_range_trend_data."""
    from_str, to_str = str(from_date), str(to_date)
    dates_in_range = [d for d in storage1.get_available_snapshot_dates() if from_str <= d <= to_str]

    rows = []
    for d in sorted(dates_in_range):
        df = load_snapshot_sheet(d, sheet_name)
        if df is None or df.empty:
            continue
        needed = {"Plot", "Block", "Working String Count", "Failed String Count", "Total Active Strings"}
        if not needed.issubset(df.columns):
            continue

        grouped = df.groupby(["Plot", "Block"], as_index=False).agg(
            Working=("Working String Count", "sum"),
            Failed=("Failed String Count", "sum"),
            Total=("Total Active Strings", "sum"),
        )
        grouped["Date"] = d
        grouped["Availability (%)"] = (grouped["Working"] / grouped["Total"] * 100).fillna(0).round(2)
        rows.append(grouped)

    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True)


def build_range_trend_data(from_date, to_date, sheet_name="Sheet1"):
    """Build trend data with caching."""
    return build_range_trend_data_cached(from_date, to_date, sheet_name)


# ==========================================
# UI FUNCTIONS
# ==========================================
def display_upload_registry(user_role, upload_handler=None):
    """Display upload registry."""
    st.subheader("📤 Snapshot Upload Registry")
    st.caption(
        "SCADA workbooks are uploaded from the main sidebar (admin only) and "
        "automatically saved here, day-wise, so they can be compared later."
    )

    if can_upload(user_role) and upload_handler:
        with st.expander("⬆️ Backfill a Previous Date's Snapshot", expanded=False):
            st.caption(
                "Upload a SCADA workbook for any past calendar date. It will be "
                "processed and stored exactly as if it had been uploaded that day, "
                "so it's available for history, TAT and calendar-comparison."
            )
            backfill_date = st.date_input(
                "Snapshot Date", value=datetime.now().date(),
                max_value=datetime.now().date(), key="restore_backfill_date",
            )
            backfill_file = st.file_uploader(
                "SCADA Excel (.xlsx)", type=["xlsx"], key="restore_backfill_file",
            )
            if backfill_file is not None and st.button("Process & Save Snapshot", key="restore_backfill_btn"):
                ok, msg = upload_handler(backfill_file.getvalue(), backfill_file.name, backfill_date)
                if ok:
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)

    uploads = storage1.get_all_uploads()
    if not uploads:
        st.info("No snapshots uploaded yet.")
        return

    df_uploads = pd.DataFrame(uploads)[
        ["snapshot_date", "original_filename", "uploaded_by", "upload_timestamp", "saved_sheets"]
    ].sort_values("snapshot_date", ascending=False)
    df_uploads.columns = ["Snapshot Date", "File Name", "Uploaded By", "Upload Time", "Sheets Saved"]
    st.dataframe(df_uploads, use_container_width=True)


def display_summary_dashboard(history, current_df):
    """Display summary dashboard."""
    st.subheader("📊 Summary Dashboard")

    if not history.get("strings"):
        st.info("No string history available. Please upload SCADA data first.")
        return

    total_inverters = len([k for k in history["strings"].keys() if not k.startswith("_")])
    total_strings = 0
    total_failures = 0
    total_restorations = 0
    all_failures = []

    for inverter_id, strings in history["strings"].items():
        if inverter_id.startswith("_"):
            continue
        for string_id, data in strings.items():
            if string_id.startswith("_"):
                continue
            total_strings += 1
            status_history = data.get("status_history", [])
            for i in range(1, len(status_history)):
                prev_status = status_history[i - 1].get("status")
                curr_status = status_history[i].get("status")
                if curr_status == "failed" and prev_status == "working":
                    total_failures += 1
                    all_failures.append({
                        "inverter": inverter_id,
                        "string": string_id,
                        "date": status_history[i].get("date", ""),
                    })
                elif curr_status == "working" and prev_status == "failed":
                    total_restorations += 1

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Total Inverters", total_inverters)
    col2.metric("Total Strings", total_strings)
    col3.metric("Total Failures", total_failures)
    col4.metric("Total Restorations", total_restorations)

    if current_df is not None and not current_df.empty:
        total_active = current_df["Total Active Strings"].sum() if "Total Active Strings" in current_df.columns else 0
        working = current_df["Working String Count"].sum() if "Working String Count" in current_df.columns else 0
        availability = (working / total_active * 100) if total_active > 0 else 0
        col5.metric("Current Availability", f"{availability:.1f}%")

    st.markdown("---")

    if all_failures:
        df_failures = pd.DataFrame(all_failures)
        df_failures["date"] = pd.to_datetime(df_failures["date"], errors="coerce")
        daily_failures = df_failures.groupby(df_failures["date"].dt.date).size().reset_index(name="failures")
        fig = px.bar(daily_failures, x="date", y="failures", title="Daily Failure Count",
                     labels={"date": "Date", "failures": "Number of Failures"})
        fig.update_layout(height=400)
        st.plotly_chart(fig, use_container_width=True)

    status_counts = {"working": 0, "failed": 0, "open": 0}
    if current_df is not None and not current_df.empty:
        status_counts["working"] = int(current_df["Working String Count"].sum()) if "Working String Count" in current_df.columns else 0
        status_counts["failed"] = int(current_df["Failed String Count"].sum()) if "Failed String Count" in current_df.columns else 0

    col_a, col_b = st.columns([2, 1])
    with col_a:
        if status_counts["working"] + status_counts["failed"] + status_counts["open"] > 0:
            fig_status = go.Figure(data=[go.Pie(
                labels=["Working", "Failed", "Open/NA"],
                values=[status_counts["working"], status_counts["failed"], status_counts["open"]],
                hole=0.5,
                marker_colors=["#10b981", "#ef4444", "#94a3b8"],
                textinfo="label+percent+value",
            )])
            fig_status.update_layout(title="Current String Status", height=350)
            st.plotly_chart(fig_status, use_container_width=True)

    with col_b:
        st.write("History last updated")
        st.info(str(history.get("last_updated", "N/A")))


def display_string_analysis(history, current_df):
    """Display string TAT analysis."""
    st.subheader("🔌 String TAT Analysis")

    if not history.get("strings"):
        st.info("No string history available.")
        return

    inverters = [inv for inv in history["strings"].keys() if not inv.startswith("_")]
    if not inverters:
        st.info("No inverters found in history.")
        return

    col1, col2, col3 = st.columns([1, 1, 1])
    with col1:
        selected_inverter = st.selectbox(
            "Select Inverter", 
            sorted(inverters),
            key="tat_inverter"
        )
    with col2:
        string_ids = [s for s in history["strings"].get(selected_inverter, {}).keys() if not s.startswith("_")]
        selected_strings = st.multiselect(
            "Select Strings (or all)",
            options=sorted(string_ids),
            default=sorted(string_ids)[:5] if len(string_ids) > 5 else sorted(string_ids),
            key="tat_strings"
        )
    with col3:
        date_range = st.date_input(
            "Date Range",
            value=(datetime.now().date() - timedelta(days=30), datetime.now().date()),
            max_value=datetime.now().date(),
            key="tat_date_range"
        )

    if not selected_strings:
        st.warning("Please select at least one string.")
        return

    start_date, end_date = None, None
    if isinstance(date_range, (list, tuple)) and len(date_range) == 2:
        start_date, end_date = date_range

    events_data = []
    with st.spinner("Calculating TAT events..."):
        for string_id in selected_strings:
            for event in calculate_failure_to_restore_tat(history, selected_inverter, string_id, start_date, end_date):
                events_data.append({
                    "Inverter": selected_inverter,
                    "String": string_id,
                    "Failure Date": event["failure_date"],
                    "Restore Date": event["restore_date"],
                    "TAT (Working Hours)": event["tat_working_hours"],
                    "TAT (Actual Hours)": event["tat_actual_hours"],
                    "Status": event["status"],
                })

    if not events_data:
        st.info("No TAT events found for the selected criteria.")
        return

    df_events = pd.DataFrame(events_data)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Events", len(df_events))
    
    tat_values = df_events[df_events["TAT (Working Hours)"] != "Ongoing"]["TAT (Working Hours)"]
    avg_tat = pd.to_numeric(tat_values, errors='coerce').mean()
    c2.metric("Avg TAT (Working Hours)", f"{avg_tat:.1f}h" if pd.notna(avg_tat) else "N/A")
    
    restored = len(df_events[df_events["Status"] == "restored"])
    c3.metric("Total Restorations", restored)
    
    ongoing = len(df_events[df_events["Status"] == "ongoing_failure"])
    c4.metric("Ongoing Failures", ongoing)

    st.dataframe(df_events, use_container_width=True)

    df_tat = df_events[df_events["TAT (Working Hours)"] != "Ongoing"].copy()
    if not df_tat.empty:
        df_tat["TAT (Working Hours)"] = pd.to_numeric(df_tat["TAT (Working Hours)"], errors="coerce")
        
        fig_tat = px.bar(
            df_tat, 
            x="String", 
            y="TAT (Working Hours)",
            color="Status",
            title="TAT by String (Working Hours)",
            labels={"TAT (Working Hours)": "TAT (Hours)"}
        )
        fig_tat.update_layout(height=400)
        st.plotly_chart(fig_tat, use_container_width=True)


def display_tat_tracking(history, current_df):
    """Display TAT tracking."""
    st.subheader("⏱️ TAT Tracking")

    if not history.get("strings"):
        st.info("No TAT history available.")
        return

    rows = []
    with st.spinner("Loading TAT data..."):
        for inverter_id, strings in history["strings"].items():
            if inverter_id.startswith("_"):
                continue
            for string_id in strings.keys():
                if string_id.startswith("_"):
                    continue
                for event in calculate_failure_to_restore_tat(history, inverter_id, string_id):
                    rows.append({
                        "Inverter": inverter_id, 
                        "String": string_id,
                        "Failure Date": event["failure_date"], 
                        "Restore Date": event["restore_date"],
                        "TAT Working Hours": event["tat_working_hours"],
                        "TAT Actual Hours": event["tat_actual_hours"], 
                        "Status": event["status"],
                    })

    if not rows:
        st.info("No TAT events found.")
        return

    df_tat = pd.DataFrame(rows)
    st.dataframe(df_tat, use_container_width=True)

    restored = df_tat[df_tat["Status"] == "restored"].copy()
    if not restored.empty:
        restored["TAT Working Hours"] = pd.to_numeric(restored["TAT Working Hours"], errors="coerce")
        fig = px.histogram(restored, x="TAT Working Hours", nbins=20, title="Distribution of TAT Working Hours")
        fig.update_layout(height=400)
        st.plotly_chart(fig, use_container_width=True)


def display_working_hours_analysis(history, current_df):
    """Display working hours analysis."""
    st.subheader("⏰ Working Hours")
    st.write(f"Configured working hours: {WORKING_HOURS_START}:00 to {WORKING_HOURS_END}:00")
    st.write(f"Working hours counted per full day: {WORKING_HOURS_PER_DAY} hours")

    available_dates = get_available_snapshot_dates()
    if available_dates:
        st.dataframe(pd.DataFrame({"Snapshot Date": available_dates}), use_container_width=True)
    else:
        st.info("No saved snapshot dates available.")


def display_calendar_comparison(sheet_name="Sheet1"):
    """Display calendar comparison."""
    st.subheader("📅 Calendar-wise Comparison (From Date -> To Date)")

    available_dates = get_available_snapshot_dates()
    if len(available_dates) < 2:
        st.info("At least 2 saved snapshot dates are required for comparison.")
        return

    min_date = datetime.strptime(available_dates[0], "%Y-%m-%d").date()
    max_date = datetime.strptime(available_dates[-1], "%Y-%m-%d").date()

    col1, col2 = st.columns(2)
    with col1:
        from_date = st.date_input("From Date", value=min_date, min_value=min_date, max_value=max_date, key="restore_range_from")
    with col2:
        to_date = st.date_input("To Date", value=max_date, min_value=min_date, max_value=max_date, key="restore_range_to")

    if from_date > to_date:
        st.error("From Date must be on or before To Date.")
        return

    with st.spinner("Loading comparison data..."):
        result, error = compare_two_snapshots_by_date(str(from_date), str(to_date), sheet_name)
    
    if error:
        st.error(error)
    else:
        df_history = result["df_history"]
        baseline_current = result["baseline_avg_working_current"]

        c1, c2, c3 = st.columns(3)
        c1.metric("From Date", from_date.strftime("%Y-%m-%d"))
        c2.metric("To Date", to_date.strftime("%Y-%m-%d"))
        c3.metric("Baseline Avg Working Current", f"{baseline_current:.2f} A")

        st.dataframe(
            df_history.sort_values(by="Current_Failure_Hours", ascending=False),
            use_container_width=True,
        )
        st.caption("Time-based metrics assume one interval equals working hours from 6 AM to 6 PM.")

    st.markdown("---")
    st.markdown("#### 📈 Trend Across the Selected Range")

    with st.spinner("Loading trend data..."):
        df_trend = build_range_trend_data(from_date, to_date, sheet_name)
    
    if df_trend.empty:
        st.info("No trend data available for this range.")
        return

    fig_avail = px.line(
        df_trend, x="Date", y="Availability (%)", color="Plot", line_group="Block",
        markers=True, title="Availability Trend by Plot / Block",
    )
    fig_avail.update_layout(height=450)
    st.plotly_chart(fig_avail, use_container_width=True)

    fig_wf = px.bar(
        df_trend, x="Date", y=["Working", "Failed"], facet_col="Plot",
        barmode="stack", title="Working vs Failed Strings Over Time (by Plot)",
        color_discrete_sequence=["#10b981", "#ef4444"],
    )
    fig_wf.update_layout(height=450)
    st.plotly_chart(fig_wf, use_container_width=True)


def display_previous_data(processed_dataframes=None, sheet_name="Sheet1"):
    """Display previous preprocessed data."""
    st.subheader("📋 Previous Preprocessed Data")
    st.caption("View previously processed SCADA data from saved snapshots.")
    
    available_dates = get_available_snapshot_dates()
    if not available_dates:
        st.info("No preprocessed data available.")
        return
    
    selected_date = st.selectbox(
        "Select Snapshot Date",
        sorted(available_dates, reverse=True),
        key="prev_data_date"
    )
    
    if not selected_date:
        return
    
    with st.spinner(f"Loading data from {selected_date}..."):
        df = load_snapshot_sheet(selected_date, sheet_name)
    
    if df is None or df.empty:
        st.warning(f"No data found for {selected_date} in sheet '{sheet_name}'.")
        return
    
    st.caption(f"Showing data from **{selected_date}**")
    
    col1, col2, col3 = st.columns(3)
    with col1:
        plots = ["All"] + sorted([p for p in df["Plot"].dropna().unique() if p])
        selected_plot = st.selectbox("Plot", plots, key="prev_plot")
    with col2:
        filtered_by_plot = df if selected_plot == "All" else df[df["Plot"] == selected_plot]
        blocks = ["All"] + sorted([b for b in filtered_by_plot["Block"].dropna().unique() if b])
        selected_block = st.selectbox("Block", blocks, key="prev_block")
    with col3:
        filtered_by_block = filtered_by_plot if selected_block == "All" else filtered_by_plot[filtered_by_plot["Block"] == selected_block]
        sacus = ["All"] + sorted([s for s in filtered_by_block["SACU"].dropna().unique() if s])
        selected_sacu = st.selectbox("SACU", sacus, key="prev_sacu")
    
    filtered_df = df.copy()
    if selected_plot != "All":
        filtered_df = filtered_df[filtered_df["Plot"] == selected_plot]
    if selected_block != "All":
        filtered_df = filtered_df[filtered_df["Block"] == selected_block]
    if selected_sacu != "All":
        filtered_df = filtered_df[filtered_df["SACU"] == selected_sacu]
    
    if not filtered_df.empty:
        inverter_col = get_inverter_column(filtered_df)
        total_inverters = filtered_df[inverter_col].nunique() if inverter_col else 0
        total_strings = int(filtered_df["Total Active Strings"].sum()) if "Total Active Strings" in filtered_df.columns else 0
        working = int(filtered_df["Working String Count"].sum()) if "Working String Count" in filtered_df.columns else 0
        failed = int(filtered_df["Failed String Count"].sum()) if "Failed String Count" in filtered_df.columns else 0
        availability = (working / total_strings * 100) if total_strings > 0 else 0
        
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Inverters", total_inverters)
        c2.metric("Total Strings", total_strings)
        c3.metric("Working", working)
        c4.metric("Failed", failed)
        c5.metric("Availability", f"{availability:.1f}%")
    
    st.dataframe(filtered_df, use_container_width=True)
    
    if not filtered_df.empty:
        csv = filtered_df.to_csv(index=False)
        st.download_button(
            label="📥 Download Filtered Data (CSV)",
            data=csv,
            file_name=f"snapshot_{selected_date}_{sheet_name}.csv",
            mime="text/csv",
            use_container_width=True
        )


# ==========================================
# MAIN ENTRY
# ==========================================
def display_tat_dashboard(processed_dataframes=None, current_df=None, sheet_name="Sheet1",
                           user_role="viewer", username="unknown", upload_handler=None):
    """Main entry point for the Restore & TAT dashboard."""
    st.title("🔄 Restore & TAT Analysis")
    st.caption("Day-wise SCADA snapshots power history, TAT, and calendar comparisons.")
    
    init_history()
    history = load_string_history()

    if current_df is not None and not current_df.empty:
        current_date = datetime.now().strftime("%Y-%m-%d")
        update_string_history(current_df, current_date)
        history = load_string_history()

    tabs = st.tabs([
        "📤 Upload Registry",
        "📊 Summary Dashboard",
        "📅 String History Matrix",
        "🔌 String TAT Analysis",
        "⏱️ TAT Tracking",
        "⏰ Working Hours",
        "📅 Calendar Compare",
        "📋 Previous Data",
    ])

    with tabs[0]:
        display_upload_registry(user_role, upload_handler=upload_handler)

    with tabs[1]:
        display_summary_dashboard(history, current_df)

    with tabs[2]:
        display_string_history_matrix(history, current_df)

    with tabs[3]:
        display_string_analysis(history, current_df)

    with tabs[4]:
        display_tat_tracking(history, current_df)

    with tabs[5]:
        display_working_hours_analysis(history, current_df)

    with tabs[6]:
        display_calendar_comparison(sheet_name)
    
    with tabs[7]:
        display_previous_data(processed_dataframes, sheet_name)


# ==========================================
# BACKWARD COMPATIBILITY WRAPPER
# ==========================================
def get_restore_tab(processed_dataframes=None, filtered_df=None, sheet_name="Sheet1",
                     user_role="viewer", username="unknown", process_scada_excel=None,
                     upload_handler=None):
    """Kept for backward compatibility with older app.py calls."""
    return display_tat_dashboard(
        processed_dataframes=processed_dataframes,
        current_df=filtered_df,
        sheet_name=sheet_name,
        user_role=user_role,
        username=username,
        upload_handler=upload_handler,
    )