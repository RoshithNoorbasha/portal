# app.py
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
import requests
from typing import Dict, List, Optional, Tuple

# ==========================================
# 1. PAGE CONFIGURATION & STYLING
# ==========================================
st.set_page_config(
    page_title="PV String Analytics",
    page_icon="☀️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Font Awesome CDN
st.markdown("""
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
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
    .fa-icon {
        margin-right: 8px;
    }
    .api-status-connected {
        color: #10b981;
        font-weight: 600;
    }
    .api-status-disconnected {
        color: #ef4444;
        font-weight: 600;
    }
    .status-badge {
        display: inline-block;
        padding: 2px 10px;
        border-radius: 12px;
        font-size: 0.8rem;
        font-weight: 600;
    }
    .status-badge-success {
        background-color: #10b981;
        color: white;
    }
    .status-badge-danger {
        background-color: #ef4444;
        color: white;
    }
    .login-container {
        max-width: 400px;
        margin: 0 auto;
        padding: 2rem;
        background-color: #0f172a;
        border-radius: 12px;
        border: 1px solid #1e293b;
    }
    .login-container h2 {
        text-align: center;
        color: #38bdf8;
        margin-bottom: 1.5rem;
    }
    .app-title {
        text-align: center;
        font-size: 2.5rem;
        font-weight: 700;
        color: #38bdf8;
        margin-bottom: 0.5rem;
    }
    .app-subtitle {
        text-align: center;
        color: #94a3b8;
        margin-bottom: 2rem;
    }
</style>
""", unsafe_allow_html=True)

# ==========================================
# 2. CONFIGURATION
# ==========================================
API_BASE_URL = "http://localhost:8000"

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

ALL_PLOTS = ["P2", "P6", "P8", "P9", "P10", "P12"]

ROLE_BADGES = {
    "admin": '<i class="fas fa-crown"></i> Admin',
    "manager": '<i class="fas fa-compass"></i> Manager',
    "engineer": '<i class="fas fa-wrench"></i> Engineer',
}

# ==========================================
# 3. SESSION STATE INITIALIZATION
# ==========================================
# Initialize session state keys if they don't exist
if "user" not in st.session_state:
    st.session_state.user = None
if "login_attempt" not in st.session_state:
    st.session_state.login_attempt = False

# ==========================================
# 4. API CLIENT FUNCTIONS
# ==========================================
def api_health_check() -> bool:
    """Check if the FastAPI server is running"""
    try:
        response = requests.get(f"{API_BASE_URL}/", timeout=5)
        return response.status_code == 200
    except:
        return False

def api_upload_file(file_bytes: bytes, filename: str, snapshot_date: str) -> Tuple[bool, Dict]:
    """Upload and process file through FastAPI"""
    try:
        files = {
            'file': (filename, file_bytes, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        }
        params = {'snapshot_date': snapshot_date}
        
        response = requests.post(
            f"{API_BASE_URL}/upload",
            files=files,
            params=params,
            timeout=300
        )
        
        if response.status_code == 200:
            return True, response.json()
        else:
            return False, {"error": response.text}
            
    except requests.exceptions.Timeout:
        return False, {"error": "Upload timeout - file may be too large"}
    except requests.exceptions.ConnectionError:
        return False, {"error": "Cannot connect to FastAPI server. Is it running?"}
    except Exception as e:
        return False, {"error": str(e)}

def api_get_data(
    skip: int = 0,
    limit: int = 1000,
    plot: Optional[str] = None,
    block: Optional[str] = None,
    sacu: Optional[str] = None,
    file_name: Optional[str] = None,
    snapshot_date: Optional[str] = None
) -> Tuple[bool, Dict]:
    """Retrieve processed data from FastAPI"""
    try:
        params = {"skip": skip, "limit": limit}
        if plot:
            params["plot"] = plot
        if block:
            params["block"] = block
        if sacu:
            params["sacu"] = sacu
        if file_name:
            params["file_name"] = file_name
        if snapshot_date:
            params["snapshot_date"] = snapshot_date
        
        response = requests.get(
            f"{API_BASE_URL}/data",
            params=params,
            timeout=30
        )
        
        if response.status_code == 200:
            return True, response.json()
        else:
            return False, {"error": response.text}
            
    except Exception as e:
        return False, {"error": str(e)}

def api_get_statistics() -> Tuple[bool, Dict]:
    """Get aggregated statistics from FastAPI"""
    try:
        response = requests.get(
            f"{API_BASE_URL}/data/statistics",
            timeout=30
        )
        
        if response.status_code == 200:
            return True, response.json()
        else:
            return False, {"error": response.text}
            
    except Exception as e:
        return False, {"error": str(e)}

def api_get_files() -> Tuple[bool, Dict]:
    """Get list of uploaded files"""
    try:
        response = requests.get(
            f"{API_BASE_URL}/data/files",
            timeout=30
        )
        
        if response.status_code == 200:
            return True, response.json()
        else:
            return False, {"error": response.text}
            
    except Exception as e:
        return False, {"error": str(e)}

def api_delete_record(record_id: int) -> Tuple[bool, str]:
    """Delete a specific record"""
    try:
        response = requests.delete(
            f"{API_BASE_URL}/data/{record_id}",
            timeout=30
        )
        
        if response.status_code == 200:
            return True, response.json().get("message", "Deleted successfully")
        else:
            return False, response.text
            
    except Exception as e:
        return False, str(e)

def api_delete_file_data(file_name: str) -> Tuple[bool, str]:
    """Delete all records for a specific file"""
    try:
        response = requests.delete(
            f"{API_BASE_URL}/data/file/{file_name}",
            timeout=30
        )
        
        if response.status_code == 200:
            return True, response.json().get("message", "Deleted successfully")
        else:
            return False, response.text
            
    except Exception as e:
        return False, str(e)

def api_authenticate_user(username: str, password: str) -> Tuple[bool, Dict]:
    """Authenticate user through FastAPI"""
    try:
        response = requests.post(
            f"{API_BASE_URL}/users/authenticate",
            params={"username": username, "password": password},
            timeout=30
        )
        
        if response.status_code == 200:
            return True, response.json()
        else:
            return False, {"error": response.text}
            
    except Exception as e:
        return False, {"error": str(e)}

# ==========================================
# 5. LOCAL STORAGE FUNCTIONS (for compatibility)
# ==========================================
def load_users():
    """Load users from local storage (for demo)"""
    return {
        "admin": {
            "username": "admin",
            "role": "admin",
            "full_name": "Administrator",
            "assigned_plots": ALL_PLOTS,
            "password_hash": "admin123"
        }
    }

def authenticate_user(username: str, password: str):
    """Authenticate user - tries API first, falls back to local"""
    # Try API authentication first
    success, response = api_authenticate_user(username, password)
    if success:
        return response
    
    # Fallback to local authentication for demo
    users = load_users()
    if username in users and users[username].get("password_hash") == password:
        return {
            "username": username,
            "role": users[username]["role"],
            "full_name": users[username]["full_name"],
            "assigned_plots": users[username]["assigned_plots"]
        }
    return None

def can_manage_users(role: str) -> bool:
    return role in ["admin", "manager"]

def creatable_roles(role: str) -> List[str]:
    if role == "admin":
        return ["admin", "manager", "engineer"]
    elif role == "manager":
        return ["engineer"]
    return []

def can_delete_users(role: str) -> bool:
    return role == "admin"

def can_assign_plots(role: str) -> bool:
    return role in ["admin", "manager"]

def can_reset_password_for(role: str, target_role: str) -> bool:
    if role == "admin":
        return True
    if role == "manager" and target_role == "engineer":
        return True
    return False

def can_view_audit_log(role: str) -> bool:
    return role in ["admin", "manager", "engineer"]

def log_audit_event(username: str, role: str, event_type: str, details: Dict):
    """Log audit event - tries API, falls back to local"""
    try:
        response = requests.post(
            f"{API_BASE_URL}/audit/log",
            json={
                "username": username,
                "role": role,
                "event_type": event_type,
                "details": details
            },
            timeout=10
        )
        if response.status_code == 200:
            return
    except:
        pass
    print(f"Audit: {username} - {event_type} - {details}")

# ==========================================
# 6. HELPER FUNCTIONS (for display)
# ==========================================
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

def get_pv_string_columns(df):
    """Get PV voltage and current columns from dataframe"""
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

def create_excel_download(dataframes_dict):
    """Create Excel file from dataframes"""
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for sheet_name, df in dataframes_dict.items():
            safe_sheet_name = str(sheet_name)[:31]
            df.to_excel(writer, sheet_name=safe_sheet_name, index=False)
    buffer.seek(0)
    return buffer.getvalue()

# ==========================================
# 7. UI FUNCTIONS
# ==========================================
def user_management_ui():
    """User management UI for admin and manager roles"""
    current_user = st.session_state.user
    if current_user is None:
        return
        
    role = current_user.get("role")
    
    if not can_manage_users(role):
        return

    st.sidebar.markdown("---")
    st.sidebar.markdown('<i class="fas fa-users"></i> User Management', unsafe_allow_html=True)

    with st.sidebar.expander('<i class="fas fa-user-cog"></i> Manage Users', expanded=False):
        st.info("User management is available through the API. Please use the API endpoints for full user management.")
        
        if st.button("View Users"):
            users = load_users()
            for username, user_data in users.items():
                st.write(f"- {username} ({user_data.get('role')})")

def audit_log_tab():
    """Audit log tab"""
    current_user = st.session_state.user
    if current_user is None:
        return
        
    role = current_user.get("role")
    username = current_user.get("username")

    if not can_view_audit_log(role):
        st.info("Audit log isn't available for your role.")
        return

    st.markdown('<i class="fas fa-search"></i> Audit Log', unsafe_allow_html=True)
    
    try:
        response = requests.get(
            f"{API_BASE_URL}/audit/log",
            params={"limit": 100},
            timeout=30
        )
        
        if response.status_code == 200:
            data = response.json()
            entries = data.get("entries", [])
            
            if entries:
                df_audit = pd.DataFrame(entries)
                df_audit["timestamp"] = pd.to_datetime(df_audit["timestamp"])
                df_audit = df_audit.sort_values("timestamp", ascending=False)
                
                filter_col1, filter_col2 = st.columns(2)
                with filter_col1:
                    event_types = ["All"] + sorted_filter_options(pd.Series([e["event_type"] for e in entries]))
                    selected_event = st.selectbox("Filter by Event Type", event_types, key="audit_event_filter")
                with filter_col2:
                    if role == "admin":
                        users_in_log = ["All"] + sorted_filter_options(pd.Series([e["username"] for e in entries]))
                        selected_user = st.selectbox("Filter by User", users_in_log, key="audit_user_filter")
                    else:
                        selected_user = "All"
                
                filtered = df_audit.copy()
                if selected_event != "All":
                    filtered = filtered[filtered["event_type"] == selected_event]
                if selected_user != "All":
                    filtered = filtered[filtered["username"] == selected_user]
                
                st.caption(f"Showing {len(filtered)} of {len(df_audit)} event(s)")
                st.dataframe(
                    filtered[["timestamp", "username", "role", "event_type", "details"]],
                    use_container_width=True,
                    height=450
                )
            else:
                st.info("No audit events found.")
        else:
            st.warning("Could not retrieve audit log from API")
            
    except Exception as e:
        st.warning(f"Audit log retrieval failed: {str(e)}")

def create_pv_string_tab(df):
    """Create the inverter-wise PV string details tab"""
    st.markdown('<i class="fas fa-plug"></i> Inverter-wise PV String Details', unsafe_allow_html=True)
    st.caption("Data loaded from FastAPI backend")

    if df.empty:
        st.warning("No data available. Please upload a file first.")
        return

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

    pv_voltage_cols, pv_current_cols = [], []
    if "raw_data" in df.columns:
        try:
            sample = df.iloc[0]["raw_data"]
            if isinstance(sample, dict):
                pv_voltage_cols, pv_current_cols = get_pv_string_columns(pd.DataFrame([sample]))
        except:
            pass

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        available_plots = sorted_filter_options(df["plot"])
        selected_plot = st.selectbox("Filter by Plot", ["All"] + available_plots, key="pv_plot_filter")
    with col2:
        filtered_by_plot = df if selected_plot == "All" else df[df["plot"] == selected_plot]
        available_blocks = sorted_filter_options(filtered_by_plot["block"])
        selected_block = st.selectbox("Filter by Block", ["All"] + available_blocks, key="pv_block_filter")
    with col3:
        filtered_by_block = filtered_by_plot if selected_block == "All" else filtered_by_plot[filtered_by_plot["block"] == selected_block]
        available_sacus = sorted_filter_options(filtered_by_block["sacu"])
        selected_sacu = st.selectbox("Filter by SACU", ["All"] + available_sacus, key="pv_sacu_filter")
    with col4:
        filtered_by_sacu = filtered_by_block if selected_sacu == "All" else filtered_by_block[filtered_by_block["sacu"] == selected_sacu]
        available_inverters = sorted_filter_options(filtered_by_sacu[inverter_col])
        selected_inverter = st.selectbox("Filter by Inverter", ["All"] + available_inverters, key="pv_inverter_filter")

    filtered_df = df.copy()
    if selected_plot != "All":
        filtered_df = filtered_df[filtered_df["plot"] == selected_plot]
    if selected_block != "All":
        filtered_df = filtered_df[filtered_df["block"] == selected_block]
    if selected_sacu != "All":
        filtered_df = filtered_df[filtered_df["sacu"] == selected_sacu]
    if selected_inverter != "All":
        filtered_df = filtered_df[filtered_df[inverter_col] == selected_inverter]

    if filtered_df.empty:
        st.warning("No data available for the selected filters")
        return

    create_summary_view(filtered_df)

def create_summary_view(df):
    """Create a summary view with key metrics"""
    st.markdown("### Summary Metrics")
    
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        total_inverters = len(df)
        st.metric("Total Inverters", total_inverters)
    with col2:
        avg_availability = df["availability"].mean() if "availability" in df.columns else 0
        st.metric("Average Availability", f"{avg_availability:.1f}%")
    with col3:
        avg_failure = df["failure_percentage"].mean() if "failure_percentage" in df.columns else 0
        st.metric("Average Failure", f"{avg_failure:.1f}%")
    with col4:
        total_strings = df["total_active_strings"].sum() if "total_active_strings" in df.columns else 0
        st.metric("Total Active Strings", int(total_strings))
    
    st.markdown("### Inverter Details")
    
    display_columns = [
        "plot", "block", "inverter_id", "sacu",
        "total_active_strings", "working_string_count",
        "failed_string_count", "availability", "failure_percentage"
    ]
    
    available_cols = [col for col in display_columns if col in df.columns]
    display_df = df[available_cols].copy()
    display_df.columns = [col.replace("_", " ").title() for col in display_df.columns]
    
    st.dataframe(display_df, use_container_width=True, height=400)
    
    if "plot" in df.columns and "availability" in df.columns:
        col1, col2 = st.columns(2)
        with col1:
            plot_stats = df.groupby("plot")["availability"].mean().reset_index()
            fig = px.bar(
                plot_stats,
                x="plot",
                y="availability",
                title="Average Availability by Plot",
                color="availability",
                color_continuous_scale="RdYlGn",
                range_color=[0, 100]
            )
            fig.update_layout(height=400)
            st.plotly_chart(fig, use_container_width=True)
        
        with col2:
            failure_stats = df.groupby("plot")["failure_percentage"].mean().reset_index()
            fig = px.bar(
                failure_stats,
                x="plot",
                y="failure_percentage",
                title="Average Failure Rate by Plot",
                color="failure_percentage",
                color_continuous_scale="RdYlGn_r",
                range_color=[0, 50]
            )
            fig.update_layout(height=400)
            st.plotly_chart(fig, use_container_width=True)
    
    if st.button("Download Filtered Data as Excel"):
        excel_data = create_excel_download({"Filtered_Data": df})
        st.download_button(
            label="Download Excel",
            data=excel_data,
            file_name="filtered_data.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

def show_login_page():
    """Display the login page"""
    st.markdown('<div class="app-title">☀️ PV String Analytics</div>', unsafe_allow_html=True)
    st.markdown('<div class="app-subtitle">SCADA Data Processing & Analysis Platform</div>', unsafe_allow_html=True)
    
    with st.container():
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            with st.container():
                st.markdown('<div class="login-container">', unsafe_allow_html=True)
                st.markdown('<h2>🔐 Login</h2>', unsafe_allow_html=True)
                
                # Check API status
                api_status = api_health_check()
                if api_status:
                    st.success("✅ Connected to API server")
                else:
                    st.warning("⚠️ API server not running. Using local authentication (demo mode)")
                
                with st.form("login_form"):
                    username = st.text_input("Username", placeholder="Enter your username")
                    password = st.text_input("Password", type="password", placeholder="Enter your password")
                    
                    if st.form_submit_button("Login", use_container_width=True):
                        if username and password:
                            user = authenticate_user(username, password)
                            if user:
                                st.session_state.user = user
                                st.success("Login successful!")
                                st.rerun()
                            else:
                                st.error("Invalid username or password")
                        else:
                            st.warning("Please enter both username and password")
                
                st.markdown("</div>", unsafe_allow_html=True)
                
                # Demo credentials
                st.markdown("---")
                st.markdown("#### Demo Credentials")
                st.markdown("**Admin:** admin / admin123")
                st.markdown("**Manager:** manager / manager123")
                st.markdown("**Engineer:** engineer / engineer123")

# ==========================================
# 8. MAIN APP
# ==========================================
def main():
    """Main application entry point"""
    
    # Show login page if not logged in
    if st.session_state.user is None:
        show_login_page()
        return
    
    # User is logged in - show the main app
    user = st.session_state.user
    
    # Sidebar
    with st.sidebar:
        st.markdown('<i class="fas fa-sun"></i> PV String Analytics', unsafe_allow_html=True)
        
        # API Status
        api_status = api_health_check()
        if api_status:
            st.markdown('<span class="status-badge status-badge-success">● API Connected</span>', unsafe_allow_html=True)
        else:
            st.markdown('<span class="status-badge status-badge-danger">● API Disconnected</span>', unsafe_allow_html=True)
            st.warning("FastAPI backend not running. Some features may not work.")
        
        st.markdown("---")
        
        # User info
        st.markdown(f"### Welcome, {user.get('full_name', user['username'])}")
        st.markdown(f"Role: {ROLE_BADGES.get(user['role'], user['role'])}")
        
        # User management
        user_management_ui()
        
        # Logout button
        if st.button("Logout", use_container_width=True):
            st.session_state.user = None
            st.rerun()
        
        # File upload section
        st.markdown("---")
        st.markdown('<i class="fas fa-upload"></i> Upload SCADA File', unsafe_allow_html=True)
        
        uploaded_file = st.file_uploader(
            "Upload Excel File",
            type=["xlsx", "xls"],
            help="Upload a SCADA Excel file for processing"
        )
        
        snapshot_date = st.date_input(
            "Snapshot Date",
            datetime.now().date(),
            help="Date when this data was collected"
        )
        
        if uploaded_file and st.button("Process File", use_container_width=True):
            file_bytes = uploaded_file.read()
            filename = uploaded_file.name
            
            if not api_status:
                st.error("FastAPI backend is not available")
            else:
                with st.spinner("Processing file through FastAPI..."):
                    success, response = api_upload_file(
                        file_bytes,
                        filename,
                        snapshot_date.strftime("%Y-%m-%d")
                    )
                
                if success:
                    st.success("File processed successfully!")
                    summary = response.get("summary", {})
                    st.json(summary)
                    st.rerun()
                else:
                    st.error(f"File processing failed: {response.get('error', 'Unknown error')}")

    # Main content tabs
    if st.session_state.user:
        tab1, tab2, tab3 = st.tabs([
            "<i class='fas fa-chart-pie'></i> Dashboard",
            "<i class='fas fa-plug'></i> PV String Details",
            "<i class='fas fa-clipboard-list'></i> Audit Log"
        ])

        # Tab 1: Dashboard
        with tab1:
            st.markdown('<i class="fas fa-chart-pie"></i> Dashboard', unsafe_allow_html=True)
            
            if api_status:
                success, stats = api_get_statistics()
                if success:
                    col1, col2, col3 = st.columns(3)
                    with col1:
                        st.metric("Total Records", stats.get("total_records", 0))
                    with col2:
                        st.metric("Average Availability", f"{stats.get('average_availability', 0):.1f}%")
                    with col3:
                        st.metric("Average Failure Rate", f"{stats.get('average_failure_percentage', 0):.1f}%")
                    
                    if stats.get("plot_breakdown"):
                        plot_data = pd.DataFrame(stats["plot_breakdown"])
                        fig = px.bar(
                            plot_data,
                            x="plot",
                            y="avg_availability",
                            title="Availability by Plot",
                            color="avg_availability",
                            color_continuous_scale="RdYlGn",
                            range_color=[0, 100]
                        )
                        fig.update_layout(height=400)
                        st.plotly_chart(fig, use_container_width=True)
                else:
                    st.warning("Could not load statistics from API")
            else:
                st.warning("API is disconnected. Please start the FastAPI backend.")

        # Tab 2: PV String Details
        with tab2:
            if api_status:
                success, response = api_get_data(limit=10000)
                if success and response.get("data"):
                    df = pd.DataFrame(response["data"])
                    create_pv_string_tab(df)
                else:
                    st.info("No data available. Upload a file to see details.")
            else:
                st.warning("API is disconnected. Please start the FastAPI backend.")

        # Tab 3: Audit Log
        with tab3:
            audit_log_tab()

# ==========================================
# 9. RUN THE APP
# ==========================================
if __name__ == "__main__":
    main()