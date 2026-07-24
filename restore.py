"""
restore.py
==========
Restore & TAT Analysis Module for PV SCADA Analytics.

Changes vs the previous version:
- No longer keeps its own snapshot folder / metadata file. It reads
  snapshots straight from storage.py's single upload registry, which is
  the same registry app.py writes to when the admin uploads a workbook.
  -> one JSON file, one place to look, easy to keep in sync.
- The old "pick exactly 3 dates" comparison is replaced with a calendar
  (From Date -> To Date) comparison: pick a date range, and the tool
  automatically compares the first vs last snapshot in that range and
  plots the trend across every snapshot that falls inside the range.
- Per-string failure/restoration TAT tracking (string_history.json) is
  unchanged - it is already calendar/date-range aware.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import storage

# ==========================================
# CONFIGURATION
# ==========================================
WORKING_HOURS_START = 6
WORKING_HOURS_END = 18
WORKING_HOURS_PER_DAY = WORKING_HOURS_END - WORKING_HOURS_START
WORKING_CURRENT_THRESHOLD = 0.5

DATA_DIR = Path("data")
HISTORY_FILE = DATA_DIR / "string_history.json"
DATA_DIR.mkdir(exist_ok=True)

INVERTER_ID_COLS = [
    "Inverter ID", "Inverter_ID", "Inverter", "ID",
    "Device Name", "String Inverter", "Inverters",
]


# ==========================================
# ROLE HELPERS
# ==========================================
def can_upload(user_role: str) -> bool:
    return str(user_role).strip().lower() == "admin"


# ==========================================
# STRING HISTORY MANAGEMENT (per-string TAT tracking)
# ==========================================
def load_string_history():
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


def save_string_history(history):
    history.setdefault("strings", {})
    history["last_updated"] = datetime.now().isoformat()
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)


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

    for _, row in df.iterrows():
        inverter_id = str(row[inverter_col])
        history["strings"].setdefault(inverter_id, {})

        for col in pv_current_cols:
            string_id = str(col)
            current_value = pd.to_numeric(row.get(col), errors="coerce")

            if string_id not in history["strings"][inverter_id]:
                history["strings"][inverter_id][string_id] = {
                    "status_history": [],
                    "current_status": "unknown",
                    "last_change": None,
                }

            status = "working" if pd.notna(current_value) and current_value > WORKING_CURRENT_THRESHOLD else "failed"
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
def get_available_snapshot_dates():
    return storage.get_available_snapshot_dates()


def load_snapshot_sheet(snapshot_date, sheet_name):
    entry = storage.get_upload_for_date(snapshot_date)
    if not entry:
        return None
    return storage.load_sheet_csv(entry["upload_id"], sheet_name)


# ==========================================
# TAT & RESTORE CALCULATIONS (per-string, arbitrary date range)
# ==========================================
def calculate_failure_to_restore_tat(history, inverter_id, string_id, date_start=None, date_end=None):
    """Calculate failure -> restore TAT events for one string within an
    optional [date_start, date_end] calendar window."""
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


# ==========================================
# CALENDAR (FROM DATE -> TO DATE) COMPARISON
# ==========================================
def compare_two_snapshots_by_date(old_date, new_date, sheet_name="Sheet1"):
    """Compare exactly two calendar snapshots (baseline vs latest)."""
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
        data = {
            "Plot": row["Plot"],
            "Block": row["Block"],
            inverter_col: row[inverter_col],
            "Failed_to_Working": 0,
            "Working_to_Failed": 0,
            "Current_Failure_Hours": 0,
            "Restoration_TAT_Hours": 0,
        }
        for i in range(1, 29):
            pv_col_name = f"PV-I{i}"
            if pv_col_name not in pv_cols:
                continue

            is_active_old = i <= row["Total Active Strings_old"]
            is_active_new = i <= row["Total Active Strings_new"]

            pv_old = row[f"{pv_col_name}_old"]
            pv_new = row[f"{pv_col_name}_new"]

            is_working_old = is_active_old and (pv_old > WORKING_CURRENT_THRESHOLD)
            is_working_new = is_active_new and (pv_new > WORKING_CURRENT_THRESHOLD)

            if not is_working_old and is_working_new:
                data["Failed_to_Working"] += 1
                data["Restoration_TAT_Hours"] += op_hours
            elif is_working_old and not is_working_new:
                data["Working_to_Failed"] += 1
                data["Current_Failure_Hours"] += op_hours
            elif not is_working_old and not is_working_new:
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


def build_range_trend_data(from_date, to_date, sheet_name="Sheet1"):
    """Build a long-form Date/Plot/Block trend dataframe for every snapshot
    that falls inside [from_date, to_date] (inclusive), using the metrics
    already computed at upload time (Working/Failed/Total Active Strings)."""
    from_str, to_str = str(from_date), str(to_date)
    dates_in_range = [d for d in storage.get_available_snapshot_dates() if from_str <= d <= to_str]

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


# ==========================================
# UI - UPLOAD REGISTRY (read-only, admin uploads happen from the main app sidebar)
# ==========================================
def display_upload_registry(user_role, upload_handler=None):
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

    uploads = storage.get_all_uploads()
    if not uploads:
        st.info("No snapshots uploaded yet.")
        return

    df_uploads = pd.DataFrame(uploads)[
        ["snapshot_date", "original_filename", "uploaded_by", "upload_timestamp", "saved_sheets"]
    ].sort_values("snapshot_date", ascending=False)
    df_uploads.columns = ["Snapshot Date", "File Name", "Uploaded By", "Upload Time", "Sheets Saved"]
    st.dataframe(df_uploads, use_container_width=True)


# ==========================================
# UI - SUMMARY & ANALYSIS
# ==========================================
def display_summary_dashboard(history, current_df):
    st.subheader("📊 Summary Dashboard")

    if not history.get("strings"):
        st.info("No string history available. Please upload SCADA data first.")
        return

    total_inverters = len(history["strings"])
    total_strings = 0
    total_failures = 0
    total_restorations = 0
    all_failures = []

    for inverter_id, strings in history["strings"].items():
        for string_id, data in strings.items():
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

    status_counts = {"working": 0, "failed": 0}
    if current_df is not None and not current_df.empty:
        status_counts["working"] = int(current_df["Working String Count"].sum()) if "Working String Count" in current_df.columns else 0
        status_counts["failed"] = int(current_df["Failed String Count"].sum()) if "Failed String Count" in current_df.columns else 0

    col_a, col_b = st.columns([2, 1])
    with col_a:
        if status_counts["working"] + status_counts["failed"] > 0:
            fig_status = go.Figure(data=[go.Pie(
                labels=["Working", "Failed"],
                values=[status_counts["working"], status_counts["failed"]],
                hole=0.5,
                marker_colors=["#10b981", "#ef4444"],
                textinfo="label+percent+value",
            )])
            fig_status.update_layout(title="Current String Status", height=350)
            st.plotly_chart(fig_status, use_container_width=True)

    with col_b:
        st.write("History last updated")
        st.info(str(history.get("last_updated", "N/A")))


def display_string_analysis(history, current_df):
    st.subheader("🔌 String Analysis (Calendar Date Range)")

    if not history.get("strings"):
        st.info("No string history available.")
        return

    all_strings = [f"{inv}_{sid}" for inv, strings in history["strings"].items() for sid in strings.keys()]
    if not all_strings:
        st.info("No strings found in history.")
        return

    col1, col2 = st.columns(2)
    with col1:
        selected_strings = st.multiselect(
            "Select Strings", all_strings,
            default=all_strings[:5] if len(all_strings) > 5 else all_strings,
            key="restore_select_strings",
        )
    with col2:
        date_range = st.date_input(
            "From Date -> To Date",
            value=(datetime.now().date() - timedelta(days=7), datetime.now().date()),
            max_value=datetime.now().date(),
            key="restore_date_range_strings",
        )

    if not selected_strings:
        st.warning("Please select at least one string.")
        return

    start_date, end_date = None, None
    if isinstance(date_range, (list, tuple)) and len(date_range) == 2:
        start_date, end_date = date_range

    string_data = []
    for string_key in selected_strings:
        parts = string_key.split("_", 1)
        if len(parts) == 2:
            inverter_id, string_id = parts
            for event in calculate_failure_to_restore_tat(history, inverter_id, string_id, start_date, end_date):
                string_data.append({
                    "Inverter": inverter_id, "String": string_id,
                    "Failure Date": event["failure_date"], "Restore Date": event["restore_date"],
                    "TAT (Working Hours)": event["tat_working_hours"],
                    "TAT (Actual Hours)": event["tat_actual_hours"], "Status": event["status"],
                })

    if not string_data:
        st.info("No events found for the selected strings in the date range.")
        return

    df_strings = pd.DataFrame(string_data)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Events", len(df_strings))
    tat_values = df_strings[df_strings["TAT (Working Hours)"] != "Ongoing"]["TAT (Working Hours)"]
    c2.metric("Avg TAT (Working Hours)", f"{pd.to_numeric(tat_values, errors='coerce').mean():.1f}h" if not tat_values.empty else "N/A")
    c3.metric("Total Restorations", len(df_strings[df_strings["Status"] == "restored"]))
    c4.metric("Ongoing Failures", len(df_strings[df_strings["Status"] == "ongoing_failure"]))

    st.dataframe(df_strings, use_container_width=True)

    df_tat = df_strings[df_strings["TAT (Working Hours)"] != "Ongoing"].copy()
    if not df_tat.empty:
        df_tat["TAT (Working Hours)"] = pd.to_numeric(df_tat["TAT (Working Hours)"], errors="coerce")
        fig = px.bar(df_tat, x="String", y="TAT (Working Hours)", color="Inverter",
                     title="TAT by String (Working Hours)", labels={"TAT (Working Hours)": "TAT (Hours)"})
        fig.update_layout(height=400)
        st.plotly_chart(fig, use_container_width=True)


def display_tat_tracking(history, current_df):
    st.subheader("⏱️ TAT Tracking")

    if not history.get("strings"):
        st.info("No TAT history available.")
        return

    rows = []
    for inverter_id, strings in history["strings"].items():
        for string_id in strings.keys():
            for event in calculate_failure_to_restore_tat(history, inverter_id, string_id):
                rows.append({
                    "Inverter": inverter_id, "String": string_id,
                    "Failure Date": event["failure_date"], "Restore Date": event["restore_date"],
                    "TAT Working Hours": event["tat_working_hours"],
                    "TAT Actual Hours": event["tat_actual_hours"], "Status": event["status"],
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
    st.subheader("⏰ Working Hours")
    st.write(f"Configured working hours: {WORKING_HOURS_START}:00 to {WORKING_HOURS_END}:00")
    st.write(f"Working hours counted per full day: {WORKING_HOURS_PER_DAY} hours")

    available_dates = storage.get_available_snapshot_dates()
    if available_dates:
        st.dataframe(pd.DataFrame({"Snapshot Date": available_dates}), use_container_width=True)
    else:
        st.info("No saved snapshot dates available.")


def display_calendar_comparison(sheet_name="Sheet1"):
    """Calendar-wise (From Date -> To Date) comparison, replacing the old
    'pick exactly 3 dates' workflow."""
    st.subheader("📅 Calendar-wise Comparison (From Date -> To Date)")

    available_dates = storage.get_available_snapshot_dates()
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

    from_str, to_str = str(from_date), str(to_date)
    dates_in_range = [d for d in available_dates if from_str <= d <= to_str]

    if len(dates_in_range) < 2:
        st.warning("Need at least 2 saved snapshots within the selected date range.")
        return

    baseline_date, latest_date = dates_in_range[0], dates_in_range[-1]
    st.caption(
        f"Comparing baseline **{baseline_date}** against latest **{latest_date}** "
        f"({len(dates_in_range)} snapshots found in range)."
    )

    result, error = compare_two_snapshots_by_date(baseline_date, latest_date, sheet_name)
    if error:
        st.error(error)
    else:
        df_history = result["df_history"]
        baseline_current = result["baseline_avg_working_current"]

        c1, c2, c3 = st.columns(3)
        c1.metric("Baseline Date", baseline_date)
        c2.metric("Latest Date", latest_date)
        c3.metric("Baseline Avg Working Current", f"{baseline_current:.2f} A")

        st.dataframe(
            df_history.sort_values(by="Current_Failure_Hours", ascending=False),
            use_container_width=True,
        )
        st.caption("Time-based metrics assume one interval equals working hours from 6 AM to 6 PM.")

    st.markdown("---")
    st.markdown("#### 📈 Trend Across the Selected Range")

    df_trend = build_range_trend_data(from_date, to_date, sheet_name)
    if df_trend.empty:
        st.info("No trend data available for this range (check that sheet name matches saved snapshots).")
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


# ==========================================
# MAIN ENTRY
# ==========================================
def display_tat_dashboard(processed_dataframes=None, current_df=None, sheet_name="Sheet1",
                           user_role="viewer", username="unknown", upload_handler=None):
    st.title("🔄 Restore & TAT Analysis")
    st.caption("Day-wise SCADA snapshots (uploaded from the sidebar) power history, TAT, and calendar comparisons.")

    init_history()
    history = load_string_history()

    if current_df is not None and not current_df.empty:
        current_date = datetime.now().strftime("%Y-%m-%d")
        update_string_history(current_df, current_date)
        history = load_string_history()

    tabs = st.tabs([
        "📤 Upload Registry",
        "📊 Summary Dashboard",
        "🔌 String Analysis",
        "⏱️ TAT Tracking",
        "⏰ Working Hours",
        "📅 Calendar Compare",
    ])

    with tabs[0]:
        display_upload_registry(user_role, upload_handler=upload_handler)

    with tabs[1]:
        display_summary_dashboard(history, current_df)

    with tabs[2]:
        display_string_analysis(history, current_df)

    with tabs[3]:
        display_tat_tracking(history, current_df)

    with tabs[4]:
        display_working_hours_analysis(history, current_df)

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
