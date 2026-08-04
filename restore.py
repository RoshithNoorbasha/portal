"""
restore.py
==========
Restore & TAT Analysis Module for PV SCADA Analytics.
Optimized with caching, session-backed pagination, and a trimmed set of
tabs focused on: registry health, a fast 3-day summary, a full string
history matrix, working hours reference, and calendar comparison.
"""

import json
import io
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

DEFAULT_PAGE_SIZE = 25


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
# PAGINATION HELPER (session-state backed, so switching tabs/filters
# doesn't reset your place, and large tables never render in one shot)
# ==========================================
def paginate_dataframe(df, page_size=DEFAULT_PAGE_SIZE, key_prefix="pg"):
    """Slice a dataframe into pages using session_state, with Prev/Next
    controls and a direct page-number jump. Returns the current page slice."""
    total_rows = len(df)
    if total_rows == 0:
        st.caption("No rows to display.")
        return df

    total_pages = max(1, (total_rows - 1) // page_size + 1)
    page_key = f"{key_prefix}_page"

    if page_key not in st.session_state:
        st.session_state[page_key] = 1
    # Clamp in case the filtered result shrank since the last run
    st.session_state[page_key] = max(1, min(st.session_state[page_key], total_pages))

    nav_col1, nav_col2, nav_col3 = st.columns([1, 2, 1])
    with nav_col1:
        if st.button("Previous", key=f"{key_prefix}_prev_btn",
                     disabled=st.session_state[page_key] <= 1, use_container_width=True):
            st.session_state[page_key] -= 1
    with nav_col3:
        if st.button("Next", key=f"{key_prefix}_next_btn",
                     disabled=st.session_state[page_key] >= total_pages, use_container_width=True):
            st.session_state[page_key] += 1
    with nav_col2:
        st.number_input(
            "Page", min_value=1, max_value=total_pages,
            key=page_key, step=1, label_visibility="collapsed",
        )

    page = st.session_state[page_key]
    start = (page - 1) * page_size
    end = start + page_size
    st.caption(f"Rows {start + 1}-{min(end, total_rows)} of {total_rows} - Page {page}/{total_pages}")
    return df.iloc[start:end]


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
    if df is None or df.empty:
        return None
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
    df = df.copy()
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
# SNAPSHOT ACCESS (delegates to storage1.py)
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
# BLOCK -> INVERTER WORKING/FAILED MATRIX (day-wise), built straight from
# the preprocessed CSV snapshots - it does NOT need string_history.json.
# Each day's CSV already has each inverter's PV-I currents, which is all
# that's needed to classify that inverter as working/failed for that day,
# so this reads the daily snapshots directly instead of a separate
# cumulative history file.
# ==========================================
@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def build_inverter_history_matrix(plot, block, date_start_str, date_end_str, max_dates, sheet_name="Sheet1"):
    """
    For a given Plot+Block, returns (df_matrix, dates_sorted):
      df_matrix: one row per inverter, one column per date, cell = "Working"/"Failed"/"No Data"
                 (an inverter counts as Working for a date if it has at least one PV string
                 above the working-current threshold that day).
      dates_sorted: the date columns used, oldest -> newest.
    Also returns a per-date working/failed COUNT summary for the block.
    """
    available_dates = get_available_snapshot_dates()
    dates_in_range = [
        d for d in available_dates
        if (not date_start_str or d >= date_start_str) and (not date_end_str or d <= date_end_str)
    ]
    dates_sorted = sorted(dates_in_range)[-max_dates:] if max_dates else sorted(dates_in_range)

    inverter_status_by_date = {}  # {inverter_id: {date: "Working"/"Failed"}}
    inverter_meta = {}

    for date_str in dates_sorted:
        day_df = load_snapshot_sheet(date_str, sheet_name)
        if day_df is None or day_df.empty:
            continue
        inverter_col = get_inverter_column(day_df)
        if not inverter_col:
            continue
        if "Plot" in day_df.columns:
            day_df = day_df[day_df["Plot"].astype(str).str.strip() == str(plot).strip()]
        if "Block" in day_df.columns:
            day_df = day_df[day_df["Block"].astype(str).str.strip() == str(block).strip()]
        if day_df.empty:
            continue

        pv_cols = get_pv_current_columns(day_df)
        for _, row in day_df.iterrows():
            inv_id = str(row[inverter_col])
            numeric_vals = pd.to_numeric(row[pv_cols], errors="coerce") if pv_cols else pd.Series(dtype=float)
            has_working = (numeric_vals > WORKING_CURRENT_THRESHOLD).any() if len(numeric_vals) else False
            inverter_status_by_date.setdefault(inv_id, {})[date_str] = "Working" if has_working else "Failed"
            inverter_meta.setdefault(inv_id, {"sacu": str(row.get("SACU", ""))})

    if not inverter_status_by_date:
        return pd.DataFrame(), [], pd.DataFrame()

    rows = []
    for inv_id in sorted(inverter_status_by_date.keys()):
        row = {"Inverter": inv_id, "SACU": inverter_meta.get(inv_id, {}).get("sacu", "")}
        for date_str in dates_sorted:
            row[date_str] = inverter_status_by_date[inv_id].get(date_str, "No Data")
        rows.append(row)
    df_matrix = pd.DataFrame(rows)

    # Per-date working/failed counts for the block (for a summary chart)
    count_rows = []
    for date_str in dates_sorted:
        working = sum(1 for inv_id in inverter_status_by_date if inverter_status_by_date[inv_id].get(date_str) == "Working")
        failed = sum(1 for inv_id in inverter_status_by_date if inverter_status_by_date[inv_id].get(date_str) == "Failed")
        count_rows.append({"Date": date_str, "Working": working, "Failed": failed})
    df_counts = pd.DataFrame(count_rows)

    return df_matrix, dates_sorted, df_counts


def display_inverter_history_matrix():
    """New tab: Block-wise inverter working/failed history, built directly
    from preprocessed snapshots (answers 'is string_history.json mandatory?'
    - no, this view proves the daily CSVs alone are enough)."""
    st.subheader("Inverter History Matrix (Block-wise)")
    st.caption(
        "Each inverter's working/failed status, day-wise, for one Block - read directly "
        "from the preprocessed daily snapshots. Green = Working, Red = Failed, Gray = No Data."
    )

    available_dates = get_available_snapshot_dates()
    if not available_dates:
        st.info("No snapshot data available yet.")
        return

    # Build the Plot/Block choice list from the most recent snapshot
    latest_df = load_snapshot_sheet(available_dates[-1], "Sheet1")
    if latest_df is None or latest_df.empty or "Plot" not in latest_df.columns:
        st.info("Could not read Plot/Block list from the latest snapshot.")
        return

    col1, col2 = st.columns(2)
    with col1:
        plots = sorted_filter_options(latest_df["Plot"])
        selected_plot = st.selectbox("Plot (required)", plots, key="inv_matrix_plot") if plots else None

    blocks_df = latest_df[latest_df["Plot"] == selected_plot] if selected_plot else latest_df
    with col2:
        blocks = sorted_filter_options(blocks_df["Block"]) if "Block" in blocks_df.columns else []
        selected_block = st.selectbox("Block (required)", blocks, key="inv_matrix_block") if blocks else None

    if not selected_plot or not selected_block:
        st.info("Select a **Plot** and a **Block** to build the matrix.")
        return

    col3, col4 = st.columns(2)
    with col3:
        min_date = datetime.strptime(available_dates[0], "%Y-%m-%d").date()
        max_date = datetime.strptime(available_dates[-1], "%Y-%m-%d").date()
        date_range = st.date_input(
            "Date Range", value=(min_date, max_date),
            min_value=min_date, max_value=max_date, key="inv_matrix_date_range",
        )
    with col4:
        max_dates_shown = st.slider("Most recent dates to show", min_value=5, max_value=60, value=30, key="inv_matrix_max_dates")

    date_start, date_end = (None, None)
    if isinstance(date_range, (list, tuple)) and len(date_range) == 2:
        date_start, date_end = date_range

    with st.spinner("Building inverter history matrix from preprocessed snapshots..."):
        df_matrix, dates_sorted, df_counts = build_inverter_history_matrix(
            selected_plot, selected_block,
            str(date_start) if date_start else None, str(date_end) if date_end else None,
            max_dates_shown,
        )

    if df_matrix.empty:
        st.info("No matrix data available for the selected Plot/Block/date range.")
        return

    m1, m2, m3 = st.columns(3)
    m1.metric("Inverters", len(df_matrix))
    m2.metric("Dates Shown", len(dates_sorted))
    if not df_counts.empty:
        m3.metric("Working Today (latest date)", int(df_counts.iloc[-1]["Working"]))

    if not df_counts.empty:
        st.markdown("##### Working vs Failed Inverters, Date-wise")
        st.bar_chart(df_counts.set_index("Date")[["Working", "Failed"]])

    def color_inv_status(val):
        if val == "Working":
            return "background-color: #10b981; color: white; font-weight: bold;"
        if val == "Failed":
            return "background-color: #ef4444; color: white; font-weight: bold;"
        return "background-color: #64748b; color: white;"

    st.markdown("##### Inverter x Date Matrix")
    date_cols = [c for c in df_matrix.columns if c not in ("Inverter", "SACU")]
    st.dataframe(
        df_matrix.style.map(color_inv_status, subset=date_cols),
        use_container_width=True, height=450,
    )

    # ---- Colorful Excel export ----
    from openpyxl.styles import PatternFill, Font
    status_fill = {
        "Working": PatternFill("solid", fgColor="10B981"),
        "Failed": PatternFill("solid", fgColor="EF4444"),
        "No Data": PatternFill("solid", fgColor="64748B"),
    }
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df_matrix.to_excel(writer, sheet_name="Inverter Matrix", index=False)
        ws = writer.sheets["Inverter Matrix"]
        header_fill = PatternFill("solid", fgColor="1E293B")
        header_font = Font(bold=True, color="FFFFFF")
        for cell in ws[1]:
            cell.fill = header_fill
            cell.font = header_font
        col_letters = {col: idx + 1 for idx, col in enumerate(df_matrix.columns)}
        for row_idx in range(2, ws.max_row + 1):
            for col_name in date_cols:
                col_idx = col_letters[col_name]
                cell = ws.cell(row=row_idx, column=col_idx)
                fill = status_fill.get(cell.value)
                if fill:
                    cell.fill = fill
                    cell.font = Font(bold=True, color="FFFFFF")
        if not df_counts.empty:
            df_counts.to_excel(writer, sheet_name="Date-wise Counts", index=False)
    buffer.seek(0)

    st.download_button(
        label="Download Inverter History Matrix (Excel, color-coded)",
        data=buffer.getvalue(),
        file_name=f"inverter_history_matrix_{selected_plot}_{selected_block}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="download_inverter_matrix_xlsx",
    )


# ==========================================
# BLOCK -> INVERTER -> STRING HISTORY MATRIX (day-wise)
# ==========================================
@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def build_full_string_matrix_cached(history_hash, plot, block, sacu, inverter,
                                     date_start_str, date_end_str, max_dates):
    """
    Cached builder for the full Block -> Inverter -> String status matrix,
    one row per string, one column per calendar date. Built once per unique
    (filters + history-content) combination instead of per inverter, so
    switching pages/filters doesn't re-walk the whole history each time.
    """
    history = load_string_history()
    strings = history.get("strings", {})

    date_start = date_start_str  # already "YYYY-MM-DD" strings, compare lexicographically
    date_end = date_end_str

    rows = []
    dates_set = set()

    for inv_id, inv_data in strings.items():
        if inv_id.startswith("_"):
            continue
        metadata = inv_data.get("_metadata", {})
        p = metadata.get("plot", "")
        b = metadata.get("block", "")
        s = metadata.get("sacu", "")

        if plot and plot != "All" and p != plot:
            continue
        if block and block != "All" and b != block:
            continue
        if sacu and sacu != "All" and s != sacu:
            continue
        if inverter and inverter != "All" and inv_id != inverter:
            continue

        for string_id, sdata in inv_data.items():
            if string_id.startswith("_"):
                continue
            status_history = sdata.get("status_history", [])
            row_statuses = {}
            for record in status_history:
                d = record.get("date", "")
                if date_start and d < date_start:
                    continue
                if date_end and d > date_end:
                    continue
                row_statuses[d] = record.get("status", "unknown")
                dates_set.add(d)

            if row_statuses:
                rows.append({
                    "Plot": p, "Block": b, "SACU": s,
                    "Inverter": inv_id, "String": string_id,
                    **row_statuses,
                })

    dates_sorted = sorted(dates_set)
    if max_dates and len(dates_sorted) > max_dates:
        dates_sorted = dates_sorted[-max_dates:]  # keep the most recent N dates

    if not rows:
        return pd.DataFrame(), dates_sorted

    df = pd.DataFrame(rows)
    for d in dates_sorted:
        if d not in df.columns:
            df[d] = "unknown"

    ordered_cols = ["Plot", "Block", "SACU", "Inverter", "String"] + dates_sorted
    ordered_cols = [c for c in ordered_cols if c in df.columns]
    df = df[ordered_cols].fillna("unknown")
    df = df.sort_values(["Plot", "Block", "Inverter", "String"]).reset_index(drop=True)
    return df, dates_sorted


def build_full_string_matrix(plot="All", block="All", sacu="All", inverter="All",
                              date_start=None, date_end=None, max_dates=30):
    """Wrapper that hashes the current history so the cached builder above
    only re-runs when the underlying data (or filters) actually change."""
    history = load_string_history()
    history_hash = hashlib.md5(json.dumps(history, sort_keys=True).encode()).hexdigest()
    date_start_str = date_start.strftime("%Y-%m-%d") if date_start else None
    date_end_str = date_end.strftime("%Y-%m-%d") if date_end else None
    return build_full_string_matrix_cached(
        history_hash, plot, block, sacu, inverter, date_start_str, date_end_str, max_dates
    )


def display_string_history_matrix(history, current_df):
    """Block -> Inverter -> String status matrix, one column per date."""
    st.subheader("String History Matrix")
    st.caption(
        "Every string's day-wise status, grouped by Block and Inverter. "
        "Green = Working, Red = Failed, Gray = Open/NA, Slate = Unknown."
    )

    if not history.get("strings"):
        st.info("No string history available yet. Upload SCADA data first.")
        return

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

    # ---- Filters ----
    # Plot and Block are now REQUIRED (no "All") - building the matrix across
    # every plot/block at once was the slow part of this tab, since it had
    # to walk the full status history for every string in the whole plant.
    # SACU/Inverter stay optional narrowing filters once Plot+Block are set.
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        plots = sorted_filter_options(df_inverters["plot"])
        selected_plot = st.selectbox("Plot (required)", plots, key="matrix_plot") if plots else None

    filtered_inverters = df_inverters.copy()
    if selected_plot:
        filtered_inverters = filtered_inverters[filtered_inverters["plot"] == selected_plot]

    with col2:
        blocks = sorted_filter_options(filtered_inverters["block"])
        selected_block = st.selectbox("Block (required)", blocks, key="matrix_block") if blocks else None

    if selected_block:
        filtered_inverters = filtered_inverters[filtered_inverters["block"] == selected_block]

    with col3:
        sacus = ["All"] + sorted_filter_options(filtered_inverters["sacu"])
        selected_sacu = st.selectbox("SACU", sacus, key="matrix_sacu")

    if selected_sacu != "All":
        filtered_inverters = filtered_inverters[filtered_inverters["sacu"] == selected_sacu]

    with col4:
        inverters = ["All"] + sorted_filter_options(filtered_inverters["inverter"])
        selected_inverter = st.selectbox("Inverter", inverters, key="matrix_inverter")

    if not selected_plot or not selected_block:
        st.info("Select a **Plot** and a **Block** above to build the matrix - this keeps it fast by only loading that block's strings instead of the whole plant.")
        return

    col5, col6, col7 = st.columns(3)
    with col5:
        available_dates = get_available_snapshot_dates()
        if available_dates:
            min_date = datetime.strptime(available_dates[0], "%Y-%m-%d").date()
            max_date = datetime.strptime(available_dates[-1], "%Y-%m-%d").date()
            date_range = st.date_input(
                "Date Range", value=(min_date, max_date),
                min_value=min_date, max_value=max_date, key="matrix_date_range",
            )
        else:
            date_range = (None, None)
    with col6:
        max_dates_shown = st.slider("Most recent dates to show", min_value=5, max_value=60, value=30, key="matrix_max_dates")
    with col7:
        page_size = st.selectbox("Rows per page", [15, 25, 50, 100], index=1, key="matrix_page_size")

    date_start, date_end = None, None
    if isinstance(date_range, (list, tuple)) and len(date_range) == 2:
        date_start, date_end = date_range

    with st.spinner("Building string history matrix..."):
        df_matrix, dates_sorted = build_full_string_matrix(
            plot=selected_plot, block=selected_block, sacu=selected_sacu,
            inverter=selected_inverter, date_start=date_start, date_end=date_end,
            max_dates=max_dates_shown,
        )

    if df_matrix.empty:
        st.info("No matrix data available for the selected filters.")
        return

    total_strings = len(df_matrix)
    total_inverters_shown = df_matrix["Inverter"].nunique()
    total_blocks_shown = df_matrix["Block"].nunique()

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Strings", total_strings)
    m2.metric("Inverters", total_inverters_shown)
    m3.metric("Blocks", total_blocks_shown)
    m4.metric("Dates Shown", len(dates_sorted))

    st.markdown("---")

    page_df = paginate_dataframe(df_matrix, page_size=page_size, key_prefix="matrix")

    def color_status(val):
        if val == "working":
            return "background-color: #10b981; color: white; text-align: center; font-weight: bold;"
        elif val == "failed":
            return "background-color: #ef4444; color: white; text-align: center; font-weight: bold;"
        elif val == "open":
            return "background-color: #94a3b8; color: white; text-align: center;"
        else:
            return "background-color: #64748b; color: white; text-align: center;"

    try:
        styled_page = page_df.style.map(color_status, subset=dates_sorted)
        st.dataframe(styled_page, use_container_width=True, height=520)
    except Exception:
        st.dataframe(page_df, use_container_width=True, height=520)

    # Full-window (unpaginated) summary stats, computed once on the whole matrix
    if dates_sorted:
        total_cells = total_strings * len(dates_sorted)
        working_cells = int((df_matrix[dates_sorted] == "working").sum().sum())
        failed_cells = int((df_matrix[dates_sorted] == "failed").sum().sum())
        open_cells = int((df_matrix[dates_sorted] == "open").sum().sum())
        unknown_cells = total_cells - working_cells - failed_cells - open_cells

        st.markdown("---")
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Total Cells", f"{total_cells:,}")
        c2.metric("Working", f"{working_cells:,} ({working_cells / total_cells * 100:.1f}%)" if total_cells else "0")
        c3.metric("Failed", f"{failed_cells:,} ({failed_cells / total_cells * 100:.1f}%)" if total_cells else "0")
        c4.metric("Open/NA", f"{open_cells:,} ({open_cells / total_cells * 100:.1f}%)" if total_cells else "0")
        c5.metric("Unknown", f"{unknown_cells:,} ({unknown_cells / total_cells * 100:.1f}%)" if total_cells else "0")

    csv_matrix = df_matrix.to_csv(index=False)
    st.download_button(
        "Download Full Matrix (CSV)", data=csv_matrix,
        file_name=f"string_history_matrix_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        mime="text/csv", key="matrix_download_btn",
    )


# ==========================================
# SNAPSHOT-TO-SNAPSHOT COMPARISON (powers both the 3-day Summary
# Dashboard and the Calendar Compare tab)
# ==========================================
@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def compare_two_snapshots_by_date_cached(old_date, new_date, sheet_name="Sheet1"):
    """Cached version of compare_two_snapshots_by_date. Diffs every PV-I
    string between two saved snapshots directly (not the JSON history file),
    so it's accurate even for dates that predate string_history.json."""
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
            return None, f"{name} snapshot ({old_date if name == 'Baseline' else new_date}) is missing columns: {missing}"

    op_hours = WORKING_HOURS_PER_DAY

    df_old_prep = df_old[required_cols].copy()
    df_new_prep = df_new[required_cols].copy()

    # Normalize the join keys - SCADA exports frequently have inconsistent
    # whitespace between two dates' workbooks (e.g. "Block 1" vs "Block 1 "),
    # which used to make the outer merge below treat the same inverter as
    # two different rows, silently corrupting the comparison. Also drop any
    # exact-duplicate id rows so the merge can't fan out into duplicates.
    for prep_df in (df_old_prep, df_new_prep):
        for col in id_cols:
            prep_df[col] = prep_df[col].astype(str).str.strip()
        prep_df.drop_duplicates(subset=id_cols, keep="last", inplace=True)

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
            "Restored_Strings": [],
            "Newly_Failed_Strings": [],
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
                    data["Restored_Strings"].append(pv_col_name)
                elif is_working_old and not is_working_new:
                    data["Working_to_Failed"] += 1
                    data["Current_Failure_Hours"] += op_hours
                    data["Newly_Failed_Strings"].append(pv_col_name)
                elif not is_working_old and not is_working_new:
                    data["Current_Failure_Hours"] += op_hours
            elif old_valid and not new_valid:
                data["Working_to_Failed"] += 1
                data["Current_Failure_Hours"] += op_hours
                data["Newly_Failed_Strings"].append(pv_col_name)

        data["Restored_Strings"] = ", ".join(data["Restored_Strings"])
        data["Newly_Failed_Strings"] = ", ".join(data["Newly_Failed_Strings"])
        results.append(data)

    df_history = pd.DataFrame(results)

    merge_back_cols = ["Plot", "Block", inverter_col,
                        "Failed String Count_old", "Failed String Count_new",
                        "Total Active Strings_old", "Total Active Strings_new",
                        "Working String Count_old", "Working String Count_new"]
    df_history = pd.merge(df_history, merged_df[merge_back_cols], on=["Plot", "Block", inverter_col], how="left")

    return {
        "df_history": df_history,
        "inverter_col": inverter_col,
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
    """Upload registry + a health check confirming every entry's files
    actually exist on disk (catches redeploys/volume resets that leave a
    registry entry with nothing backing it)."""
    st.subheader("Snapshot Upload Registry")
    st.caption(
        "SCADA workbooks are uploaded from the main sidebar (admin only) and "
        "automatically saved here, day-wise, so they can be compared later."
    )

    if can_upload(user_role) and upload_handler:
        with st.expander("Backfill a Previous Date's Snapshot", expanded=False):
            st.caption(
                "Upload a SCADA workbook for any past calendar date. It will be "
                "processed and stored exactly as if it had been uploaded that day, "
                "so it's available for history and calendar-comparison."
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

    # ---- Registry health check: does every entry's on-disk data still exist? ----
    with st.spinner("Checking registry integrity..."):
        report = storage1.get_upload_registry_report()

    ok_count = sum(1 for r in report if r["integrity_ok"])
    broken_count = len(report) - ok_count

    h1, h2, h3 = st.columns(3)
    h1.metric("Registered Uploads", len(report))
    h2.metric("Healthy", ok_count)
    h3.metric("Broken / Missing Files", broken_count)

    if broken_count > 0:
        st.error(
            f"{broken_count} registry entr{'y is' if broken_count == 1 else 'ies are'} missing their backing "
            f"file(s) on disk (likely from a deploy without a persistent volume). Re-upload the affected date(s)."
        )
    else:
        st.success("Upload registry is healthy - every entry's files are present on disk.")

    df_report = pd.DataFrame(report)[
        ["upload_id", "snapshot_date", "original_filename", "uploaded_by", "upload_timestamp",
         "saved_sheets", "integrity_ok", "integrity_message"]
    ].sort_values("snapshot_date", ascending=False)

    display_report = df_report.rename(columns={
        "snapshot_date": "Snapshot Date", "original_filename": "File Name",
        "uploaded_by": "Uploaded By", "upload_timestamp": "Upload Time",
        "saved_sheets": "Sheets Saved", "integrity_ok": "Files OK", "integrity_message": "Integrity Detail",
    })

    def color_integrity(val):
        if val is True:
            return "background-color: #10b981; color: white; font-weight: bold;"
        if val is False:
            return "background-color: #ef4444; color: white; font-weight: bold;"
        return ""

    st.dataframe(
        display_report.drop(columns=["upload_id"]).style.map(color_integrity, subset=["Files OK"]),
        use_container_width=True,
    )

    # ---- Admin-only: view a snapshot's data, or delete it entirely ----
    if can_upload(user_role):
        st.markdown("---")
        st.markdown("#### Manage a Snapshot")
        st.caption("View the saved sheet data for any snapshot date, or permanently delete it.")

        snapshot_labels = [
            f"{row['Snapshot Date']} · {row['File Name']} (uploaded by {row['Uploaded By']})"
            for _, row in display_report.iterrows()
        ]
        label_to_upload_id = dict(zip(snapshot_labels, display_report["upload_id"]))

        if snapshot_labels:
            selected_label = st.selectbox("Select a snapshot", snapshot_labels, key="registry_manage_select")
            selected_upload_id = label_to_upload_id[selected_label]
            entry = storage1.get_upload_by_id(selected_upload_id)

            with st.expander("👁️ View Snapshot Data", expanded=False):
                if entry and entry.get("saved_sheets"):
                    sheet_pick = st.selectbox(
                        "Sheet", entry["saved_sheets"], key=f"registry_view_sheet_{selected_upload_id}",
                    )
                    df_view = storage1.load_sheet_csv(selected_upload_id, sheet_pick)
                    if df_view is not None:
                        st.dataframe(df_view, use_container_width=True, height=400)
                    else:
                        st.warning("Could not load this sheet's saved data.")
                else:
                    st.info("No saved sheets found for this snapshot.")

            mcol1, mcol2 = st.columns([3, 1])
            with mcol2:
                confirm_delete = st.checkbox("Confirm", key=f"confirm_delete_{selected_upload_id}")
                if st.button("🗑️ Delete Snapshot", key=f"delete_upload_{selected_upload_id}",
                             disabled=not confirm_delete, use_container_width=True, type="primary"):
                    ok, msg = storage1.delete_upload(selected_upload_id)
                    storage1.log_audit_event(
                        st.session_state.get("user", {}).get("username", "unknown"),
                        user_role, "snapshot_deleted", {"upload_id": selected_upload_id, "result": msg},
                    )
                    if ok:
                        st.success(msg)
                        st.rerun()
                    else:
                        st.error(msg)


def display_summary_dashboard(sheet_name="Sheet1"):
    """
    Last-3-days summary: per-day inverter/string/working/failed counts,
    plus day-over-day restored vs newly-failed string counts (with the
    specific strings that restored) and supporting graphs.
    """
    st.subheader("Summary Dashboard - Last 3 Days")
    st.caption("Inverter and string counts for the most recent snapshots, with day-over-day restored/failed comparison.")

    available_dates = get_available_snapshot_dates()
    if not available_dates:
        st.info("No snapshot data available yet. Upload a SCADA workbook to get started.")
        return

    last_dates = sorted(available_dates)[-3:]  # oldest -> newest, up to 3

    # ---- Per-day counts ----
    daily_summaries = []
    with st.spinner("Loading recent snapshots..."):
        for d in last_dates:
            df = load_snapshot_sheet(d, sheet_name)
            if df is None or df.empty:
                daily_summaries.append({
                    "Date": d, "Inverters": 0, "Total Strings": 0,
                    "Working": 0, "Failed": 0, "Availability (%)": 0.0,
                })
                continue
            inverter_col = get_inverter_column(df)
            total_inverters = df[inverter_col].nunique() if inverter_col else 0
            total_strings = int(df["Total Active Strings"].sum()) if "Total Active Strings" in df.columns else 0
            working = int(df["Working String Count"].sum()) if "Working String Count" in df.columns else 0
            failed = int(df["Failed String Count"].sum()) if "Failed String Count" in df.columns else 0
            availability = round((working / total_strings * 100), 2) if total_strings else 0.0
            daily_summaries.append({
                "Date": d, "Inverters": total_inverters, "Total Strings": total_strings,
                "Working": working, "Failed": failed, "Availability (%)": availability,
            })

    df_daily = pd.DataFrame(daily_summaries)

    st.markdown("#### Daily Snapshot Counts")
    day_cols = st.columns(len(df_daily))
    for idx, row in df_daily.iterrows():
        with day_cols[idx]:
            st.metric(row["Date"], f"{row['Availability (%)']:.1f}% avail")
            st.caption(f"Inverters: {row['Inverters']:,} | Total Strings: {row['Total Strings']:,}")
            st.write(f"Working: **{row['Working']:,}**  ·  Failed: **{row['Failed']:,}**")

    st.dataframe(df_daily, use_container_width=True)

    fig_daily = go.Figure()
    fig_daily.add_trace(go.Bar(x=df_daily["Date"], y=df_daily["Working"], name="Working", marker_color="#10b981"))
    fig_daily.add_trace(go.Bar(x=df_daily["Date"], y=df_daily["Failed"], name="Failed", marker_color="#ef4444"))
    fig_daily.update_layout(barmode="group", title="Working vs Failed Strings (Last 3 Days)",
                             height=380, plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)")
    st.plotly_chart(fig_daily, use_container_width=True, key="summary_daily_bar")

    if len(last_dates) < 2:
        st.info("Need at least 2 snapshot dates to calculate restored/failed comparisons.")
        return

    # ---- Day-over-day restored / failed comparison ----
    st.markdown("---")
    st.markdown("#### Day-over-Day Restored / Failed Comparison")

    comparison_rows = []
    restored_detail_frames = []
    failed_detail_frames = []

    with st.spinner("Comparing consecutive snapshots..."):
        for i in range(1, len(last_dates)):
            prev_date, curr_date = last_dates[i - 1], last_dates[i]
            result, error = compare_two_snapshots_by_date(prev_date, curr_date, sheet_name)
            if error:
                st.warning(f"{prev_date} -> {curr_date}: {error}")
                continue

            df_hist = result["df_history"]
            inverter_col = result["inverter_col"]
            pair_label = f"{prev_date} -> {curr_date}"

            restored_total = int(df_hist["Failed_to_Working"].sum())
            newly_failed_total = int(df_hist["Working_to_Failed"].sum())
            current_failed_total = int(df_hist["Failed String Count_new"].sum())

            comparison_rows.append({
                "Comparison": pair_label,
                "Restored Strings": restored_total,
                "Newly Failed Strings": newly_failed_total,
                "Failed as of To-Date": current_failed_total,
            })

            restored_rows = df_hist[df_hist["Failed_to_Working"] > 0][
                ["Plot", "Block", inverter_col, "Failed_to_Working", "Restored_Strings"]
            ].copy()
            if not restored_rows.empty:
                restored_rows.insert(0, "Comparison", pair_label)
                restored_rows = restored_rows.rename(columns={
                    inverter_col: "Inverter", "Failed_to_Working": "Restored Count",
                })
                restored_detail_frames.append(restored_rows)

            failed_rows = df_hist[df_hist["Working_to_Failed"] > 0][
                ["Plot", "Block", inverter_col, "Working_to_Failed", "Newly_Failed_Strings"]
            ].copy()
            if not failed_rows.empty:
                failed_rows.insert(0, "Comparison", pair_label)
                failed_rows = failed_rows.rename(columns={
                    inverter_col: "Inverter", "Working_to_Failed": "Newly Failed Count",
                })
                failed_detail_frames.append(failed_rows)

    if not comparison_rows:
        st.info("No valid day-over-day comparisons could be computed.")
        return

    df_comparison = pd.DataFrame(comparison_rows)

    c1, c2, c3 = st.columns(3)
    c1.metric("Total Restored (window)", int(df_comparison["Restored Strings"].sum()))
    c2.metric("Total Newly Failed (window)", int(df_comparison["Newly Failed Strings"].sum()))
    c3.metric("Failed as of Latest Date", int(df_comparison["Failed as of To-Date"].iloc[-1]))

    st.dataframe(df_comparison, use_container_width=True)

    fig_compare = go.Figure()
    fig_compare.add_trace(go.Bar(x=df_comparison["Comparison"], y=df_comparison["Restored Strings"],
                                  name="Restored", marker_color="#10b981"))
    fig_compare.add_trace(go.Bar(x=df_comparison["Comparison"], y=df_comparison["Newly Failed Strings"],
                                  name="Newly Failed", marker_color="#ef4444"))
    fig_compare.add_trace(go.Scatter(x=df_comparison["Comparison"], y=df_comparison["Failed as of To-Date"],
                                      name="Failed as of To-Date", mode="lines+markers",
                                      line=dict(color="#fbbf24", width=3), yaxis="y"))
    fig_compare.update_layout(
        barmode="group", title="Restored vs Newly Failed Strings by Day-Pair",
        height=420, plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    st.plotly_chart(fig_compare, use_container_width=True, key="summary_compare_chart")

    # ---- Restored strings detail ----
    st.markdown("---")
    st.markdown("#### Restored Strings - Detail")
    if restored_detail_frames:
        df_restored = pd.concat(restored_detail_frames, ignore_index=True)
        df_restored = df_restored.sort_values(["Comparison", "Restored Count"], ascending=[True, False])
        page_restored = paginate_dataframe(df_restored, page_size=15, key_prefix="summary_restored")
        st.dataframe(page_restored, use_container_width=True)
        st.download_button(
            "Download Restored Strings (CSV)", data=df_restored.to_csv(index=False),
            file_name=f"restored_strings_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv", key="summary_restored_download",
        )
    else:
        st.success("No strings restored in this window.")

    st.markdown("#### Newly Failed Strings - Detail")
    if failed_detail_frames:
        df_failed = pd.concat(failed_detail_frames, ignore_index=True)
        df_failed = df_failed.sort_values(["Comparison", "Newly Failed Count"], ascending=[True, False])
        page_failed = paginate_dataframe(df_failed, page_size=15, key_prefix="summary_failed")
        st.dataframe(page_failed, use_container_width=True)
        st.download_button(
            "Download Newly Failed Strings (CSV)", data=df_failed.to_csv(index=False),
            file_name=f"newly_failed_strings_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv", key="summary_failed_download",
        )
    else:
        st.success("No new failures in this window.")


def display_working_hours_analysis():
    """Reference info: configured working-hours window + available snapshot dates."""
    st.subheader("Working Hours")
    st.write(f"Configured working hours: {WORKING_HOURS_START}:00 to {WORKING_HOURS_END}:00")
    st.write(f"Working hours counted per full day: {WORKING_HOURS_PER_DAY} hours")
    st.caption("This window is what Restoration/Failure-hour figures elsewhere in this app are based on.")

    available_dates = get_available_snapshot_dates()
    if available_dates:
        st.markdown(f"**{len(available_dates)}** snapshot date(s) available, from **{available_dates[0]}** to **{available_dates[-1]}**.")
        page_dates = paginate_dataframe(
            pd.DataFrame({"Snapshot Date": sorted(available_dates, reverse=True)}),
            page_size=20, key_prefix="working_hours_dates",
        )
        st.dataframe(page_dates, use_container_width=True)
    else:
        st.info("No saved snapshot dates available.")


def display_calendar_comparison(sheet_name="Sheet1"):
    """From-date -> To-date comparison with restored/failed breakdown, top
    movers, an availability trend, and a paginated detail table."""
    st.subheader("Calendar-wise Comparison (From Date -> To Date)")

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
        return

    df_history = result["df_history"]
    inverter_col = result["inverter_col"]
    baseline_current = result["baseline_avg_working_current"]

    restored_total = int(df_history["Failed_to_Working"].sum())
    newly_failed_total = int(df_history["Working_to_Failed"].sum())
    failed_from = int(df_history["Failed String Count_old"].sum())
    failed_to = int(df_history["Failed String Count_new"].sum())

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("From -> To", f"{from_date} -> {to_date}")
    c2.metric("Restored Strings", restored_total)
    c3.metric("Newly Failed Strings", newly_failed_total)
    c4.metric("Failed: From-Date", failed_from)
    c5.metric("Failed: To-Date", failed_to, delta=int(failed_to - failed_from), delta_color="inverse")

    st.caption(f"Baseline average working current: {baseline_current:.2f} A · Time-based metrics assume {WORKING_HOURS_START}:00-{WORKING_HOURS_END}:00 working hours per day.")

    st.markdown("---")
    st.markdown("#### Restoration TAT Summary")
    total_tat_hours = float(df_history["Restoration_TAT_Hours"].sum())
    avg_tat_hours = float(df_history.loc[df_history["Restoration_TAT_Hours"] > 0, "Restoration_TAT_Hours"].mean()) if (df_history["Restoration_TAT_Hours"] > 0).any() else 0.0
    still_failing_hours = float(df_history["Current_Failure_Hours"].sum())

    t1, t2, t3 = st.columns(3)
    t1.metric("Total Restoration TAT (hrs)", f"{total_tat_hours:,.0f}")
    t2.metric("Avg TAT per Restored String (hrs)", f"{avg_tat_hours:,.1f}")
    t3.metric("Cumulative Ongoing-Failure Hours", f"{still_failing_hours:,.0f}")

    st.markdown("---")
    st.markdown("#### Full Comparison Detail")
    display_history = df_history.rename(columns={inverter_col: "Inverter"}).sort_values(
        by="Current_Failure_Hours", ascending=False
    )
    page_history = paginate_dataframe(display_history, page_size=25, key_prefix="calendar_compare")
    st.dataframe(page_history, use_container_width=True)
    st.download_button(
        "Download Full Comparison (CSV)", data=display_history.to_csv(index=False),
        file_name=f"calendar_compare_{from_date}_{to_date}.csv",
        mime="text/csv", key="calendar_compare_download",
    )

    st.markdown("---")
    st.markdown("#### Trend Across the Selected Range")

    with st.spinner("Loading trend data..."):
        df_trend = build_range_trend_data(from_date, to_date, sheet_name)

    if df_trend.empty:
        st.info("No trend data available for this range.")
        return

    fig_avail = px.line(
        df_trend, x="Date", y="Availability (%)", color="Plot", line_group="Block",
        markers=True, title="Availability Trend by Plot / Block",
    )
    fig_avail.update_layout(height=450, plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)")
    st.plotly_chart(fig_avail, use_container_width=True, key="cal_trend_avail")

    fig_wf = px.bar(
        df_trend, x="Date", y=["Working", "Failed"], facet_col="Plot",
        barmode="stack", title="Working vs Failed Strings Over Time (by Plot)",
        color_discrete_sequence=["#10b981", "#ef4444"],
    )
    fig_wf.update_layout(height=450, plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)")
    st.plotly_chart(fig_wf, use_container_width=True, key="cal_trend_stacked")


# ==========================================
# MAIN ENTRY
# ==========================================
# def display_tat_dashboard(processed_dataframes=None, current_df=None, sheet_name="Sheet1",
#                            user_role="viewer", username="unknown", upload_handler=None):
#     st.title("🔄 Restore & TAT Analysis")
#     st.caption("Day-wise SCADA snapshots (uploaded from the sidebar) power history, TAT, and calendar comparisons.")
import streamlit as st

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
        "Upload Registry",
        "Summary Dashboard",
        "String History Matrix",
        "Inverter History Matrix",
        "Working Hours",
        "Calendar Compare",
    ])

    with tabs[0]:
        display_upload_registry(user_role, upload_handler=upload_handler)

    with tabs[1]:
        display_summary_dashboard(sheet_name)

    with tabs[2]:
        display_string_history_matrix(history, current_df)

    with tabs[3]:
        display_inverter_history_matrix()

    with tabs[4]:
        display_working_hours_analysis()

    with tabs[5]:
        display_calendar_comparison(sheet_name)


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