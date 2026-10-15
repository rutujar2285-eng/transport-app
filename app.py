import os
import io
import hashlib
import hmac
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text
from reportlab.lib.pagesizes import landscape, A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet

TZ = ZoneInfo("Asia/Kolkata")
PLANTS = ["J-Block", "F-2-Block", "T-Block", "S-Block", "GAURI-T-Block"]

st.set_page_config(
    page_title="Transport Management System",
    page_icon="🚛",
    layout="wide",
)


def get_db_url():
    try:
        url = st.secrets.get("DATABASE_URL", os.getenv("DATABASE_URL", "sqlite:///transport_local.db"))
    except Exception:
        url = os.getenv("DATABASE_URL", "sqlite:///transport_local.db")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


DB_URL = get_db_url()
engine = create_engine(DB_URL, pool_pre_ping=True, future=True)


def init_db():
    id_column = (
        "BIGSERIAL PRIMARY KEY"
        if engine.dialect.name == "postgresql"
        else "INTEGER PRIMARY KEY AUTOINCREMENT"
    )
    statements = [
        f"""CREATE TABLE IF NOT EXISTS transporters (
            id {id_column}, name TEXT NOT NULL UNIQUE,
            phone TEXT DEFAULT '', active INTEGER NOT NULL DEFAULT 1
        )""",
        f"""CREATE TABLE IF NOT EXISTS vendors (
            id {id_column}, name TEXT NOT NULL UNIQUE,
            phone TEXT DEFAULT '', active INTEGER NOT NULL DEFAULT 1
        )""",
        f"""CREATE TABLE IF NOT EXISTS vehicles (
            id {id_column}, vehicle_no TEXT NOT NULL UNIQUE,
            transporter TEXT NOT NULL, vehicle_size TEXT DEFAULT '',
            fixed_rate NUMERIC NOT NULL DEFAULT 0,
            local_trip_rate NUMERIC NOT NULL DEFAULT 0, vendor_trip_rate NUMERIC NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1
        )""",
        f"""CREATE TABLE IF NOT EXISTS trips (
            id {id_column}, trip_date TEXT NOT NULL, plant TEXT NOT NULL,
            transport_type TEXT DEFAULT '', transporter TEXT NOT NULL,
            vehicle_no TEXT NOT NULL, vehicle_size TEXT DEFAULT '',
            vendor TEXT DEFAULT '', purpose TEXT DEFAULT '',
            from_location TEXT DEFAULT '', from_type TEXT DEFAULT '',
            to_location TEXT DEFAULT '', to_type TEXT DEFAULT '',
            challan_no TEXT DEFAULT '', bill_ref TEXT DEFAULT '',
            material TEXT DEFAULT '', remarks TEXT DEFAULT '',
            rate NUMERIC NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'Pending',
            verified_by TEXT DEFAULT '', verified_at TEXT DEFAULT '',
            created_by TEXT NOT NULL, created_at TEXT NOT NULL
        )""",
        f"""CREATE TABLE IF NOT EXISTS audit_log (
            id {id_column}, username TEXT NOT NULL, action TEXT NOT NULL,
            entity TEXT NOT NULL, entity_id TEXT DEFAULT '',
            details TEXT DEFAULT '', created_at TEXT NOT NULL
        )""",
    ]
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))
        # Safely add rate columns to existing databases.
        if engine.dialect.name == "postgresql":
            conn.execute(text("ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS local_trip_rate NUMERIC NOT NULL DEFAULT 0"))
            conn.execute(text("ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS vendor_trip_rate NUMERIC NOT NULL DEFAULT 0"))
        else:
            cols = {row[1] for row in conn.execute(text("PRAGMA table_info(vehicles)")).fetchall()}
            if "local_trip_rate" not in cols:
                conn.execute(text("ALTER TABLE vehicles ADD COLUMN local_trip_rate NUMERIC NOT NULL DEFAULT 0"))
            if "vendor_trip_rate" not in cols:
                conn.execute(text("ALTER TABLE vehicles ADD COLUMN vendor_trip_rate NUMERIC NOT NULL DEFAULT 0"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_trips_date ON trips(trip_date)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_trips_plant ON trips(plant)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_trips_transporter ON trips(transporter)"))


def now_text():
    return datetime.now(TZ).isoformat(timespec="seconds")


def audit(username, action, entity, entity_id="", details=""):
    with engine.begin() as conn:
        conn.execute(
            text("""INSERT INTO audit_log
                (username, action, entity, entity_id, details, created_at)
                VALUES (:u, :a, :e, :i, :d, :t)"""),
            {
                "u": username,
                "a": action,
                "e": entity,
                "i": str(entity_id),
                "d": details,
                "t": now_text(),
            },
        )


def pw_ok(password, expected):
    password = str(password)
    expected = str(expected)
    digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return hmac.compare_digest(digest, expected) or hmac.compare_digest(password, expected)


def get_users():
    try:
        return dict(st.secrets.get("users", {}))
    except Exception:
        return {}


def read_df(sql, params=None):
    with engine.connect() as conn:
        return pd.read_sql(text(sql), conn, params=params or {})


def style_status_columns(df):
    """Render Verified status green and Pending status yellow in visible tables."""
    if df is None or df.empty:
        return df
    status_columns = [
        col for col in df.columns
        if str(col).strip().lower() in ("status", "trip status")
    ]
    if not status_columns:
        return df

    def status_color(value):
        value = str(value).strip().lower()
        if value == "verified":
            return "background-color: #d1fae5; color: #065f46; font-weight: 700;"
        if value == "pending":
            return "background-color: #fef3c7; color: #92400e; font-weight: 700;"
        return ""

    # Build a style DataFrame and use Styler.apply(axis=None), avoiding
    # Styler.applymap which is removed in newer pandas versions.
    styles = pd.DataFrame("", index=df.index, columns=df.columns)
    for col in status_columns:
        styles[col] = df[col].map(status_color)
    return df.style.apply(lambda _: styles, axis=None)


def options(sql, column):
    df = read_df(sql)
    return df[column].dropna().astype(str).tolist() if not df.empty else []


@st.cache_data(ttl=20, show_spinner=False)
def load_trip_entry_masters():
    """Cache entry dropdown master data briefly to reduce database round-trips."""
    with engine.connect() as conn:
        trans_df = pd.read_sql(
            text("SELECT name FROM transporters WHERE active=1 ORDER BY name"), conn
        )
        vehicle_df = pd.read_sql(
            text("SELECT vehicle_no, transporter, vehicle_size, fixed_rate, local_trip_rate, vendor_trip_rate "
                 "FROM vehicles WHERE active=1 ORDER BY vehicle_no"), conn
        )
        vendor_df = pd.read_sql(
            text("SELECT name FROM vendors WHERE active=1 ORDER BY name"), conn
        )
    return (
        trans_df["name"].dropna().astype(str).tolist() if not trans_df.empty else [],
        vehicle_df,
        vendor_df["name"].dropna().astype(str).tolist() if not vendor_df.empty else [],
    )


def insert_trip(values):
    """Insert a trip and return its ID in both PostgreSQL and SQLite."""
    if engine.dialect.name == "postgresql":
        statement = text("""INSERT INTO trips
            (trip_date, plant, transport_type, transporter, vehicle_no, vehicle_size,
             vendor, purpose, from_location, from_type, to_location, to_type,
             challan_no, bill_ref, material, remarks, rate, status, created_by, created_at)
            VALUES (:d, :p, :tt, :tr, :v, :vs, :ve, :pu, :fl, :ft, :tl, :to,
                    :ch, :br, :ma, :re, :ra, 'Pending', :by, :at)
            RETURNING id""")
        with engine.begin() as conn:
            return conn.execute(statement, values).scalar_one()
    statement = text("""INSERT INTO trips
        (trip_date, plant, transport_type, transporter, vehicle_no, vehicle_size,
         vendor, purpose, from_location, from_type, to_location, to_type,
         challan_no, bill_ref, material, remarks, rate, status, created_by, created_at)
        VALUES (:d, :p, :tt, :tr, :v, :vs, :ve, :pu, :fl, :ft, :tl, :to,
                :ch, :br, :ma, :re, :ra, 'Pending', :by, :at)""")
    with engine.begin() as conn:
        result = conn.execute(statement, values)
        return result.lastrowid


init_db()
# Smoke-grey theme for the full application.
st.markdown("""
<style>
:root { color-scheme: light; }
.stApp {
  background: radial-gradient(ellipse at 18% 8%, rgba(255,255,255,.98) 0%, rgba(239,242,246,.96) 28%, transparent 58%),
              linear-gradient(135deg, #cbd1d9 0%, #edf0f4 46%, #d4dae2 100%);
  color: #293847;
}
[data-testid="stHeader"] { background: rgba(232,236,241,.72); }
[data-testid="stSidebar"] { background: linear-gradient(180deg,#e3e7ec 0%,#c5ccd5 100%); border-right: 1px solid #b8c2ce; }
[data-testid="stVerticalBlockBorderWrapper"] { border-color: #c2ccd7 !important; }
[data-testid="stForm"] {
  background: linear-gradient(145deg,rgba(255,255,255,.92),rgba(229,234,240,.82)) !important;
  border: 1px solid rgba(184,197,211,.95) !important; border-radius: 18px !important;
  padding: 1.25rem 1.35rem !important;
  box-shadow: 0 14px 32px rgba(56,70,87,.13), inset 0 1px 0 rgba(255,255,255,.98) !important;
}
.stTextInput input, .stNumberInput input, .stDateInput input, .stSelectbox div[data-baseweb="select"] > div, .stTextArea textarea {
  background: linear-gradient(180deg,#f4f6f9 0%,#e1e6ed 100%) !important;
  border: 1px solid #c4ceda !important; border-radius: 10px !important; color: #293847 !important;
  box-shadow: inset 0 1px 2px rgba(50,65,80,.06), 0 1px 0 rgba(255,255,255,.8) !important;
}
.stTextInput input:focus, .stSelectbox div[data-baseweb="select"] > div:focus-within { border-color:#8d9eaf !important; box-shadow:0 0 0 3px rgba(126,147,169,.18) !important; }
.stButton button, .stFormSubmitButton button {
  background: linear-gradient(180deg,#778797 0%,#465564 52%,#344250 100%) !important;
  color:#fff !important; border:1px solid #667787 !important; border-radius:10px !important;
  box-shadow:0 5px 12px rgba(45,59,73,.22), inset 0 1px 0 rgba(255,255,255,.38) !important;
  font-weight:700 !important; transition:all .18s ease-in-out;
}
.stButton button:hover, .stFormSubmitButton button:hover { background:linear-gradient(180deg,#8797a7,#3b4a59) !important; transform:translateY(-1px); box-shadow:0 7px 15px rgba(45,59,73,.26),inset 0 1px 0 rgba(255,255,255,.42) !important; }
[data-testid="stMetric"] { background:linear-gradient(145deg,rgba(255,255,255,.88),rgba(219,225,232,.88)); border:1px solid #c5ced8; padding:12px; border-radius:12px; box-shadow:0 4px 12px rgba(55,70,85,.08); }
h1,h2,h3 { color:#2e4051 !important; }
hr { border-color:#c5cdd6 !important; }
</style>
""", unsafe_allow_html=True)

# Branded header: logo is embedded so deployment needs only app.py.
_DIVINE_LOGO = "data:image/png;base64,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"
st.markdown(
    f'''<div style="display:flex;align-items:center;gap:22px;padding:14px 20px;margin:0 0 10px 0;
                background:linear-gradient(110deg,#e5e7ea 0%,#cfd3d8 72%,#b9bec5 100%);
                border:1px solid #c1c6cc;border-bottom:2px solid #aeb4bb;border-radius:10px;">
      <img src="{_DIVINE_LOGO}" style="width:96px;height:96px;object-fit:contain;flex-shrink:0;" />
      <div style="border-left:2px solid #707983;padding-left:20px;">
        <div style="font-size:clamp(22px,2.7vw,36px);font-weight:850;letter-spacing:.4px;line-height:1.15;color:#263746;">ELYON DIVINE TRANSPORT MANAGEMENT SYSTEM (TMS)</div>
        <div style="font-size:clamp(14px,1.5vw,19px);font-weight:600;line-height:1.35;color:#526273;margin-top:7px;">Created by चेतन</div>
        <div style="height:3px;max-width:720px;margin-top:10px;background:linear-gradient(90deg,#5b6570,#b8bec5,transparent);"></div>
      </div>
    </div>''',
    unsafe_allow_html=True,
)
st.caption("5 Plants • Trip Register • Verification Control • Reports • Audit Trail")

users = get_users()
if not users:
    st.error(
        "Login is not configured. Add [users] and DATABASE_URL to Streamlit Secrets "
        "using the README instructions, then restart the app."
    )
    st.stop()

if "user" not in st.session_state:
    st.session_state.user = None

if not st.session_state.user:
    with st.form("login"):
        st.subheader("Secure Login")
        username_options = ["Select your username"] + sorted(str(name) for name in users.keys())
        uname = st.selectbox("Username", username_options, index=0, key="login_username")
        pwd = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Login", type="primary")
        if submitted and uname == "Select your username":
            st.warning("Please select your username from the dropdown.")
        elif submitted:
            record = users.get(uname)
            expected = (
                str(record.get("password", ""))
                if isinstance(record, dict)
                else str(record or "")
            )
            if record and pw_ok(pwd, expected):
                if isinstance(record, dict):
                    role = str(record.get("role", "operator")).lower()
                    raw_plants = record.get("plants", PLANTS)
                else:
                    role = "admin" if uname.lower() == "admin" else "operator"
                    raw_plants = PLANTS

                if isinstance(raw_plants, str):
                    raw_plants = [raw_plants]
                valid_plants = [p for p in (raw_plants or []) if p in PLANTS]
                st.session_state.user = {
                    "username": uname,
                    "role": role,
                    "plants": valid_plants,
                }
                st.rerun()
            else:
                st.error("Invalid username or password.")
    st.stop()

user = st.session_state.user
username = user["username"]
role = str(user.get("role", "operator")).lower()
is_admin = role == "admin"
allowed_plants = PLANTS if is_admin else [
    p for p in user.get("plants", []) if p in PLANTS
]

with st.sidebar:
    st.write(f"**User:** {username}")
    st.write(f"**Role:** {'Admin' if is_admin else 'Operator'}")
    if st.button("Logout"):
        st.session_state.user = None
        st.rerun()
    if not DB_URL.startswith(("postgresql://", "postgresql+")):
        st.warning(
            "Local SQLite mode: Streamlit Cloud local files may not be durable. "
            "Configure PostgreSQL/Supabase before using real production data."
        )

tabs = ["Trip Entry", "Trip Register", "Reports", "Verification"]
if is_admin:
    tabs += ["Masters", "Audit Log"]
pages = st.tabs(tabs)

with pages[0]:
    # Clear saved values before recreating widgets after a successful submission.
    if st.session_state.pop("reset_trip_entry_fields", False):
        for field_key in (
            "entry_trip_date", "entry_plant", "entry_transport_type",
            "entry_transporter", "entry_vehicle_no", "entry_vendor",
            "entry_purpose", "entry_challan_no", "entry_material",
            "entry_remarks", "entry_rate", "entry_rate_signature",
        ):
            st.session_state.pop(field_key, None)

    saved_message = st.session_state.pop("trip_saved_message", None)
    if saved_message:
        st.success(saved_message)
    st.subheader("New Trip Entry")
    if not allowed_plants:
        st.warning("No plant access assigned to this account. Ask an admin to assign plant access.")

    trans_list, veh_df, vendor_list = load_trip_entry_masters()
    # These selectors stay outside the form so Streamlit reruns and updates dependent fields immediately.
    top1, top2, top3 = st.columns(3)
    transporter = top1.selectbox("Transporter", [""] + trans_list, key="entry_transporter")
    transport_type = top2.selectbox("Transport Type", ["", "Inward", "Outward", "Inter-plant", "Local", "Other"], key="entry_transport_type")
    if not veh_df.empty and transporter:
        filtered_vehicles = veh_df[veh_df["transporter"].astype(str).str.strip() == str(transporter).strip()]
    else:
        filtered_vehicles = veh_df.iloc[0:0] if not veh_df.empty else pd.DataFrame()
    vehicle_options = filtered_vehicles["vehicle_no"].dropna().astype(str).tolist() if not filtered_vehicles.empty else []
    if st.session_state.get("entry_vehicle_no", "") not in ([""] + vehicle_options):
        st.session_state["entry_vehicle_no"] = ""
    vehicle_no = top3.selectbox("Vehicle No.", [""] + vehicle_options, key="entry_vehicle_no")
    selected_vehicle = filtered_vehicles[filtered_vehicles["vehicle_no"].astype(str) == vehicle_no] if vehicle_no and not filtered_vehicles.empty else pd.DataFrame()
    master_size = str(selected_vehicle.iloc[0]["vehicle_size"] or "") if not selected_vehicle.empty else ""
    if master_size.lower() == "nan":
        master_size = ""

    if not selected_vehicle.empty:
        if transport_type == "Outward":
            rate_column = "vendor_trip_rate"
        elif transport_type in ("Inter-plant", "Local"):
            rate_column = "local_trip_rate"
        else:
            rate_column = "fixed_rate"
        raw_rate = selected_vehicle.iloc[0].get(rate_column, 0)
        suggested_rate = float(raw_rate) if pd.notna(raw_rate) else 0.0
    else:
        suggested_rate = 0.0
    rate_signature = (transporter, vehicle_no, transport_type, suggested_rate)
    if st.session_state.get("entry_rate_signature") != rate_signature:
        st.session_state["entry_rate"] = suggested_rate
        st.session_state["entry_rate_signature"] = rate_signature
    rate = st.number_input(
        "Rate (₹)", min_value=0.0, step=100.0, key="entry_rate",
        disabled=not is_admin,
        help="Rate बदलण्याची परवानगी फक्त Admin ला आहे. Operator साठी Master मधील दर वापरला जाईल."
    )

    with st.form("new_trip_entry_form", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        trip_date = c1.date_input("Trip Date", value=datetime.now(TZ).date(), format="DD-MM-YYYY", key="entry_trip_date")
        plant = c2.selectbox("Plant", allowed_plants if allowed_plants else ["No plant access"], disabled=not allowed_plants, key="entry_plant")
        c3.text_input("Transport Type (selected above)", value=transport_type, disabled=True)

        c1, c2, c3 = st.columns(3)
        c1.text_input("Transporter (selected above)", value=transporter, disabled=True)
        c2.text_input("Vehicle No. (selected above)", value=vehicle_no, disabled=True)
        c3.text_input("Vehicle Size (from master)", value=master_size, disabled=True)

        c1, c2, c3 = st.columns(3)
        vendor = c1.selectbox("Vendor", [""] + vendor_list, key="entry_vendor")
        purpose = c2.selectbox("Purpose", ["", "Dispatch", "Local Trip", "Vendor"], key="entry_purpose")
        challan_no = c3.text_input("Challan No.", key="entry_challan_no")
        material = st.text_input("Material", key="entry_material")
        remarks = st.text_area("Narration (required)", key="entry_remarks", placeholder="Enter trip narration before saving")
        submitted = st.form_submit_button("Save Trip", type="primary", disabled=not allowed_plants, use_container_width=True)

    if submitted:
        if not allowed_plants:
            st.error("No plant access assigned to this account.")
        elif not transporter or not vehicle_no:
            st.error("कृपया Transporter आणि Vehicle No. निवडा.")
        elif selected_vehicle.empty or str(selected_vehicle.iloc[0]["transporter"]) != transporter:
            st.error("निवडलेला Vehicle या Transporter चा नाही. कृपया योग्य Vehicle निवडा.")
        elif not remarks.strip():
            st.error("Trip save करण्यापूर्वी Narration लिहिणे आवश्यक आहे.")
        else:
            try:
                trip_values = {
                    "d": trip_date.isoformat(), "p": plant, "tt": transport_type,
                    "tr": transporter, "v": vehicle_no, "vs": master_size,
                    "ve": vendor, "pu": purpose, "fl": "", "ft": "",
                    "tl": "", "to": "", "ch": challan_no, "br": "",
                    "ma": material, "re": remarks, "ra": rate,
                    "by": username, "at": now_text(),
                }
                trip_id = insert_trip(trip_values)
                audit(username, "CREATE", "trip", trip_id,
                      f"plant={plant}; transporter={transporter}; vehicle={vehicle_no}")
                # clear_on_submit clears all form widgets; rerun also refreshes the UI.
                st.session_state["trip_saved_message"] = f"Trip saved successfully. Trip ID: {trip_id}. पुढील एन्ट्रीसाठी फॉर्म रिकामा केला आहे."
                st.rerun()
            except Exception as exc:
                st.error(f"Trip save failed: {exc}")

with pages[1]:
    if st.session_state.get("trip_delete_notice"):
        st.success(st.session_state.pop("trip_delete_notice"))
    st.subheader("Trip Register")
    f1, f2, f3 = st.columns(3)
    d1 = f1.date_input("From Date", value=date.today().replace(day=1), key="reg_from", format="DD-MM-YYYY")
    d2 = f2.date_input("To Date", value=datetime.now(TZ).date(), key="reg_to", format="DD-MM-YYYY")
    plant_filter = f3.selectbox("Plant Filter", ["All"] + allowed_plants, key="reg_plant")
    if d1 > d2:
        st.warning("From Date ही To Date पेक्षा आधीची असावी.")
        df = pd.DataFrame()
    else:
        df = read_df(
            "SELECT * FROM trips WHERE trip_date BETWEEN :d1 AND :d2 ORDER BY trip_date DESC, id DESC",
            {"d1": d1.isoformat(), "d2": d2.isoformat()},
        )
        if not is_admin:
            df = df[df["plant"].isin(allowed_plants)]
        if plant_filter != "All":
            df = df[df["plant"] == plant_filter]

    # Flag likely duplicates for visibility; do not silently delete existing records.
    if not df.empty:
        duplicate_key = ["trip_date", "plant", "transporter", "vehicle_no", "challan_no"]
        df["Possible Duplicate"] = df.duplicated(subset=duplicate_key, keep=False) & df["challan_no"].fillna("").astype(str).str.strip().ne("")
        if df["Possible Duplicate"].any():
            st.warning("Possible duplicate trips दिसत आहेत. खालील नोंदी तपासा; योग्य नोंद निवडून Delete करा.")
    st.dataframe(style_status_columns(df), use_container_width=True, hide_index=True)
    st.download_button(
        "Download Register CSV", df.to_csv(index=False).encode("utf-8-sig"),
        "trip_register.csv", "text/csv"
    )

    # Bulk deletion is deliberately restricted to admins and requires typed confirmation.
    if is_admin:
        with st.expander("⚠️ Admin only — सर्व Trip Entries delete करा"):
            total_trip_count = int(read_df("SELECT COUNT(*) AS n FROM trips").iloc[0]["n"])
            st.warning(
                f"या कृतीने सर्व Plants मधील {total_trip_count} Trip Entries कायमच्या delete होतील. "
                "ही कृती Undo करता येणार नाही. आधी Database Backup घ्या."
            )
            bulk_delete_phrase = st.text_input(
                'पुष्टीसाठी खाली DELETE ALL TRIPS टाइप करा', key="bulk_delete_trip_phrase"
            )
            if st.button(
                "Delete ALL Trip Entries", key="bulk_delete_all_trips",
                type="primary", disabled=(bulk_delete_phrase != "DELETE ALL TRIPS" or total_trip_count == 0)
            ):
                try:
                    with engine.begin() as conn:
                        conn.execute(text("DELETE FROM trips"))
                    audit(username, "DELETE_ALL", "trip", "ALL", f"Bulk deleted {total_trip_count} trips from Trip Register")
                    st.session_state.pop("edit_trip_id", None)
                    st.session_state.pop("bulk_delete_trip_phrase", None)
                    # Clear cached Streamlit data so reports/register refresh from the database.
                    st.cache_data.clear()
                    st.session_state["trip_delete_notice"] = f"सर्व {total_trip_count} Trip Entries delete केल्या. Reports मधील आकडे refresh झाले आहेत."
                    st.rerun()
                except Exception as exc:
                    st.error(f"Bulk delete failed: {exc}")
    else:
        st.caption("सर्व Trip Entries delete करण्याचा पर्याय फक्त Admin ला उपलब्ध आहे.")

    if not df.empty:
        trip_ids = df["id"].astype(int).tolist()
        chosen_id = st.selectbox("Edit / Delete साठी Trip ID निवडा", trip_ids, key="manage_trip_id")
        selected_trip = df[df["id"] == chosen_id].iloc[0]
        can_manage = is_admin or (
            str(selected_trip["created_by"]) == username
            and str(selected_trip["status"]) != "Verified"
            and str(selected_trip["plant"]) in allowed_plants
        )
        if not can_manage:
            st.info("Edit/Delete साठी Admin ला संपर्क करा. Verified trip operator बदलू/डिलीट करू शकत नाही.")
        else:
            act1, act2 = st.columns(2)
            with act1:
                if st.button("Edit Selected Trip", key="start_edit_trip"):
                    st.session_state["edit_trip_id"] = int(chosen_id)
            with act2:
                delete_confirm = st.checkbox("मी ही Trip कायमची delete करायची खात्री करतो", key="delete_trip_confirm")
                if st.button("Delete Selected Trip", disabled=not delete_confirm, key="delete_trip_button"):
                    try:
                        with engine.begin() as conn:
                            conn.execute(text("DELETE FROM trips WHERE id=:id"), {"id": int(chosen_id)})
                        audit(username, "DELETE", "trip", chosen_id, "Deleted from Trip Register")
                        st.session_state.pop("edit_trip_id", None)
                        # Invalidate cached data after delete; all reports query the trips table.
                        st.cache_data.clear()
                        st.session_state["trip_delete_notice"] = f"Trip ID {chosen_id} delete झाली. Reports मधूनही ती वगळली आहे."
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Trip delete failed: {exc}")

        if st.session_state.get("edit_trip_id") == int(chosen_id):
            # Editing is limited to non-verified trips unless the user is admin.
            if str(selected_trip["status"]) == "Verified" and not is_admin:
                st.error("Verified trip फक्त Admin edit करू शकतो.")
            else:
                st.markdown("#### Edit Trip")
                with st.form("edit_trip_form"):
                    ec1, ec2, ec3 = st.columns(3)
                    e_date = ec1.date_input(
                        "Trip Date", value=pd.to_datetime(selected_trip["trip_date"]).date(),
                        format="DD-MM-YYYY", key="edit_date"
                    )
                    e_plant = ec2.selectbox(
                        "Plant", PLANTS, index=PLANTS.index(selected_trip["plant"]) if selected_trip["plant"] in PLANTS else 0,
                        key="edit_plant"
                    )
                    e_transport_type = ec3.selectbox(
                        "Transport Type", ["", "Inward", "Outward", "Inter-plant", "Local", "Other"],
                        index=(["", "Inward", "Outward", "Inter-plant", "Local", "Other"].index(selected_trip["transport_type"])
                               if selected_trip["transport_type"] in ["", "Inward", "Outward", "Inter-plant", "Local", "Other"] else 0),
                        key="edit_transport_type"
                    )
                    e_transporter = ec1.text_input("Transporter", value=str(selected_trip["transporter"]), key="edit_transporter")
                    e_vehicle = ec2.text_input("Vehicle No.", value=str(selected_trip["vehicle_no"]), key="edit_vehicle")
                    e_size = ec3.text_input("Vehicle Size", value=str(selected_trip["vehicle_size"] or ""), key="edit_size")
                    e_vendor = ec1.text_input("Vendor", value=str(selected_trip["vendor"] or ""), key="edit_vendor")
                    e_purpose = ec2.selectbox(
                        "Purpose", ["", "Dispatch", "Local Trip", "Vendor"],
                        index=(["", "Dispatch", "Local Trip", "Vendor"].index(selected_trip["purpose"])
                               if selected_trip["purpose"] in ["", "Dispatch", "Local Trip", "Vendor"] else 0),
                        key="edit_purpose"
                    )
                    e_challan = ec3.text_input("Challan No.", value=str(selected_trip["challan_no"] or ""), key="edit_challan")
                    # These fields are hidden in the UI; preserve existing values when editing.
                    e_from = str(selected_trip["from_location"] or "")
                    e_from_type = str(selected_trip["from_type"] or "")
                    e_to = str(selected_trip["to_location"] or "")
                    e_to_type = str(selected_trip["to_type"] or "")
                    e_bill = str(selected_trip["bill_ref"] or "")
                    e_material = ec3.text_input("Material", value=str(selected_trip["material"] or ""), key="edit_material")
                    e_narration = st.text_area("Narration (required)", value=str(selected_trip["remarks"] or ""), key="edit_narration")
                    e_rate = st.number_input("Rate (₹)", min_value=0.0, value=float(selected_trip["rate"] or 0), step=100.0, key="edit_rate")
                    save_edit = st.form_submit_button("Save Changes", type="primary")
                    cancel_edit = st.form_submit_button("Cancel")
                    if cancel_edit:
                        st.session_state.pop("edit_trip_id", None)
                        st.rerun()
                    if save_edit:
                        if not e_narration.strip():
                            st.error("Trip update करण्यापूर्वी Narration लिहिणे आवश्यक आहे.")
                        elif not is_admin and e_plant not in allowed_plants:
                            st.error("तुम्हाला या Plant मध्ये edit करण्याची परवानगी नाही.")
                        else:
                            try:
                                with engine.begin() as conn:
                                    conn.execute(text("""UPDATE trips SET
                                        trip_date=:d, plant=:p, transport_type=:tt, transporter=:tr,
                                        vehicle_no=:v, vehicle_size=:vs, vendor=:ve, purpose=:pu,
                                        challan_no=:ch, from_location=:fl, from_type=:ft,
                                        to_location=:tl, to_type=:to, bill_ref=:br, material=:ma,
                                        remarks=:re, rate=:ra
                                        WHERE id=:id"""), {
                                            "d": e_date.isoformat(), "p": e_plant, "tt": e_transport_type,
                                            "tr": e_transporter.strip(), "v": e_vehicle.strip().upper(),
                                            "vs": e_size, "ve": e_vendor, "pu": e_purpose, "ch": e_challan,
                                            "fl": e_from, "ft": e_from_type, "tl": e_to, "to": e_to_type,
                                            "br": e_bill.strip(), "ma": e_material, "re": e_narration.strip(),
                                            "ra": e_rate, "id": int(chosen_id)
                                        })
                                audit(username, "UPDATE", "trip", chosen_id, "Edited from Trip Register")
                                st.session_state.pop("edit_trip_id", None)
                                st.success("Trip updated.")
                                st.rerun()
                            except Exception as exc:
                                st.error(f"Trip update failed: {exc}")

with pages[2]:
    if st.session_state.get("trip_delete_notice"):
        st.success(st.session_state.pop("trip_delete_notice"))
    st.subheader("Transport Reports")
    st.caption("निवडलेल्या कालावधीतील ट्रिपचा सेव्ह केलेला रेट/Amount वापरला जातो. Vehicle Master मधील नवीन रेट जुन्या ट्रिपवर लागू होत नाही.")
    r1, r2 = st.columns(2)
    rd1 = r1.date_input("Report From", value=date.today().replace(day=1), key="rep_from", format="DD-MM-YYYY")
    rd2 = r2.date_input("Report To", value=datetime.now(TZ).date(), key="rep_to", format="DD-MM-YYYY")
    if rd1 > rd2:
        st.warning("Report From ही Report To पेक्षा आधीची तारीख असावी.")
        rdf = pd.DataFrame()
    else:
        rdf = read_df(
            "SELECT * FROM trips WHERE trip_date BETWEEN :a AND :b ORDER BY trip_date, id",
            {"a": rd1.isoformat(), "b": rd2.isoformat()},
        )
        if not is_admin:
            rdf = rdf[rdf["plant"].isin(allowed_plants)]

    if not rdf.empty:
        # Reports must use the rate saved on each trip, never the current vehicle master rate.
        rdf = rdf.copy()
        rdf["rate"] = pd.to_numeric(rdf["rate"], errors="coerce").fillna(0)
        total_amount = float(rdf["rate"].sum())
        zero_rate_count = int((rdf["rate"] == 0).sum())
        st.caption(f"कालावधी: {rd1.strftime('%d-%m-%Y')} ते {rd2.strftime('%d-%m-%Y')}")
        m1, m2, m3 = st.columns(3)
        m1.metric("Total Trips", f"{len(rdf):,}")
        m2.metric("Total Amount", f"₹{total_amount:,.2f}")
        m3.metric("Zero-rate trips", f"{zero_rate_count:,}")
        if zero_rate_count:
            st.warning(f"{zero_rate_count} ट्रिपचा सेव्ह केलेला Rate ₹0 आहे. Master मधील रेट बदलल्यामुळे जुन्या ट्रिपचा Amount आपोआप भरला जाणार नाही. Trip Register मधून संबंधित ट्रिप Edit करून योग्य Rate सेव्ह करा; आधीचा बॅकअप उपलब्ध असल्यास त्यातून जुना Rate पडताळा.")

        summaries = [
            ("Date-wise", "trip_date"),
            ("Transporter-wise", "transporter"),
            ("Vendor-wise", "vendor"),
            ("Plant-wise", "plant"),
            ("Status-wise", "status"),
            ("Transport Type-wise", "transport_type"),
        ]
        st.markdown("### Summary")
        summary_tabs = st.tabs([x[0] for x in summaries])
        for tab, (label, column) in zip(summary_tabs, summaries):
            with tab:
                summary = rdf.groupby(column, dropna=False, as_index=False).agg(
                    Trips=("id", "count"), Amount=("rate", "sum")
                )
                summary["Amount"] = summary["Amount"].map(lambda x: f"₹{x:,.2f}")
                st.dataframe(summary, use_container_width=True, hide_index=True)

        with st.expander("View trip-level details", expanded=False):
            display_cols = [c for c in ["id", "trip_date", "plant", "transport_type", "transporter", "vehicle_no", "vendor", "challan_no", "rate", "status"] if c in rdf.columns]
            detail = rdf[display_cols].copy()
            detail["trip_date"] = pd.to_datetime(detail["trip_date"], errors="coerce").dt.strftime("%d-%m-%Y")
            detail = detail.rename(columns={"id":"Trip ID", "trip_date":"Date", "plant":"Plant", "transport_type":"Transport Type", "transporter":"Transporter", "vehicle_no":"Vehicle No.", "vendor":"Vendor", "challan_no":"Challan No.", "rate":"Saved Amount (₹)", "status":"Status"})
            st.dataframe(style_status_columns(detail), use_container_width=True, hide_index=True)

        xbuf = io.BytesIO()
        with pd.ExcelWriter(xbuf, engine="openpyxl") as writer:
            rdf.to_excel(writer, index=False, sheet_name="Trips")
            for label, column in summaries:
                rdf.groupby(column, dropna=False, as_index=False).agg(
                    Trips=("id", "count"), Amount=("rate", "sum")
                ).to_excel(writer, index=False, sheet_name=label[:31])
        st.download_button(
            "⬇️ Download Excel Report", xbuf.getvalue(), "transport_report.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        pbuf = io.BytesIO()
        doc = SimpleDocTemplate(
            pbuf, pagesize=landscape(A4), rightMargin=20, leftMargin=20,
            topMargin=20, bottomMargin=20,
        )
        styles = getSampleStyleSheet()
        pdf_cols = [c for c in ["id", "trip_date", "plant", "transport_type", "transporter", "vehicle_no", "vendor", "challan_no", "rate", "status"] if c in rdf.columns]
        pdf_df = rdf[pdf_cols].copy().fillna("").astype(str)
        pdf_data = [list(pdf_df.columns)] + pdf_df.values.tolist()
        report_table = Table(pdf_data, repeatRows=1)
        report_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#12304a")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        doc.build([
            Paragraph("Transport Management Report", styles["Title"]),
            Paragraph(f"{rd1.strftime('%d-%m-%Y')} to {rd2.strftime('%d-%m-%Y')} | Trips: {len(rdf)} | Saved Amount Total: ₹{total_amount:,.2f}", styles["Normal"]),
            Spacer(1, 12), report_table,
        ])
        st.download_button("⬇️ Download PDF Report", pbuf.getvalue(), "transport_report.pdf", "application/pdf")
    else:
        st.info("या कालावधीमध्ये रिपोर्टसाठी नोंदी नाहीत.")

with pages[3]:
    st.subheader("Trip Verification")
    st.caption(
        "वेळ वाचवण्यासाठी अनेक ट्रिप एकाच वेळी निवडा आणि Verify Selected Trips दाबा. "
        "डेटाबेसमध्ये एकाच बॅचमध्ये पडताळणी होईल."
    )
    vdf = read_df("""SELECT id, trip_date, plant, transporter, vehicle_no, vendor,
        bill_ref, challan_no, rate, status, created_by FROM trips
        WHERE status != 'Verified' ORDER BY trip_date, id""")
    if not is_admin:
        vdf = vdf[vdf["plant"].isin(allowed_plants)]

    if not vdf.empty:
        checklist = vdf[[
            "id", "trip_date", "plant", "transporter", "vehicle_no",
            "vendor", "bill_ref", "challan_no", "rate", "status"
        ]].copy()
        checklist.insert(0, "Select", False)
        checklist = checklist.rename(columns={
            "id": "Trip ID", "trip_date": "Trip Date", "plant": "Plant",
            "transporter": "Transporter", "vehicle_no": "Vehicle No.",
            "vendor": "Vendor", "bill_ref": "Bill Ref",
            "challan_no": "Challan No.", "rate": "Amount (₹)", "status": "Status"
        })
        st.caption(f"Pending Trips: {len(checklist)}")
        edited_checklist = st.data_editor(
            checklist,
            use_container_width=True,
            hide_index=True,
            key="trip_verification_checklist",
            # Bill Ref is intentionally editable so the transporter invoice number
            # can be entered directly in the verification table.
            disabled=[c for c in checklist.columns if c not in ("Select", "Bill Ref")],
            column_config={
                "Bill Ref": st.column_config.TextColumn(
                    "Bill Ref (Transporter Bill No.)",
                    help="ट्रान्सपोर्टरच्या बिलावर असलेला बिल नंबर येथे लिहा.",
                    required=False,
                ),
                "Select": st.column_config.CheckboxColumn(
                    "✓ Select", help="Verify करायच्या प्रत्येक ट्रिपसमोर टिक करा.", default=False
                ),
                "Trip ID": st.column_config.NumberColumn("Trip ID", format="%d"),
                "Amount (₹)": st.column_config.NumberColumn("Amount (₹)", format="₹%.2f"),
                "Status": st.column_config.TextColumn("Status"),
            },
        )
        # Save edited transporter bill numbers without forcing the user to verify
        # the trip immediately.
        if st.button("💾 Save Bill Ref Changes", key="save_bill_ref_changes"):
            saved_count = 0
            try:
                with engine.begin() as conn:
                    for _, edited_row in edited_checklist.iterrows():
                        trip_id = int(edited_row["Trip ID"])
                        new_bill = "" if pd.isna(edited_row["Bill Ref"]) else str(edited_row["Bill Ref"]).strip()
                        old_rows = vdf[vdf["id"].astype(int) == trip_id]
                        old_bill = "" if old_rows.empty or pd.isna(old_rows.iloc[0]["bill_ref"]) else str(old_rows.iloc[0]["bill_ref"]).strip()
                        if new_bill != old_bill:
                            conn.execute(
                                text("UPDATE trips SET bill_ref=:bill WHERE id=:id AND status != 'Verified'"),
                                {"bill": new_bill, "id": trip_id},
                            )
                            audit(username, "UPDATE", "trip", trip_id, f"bill_ref={new_bill}")
                            saved_count += 1
                st.cache_data.clear()
                st.success(f"{saved_count} ट्रिपचे Bill Ref सेव्ह केले.")
                st.rerun()
            except Exception as exc:
                st.error(f"Bill Ref सेव्ह करता आले नाही: {exc}")

        action1, action2 = st.columns([1, 2])
        with action1:
            select_all = st.checkbox("सर्व Pending निवडा", key="verify_select_all")
        if select_all:
            edited_checklist["Select"] = True
        selected_ids = edited_checklist.loc[edited_checklist["Select"], "Trip ID"].astype(int).tolist()
        with action2:
            st.write(f"**निवडलेल्या ट्रिप: {len(selected_ids)}**")

        confirm_bulk = st.checkbox(
            "मी निवडलेल्या ट्रिप आणि supporting documents तपासले आहेत.",
            key="verify_bulk_confirm",
        )
        if st.button(
            "✓ Verify Selected Trips", type="primary",
            disabled=(not selected_ids or not confirm_bulk), key="verify_selected_trips",
        ):
            verified_ids, duplicate_messages, already_verified = [], [], []
            timestamp = now_text()
            try:
                with engine.begin() as conn:
                    for selected_id in selected_ids:
                        selected_row = vdf[vdf["id"].astype(int) == selected_id]
                        if selected_row.empty:
                            continue
                        row = selected_row.iloc[0]
                        edited_rows = edited_checklist[edited_checklist["Trip ID"].astype(int) == selected_id]
                        edited_bill = "" if edited_rows.empty or pd.isna(edited_rows.iloc[0]["Bill Ref"]) else str(edited_rows.iloc[0]["Bill Ref"]).strip()
                        bill = edited_bill
                        # Persist the entered bill number as part of this same verification transaction.
                        conn.execute(
                            text("UPDATE trips SET bill_ref=:bill WHERE id=:id AND status != 'Verified'"),
                            {"bill": bill, "id": selected_id},
                        )
                        current = conn.execute(
                            text("SELECT status FROM trips WHERE id=:id"), {"id": selected_id}
                        ).first()
                        if not current or current[0] == "Verified":
                            already_verified.append(selected_id)
                            continue
                        duplicate_id = None
                        if bill:
                            duplicate = conn.execute(text("""SELECT id FROM trips
                                WHERE transporter=:tr AND bill_ref=:b AND id != :id LIMIT 1"""),
                                {"tr": row["transporter"], "b": bill, "id": selected_id},
                            ).first()
                            duplicate_id = duplicate[0] if duplicate else None
                        if duplicate_id is not None:
                            duplicate_messages.append(
                                f"Trip ID {selected_id}: duplicate bill reference (Trip ID {duplicate_id})"
                            )
                            continue
                        result = conn.execute(text("""UPDATE trips SET status='Verified',
                            verified_by=:u, verified_at=:t
                            WHERE id=:id AND status != 'Verified'"""),
                            {"u": username, "t": timestamp, "id": selected_id},
                        )
                        if result.rowcount:
                            verified_ids.append(selected_id)
                for selected_id in verified_ids:
                    edited_rows = edited_checklist[edited_checklist["Trip ID"].astype(int) == selected_id]
                    bill_value = "" if edited_rows.empty or pd.isna(edited_rows.iloc[0]["Bill Ref"]) else str(edited_rows.iloc[0]["Bill Ref"]).strip()
                    audit(username, "VERIFY", "trip", selected_id, f"bill_ref={bill_value}")
                st.cache_data.clear()
                if verified_ids:
                    st.success(f"यशस्वी! {len(verified_ids)} ट्रिप Verified केल्या. IDs: {', '.join(map(str, verified_ids))}")
                if duplicate_messages:
                    st.warning("खालील ट्रिप duplicate bill मुळे Verify केल्या नाहीत: " + " | ".join(duplicate_messages))
                if already_verified:
                    st.info("आधीच Verify झालेल्या Trip IDs: " + ", ".join(map(str, already_verified)))
                st.rerun()
            except Exception as exc:
                st.error(f"Bulk verification failed: {exc}")
    else:
        st.success("No pending trips to verify.")

    st.markdown("### Verification Status")
    status_df = read_df("""SELECT id AS "Trip ID", trip_date AS "Trip Date", plant AS Plant,
        transporter AS Transporter, vehicle_no AS "Vehicle No.", rate AS "Amount (₹)",
        status AS Status, verified_by AS "Verified By", verified_at AS "Verified At"
        FROM trips ORDER BY trip_date DESC, id DESC LIMIT 500""")
    if not is_admin and not status_df.empty:
        status_df = status_df[status_df["Plant"].isin(allowed_plants)]
    st.dataframe(style_status_columns(status_df), use_container_width=True, hide_index=True)

if is_admin:
    with pages[4]:
        st.subheader("Master Data (Admin only)")
        mt, mv, mveh = st.tabs(["Transporters", "Vendors", "Vehicles / Rates"])

        with mt:
            with st.form("new_transporter"):
                n = st.text_input("Transporter Name")
                ph = st.text_input("Phone")
                ok = st.form_submit_button("Add Transporter")
                if ok:
                    if not n.strip():
                        st.error("Transporter name is required.")
                    else:
                        try:
                            with engine.begin() as conn:
                                conn.execute(
                                    text("INSERT INTO transporters(name, phone) VALUES(:n, :p)"),
                                    {"n": n.strip(), "p": ph.strip()},
                                )
                            audit(username, "CREATE", "transporter", n.strip())
                            st.success("Transporter added.")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Could not add transporter: {exc}")
            st.dataframe(read_df("SELECT * FROM transporters ORDER BY name"), use_container_width=True, hide_index=True)
            de = st.selectbox(
                "Deactivate transporter",
                [""] + options("SELECT name FROM transporters WHERE active=1 ORDER BY name", "name"),
                key="de_trans",
            )
            if st.button("Deactivate selected transporter") and de:
                with engine.begin() as conn:
                    conn.execute(text("UPDATE transporters SET active=0 WHERE name=:n"), {"n": de})
                audit(username, "DEACTIVATE", "transporter", de)
                st.rerun()

        with mv:
            with st.form("new_vendor"):
                n = st.text_input("Vendor Name")
                ph = st.text_input("Vendor Phone")
                ok = st.form_submit_button("Add Vendor")
                if ok:
                    if not n.strip():
                        st.error("Vendor name is required.")
                    else:
                        try:
                            with engine.begin() as conn:
                                conn.execute(
                                    text("INSERT INTO vendors(name, phone) VALUES(:n, :p)"),
                                    {"n": n.strip(), "p": ph.strip()},
                                )
                            audit(username, "CREATE", "vendor", n.strip())
                            st.success("Vendor added.")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Could not add vendor: {exc}")
            st.dataframe(read_df("SELECT * FROM vendors ORDER BY name"), use_container_width=True, hide_index=True)
            de = st.selectbox(
                "Deactivate vendor",
                [""] + options("SELECT name FROM vendors WHERE active=1 ORDER BY name", "name"),
                key="de_vendor",
            )
            if st.button("Deactivate selected vendor") and de:
                with engine.begin() as conn:
                    conn.execute(text("UPDATE vendors SET active=0 WHERE name=:n"), {"n": de})
                audit(username, "DEACTIVATE", "vendor", de)
                st.rerun()

        with mveh:
            trans = options("SELECT name FROM transporters WHERE active=1 ORDER BY name", "name")
            with st.form("new_vehicle"):
                v = st.text_input("Vehicle Number")
                tr = st.selectbox("Transporter", [""] + trans)
                sz = st.text_input("Vehicle Size")
                local_rt = st.number_input("Local + Inter-plant Rate (₹)", min_value=0.0, step=100.0, help="Transport Type = Local किंवा Inter-plant साठी समान दर.")
                vendor_rt = st.number_input("Outward Rate (₹)", min_value=0.0, step=100.0, help="Transport Type = Outward साठी स्वतंत्र दर.")
                rt = st.number_input("Other Rate (₹)", min_value=0.0, step=100.0, help="Transport Type = Other साठी स्वतंत्र दर. Inward साठीही हा fallback दर वापरला जाईल.")
                ok = st.form_submit_button("Add / Update Vehicle")
                if ok:
                    if not v.strip() or not tr:
                        st.error("Vehicle number and transporter are required.")
                    else:
                        vehicle_key = v.strip().upper()
                        try:
                            with engine.begin() as conn:
                                exists = conn.execute(
                                    text("SELECT id FROM vehicles WHERE vehicle_no=:v"),
                                    {"v": vehicle_key},
                                ).first()
                                if exists:
                                    conn.execute(text("""UPDATE vehicles SET transporter=:t,
                                        vehicle_size=:s, fixed_rate=:r, local_trip_rate=:lr,
                                        vendor_trip_rate=:vr, active=1 WHERE vehicle_no=:v"""),
                                        {"t": tr, "s": sz.strip(), "r": rt, "lr": local_rt, "vr": vendor_rt, "v": vehicle_key},
                                    )
                                else:
                                    conn.execute(text("""INSERT INTO vehicles
                                        (vehicle_no, transporter, vehicle_size, fixed_rate, local_trip_rate, vendor_trip_rate)
                                        VALUES (:v, :t, :s, :r, :lr, :vr)"""),
                                        {"v": vehicle_key, "t": tr, "s": sz.strip(), "r": rt, "lr": local_rt, "vr": vendor_rt},
                                    )
                            audit(username, "UPSERT", "vehicle", vehicle_key, f"transporter={tr};local_interplant_rate={local_rt};outward_rate={vendor_rt};other_rate={rt}")
                            st.success("Vehicle/rate saved.")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Could not save vehicle: {exc}")
            vehicle_master_df = read_df("SELECT id, vehicle_no, transporter, vehicle_size, fixed_rate, active, local_trip_rate, vendor_trip_rate FROM vehicles ORDER BY vehicle_no")
            if not vehicle_master_df.empty:
                # fixed_rate is retained internally for compatibility with existing databases,
                # but shown to users as the Other Rate (not as a separate Fixed Rate).
                vehicle_master_df = vehicle_master_df.rename(columns={"fixed_rate": "other_rate"})
                vehicle_master_df = vehicle_master_df[["id", "vehicle_no", "transporter", "vehicle_size", "local_trip_rate", "vendor_trip_rate", "other_rate", "active"]]
                vehicle_master_df = vehicle_master_df.rename(columns={
                    "local_trip_rate": "Local + Inter-plant Rate (₹)",
                    "vendor_trip_rate": "Outward Rate (₹)",
                    "other_rate": "Other Rate (₹)",
                })
            st.dataframe(vehicle_master_df, use_container_width=True, hide_index=True)

    with pages[5]:
        st.subheader("Audit Log")
        adf = read_df("SELECT * FROM audit_log ORDER BY id DESC LIMIT 1000")
        st.dataframe(adf, use_container_width=True, hide_index=True)
        st.download_button(
            "Download Audit CSV",
            adf.to_csv(index=False).encode("utf-8-sig"),
            "audit_log.csv",
            "text/csv",
        )

st.caption(
    "Dates are stored as ISO dates and displayed by Streamlit date controls in DD-MM-YYYY format. "
    "Configure managed PostgreSQL for persistent online storage and scheduled backups."
)
