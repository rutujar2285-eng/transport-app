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
st.title("🚛 Multi-Plant Transport Management System")
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
        uname = st.text_input("Username").strip()
        pwd = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Login", type="primary")
        if submitted:
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
    rate = st.number_input("Rate (₹)", min_value=0.0, step=100.0, key="entry_rate")

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
    st.dataframe(df, use_container_width=True, hide_index=True)
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
            st.dataframe(detail, use_container_width=True, hide_index=True)

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
    vdf = read_df("""SELECT id, trip_date, plant, transporter, vehicle_no, vendor,
        bill_ref, challan_no, rate, status, created_by FROM trips
        WHERE status != 'Verified' ORDER BY trip_date, id""")
    if not is_admin:
        vdf = vdf[vdf["plant"].isin(allowed_plants)]
    st.dataframe(vdf, use_container_width=True, hide_index=True)

    if not vdf.empty:
        trip_id = st.selectbox("Select Trip ID to verify", vdf["id"].tolist())
        selected = vdf[vdf["id"] == trip_id].iloc[0]
        st.write(f"**{selected.transporter}** • {selected.vehicle_no} • ₹{float(selected.rate):,.2f}")
        confirm = st.checkbox("I checked this trip and supporting documents.")
        if st.button("Verify Trip", type="primary", disabled=not confirm):
            bill = str(selected["bill_ref"] or "").strip()
            try:
                duplicate_id = None
                with engine.begin() as conn:
                    current = conn.execute(
                        text("SELECT status FROM trips WHERE id=:id"),
                        {"id": int(trip_id)},
                    ).first()
                    if not current or current[0] == "Verified":
                        st.error("ALREADY VERIFIED — This trip has already been checked.")
                    else:
                        if bill:
                            duplicate = conn.execute(text("""SELECT id FROM trips
                                WHERE transporter=:tr AND bill_ref=:b AND id != :id LIMIT 1"""),
                                {"tr": selected["transporter"], "b": bill, "id": int(trip_id)},
                            ).first()
                            duplicate_id = duplicate[0] if duplicate else None
                        if duplicate_id is None:
                            conn.execute(text("""UPDATE trips SET status='Verified',
                                verified_by=:u, verified_at=:t
                                WHERE id=:id AND status != 'Verified'"""),
                                {"u": username, "t": now_text(), "id": int(trip_id)},
                            )
                if duplicate_id is not None:
                    st.error(
                        f"Duplicate bill reference detected for this transporter "
                        f"(Trip ID {duplicate_id}). Verification cancelled."
                    )
                elif current and current[0] != "Verified":
                    audit(username, "VERIFY", "trip", trip_id, f"bill_ref={bill}")
                    st.success("Trip verified successfully.")
                    st.rerun()
            except Exception as exc:
                st.error(f"Verification failed: {exc}")
    else:
        st.success("No pending trips to verify.")

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
