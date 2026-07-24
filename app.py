import io
import re
import json
import hashlib
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from datetime import datetime
from pathlib import Path
import restore  # Restore & TAT module
import storage  # Shared backend storage (uploads, users, audit log)

# ==========================================
# 1. PAGE CONFIGURATION & STYLING
# ==========================================
st.set_page_config(
    page_title="PV String Analytics",
    page_icon="☀️",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
<style>
    .main .block-container { padding-top: 1.2rem; padding-bottom: 2rem; }
    .stMetric {
        background-color: #0f172a;
        border: 1px solid #1e293b;
        padding: 1rem;
        border-radius: 0.75rem;
    }
    div[data-testid="stMetricValue"] {
        font-size: 1.7rem;
        font-weight: 700;
        color: #38bdf8;
    }
    .stDataFrame thead th {
        background-color: #1e293b;
        color: white;
        font-weight: 600;
    }
    .user-badge-admin {
        background-color: #8b5cf6; color: white; padding: 2px 8px;
        border-radius: 12px; font-size: 0.7rem; font-weight: 600;
    }
    .user-badge-manager {
        background-color: #f59e0b; color: white; padding: 2px 8px;
        border-radius: 12px; font-size: 0.7rem; font-weight: 600;
    }
    .user-badge-engineer {
        background-color: #3b82f6; color: white; padding: 2px 8px;
        border-radius: 12px; font-size: 0.7rem; font-weight: 600;
    }
    .welcome-banner {
        background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
        border: 1px solid #334155; border-radius: 12px;
        padding: 14px 18px; margin-bottom: 12px;
    }
</style>
""", unsafe_allow_html=True)

# ==========================================
# 2. CONFIGURATION
# ==========================================
DEFAULT_TOTAL_ACTIVE_STRINGS = 19
WORKING_CURRENT_THRESHOLD = 0.5
PV_CURRENT_COLUMNS = [f"PV-I{i}" for i in range(1, 29)]

ACTIVE_STRING_OVERRIDES = {
    "P2": {"IB1": 18, "IB3": 17, "IB4": 18, "IB5": 18},
    "P6": {"IB1": 18, "IB2": 18, "IB3": 18, "IB5": 18, "IB6": 18, "IB7": 18},
}

INVERTER_ID_COLS = [
    "Inverter ID", "Inverter_ID", "Inverter", "ID",
    "Device Name", "String Inverter", "Inverters"
]


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


MANUAL_SCADA_COLUMNS = [
    "String Inverter", "MBUS", "Grid", "E-Daily(KWH)", "Active Power", "Reactive Power",
    "PV1", "PV2", "PV3", "PV4", "PV5", "PV6", "PV7", "PV8", "PV9", "PV10",
    "PV11", "PV12", "PV13", "PV14", "PV15", "PV16", "PV17", "PV18", "PV19", "PV20",
    "PV21", "PV22", "PV23", "PV24", "PV25", "PV26", "PV27", "PV28",
    "PV-I1", "PV-I2", "PV-I3", "PV-I4", "PV-I5", "PV-I6", "PV-I7", "PV-I8", "PV-I9", "PV-I10",
    "PV-I11", "PV-I12", "PV-I13", "PV-I14", "PV-I15", "PV-I16", "PV-I17", "PV-I18", "PV-I19", "PV-I20",
    "PV-I21", "PV-I22", "PV-I23", "PV-I24", "PV-I25", "PV-I26", "PV-I27", "PV-I28",
    "VAB", "VBC", "VCA", "IA", "IB", "IC"
]

ROLE_BADGES = {
    "admin": "👑 Admin",
    "manager": "🧭 Manager",
    "engineer": "🔧 Engineer",
}

# ==========================================
# 3. USER / SESSION HELPERS  (delegates to storage.py)
# ==========================================
def get_current_user():
    return st.session_state.get("user")


def is_admin():
    user = get_current_user()
    return bool(user) and user.get("role") == "admin"


def is_manager():
    user = get_current_user()
    return bool(user) and user.get("role") == "manager"


def is_admin_or_manager():
    user = get_current_user()
    return bool(user) and user.get("role") in ("admin", "manager")


def is_engineer():
    user = get_current_user()
    return bool(user) and user.get("role") == "engineer"


# ==========================================
# 4. HELPERS (parsing / metrics - unchanged logic)
# ==========================================
def normalize_text(value):
    if pd.isna(value):
        return ""
    return str(value).strip().upper()

def clean_manual_columns(col_list):
    cleaned = []
    for col in col_list:
        col = str(col).strip()
        if col and col.lower() != "nan":
            cleaned.append(col)
    return cleaned

def extract_plot(inverter_id_str):
    if isinstance(inverter_id_str, str):
        parts = inverter_id_str.split("-")
        if len(parts) > 0:
            return parts[0].strip()
    return "Unknown Plot"

def extract_block(inverter_id_str):
    if isinstance(inverter_id_str, str):
        parts = inverter_id_str.split("-")
        if len(parts) > 1:
            return parts[1].strip()
    return "Unknown Block"

def map_inverter_to_sacu(inverter_id_str):
    if not isinstance(inverter_id_str, str):
        return "Invalid Inverter ID"

    match = re.search(r'-(\d[\.\-]\d)-', inverter_id_str)
    if match:
        sacu_identifier = match.group(1)
        try:
            if "." in sacu_identifier:
                first_digit_str = sacu_identifier.split(".")[0]
            else:
                first_digit_str = sacu_identifier.split("-")[0]
            first_digit = int(first_digit_str)
            if first_digit in [1, 2]:
                return "SACU-1"
            elif first_digit in [3, 4]:
                return "SACU-2"
        except ValueError:
            pass
    return "Unknown SACU"

def get_total_active_strings(plot, block):
    plot_key = normalize_text(plot)
    block_key = normalize_text(block)
    if plot_key in ACTIVE_STRING_OVERRIDES and block_key in ACTIVE_STRING_OVERRIDES[plot_key]:
        return ACTIVE_STRING_OVERRIDES[plot_key][block_key]
    return DEFAULT_TOTAL_ACTIVE_STRINGS

def get_available_pv_columns(df):
    normalized_map = {str(col).strip().upper(): col for col in df.columns}
    available_columns = []
    for col in PV_CURRENT_COLUMNS:
        if col.upper() in normalized_map:
            available_columns.append(normalized_map[col.upper()])
    return available_columns

def calculate_working_string_count(row, pv_columns):
    count = 0
    for col in pv_columns:
        value = pd.to_numeric(row.get(col), errors="coerce")
        if pd.notna(value) and value > WORKING_CURRENT_THRESHOLD:
            count += 1
    return count

def apply_string_metrics(df, plot_col="Plot", block_col="Block"):
    pv_columns = get_available_pv_columns(df)

    df["Total Active Strings"] = df.apply(
        lambda row: get_total_active_strings(row.get(plot_col), row.get(block_col)), axis=1
    )

    if pv_columns:
        df["Working String Count"] = df.apply(
            lambda row: calculate_working_string_count(row, pv_columns), axis=1
        )
    else:
        df["Working String Count"] = 0

    df["Failed String Count"] = (df["Total Active Strings"] - df["Working String Count"]).clip(lower=0)
    df["Availability (%)"] = ((df["Working String Count"] / df["Total Active Strings"]) * 100).fillna(0).round(2)
    df["Failure Percentage (%)"] = ((df["Failed String Count"] / df["Total Active Strings"]) * 100).fillna(0).round(2)
    return df

def find_header_row_index(file_stream, sheet_name, possible_header_columns, max_rows_to_check=100):
    file_stream.seek(0)
    temp_df = pd.read_excel(file_stream, sheet_name=sheet_name, header=None,
                             nrows=max_rows_to_check, engine="openpyxl")
    possible_headers_lower = [str(col).strip().lower() for col in possible_header_columns]

    for i, row in temp_df.iterrows():
        row_values = [str(val).strip() for val in row.dropna()]
        row_values_lower = [v.lower() for v in row_values]
        if any(col in row_values_lower for col in possible_headers_lower):
            return i
    return None

def assign_manual_headers(df, manual_headers):
    manual_headers = clean_manual_columns(manual_headers)
    if len(df.columns) >= len(manual_headers):
        df = df.iloc[:, :len(manual_headers)].copy()
        df.columns = manual_headers
    else:
        df.columns = manual_headers[:len(df.columns)]
    return df

def read_sheet_with_fallback(file_stream, sheet_name):
    header_row_index = find_header_row_index(file_stream, sheet_name, INVERTER_ID_COLS)
    file_stream.seek(0)
    if header_row_index is not None:
        df = pd.read_excel(file_stream, sheet_name=sheet_name, skiprows=header_row_index,
                            header=0, engine="openpyxl")
    else:
        df = pd.read_excel(file_stream, sheet_name=sheet_name, header=None, engine="openpyxl")
        df = assign_manual_headers(df, MANUAL_SCADA_COLUMNS)
    return df

def get_pv_string_columns(df):
    pv_voltage_cols, pv_current_cols = [], []
    for col in df.columns:
        col_str = str(col).strip()
        if col_str.startswith("PV-I"):
            pv_current_cols.append(col)
        elif col_str.startswith("PV") and col_str != "PV" and not col_str.startswith("PV-I"):
            try:
                num = int(col_str[2:])
                if 1 <= num <= 28:
                    pv_voltage_cols.append(col)
            except Exception:
                pass
    return pv_voltage_cols, pv_current_cols

def get_string_health_color(value):
    if pd.isna(value):
        return "#64748b"
    if value > 5.0:
        return "#10b981"
    elif value > 3.0:
        return "#34d399"
    elif value > 1.5:
        return "#fbbf24"
    elif value > 0.5:
        return "#f59e0b"
    else:
        return "#ef4444"

def get_column_header_color(value):
    if pd.isna(value):
        return "#64748b"
    if value >= 80:
        return "#10b981"
    elif value >= 60:
        return "#f59e0b"
    elif value >= 40:
        return "#f97316"
    else:
        return "#ef4444"

# ==========================================
# 5. PARSER
# ==========================================
@st.cache_data(show_spinner="Processing SCADA workbook...", ttl=3600)
def process_scada_excel_bytes(file_bytes):
    file_stream = io.BytesIO(file_bytes)
    excel_file = pd.ExcelFile(file_stream, engine="openpyxl")
    processed_dfs = {}

    for sheet_name in excel_file.sheet_names:
        try:
            df = read_sheet_with_fallback(file_stream, sheet_name)
        except Exception:
            continue

        df.dropna(how="all", inplace=True)
        df = df.loc[:, ~df.columns.astype(str).str.contains("^Unnamed:", case=False, regex=True)]
        df = df.loc[:, ~df.columns.duplicated()].copy()

        df_columns_lower_map = {str(c).strip().lower(): c for c in df.columns}
        actual_inverter_col = None
        for col in INVERTER_ID_COLS:
            if col in df.columns:
                actual_inverter_col = col
                break
            elif col.strip().lower() in df_columns_lower_map:
                actual_inverter_col = df_columns_lower_map[col.strip().lower()]
                break

        if not actual_inverter_col:
            continue

        df["Plot"] = df[actual_inverter_col].apply(extract_plot)
        df["Block"] = df[actual_inverter_col].apply(extract_block)
        df["SACU"] = df[actual_inverter_col].apply(map_inverter_to_sacu)
        df = apply_string_metrics(df, plot_col="Plot", block_col="Block")

        preferred_columns = [
            "Plot", "Block", actual_inverter_col, "SACU",
            "Total Active Strings", "Working String Count", "Failed String Count",
            "Availability (%)", "Failure Percentage (%)"
        ]
        remaining_cols = [c for c in df.columns if c not in preferred_columns]
        final_columns = [c for c in preferred_columns if c in df.columns] + remaining_cols
        df = df[final_columns]
        processed_dfs[sheet_name] = df

    if processed_dfs:
        first_sheet = next(iter(processed_dfs))
        history_df = processed_dfs[first_sheet].copy()
        restore.update_string_history(history_df, datetime.now().strftime("%Y-%m-%d"))

    return processed_dfs

def process_and_save_upload(file_bytes, filename, snapshot_date, username, role):
    """Shared upload pipeline used by both the sidebar 'today's file'
    uploader and the Restore & TAT tab's 'backfill a previous date'
    uploader, so both paths process/save/audit-log identically."""
    processed = process_scada_excel_bytes(file_bytes)
    if not processed:
        return False, "Could not process this workbook - no valid sheets/inverter column found."

    upload_id = storage.save_preprocessed_upload(
        file_bytes=file_bytes, original_filename=filename,
        processed_dataframes=processed, snapshot_date=str(snapshot_date),
        uploaded_by=username,
    )
    storage.log_audit_event(username, role, "file_uploaded",
                             {"filename": filename, "snapshot_date": str(snapshot_date), "upload_id": upload_id})
    return True, f"File uploaded and saved for {snapshot_date}"


def create_excel_download(dataframes_dict):
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for sheet_name, df in dataframes_dict.items():
            safe_sheet_name = str(sheet_name)[:31]
            df.to_excel(writer, sheet_name=safe_sheet_name, index=False)
    buffer.seek(0)
    return buffer.getvalue()

# ==========================================
# 6. UI - USER MANAGEMENT  (admin + manager, role-aware)
# ==========================================
def user_management_ui():
    """
    admin   -> create/delete any user, change roles, assign plots, full access
    manager -> create engineer users only, CANNOT delete anyone, can assign plots
    engineer-> no access (menu not shown)
    """
    current_user = get_current_user()
    role = current_user.get("role")
    if not storage.can_manage_users(role):
        return

    st.sidebar.markdown("---")
    st.sidebar.subheader("👥 User Management")

    users = storage.load_users()
    allowed_roles = storage.creatable_roles(role)

    with st.sidebar.expander("Manage Users", expanded=False):
        # ---- Create user ----
        st.write("### Create New User")
        new_full_name = st.text_input("Full Name", key="new_user_fullname")
        new_username = st.text_input("Username", key="new_user")
        new_password = st.text_input("Password", type="password", key="new_pass")
        new_role = st.selectbox("Role", allowed_roles, key="new_role")
        default_plots = storage.ALL_PLOTS if new_role in ("admin", "manager") else storage.ALL_PLOTS[:3]
        new_plots = st.multiselect("Assign Plots", storage.ALL_PLOTS, default=default_plots, key="new_user_plots")

        if st.button("Create User", key="create_user_btn"):
            ok, msg = storage.create_user(
                username=new_username, password=new_password, role=new_role,
                full_name=new_full_name, assigned_plots=new_plots,
                created_by=current_user.get("username"),
            )
            if ok:
                storage.log_audit_event(current_user["username"], role, "user_created",
                                         {"created_user": new_username, "assigned_role": new_role})
                st.success(msg)
                st.rerun()
            else:
                st.error(msg)

        # ---- Existing users ----
        st.write("### Existing Users")
        for username, user_data in users.items():
            if username == current_user.get("username"):
                continue
            # Manager cannot see/manage admin accounts
            if role == "manager" and user_data.get("role") == "admin":
                continue

            col1, col2, col3, col4 = st.columns([2, 1, 1, 1])
            with col1:
                st.write(f"**{user_data.get('full_name', username)}** (`{username}` - {user_data['role']})")
            with col2:
                if storage.can_delete_users(role):
                    if st.button("Delete", key=f"del_{username}"):
                        storage.delete_user(username)
                        storage.log_audit_event(current_user["username"], role, "user_deleted",
                                                 {"deleted_user": username})
                        st.rerun()
                else:
                    st.caption("no delete")
            with col3:
                if storage.can_assign_plots(role) and user_data["role"] in ("engineer", "manager"):
                    if st.button("Assign Plots", key=f"assign_{username}"):
                        st.session_state.assign_user = username
                        st.rerun()
            with col4:
                if storage.can_reset_password_for(role, user_data["role"]):
                    if st.button("Reset Pwd", key=f"resetpwd_{username}"):
                        st.session_state.reset_pwd_user = username
                        st.rerun()

        # ---- Password reset for another user (lost password) ----
        if "reset_pwd_user" in st.session_state:
            target_username = st.session_state.reset_pwd_user
            target_data = users.get(target_username)
            if target_data and storage.can_reset_password_for(role, target_data["role"]):
                st.write(f"### Reset Password for {target_data.get('full_name', target_username)}")
                admin_new_pw = st.text_input("New Password", type="password", key="admin_reset_pw_1")
                admin_new_pw_confirm = st.text_input("Confirm New Password", type="password", key="admin_reset_pw_2")
                if st.button("Confirm Reset", key="admin_reset_pw_confirm_btn"):
                    if not admin_new_pw or admin_new_pw != admin_new_pw_confirm:
                        st.error("Passwords don't match or are empty.")
                    else:
                        ok, msg = storage.reset_password(target_username, admin_new_pw)
                        if ok:
                            storage.log_audit_event(current_user["username"], role, "password_reset_admin",
                                                     {"target_user": target_username})
                            st.success(msg)
                            del st.session_state.reset_pwd_user
                            st.rerun()
                        else:
                            st.error(msg)
                if st.button("Cancel", key="admin_reset_pw_cancel_btn"):
                    del st.session_state.reset_pwd_user
                    st.rerun()
            else:
                del st.session_state.reset_pwd_user

        # ---- Plot assignment ----
        if "assign_user" in st.session_state:
            username = st.session_state.assign_user
            user_data = users.get(username)
            if user_data:
                st.write(f"### Assign Plots for {user_data.get('full_name', username)}")
                assigned = user_data.get("assigned_plots", [])
                selected_plots = st.multiselect(
                    f"Select plots for {username}", options=storage.ALL_PLOTS, default=assigned,
                    key="assign_plots_multiselect",
                )
                if st.button("Save Assignments", key="save_assign_plots"):
                    storage.update_user_plots(username, selected_plots)
                    storage.log_audit_event(current_user["username"], role, "plots_assigned",
                                             {"target_user": username, "plots": selected_plots})
                    st.success(f"Plots assigned for {username}")
                    del st.session_state.assign_user
                    st.rerun()
                if st.button("Cancel", key="cancel_assign_plots"):
                    del st.session_state.assign_user
                    st.rerun()


def audit_log_tab():
    """Role-scoped audit log, shown in its own dashboard tab:
      - admin:    sees every event, from every user
      - manager:  sees engineer-level events only
      - engineer: sees only their own events
    """
    current_user = get_current_user()
    role = current_user.get("role")
    username = current_user.get("username")

    if not storage.can_view_audit_log(role):
        st.info("Audit log isn't available for your role.")
        return

    st.subheader("🕵️ Audit Log")

    intact = storage.verify_audit_chain()
    if intact:
        st.success("✅ Log integrity verified (hash chain intact)")
    else:
        st.error("⚠️ Log integrity check FAILED - entries may have been altered")

    entries = storage.get_audit_log_for(username, role)
    if not entries:
        st.info("No audit events to show.")
        return

    df_audit = pd.DataFrame(entries)[["timestamp", "username", "role", "event_type", "details"]]
    df_audit = df_audit.sort_values("timestamp", ascending=False)

    scope_label = "All Users" if role == "admin" else "Engineers" if role == "manager" else "My Activity"

    filter_col1, filter_col2, metric_col = st.columns([1, 1, 1])
    with filter_col1:
        event_types = ["All"] + sorted_filter_options(df_audit["event_type"])
        selected_event = st.selectbox("Filter by Event Type", event_types, key="audit_event_filter")
    with filter_col2:
        if role == "admin":
            users_in_log = ["All"] + sorted_filter_options(df_audit["username"])
            selected_user = st.selectbox("Filter by User", users_in_log, key="audit_user_filter")
        else:
            selected_user = "All"
    with metric_col:
        st.metric("Scope", scope_label)

    filtered = df_audit.copy()
    if selected_event != "All":
        filtered = filtered[filtered["event_type"] == selected_event]
    if selected_user != "All":
        filtered = filtered[filtered["username"] == selected_user]

    st.caption(f"Showing {len(filtered)} of {len(df_audit)} event(s) you have access to.")
    st.dataframe(filtered, use_container_width=True, height=450)


# ==========================================
# 7. UI - Main Tabs  (unchanged business logic)
# ==========================================
def create_pv_string_tab(df):
    """Create the inverter-wise PV string details tab"""
    st.subheader("🔌 Inverter-wise PV String Details")
    st.caption("Color-coded headers show string health status")

    inverter_col = None
    df_columns_lower_map = {str(c).strip().lower(): c for c in df.columns}
    for col in INVERTER_ID_COLS:
        if col in df.columns:
            inverter_col = col
            break
        elif col.strip().lower() in df_columns_lower_map:
            inverter_col = df_columns_lower_map[col.strip().lower()]
            break

    if not inverter_col:
        st.warning("No inverter ID column found in the dataset")
        return

    pv_voltage_cols, pv_current_cols = get_pv_string_columns(df)
    if not pv_voltage_cols and not pv_current_cols:
        st.warning("No PV string data columns found in the dataset")
        return

    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        available_plots = sorted_filter_options(df["Plot"])
        selected_plot = st.selectbox("Filter by Plot", ["All"] + available_plots, key="pv_plot_filter")
    with col2:
        filtered_by_plot = df if selected_plot == "All" else df[df["Plot"] == selected_plot]
        available_blocks = sorted_filter_options(filtered_by_plot["Block"])
        selected_block = st.selectbox("Filter by Block", ["All"] + available_blocks, key="pv_block_filter")
    with col3:
        filtered_by_block = filtered_by_plot if selected_block == "All" else filtered_by_plot[filtered_by_plot["Block"] == selected_block]
        available_sacus = sorted_filter_options(filtered_by_block["SACU"])
        selected_sacu = st.selectbox("Filter by SACU", ["All"] + available_sacus, key="pv_sacu_filter")
    with col4:
        filtered_by_sacu = filtered_by_block if selected_sacu == "All" else filtered_by_block[filtered_by_block["SACU"] == selected_sacu]
        available_inverters = sorted_filter_options(filtered_by_sacu[inverter_col])
        selected_inverter = st.selectbox("Filter by Inverter", ["All"] + available_inverters, key="pv_inverter_filter")
    with col5:
        show_voltage = st.checkbox("Show Voltage", value=False, key="show_voltage")
        show_current = st.checkbox("Show Current", value=True, key="show_current")

    filtered_df = df.copy()
    if selected_plot != "All":
        filtered_df = filtered_df[filtered_df["Plot"] == selected_plot]
    if selected_block != "All":
        filtered_df = filtered_df[filtered_df["Block"] == selected_block]
    if selected_sacu != "All":
        filtered_df = filtered_df[filtered_df["SACU"] == selected_sacu]
    if selected_inverter != "All":
        filtered_df = filtered_df[filtered_df[inverter_col] == selected_inverter]

    if filtered_df.empty:
        st.warning("No data available for the selected filters")
        return

    summary_metrics = []
    for idx, row in filtered_df.iterrows():
        inverter_id = row[inverter_col]
        plot = row.get("Plot", "")
        block = row.get("Block", "")
        sacu = row.get("SACU", "")

        if "Total Active Strings" in row and "Working String Count" in row:
            total_strings = int(row["Total Active Strings"]) if pd.notna(row["Total Active Strings"]) else 0
            working_strings = int(row["Working String Count"]) if pd.notna(row["Working String Count"]) else 0
            failed_strings = int(row["Failed String Count"]) if pd.notna(row["Failed String Count"]) else 0
            availability = row["Availability (%)"] if pd.notna(row["Availability (%)"]) else 0
        else:
            total_strings, working_strings, failed_strings = 0, 0, 0
            for col in pv_current_cols:
                if col in row and pd.notna(row[col]):
                    total_strings += 1
                    if row[col] > WORKING_CURRENT_THRESHOLD:
                        working_strings += 1
                    else:
                        failed_strings += 1
            availability = (working_strings / total_strings * 100) if total_strings > 0 else 0

        grid = row.get("Grid", "")
        e_daily = row.get("E-Daily(KWH)", "")
        active_power = row.get("Active Power", "")
        reactive_power = row.get("Reactive Power", "")

        voltage_values = [row[col] for col in pv_voltage_cols if col in row and pd.notna(row[col])]
        avg_voltage = sum(voltage_values) / len(voltage_values) if voltage_values else 0

        current_values = [row[col] for col in pv_current_cols if col in row and pd.notna(row[col])]
        avg_current = sum(current_values) / len(current_values) if current_values else 0

        health_status = "Excellent" if availability >= 90 else "Good" if availability >= 70 else "Fair" if availability >= 50 else "Poor"

        summary_metrics.append({
            "Inverter ID": inverter_id, "Plot": plot, "Block": block, "SACU": sacu,
            "Total Strings": total_strings, "Working Strings": working_strings,
            "Failed Strings": failed_strings, "Availability (%)": round(availability, 2),
            "Health Status": health_status, "Avg PV Voltage (V)": round(avg_voltage, 1),
            "Avg PV Current (A)": round(avg_current, 2), "Grid": grid,
            "E-Daily (KWH)": e_daily, "Active Power (KW)": active_power,
            "Reactive Power (KVAR)": reactive_power
        })

    summary_df = pd.DataFrame(summary_metrics)

    st.markdown("### 📊 Inverter Summary")
    total_inverters = len(summary_df)
    total_working = summary_df["Working Strings"].sum()
    total_strings_all = summary_df["Total Strings"].sum()
    total_failed = summary_df["Failed Strings"].sum()
    overall_availability = (total_working / total_strings_all * 100) if total_strings_all > 0 else 0

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Total Inverters", total_inverters)
    col2.metric("Total Strings", total_strings_all)
    col3.metric("Working Strings", total_working)
    col4.metric("Failed Strings", total_failed)
    col5.metric("Overall Availability", f"{overall_availability:.1f}%")

    st.markdown("---")
    st.subheader("📋 Inverter-wise Summary")

    def color_availability(val):
        if isinstance(val, (int, float)):
            if val >= 90: return 'background-color: #10b981; color: white; font-weight: bold'
            elif val >= 70: return 'background-color: #34d399; color: white; font-weight: bold'
            elif val >= 50: return 'background-color: #fbbf24; color: black; font-weight: bold'
            elif val >= 30: return 'background-color: #f59e0b; color: white; font-weight: bold'
            else: return 'background-color: #ef4444; color: white; font-weight: bold'
        return ''

    def color_health_status(val):
        if val == "Excellent": return 'background-color: #10b981; color: white; font-weight: bold'
        elif val == "Good": return 'background-color: #34d399; color: white; font-weight: bold'
        elif val == "Fair": return 'background-color: #fbbf24; color: black; font-weight: bold'
        elif val == "Poor": return 'background-color: #ef4444; color: white; font-weight: bold'
        return ''

    def color_failed_strings(val):
        if isinstance(val, (int, float)):
            if val == 0: return 'background-color: #10b981; color: white; font-weight: bold'
            elif val <= 2: return 'background-color: #fbbf24; color: black; font-weight: bold'
            elif val <= 5: return 'background-color: #f59e0b; color: white; font-weight: bold'
            else: return 'background-color: #ef4444; color: white; font-weight: bold'
        return ''

    styled_summary = summary_df.style.map(color_availability, subset=['Availability (%)'])
    styled_summary = styled_summary.map(color_health_status, subset=['Health Status'])
    styled_summary = styled_summary.map(color_failed_strings, subset=['Failed Strings'])
    styled_summary = styled_summary.format({
        'Availability (%)': '{:.1f}%', 'Avg PV Voltage (V)': '{:.1f}', 'Avg PV Current (A)': '{:.2f}'
    })
    st.dataframe(styled_summary, use_container_width=True)

    st.markdown("---")
    st.subheader("🔌 Detailed PV String Data")
    st.caption("Green = Good (>5A), Yellow = Fair (1.5-5A), Orange = Poor (0.5-1.5A), Red = Critical (<0.5A)")

    display_cols = [inverter_col, "Plot", "Block", "SACU"]
    for col in ["Total Active Strings", "Working String Count", "Failed String Count", "Availability (%)", "Failure Percentage (%)"]:
        if col in filtered_df.columns:
            display_cols.append(col)

    additional_cols = ["Grid", "E-Daily(KWH)", "Active Power", "Reactive Power", "VAB", "VBC", "VCA", "IA", "IB", "IC"]
    for col in additional_cols:
        if col in filtered_df.columns:
            display_cols.append(col)

    pv_columns_to_show = []
    if show_voltage:
        pv_columns_to_show.extend(sorted(pv_voltage_cols))
    if show_current:
        pv_columns_to_show.extend(sorted(pv_current_cols))
    display_cols.extend(pv_columns_to_show)

    display_df = filtered_df[display_cols].copy()

    rename_map = {inverter_col: "Inverter ID"}
    rename_pairs = {
        "E-Daily(KWH)": "Energy (KWh)", "Active Power": "Active Power (KW)",
        "Reactive Power": "Reactive Power (KVAR)", "Total Active Strings": "Total Strings",
        "Working String Count": "Working", "Failed String Count": "Failed",
        "Failure Percentage (%)": "Failure %", "VAB": "VAB (V)", "VBC": "VBC (V)",
        "VCA": "VCA (V)", "IA": "IA (A)", "IB": "IB (A)", "IC": "IC (A)",
    }
    for k, v in rename_pairs.items():
        if k in display_df.columns:
            rename_map[k] = v
    display_df = display_df.rename(columns=rename_map)

    pv_current_cols_display = [col for col in pv_current_cols if col in display_df.columns]
    pv_voltage_cols_display = [col for col in pv_voltage_cols if col in display_df.columns]

    def apply_detailed_styling(df_display):
        styled = df_display.style
        for col in pv_columns_to_show:
            if col in df_display.columns:
                non_null = df_display[col].notna().sum()
                if non_null > 0:
                    working_count = (df_display[col] > WORKING_CURRENT_THRESHOLD).sum()
                    working_pct = (working_count / non_null) * 100
                    color = get_column_header_color(working_pct)
                    styled = styled.set_table_styles(
                        [{'selector': f'th.col{df_display.columns.get_loc(col)}',
                          'props': [('background-color', color), ('color', 'white'), ('font-weight', 'bold')]}],
                        overwrite=False
                    )
        for col in pv_current_cols_display:
            if col in df_display.columns:
                styled = styled.map(
                    lambda x: f'background-color: {get_string_health_color(x)}; color: white; font-weight: bold;'
                    if pd.notna(x) and isinstance(x, (int, float)) else '', subset=[col]
                )
        if "Availability (%)" in df_display.columns:
            styled = styled.map(color_availability, subset=['Availability (%)'])
        if "Failed" in df_display.columns:
            styled = styled.map(color_failed_strings, subset=['Failed'])
        styled = styled.set_table_styles([
            {'selector': 'thead th', 'props': [('position', 'sticky'), ('top', '0'), ('z-index', '999')]},
            {'selector': 'td', 'props': [('padding', '2px 4px'), ('font-size', '12px')]},
            {'selector': 'th', 'props': [('padding', '4px 8px'), ('font-size', '11px')]}
        ], overwrite=False)
        return styled

    if not display_df.empty:
        try:
            styled_df = apply_detailed_styling(display_df)
            st.dataframe(styled_df, use_container_width=True, height=400)
        except Exception as e:
            st.warning(f"Styling error: {str(e)}. Showing unstyled data.")
            st.dataframe(display_df, use_container_width=True, height=400)

        st.markdown("---")
        st.subheader("🔍 Individual Inverter Analysis")
        inverter_list = sorted_filter_options(filtered_df[inverter_col])
        selected_single_inverter = st.selectbox("Select Inverter for Detailed View", inverter_list, key="single_inverter_view")

        if selected_single_inverter:
            inverter_data = filtered_df[filtered_df[inverter_col] == selected_single_inverter].iloc[0]

            col1, col2, col3, col4, col5, col6 = st.columns(6)
            with col1: st.metric("Inverter", inverter_data[inverter_col])
            with col2: st.metric("Plot", inverter_data.get("Plot", "N/A"))
            with col3: st.metric("Block", inverter_data.get("Block", "N/A"))
            with col4: st.metric("SACU", inverter_data.get("SACU", "N/A"))
            with col5:
                if "Availability (%)" in inverter_data and pd.notna(inverter_data["Availability (%)"]):
                    availability = inverter_data["Availability (%)"]
                else:
                    working, total = 0, 0
                    for col in pv_current_cols:
                        if col in inverter_data and pd.notna(inverter_data[col]):
                            total += 1
                            if inverter_data[col] > WORKING_CURRENT_THRESHOLD:
                                working += 1
                    availability = (working / total * 100) if total > 0 else 0
                st.metric("Availability", f"{availability:.1f}%")
            with col6:
                if "Failed String Count" in inverter_data and pd.notna(inverter_data["Failed String Count"]):
                    st.metric("Failed Strings", int(inverter_data["Failed String Count"]))

            st.markdown("#### PV String Status")
            cols_per_row = 8
            for i in range(0, len(pv_current_cols), cols_per_row):
                string_cols = st.columns(cols_per_row)
                for idx, col in enumerate(pv_current_cols[i:i+cols_per_row]):
                    if col in inverter_data:
                        value = inverter_data[col]
                        if pd.notna(value):
                            status = "Working" if value > WORKING_CURRENT_THRESHOLD else "Failed"
                            color = "#10b981" if value > WORKING_CURRENT_THRESHOLD else "#ef4444"
                            with string_cols[idx]:
                                st.markdown(f"""
                                <div style='background-color: {color}; padding: 8px; border-radius: 5px; text-align: center; color: white; margin: 2px;'>
                                    <div style='font-size: 10px;'>{col}</div>
                                    <div style='font-size: 14px; font-weight: bold;'>{value:.1f}A</div>
                                    <div style='font-size: 9px;'>{status}</div>
                                </div>
                                """, unsafe_allow_html=True)

            st.markdown("#### Additional Metrics")
            metric_cols = st.columns(4)
            additional_metrics = [
                ("Grid", "Grid"), ("E-Daily(KWH)", "Energy (KWh)"),
                ("Active Power", "Active Power (KW)"), ("Reactive Power", "Reactive Power (KVAR)")
            ]
            for idx, (col, label) in enumerate(additional_metrics):
                if col in inverter_data and pd.notna(inverter_data[col]):
                    with metric_cols[idx]:
                        st.metric(label, f"{inverter_data[col]:.2f}" if isinstance(inverter_data[col], (int, float)) else inverter_data[col])

            if pv_voltage_cols:
                st.markdown("#### PV Voltage Summary")
                voltage_values = [inverter_data[col] for col in pv_voltage_cols if col in inverter_data and pd.notna(inverter_data[col])]
                if voltage_values:
                    vol_cols = st.columns(4)
                    vol_cols[0].metric("Average Voltage", f"{sum(voltage_values)/len(voltage_values):.1f}V")
                    vol_cols[1].metric("Min Voltage", f"{min(voltage_values):.1f}V")
                    vol_cols[2].metric("Max Voltage", f"{max(voltage_values):.1f}V")
                    vol_cols[3].metric("Voltage Differential", f"{max(voltage_values)-min(voltage_values):.1f}V")

            if "VAB" in inverter_data and "VBC" in inverter_data and "VCA" in inverter_data:
                st.markdown("#### Grid Voltage")
                grid_cols = st.columns(3)
                grid_cols[0].metric("VAB", f"{inverter_data['VAB']:.1f}V" if pd.notna(inverter_data['VAB']) else "N/A")
                grid_cols[1].metric("VBC", f"{inverter_data['VBC']:.1f}V" if pd.notna(inverter_data['VBC']) else "N/A")
                grid_cols[2].metric("VCA", f"{inverter_data['VCA']:.1f}V" if pd.notna(inverter_data['VCA']) else "N/A")

            if "IA" in inverter_data and "IB" in inverter_data and "IC" in inverter_data:
                st.markdown("#### Grid Current")
                grid_cols = st.columns(3)
                grid_cols[0].metric("IA", f"{inverter_data['IA']:.1f}A" if pd.notna(inverter_data['IA']) else "N/A")
                grid_cols[1].metric("IB", f"{inverter_data['IB']:.1f}A" if pd.notna(inverter_data['IB']) else "N/A")
                grid_cols[2].metric("IC", f"{inverter_data['IC']:.1f}A" if pd.notna(inverter_data['IC']) else "N/A")
    else:
        st.info("No PV string data available for the selected filters")

# ==========================================
# DASHBOARD FUNCTIONS  (unchanged logic)
# ==========================================
@st.cache_data(ttl=300)
def calculate_plot_summary(df, inverter_col):
    if df.empty:
        return pd.DataFrame()

    plot_summary = df.groupby("Plot", as_index=False).agg(
        Total_Inverters=(inverter_col if inverter_col else df.columns[0], "nunique"),
        Total_Active_Strings=("Total Active Strings", "sum"),
        Total_Working_Strings=("Working String Count", "sum"),
        Total_Failed_Strings=("Failed String Count", "sum")
    )
    plot_summary["Availability (%)"] = ((plot_summary["Total_Working_Strings"] / plot_summary["Total_Active_Strings"]) * 100).fillna(0).round(2)
    plot_summary["Failure Percentage (%)"] = ((plot_summary["Total_Failed_Strings"] / plot_summary["Total_Active_Strings"]) * 100).fillna(0).round(2)
    plot_summary["Health Status"] = plot_summary["Availability (%)"].apply(
        lambda x: "🟢 Excellent" if x >= 90 else "🟡 Good" if x >= 70 else "🟠 Fair" if x >= 50 else "🔴 Poor"
    )
    block_count = df.groupby("Plot")["Block"].nunique().reset_index(name="Total_Blocks")
    plot_summary = plot_summary.merge(block_count, on="Plot", how="left")
    return plot_summary

@st.cache_data(ttl=300)
def create_plot_charts(plot_summary):
    charts = {}

    fig_bar = px.bar(
        plot_summary, x="Plot", y=["Total_Working_Strings", "Total_Failed_Strings"], barmode="stack",
        title="📊 Plot-wise String Status", labels={"value": "Number of Strings", "Plot": "Plot", "variable": "Status"},
        color_discrete_map={"Total_Working_Strings": "#10b981", "Total_Failed_Strings": "#ef4444"}, text_auto=True
    )
    fig_bar.update_layout(height=450, hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)", font=dict(size=12))
    fig_bar.update_traces(textfont_size=12, textposition="inside", insidetextanchor="middle")
    fig_bar.update_yaxes(tickformat=",.0f")
    charts["bar"] = fig_bar

    plot_summary_sorted = plot_summary.sort_values("Availability (%)", ascending=True)
    fig_avail = px.bar(
        plot_summary_sorted, x="Availability (%)", y="Plot", orientation="h",
        title="📈 Plot-wise Availability (%)", labels={"Availability (%)": "Availability (%)", "Plot": "Plot"},
        color="Availability (%)",
        color_continuous_scale=[[0, "#ef4444"], [0.3, "#f59e0b"], [0.5, "#fbbf24"], [0.7, "#34d399"], [1, "#10b981"]],
        range_color=[0, 100], text_auto=".1f"
    )
    fig_avail.update_layout(height=400, hovermode="y unified", plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)", font=dict(size=12), coloraxis_showscale=False, xaxis=dict(range=[0, 105]))
    fig_avail.update_traces(textposition="outside", textfont_size=12)
    charts["availability"] = fig_avail

    total_working = plot_summary["Total_Working_Strings"].sum()
    total_failed = plot_summary["Total_Failed_Strings"].sum()
    fig_donut = go.Figure(data=[go.Pie(
        labels=["✅ Working Strings", "❌ Failed Strings"], values=[total_working, total_failed], hole=0.6,
        marker_colors=["#10b981", "#ef4444"], textinfo="label+percent", textposition="auto", pull=[0.05, 0]
    )])
    fig_donut.update_layout(height=400, title="🎯 Overall String Health", plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)", font=dict(size=12),
        annotations=[dict(text=f"<b>{total_working + total_failed:,}</b><br>Total Strings", x=0.5, y=0.5, font_size=16, showarrow=False)])
    charts["donut"] = fig_donut

    fig_scatter = px.scatter(
        plot_summary, x="Total_Inverters", y="Total_Active_Strings", size="Total_Active_Strings",
        color="Availability (%)", text="Plot", title="📍 Plot Distribution: Inverters vs Strings",
        labels={"Total_Inverters": "Number of Inverters", "Total_Active_Strings": "Total Strings", "Availability (%)": "Availability"},
        color_continuous_scale=[[0, "#ef4444"], [0.3, "#f59e0b"], [0.5, "#fbbf24"], [0.7, "#34d399"], [1, "#10b981"]],
        range_color=[0, 100], size_max=60
    )
    fig_scatter.update_traces(textposition="top center", marker=dict(line=dict(width=1, color='white')))
    fig_scatter.update_layout(height=400, plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        font=dict(size=12), hovermode="closest")
    fig_scatter.update_xaxes(tickformat=",.0f")
    fig_scatter.update_yaxes(tickformat=",.0f")
    charts["scatter"] = fig_scatter

    fig_treemap = px.treemap(
        plot_summary, path=["Plot"], values="Total_Active_Strings", color="Availability (%)",
        color_continuous_scale=[[0, "#ef4444"], [0.3, "#f59e0b"], [0.5, "#fbbf24"], [0.7, "#34d399"], [1, "#10b981"]],
        range_color=[0, 100], title="🎨 String Distribution by Plot",
        hover_data={"Total_Active_Strings": True, "Total_Working_Strings": True, "Total_Failed_Strings": True,
                    "Total_Inverters": True, "Total_Blocks": True, "Availability (%)": ":.1f%"}
    )
    fig_treemap.update_traces(textinfo="label+value", textfont_size=14, marker=dict(cornerradius=4),
        hovertemplate='<b>%{label}</b><br>Total Strings: %{value:,.0f}<br>Availability: %{color:,.1f}%<extra></extra>')
    fig_treemap.update_layout(height=400, plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        font=dict(size=12), coloraxis_showscale=False)
    charts["treemap"] = fig_treemap

    return charts

def display_plot_metrics(plot_summary):
    st.subheader("📊 Plot-wise Performance Overview")
    cols = st.columns(min(5, len(plot_summary)))

    for idx, (_, row) in enumerate(plot_summary.iterrows()):
        if idx >= 5:
            break
        col_idx = idx % 5
        with cols[col_idx]:
            avail = row["Availability (%)"]
            if avail >= 90: status_color, status_icon, status_text = "#10b981", "🟢", "Excellent"
            elif avail >= 70: status_color, status_icon, status_text = "#34d399", "🟡", "Good"
            elif avail >= 50: status_color, status_icon, status_text = "#fbbf24", "🟠", "Fair"
            else: status_color, status_icon, status_text = "#ef4444", "🔴", "Poor"

            st.markdown(f"""
            <div style='background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%); border: 2px solid {status_color}; border-radius: 12px; padding: 15px; margin: 5px 0;'>
                <div style='display: flex; justify-content: space-between; align-items: center;'>
                    <h3 style='margin: 0; color: #f1f5f9;'>{row['Plot']}</h3>
                    <span style='font-size: 24px;'>{status_icon}</span>
                </div>
                <div style='margin-top: 8px;'>
                    <div style='display: flex; justify-content: space-between;'>
                        <span style='color: #94a3b8; font-size: 12px;'>Status</span>
                        <span style='color: {status_color}; font-weight: bold; font-size: 14px;'>{status_text}</span>
                    </div>
                    <div style='display: flex; justify-content: space-between; margin-top: 4px;'>
                        <span style='color: #94a3b8; font-size: 12px;'>Inverters</span>
                        <span style='color: #f1f5f9; font-weight: bold;'>{int(row['Total_Inverters']):,}</span>
                    </div>
                    <div style='display: flex; justify-content: space-between;'>
                        <span style='color: #94a3b8; font-size: 12px;'>Total Strings</span>
                        <span style='color: #f1f5f9; font-weight: bold;'>{int(row['Total_Active_Strings']):,}</span>
                    </div>
                    <div style='display: flex; justify-content: space-between;'>
                        <span style='color: #94a3b8; font-size: 12px;'>✅ Working</span>
                        <span style='color: #10b981; font-weight: bold;'>{int(row['Total_Working_Strings']):,}</span>
                    </div>
                    <div style='display: flex; justify-content: space-between; margin-bottom: 8px;'>
                        <span style='color: #94a3b8; font-size: 12px;'>❌ Failed</span>
                        <span style='color: #ef4444; font-weight: bold;'>{int(row['Total_Failed_Strings']):,}</span>
                    </div>
                    <div style='background-color: #1e293b; height: 8px; border-radius: 4px; overflow: hidden;'>
                        <div style='background: linear-gradient(90deg, {status_color}, {status_color}88); width: {avail}%; height: 100%;'></div>
                    </div>
                    <div style='display: flex; justify-content: space-between; margin-top: 4px;'>
                        <span style='color: #94a3b8; font-size: 11px;'>Availability</span>
                        <span style='color: {status_color}; font-weight: bold; font-size: 16px;'>{avail:.1f}%</span>
                    </div>
                </div>
            </div>
            """, unsafe_allow_html=True)

def main_dashboard_tab(df):
    st.title("☀️ Solar PV String Performance Dashboard")
    st.caption("Internal beta release — data processing, history, and comparison features are under testing.")

    inverter_col = None
    df_columns_lower_map = {str(c).strip().lower(): c for c in df.columns}
    for col in INVERTER_ID_COLS:
        if col in df.columns:
            inverter_col = col
            break
        elif col.strip().lower() in df_columns_lower_map:
            inverter_col = df_columns_lower_map[col.strip().lower()]
            break

    plot_summary = calculate_plot_summary(df, inverter_col)

    st.markdown("### 📊 Key Performance Indicators")
    kpi1, kpi2, kpi3, kpi4, kpi5, kpi6 = st.columns(6)

    total_inverters = df[inverter_col].nunique() if inverter_col and inverter_col in df.columns else 0
    total_strings = int(df["Total Active Strings"].sum()) if "Total Active Strings" in df.columns else 0
    working_strings = int(df["Working String Count"].sum()) if "Working String Count" in df.columns else 0
    failed_strings = int(df["Failed String Count"].sum()) if "Failed String Count" in df.columns else 0
    overall_availability = round((working_strings / total_strings) * 100, 2) if total_strings > 0 else 0.0
    num_plots = plot_summary["Plot"].nunique() if not plot_summary.empty else 0

    kpi1.metric("🏗️ Total Plots", f"{num_plots:,}")
    kpi2.metric("🔌 Total Inverters", f"{total_inverters:,}")
    kpi3.metric("📊 Total Strings", f"{total_strings:,}")
    kpi4.metric("✅ Working", f"{working_strings:,}")
    kpi5.metric("❌ Failed", f"{failed_strings:,}")
    kpi6.metric("📈 Availability", f"{overall_availability:.1f}%")

    st.markdown("---")
    if not plot_summary.empty:
        display_plot_metrics(plot_summary)
        st.markdown("---")

    st.subheader("Plot-wise Visualization Dashboard")
    st.caption("Understanding your PV plant performance at a glance")
    charts = create_plot_charts(plot_summary)

    col1, col2 = st.columns([2, 1])
    with col1: st.plotly_chart(charts["bar"], use_container_width=True, key="plot_bar")
    with col2: st.plotly_chart(charts["donut"], use_container_width=True, key="overall_donut")

    col1, col2 = st.columns(2)
    with col1: st.plotly_chart(charts["availability"], use_container_width=True, key="avail_bar")
    with col2: st.plotly_chart(charts["treemap"], use_container_width=True, key="plot_treemap")

    st.plotly_chart(charts["scatter"], use_container_width=True, key="plot_scatter")
    st.markdown("---")
    st.subheader("Detailed Plot Summary")

    if not plot_summary.empty:
        display_plot_df = plot_summary.copy()
        display_plot_df = display_plot_df[[
            "Plot", "Total_Blocks", "Total_Inverters", "Total_Active_Strings",
            "Total_Working_Strings", "Total_Failed_Strings", "Availability (%)",
            "Failure Percentage (%)", "Health Status"
        ]]

        def color_health_status(val):
            if "Excellent" in str(val): return 'background-color: #10b981; color: white; font-weight: bold; border-radius: 4px;'
            elif "Good" in str(val): return 'background-color: #34d399; color: white; font-weight: bold; border-radius: 4px;'
            elif "Fair" in str(val): return 'background-color: #fbbf24; color: black; font-weight: bold; border-radius: 4px;'
            elif "Poor" in str(val): return 'background-color: #ef4444; color: white; font-weight: bold; border-radius: 4px;'
            return ''

        styled_plot_df = display_plot_df.style.map(color_health_status, subset=['Health Status'])
        styled_plot_df = styled_plot_df.format({
            'Total_Active_Strings': '{:,.0f}', 'Total_Working_Strings': '{:,.0f}',
            'Total_Failed_Strings': '{:,.0f}', 'Total_Inverters': '{:,.0f}',
            'Total_Blocks': '{:,.0f}', 'Availability (%)': '{:.1f}%', 'Failure Percentage (%)': '{:.1f}%'
        })

        def availability_bar(val):
            if isinstance(val, (int, float)):
                color = "#10b981" if val >= 90 else "#34d399" if val >= 70 else "#fbbf24" if val >= 50 else "#ef4444"
                return f'background: linear-gradient(90deg, {color} {val}%, transparent {val}%); font-weight: bold; padding: 4px 8px; border-radius: 4px;'
            return ''

        styled_plot_df = styled_plot_df.map(availability_bar, subset=['Availability (%)'])

        st.dataframe(styled_plot_df, use_container_width=True, column_config={
            "Plot": "📍 Plot", "Total_Blocks": "🏗️ Blocks", "Total_Inverters": "🔌 Inverters",
            "Total_Active_Strings": "📊 Total Strings", "Total_Working_Strings": "✅ Working",
            "Total_Failed_Strings": "❌ Failed", "Availability (%)": "📈 Availability",
            "Failure Percentage (%)": "⚠️ Failure %", "Health Status": "💚 Health"
        })

        col1, col2 = st.columns(2)
        with col1:
            csv = plot_summary.to_csv(index=False)
            st.download_button(
                label="📥 Download Plot Summary (CSV)", data=csv,
                file_name=f"plot_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv", use_container_width=True,
                on_click=_log_download, args=("plot_summary_csv",),
            )
        with col2:
            best_plot = plot_summary.loc[plot_summary["Availability (%)"].idxmax()]
            worst_plot = plot_summary.loc[plot_summary["Availability (%)"].idxmin()]
            st.info(f"""
            **💡 Insights:**
            - Best performing plot: **{best_plot['Plot']}** ({best_plot['Availability (%)']:.1f}% availability)
            - Needs attention: **{worst_plot['Plot']}** ({worst_plot['Availability (%)']:.1f}% availability)
            - Total working strings: **{working_strings:,}** out of **{total_strings:,}**
            """)
    else:
        st.warning("No plot summary available.")


# ==========================================
# 8. AUDIT LOGGING HELPERS FOR DOWNLOADS
# ==========================================
def _log_download(report_name):
    """on_click callback for download buttons - records who downloaded what and when."""
    user = get_current_user()
    if not user:
        return
    storage.log_audit_event(user["username"], user["role"], "download_report", {"report": report_name})


# ==========================================
# 9. MAIN APP
# ==========================================
def main():
    storage.init_default_users()

    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False

    # ---------------- LOGIN ----------------
    if not st.session_state.authenticated:
        st.title("☀️ Solar PV String Analytics")
        st.markdown("### Login to access the dashboard")

        col1, col2, col3 = st.columns([1, 1, 1])
        with col2:
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")

            if st.button("Login", use_container_width=True):
                user_data = storage.authenticate_user(username, password)
                if user_data:
                    st.session_state.user = {
                        "username": username,
                        "role": user_data["role"],
                        "full_name": user_data.get("full_name", username),
                        "assigned_plots": user_data.get("assigned_plots", []),
                    }
                    st.session_state.authenticated = True
                    storage.log_audit_event(username, user_data["role"], "login", {})
                    st.rerun()
                else:
                    st.error("Invalid username or password")
        st.markdown("---")
        return

    # ---------------- AUTHENTICATED ----------------
    current_user = get_current_user()
    if not current_user:
        st.error("User not found")
        return

    role = current_user["role"]
    full_name = current_user.get("full_name", current_user["username"])

    # ---- Welcome banner / greeting ----
    st.markdown(f"""
    <div class="welcome-banner">
        <span style="font-size:1.15rem;">👋 Hi, <b>{full_name}</b>!</span>
        &nbsp;&nbsp;<span class="user-badge-{role}">{ROLE_BADGES.get(role, role)}</span>
    </div>
    """, unsafe_allow_html=True)

    # ---- Sidebar ----
    st.sidebar.title("⚡ PV String Template")

    with st.sidebar.expander("👤 My Profile", expanded=False):
        st.write(f"**Full Name:** {full_name}")
        st.write(f"**Username:** {current_user['username']}")
        st.write(f"**Role:** {ROLE_BADGES.get(role, role)}")
        st.write(f"**Assigned Plots:** {', '.join(current_user.get('assigned_plots', [])) or 'None'}")

        st.markdown("---")
        st.write("**🔑 Change My Password**")
        self_new_pw = st.text_input("New Password", type="password", key="self_pw_1")
        self_new_pw_confirm = st.text_input("Confirm New Password", type="password", key="self_pw_2")
        if st.button("Update Password", key="self_pw_update_btn"):
            if not self_new_pw or self_new_pw != self_new_pw_confirm:
                st.error("Passwords don't match or are empty.")
            else:
                ok, msg = storage.reset_password(current_user["username"], self_new_pw)
                if ok:
                    storage.log_audit_event(current_user["username"], role, "password_reset_self", {})
                    st.success(msg)
                else:
                    st.error(msg)

    st.sidebar.markdown(f"**User:** {current_user['username']} ({ROLE_BADGES.get(role, role)})")

    if st.sidebar.button("🚪 Logout"):
        storage.log_audit_event(current_user["username"], role, "logout", {})
        st.session_state.authenticated = False
        st.session_state.user = None
        st.rerun()

    st.sidebar.markdown("---")

    # ---- File upload (admin only) ----
    st.sidebar.subheader("📁 File Management")

    if is_admin():
        latest_upload = storage.get_latest_upload()
        if latest_upload:
            st.sidebar.info(f"📄 Current file: {latest_upload['original_filename']}\n"
                             f"📅 Snapshot date: {latest_upload['snapshot_date']}\n"
                             f"🕒 Uploaded: {latest_upload['upload_timestamp']}")

        snapshot_date = st.sidebar.date_input("Snapshot date for this upload", value=datetime.now().date())
        uploaded_file = st.sidebar.file_uploader("Upload new SCADA Report (.xlsx)", type=["xlsx"])
        if uploaded_file:
            ok, msg = process_and_save_upload(
                uploaded_file.getvalue(), uploaded_file.name, snapshot_date,
                current_user["username"], role,
            )
            if ok:
                st.sidebar.success(f"✅ {msg}")
                st.rerun()
            else:
                st.sidebar.error(msg)
    else:
        latest_upload = storage.get_latest_upload()
        if latest_upload:
            st.sidebar.info(f"📄 Current file: {latest_upload['original_filename']}")
            st.sidebar.caption(f"📅 Snapshot date: {latest_upload['snapshot_date']}")
        else:
            st.sidebar.warning("No file available. Please contact admin.")

    st.sidebar.markdown("---")

    # ---- Load current data ----
    latest_upload = storage.get_latest_upload()
    if not latest_upload:
        st.info("No SCADA file available. Please contact admin to upload one.")
        return

    file_bytes = storage.load_original_bytes(latest_upload["upload_id"])
    if not file_bytes:
        st.error("Could not load file from backend storage")
        return

    processed_dataframes = process_scada_excel_bytes(file_bytes)
    if not processed_dataframes:
        st.error("No valid sheets or inverter columns were identified in the uploaded workbook.")
        return

    if role == "engineer":
        allowed_plots = current_user.get("assigned_plots", [])
        if allowed_plots:
            st.sidebar.markdown("---")
            st.sidebar.subheader("🔒 Assigned Plots")
            st.sidebar.write(", ".join(allowed_plots))

    sheet_selection = st.sidebar.selectbox("Select Sheet", list(processed_dataframes.keys()))
    df_selected = processed_dataframes[sheet_selection].copy()

    if role == "engineer":
        allowed_plots = current_user.get("assigned_plots", [])
        if allowed_plots:
            df_selected = df_selected[df_selected["Plot"].isin(allowed_plots)]

    st.sidebar.markdown("---")
    st.sidebar.subheader("Filters")

    plots = ["All"] + sorted_filter_options(df_selected["Plot"])
    selected_plot = st.sidebar.selectbox("Plot", plots)

    filtered_df = df_selected.copy()
    if selected_plot != "All":
        filtered_df = filtered_df[filtered_df["Plot"] == selected_plot]

    blocks = ["All"] + sorted_filter_options(filtered_df["Block"])
    selected_block = st.sidebar.selectbox("Block", blocks)
    if selected_block != "All":
        filtered_df = filtered_df[filtered_df["Block"] == selected_block]

    sacus = ["All"] + sorted_filter_options(filtered_df["SACU"])
    selected_sacu = st.sidebar.selectbox("SACU", sacus)
    if selected_sacu != "All":
        filtered_df = filtered_df[filtered_df["SACU"] == selected_sacu]

    # ---- User management (admin / manager) ----
    user_management_ui()

    inverter_col = None
    df_columns_lower_map = {str(c).strip().lower(): c for c in filtered_df.columns}
    for col in INVERTER_ID_COLS:
        if col in filtered_df.columns:
            inverter_col = col
            break
        elif col.strip().lower() in df_columns_lower_map:
            inverter_col = df_columns_lower_map[col.strip().lower()]
            break

    tab1, tab2, tab3, tab4, tab5 = st.tabs(
        ["📊 Dashboard", "🔌 PV String Details", "📋 Data Table", "🔄 Restore & TAT", "🕵️ Audit Log"]
    )

    with tab1:
        if not filtered_df.empty:
            main_dashboard_tab(filtered_df)
        else:
            st.warning("No data available with current filters and permissions")

    with tab2:
        if not filtered_df.empty:
            create_pv_string_tab(filtered_df)
        else:
            st.warning("No data available for PV string analysis")

    with tab3:
        st.subheader("Inverter Data Table")
        if not filtered_df.empty:
            display_df = filtered_df.copy()
            if inverter_col and inverter_col != "Inverter ID":
                display_df = display_df.rename(columns={inverter_col: "Inverter ID"})

            st.dataframe(display_df, use_container_width=True, column_config={
                "Availability (%)": st.column_config.ProgressColumn("Availability (%)", min_value=0, max_value=100, format="%.2f%%"),
                "Failure Percentage (%)": st.column_config.NumberColumn("Failure Percentage (%)", format="%.2f%%")
            })

            download_bytes = create_excel_download({sheet_selection: filtered_df})
            st.download_button(
                label="📥 Download Filtered Excel", data=download_bytes,
                file_name=f"processed_{sheet_selection}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                on_click=_log_download, args=(f"filtered_excel:{sheet_selection}",),
            )
        else:
            st.info("No data available")

    with tab4:
        if not filtered_df.empty:
            restore.get_restore_tab(
                processed_dataframes, filtered_df,
                sheet_name=sheet_selection, user_role=role, username=current_user["username"],
                upload_handler=lambda file_bytes, filename, snap_date: process_and_save_upload(
                    file_bytes, filename, snap_date, current_user["username"], role
                ) if is_admin() else (False, "Only admins can upload snapshots."),
            )
        else:
            st.warning("No data available for TAT analysis")

    with tab5:
        audit_log_tab()

if __name__ == "__main__":
    main()
