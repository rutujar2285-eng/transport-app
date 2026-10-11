import os, io, hashlib, hmac
from datetime import date, datetime
from zoneinfo import ZoneInfo
import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from reportlab.lib.pagesizes import landscape, A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet

TZ = ZoneInfo("Asia/Kolkata")
PLANTS = [
    "J-Block",
    "F-2-Block",
    "T-Block",
    "S-Block",
    "GAURI-T-Block"
]
st.set_page_config(page_title="Transport Management System", page_icon="🚛", layout="wide")

def get_db_url():
    try:
        return st.secrets.get("DATABASE_URL", os.getenv("DATABASE_URL", "sqlite:///transport_local.db"))
    except Exception:
        return os.getenv("DATABASE_URL", "sqlite:///transport_local.db")

DB_URL = get_db_url()
if DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)
engine = create_engine(DB_URL, pool_pre_ping=True, future=True)

def init_db():
    id_column = "BIGSERIAL PRIMARY KEY" if engine.dialect.name == "postgresql" else "INTEGER PRIMARY KEY AUTOINCREMENT"
    statements = [
    f"""CREATE TABLE IF NOT EXISTS transporters (
      id {id_column}, name TEXT NOT NULL UNIQUE, phone TEXT DEFAULT '', active INTEGER NOT NULL DEFAULT 1)""",
    f"""CREATE TABLE IF NOT EXISTS vendors (
      id {id_column}, name TEXT NOT NULL UNIQUE, phone TEXT DEFAULT '', active INTEGER NOT NULL DEFAULT 1)""",
    f"""CREATE TABLE IF NOT EXISTS vehicles (
      id {id_column}, vehicle_no TEXT NOT NULL UNIQUE, transporter TEXT NOT NULL,
      vehicle_size TEXT DEFAULT '', fixed_rate NUMERIC NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1)""",
    f"""CREATE TABLE IF NOT EXISTS trips (
      id {id_column}, trip_date TEXT NOT NULL, plant TEXT NOT NULL, transport_type TEXT DEFAULT '',
      transporter TEXT NOT NULL, vehicle_no TEXT NOT NULL, vehicle_size TEXT DEFAULT '', vendor TEXT DEFAULT '',
      purpose TEXT DEFAULT '', from_location TEXT DEFAULT '', from_type TEXT DEFAULT '',
      to_location TEXT DEFAULT '', to_type TEXT DEFAULT '', challan_no TEXT DEFAULT '',
      bill_ref TEXT DEFAULT '', material TEXT DEFAULT '', remarks TEXT DEFAULT '',
      rate NUMERIC NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'Pending',
      verified_by TEXT DEFAULT '', verified_at TEXT DEFAULT '', created_by TEXT NOT NULL,
      created_at TEXT NOT NULL)""",
    f"""CREATE TABLE IF NOT EXISTS audit_log (
      id {id_column}, username TEXT NOT NULL, action TEXT NOT NULL, entity TEXT NOT NULL,
      entity_id TEXT DEFAULT '', details TEXT DEFAULT '', created_at TEXT NOT NULL)"""
    ]
    with engine.begin() as conn:
        for sql in statements:
            conn.execute(text(sql))
        # Portable indexes. Duplicate bill refs are checked transactionally in application logic.
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_trips_date ON trips(trip_date)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_trips_plant ON trips(plant)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_trips_transporter ON trips(transporter)"))

def now_text():
    return datetime.now(TZ).isoformat(timespec="seconds")

def audit(username, action, entity, entity_id="", details=""):
    with engine.begin() as c:
        c.execute(text("""INSERT INTO audit_log(username,action,entity,entity_id,details,created_at)
        VALUES(:u,:a,:e,:i,:d,:t)"""), {"u":username,"a":action,"e":entity,"i":str(entity_id),"d":details,"t":now_text()})

def pw_ok(password, expected):
    # Store SHA-256 hashes in Streamlit secrets; plain passwords are also accepted for easy initial setup.
    digest = hashlib.sha256(password.encode()).hexdigest()
    return hmac.compare_digest(digest, expected) or hmac.compare_digest(password, expected)

def get_users():
    try:
        users = st.secrets.get("users", {})
        return dict(users)
    except Exception:
        return {}

init_db()
st.title("🚛 Multi-Plant Transport Management System")
st.caption("5 Plants • Trip Register • Verification Control • Reports • Audit Trail")

users = get_users()
if not users:
    st.error("Login is not configured. Add [users] and DATABASE_URL to Streamlit Secrets using the README instructions, then restart the app.")
    st.stop()

if "user" not in st.session_state:
    st.session_state.user = None
if not st.session_state.user:
    with st.form("login"):
        st.subheader("Secure Login")
        uname = st.text_input("Username")
        pwd = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Login", type="primary")
        
        if submitted:
            record = users.get(uname)
            if record and pw_ok(
                pwd,
                str(record.get("password", "")) if isinstance(record, dict) else str(record)
            ):
                if isinstance(record, dict):
                    role = record.get("role", "operator")
                    plants = record.get("plants", PLANTS)
                else:
                    role = "admin" if uname == "admin" else "operator"
                    plants = PLANTS

                st.session_state.user = {
                    "username": uname,
                    "role": role,
                    "plants": plants
                }
                st.rerun()
            else:
                st.error("Invalid username or password.")
        st.stop()
      user = st.session_state.user
username = user["username"]
role = user["role"].lower()
is_admin = role == "admin"
allowed_plants = PLANTS if is_admin else [p for p in user.get("plants", []) if p in PLANTS]
with st.sidebar:
    st.write(f"**User:** {username}")
    st.write(f"**Role:** {'Admin' if is_admin else 'Operator'}")
    if st.button("Logout"):
        del st.session_state.user
        st.rerun()
    if not DB_URL.startswith("postgresql://"):
        st.warning("Local SQLite mode: Streamlit Cloud local files may not be durable. Configure PostgreSQL/Supabase before using real production data.")

def read_df(sql, params=None):
    with engine.connect() as c:
        return pd.read_sql(text(sql), c, params=params or {})

def options(sql, column):
    df = read_df(sql)
    return df[column].dropna().astype(str).tolist() if not df.empty else []

tabs = ["Trip Entry", "Trip Register", "Reports", "Verification"]
if is_admin:
    tabs += ["Masters", "Audit Log"]
pages = st.tabs(tabs)

with pages[0]:
    st.subheader("New Trip Entry")
    trans_list = options("SELECT name FROM transporters WHERE active=1 ORDER BY name", "name")
    veh_df = read_df("SELECT vehicle_no, transporter, vehicle_size, fixed_rate FROM vehicles WHERE active=1 ORDER BY vehicle_no")
    vendor_list = options("SELECT name FROM vendors WHERE active=1 ORDER BY name", "name")
    with st.form("trip_entry", clear_on_submit=True):
        c1,c2,c3 = st.columns(3)
        trip_date = c1.date_input("Trip Date", value=datetime.now(TZ).date(), format="DD-MM-YYYY")
        plant = c2.selectbox("Plant", allowed_plants if allowed_plants else ["No plant access"])
        transport_type = c3.selectbox("Transport Type", ["", "Inward", "Outward", "Inter-plant", "Local", "Other"])
        transporter = c1.selectbox("Transporter", [""] + trans_list)
        matching_vehicles = veh_df[veh_df.transporter == transporter] if transporter and not veh_df.empty else veh_df.iloc[0:0]
        vehicle_options = matching_vehicles.vehicle_no.tolist()
        vehicle_no = c2.selectbox("Vehicle No.", [""] + vehicle_options)
        vehicle_size = c3.text_input("Vehicle Size (if not in master)")
        vendor = c1.selectbox("Vendor", [""] + vendor_list)
        purpose = c2.text_input("Purpose")
        challan_no = c3.text_input("Challan No.")
        from_location = c1.text_input("From Location")
        from_type = c2.selectbox("From Type", ["", "Plant", "Vendor", "Customer", "Warehouse", "Other"])
        to_location = c3.text_input("To Location")
        to_type = c1.selectbox("To Type", ["", "Plant", "Vendor", "Customer", "Warehouse", "Other"])
        bill_ref = c2.text_input("Bill / Invoice Reference (optional)")
        material = c3.text_input("Material")
        remarks = st.text_area("Remarks")
        matched = matching_vehicles[matching_vehicles.vehicle_no == vehicle_no] if vehicle_no else pd.DataFrame()
        suggested_rate = float(matched.iloc[0].fixed_rate) if not matched.empty else 0.0
        rate = st.number_input("Rate (₹)", min_value=0.0, value=suggested_rate, step=100.0)
        save = st.form_submit_button("Save Trip", type="primary")
        if save:
            if not allowed_plants:
                st.error("No plant access assigned to this account.")
            elif not transporter or not vehicle_no:
                st.error("Transporter आणि Vehicle No. निवडा.")
            else:
                try:
                    with engine.begin() as conn:
                        conn.execute(text("""INSERT INTO trips
                        (trip_date,plant,transport_type,transporter,vehicle_no,vehicle_size,vendor,purpose,
                         from_location,from_type,to_location,to_type,challan_no,bill_ref,material,remarks,rate,
                         status,created_by,created_at)
                        VALUES (:d,:p,:tt,:tr,:v,:vs,:ve,:pu,:fl,:ft,:tl,:to,:ch,:br,:ma,:re,:ra,'Pending',:by,:at) RETURNING id"""),
                        {"d":trip_date.isoformat(),"p":plant,"tt":transport_type,"tr":transporter,"v":vehicle_no,
                         "vs":vehicle_size or (str(matched.iloc[0].vehicle_size) if not matched.empty else ""),
                         "ve":vendor,"pu":purpose,"fl":from_location,"ft":from_type,"tl":to_location,"to":to_type,
                         "ch":challan_no,"br":bill_ref.strip(),"ma":material,"re":remarks,"ra":rate,"by":username,"at":now_text()})
                        trip_id = conn.execute(text("SELECT MAX(id) FROM trips")).scalar()
                    audit(username, "CREATE", "trip", trip_id, f"plant={plant}; transporter={transporter}; vehicle={vehicle_no}")
                    st.success(f"Trip saved. Trip ID: {trip_id}")
                except Exception as e:
                    st.error(f"Trip save failed: {e}")

with pages[1]:
    st.subheader("Trip Register")
    f1,f2,f3 = st.columns(3)
    d1 = f1.date_input("From Date", value=date.today().replace(day=1), key="reg_from", format="DD-MM-YYYY")
    d2 = f2.date_input("To Date", value=datetime.now(TZ).date(), key="reg_to", format="DD-MM-YYYY")
    plant_filter = f3.selectbox("Plant Filter", ["All"] + allowed_plants, key="reg_plant")
    df = read_df("SELECT * FROM trips WHERE trip_date BETWEEN :d1 AND :d2 ORDER BY trip_date DESC,id DESC",
                 {"d1":d1.isoformat(),"d2":d2.isoformat()})
    if not is_admin and not df.empty: df = df[df.plant.isin(allowed_plants)]
    if plant_filter != "All" and not df.empty: df = df[df.plant == plant_filter]
    st.dataframe(df, use_container_width=True, hide_index=True)
    st.download_button("Download Register CSV", df.to_csv(index=False).encode("utf-8-sig"), "trip_register.csv", "text/csv")

with pages[2]:
    st.subheader("Reports")
    r1,r2 = st.columns(2)
    rd1 = r1.date_input("Report From", value=date.today().replace(day=1), key="rep_from", format="DD-MM-YYYY")
    rd2 = r2.date_input("Report To", value=datetime.now(TZ).date(), key="rep_to", format="DD-MM-YYYY")
    rdf = read_df("SELECT * FROM trips WHERE trip_date BETWEEN :a AND :b ORDER BY trip_date,id",
                  {"a":rd1.isoformat(),"b":rd2.isoformat()})
    if not is_admin and not rdf.empty: rdf = rdf[rdf.plant.isin(allowed_plants)]
    if not rdf.empty:
        st.metric("Total Trips", len(rdf))
        st.metric("Total Amount", f"₹{pd.to_numeric(rdf.rate).sum():,.2f}")
        c1,c2 = st.columns(2)
        c1.write("**Date-wise**"); c1.dataframe(rdf.groupby("trip_date", as_index=False).agg(Trips=("id","count"),Amount=("rate","sum")), use_container_width=True)
        c2.write("**Transporter-wise**"); c2.dataframe(rdf.groupby("transporter", as_index=False).agg(Trips=("id","count"),Amount=("rate","sum")), use_container_width=True)
        c1.write("**Vendor-wise**"); c1.dataframe(rdf.groupby("vendor", as_index=False).agg(Trips=("id","count"),Amount=("rate","sum")), use_container_width=True)
        c2.write("**Plant-wise**"); c2.dataframe(rdf.groupby("plant", as_index=False).agg(Trips=("id","count"),Amount=("rate","sum")), use_container_width=True)
        c1.write("**Status-wise**"); c1.dataframe(rdf.groupby("status", as_index=False).agg(Trips=("id","count"),Amount=("rate","sum")), use_container_width=True)
        xbuf=io.BytesIO()
        with pd.ExcelWriter(xbuf, engine="openpyxl") as writer:
            rdf.to_excel(writer, index=False, sheet_name="Trips")
            rdf.groupby("trip_date",as_index=False).agg(Trips=("id","count"),Amount=("rate","sum")).to_excel(writer,index=False,sheet_name="Date-wise")
            rdf.groupby("transporter",as_index=False).agg(Trips=("id","count"),Amount=("rate","sum")).to_excel(writer,index=False,sheet_name="Transporter-wise")
            rdf.groupby("vendor",as_index=False).agg(Trips=("id","count"),Amount=("rate","sum")).to_excel(writer,index=False,sheet_name="Vendor-wise")
            rdf.groupby("plant",as_index=False).agg(Trips=("id","count"),Amount=("rate","sum")).to_excel(writer,index=False,sheet_name="Plant-wise")
        st.download_button("Download Excel Report", xbuf.getvalue(), "transport_report.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        pbuf=io.BytesIO()
        doc=SimpleDocTemplate(pbuf,pagesize=landscape(A4),rightMargin=20,leftMargin=20,topMargin=20,bottomMargin=20)
        styles=getSampleStyleSheet()
        doc.build([Paragraph("Transport Management Report",styles["Title"]),Paragraph(f"{rd1.strftime('%d-%m-%Y')} to {rd2.strftime('%d-%m-%Y')} | Trips: {len(rdf)} | Total: ₹{pd.to_numeric(rdf.rate).sum():,.2f}",styles["Normal"]),Spacer(1,12),
                   Table([list(map(str,rdf.columns))]+rdf.fillna("").astype(str).values.tolist(), repeatRows=1, style=TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#12304a")),("TEXTCOLOR",(0,0),(-1,0),colors.white),("GRID",(0,0),(-1,-1),0.25,colors.grey),("FONTSIZE",(0,0),(-1,-1),6),("VALIGN",(0,0),(-1,-1),"TOP")]))])
        st.download_button("Download PDF Report", pbuf.getvalue(), "transport_report.pdf", "application/pdf")
    else: st.info("या कालावधीमध्ये रिपोर्टसाठी नोंदी नाहीत.")

with pages[3]:
    st.subheader("Trip Verification")
    vdf=read_df("SELECT id,trip_date,plant,transporter,vehicle_no,vendor,bill_ref,challan_no,rate,status,created_by FROM trips WHERE status != 'Verified' ORDER BY trip_date,id")
    if not is_admin and not vdf.empty: vdf=vdf[vdf.plant.isin(allowed_plants)]
    st.dataframe(vdf,use_container_width=True,hide_index=True)
    if not vdf.empty:
        trip_id=st.selectbox("Select Trip ID to verify",vdf.id.tolist())
        selected=vdf[vdf.id==trip_id].iloc[0]
        st.write(f"**{selected.transporter}** • {selected.vehicle_no} • ₹{float(selected.rate):,.2f}")
        confirm=st.checkbox("I checked this trip and supporting documents.")
        if st.button("Verify Trip",type="primary",disabled=not confirm):
            try:
                with engine.begin() as conn:
                    # A conditional update ensures only one verifier can transition a trip to Verified.
                    result=conn.execute(text("""UPDATE trips SET status='Verified',verified_by=:u,verified_at=:t
                        WHERE id=:id AND status != 'Verified'"""),{"u":username,"t":now_text(),"id":int(trip_id)})
                    if result.rowcount != 1:
                        st.error("ALREADY VERIFIED — This trip has already been checked. Do not process it again.")
                    else:
                        bill=str(selected.bill_ref or "").strip()
                        if bill:
                            duplicate=conn.execute(text("""SELECT id FROM trips WHERE transporter=:tr AND bill_ref=:b
                                AND id != :id LIMIT 1"""),{"tr":selected.transporter,"b":bill,"id":int(trip_id)}).first()
                        else: duplicate=None
                        if duplicate:
                            conn.execute(text("UPDATE trips SET status='Pending',verified_by='',verified_at='' WHERE id=:id"),{"id":int(trip_id)})
                            st.error(f"Duplicate bill reference detected for this transporter (Trip ID {duplicate[0]}). Verification cancelled.")
                        else:
                            audit(username,"VERIFY","trip",trip_id,f"bill_ref={bill}")
                            st.success("Trip verified successfully.")
                            st.rerun()
            except Exception as e: st.error(f"Verification failed: {e}")
    else: st.success("No pending trips to verify.")

if is_admin:
    with pages[4]:
        st.subheader("Master Data (Admin only)")
        mt,mv,mveh=st.tabs(["Transporters","Vendors","Vehicles / Rates"])
        with mt:
            with st.form("new_transporter"):
                n=st.text_input("Transporter Name"); ph=st.text_input("Phone"); ok=st.form_submit_button("Add Transporter")
                if ok and n.strip():
                    try:
                        with engine.begin() as c: c.execute(text("INSERT INTO transporters(name,phone) VALUES(:n,:p)"),{"n":n.strip(),"p":ph})
                        audit(username,"CREATE","transporter",n.strip()); st.success("Transporter added."); st.rerun()
                    except Exception as e: st.error(f"Could not add transporter: {e}")
            st.dataframe(read_df("SELECT * FROM transporters ORDER BY name"),use_container_width=True,hide_index=True)
            de=st.selectbox("Deactivate transporter",[""]+options("SELECT name FROM transporters WHERE active=1 ORDER BY name","name"),key="de_trans")
            if st.button("Deactivate selected transporter") and de:
                with engine.begin() as c: c.execute(text("UPDATE transporters SET active=0 WHERE name=:n"),{"n":de})
                audit(username,"DEACTIVATE","transporter",de); st.rerun()
        with mv:
            with st.form("new_vendor"):
                n=st.text_input("Vendor Name"); ph=st.text_input("Vendor Phone"); ok=st.form_submit_button("Add Vendor")
                if ok and n.strip():
                    try:
                        with engine.begin() as c: c.execute(text("INSERT INTO vendors(name,phone) VALUES(:n,:p)"),{"n":n.strip(),"p":ph})
                        audit(username,"CREATE","vendor",n.strip()); st.success("Vendor added."); st.rerun()
                    except Exception as e: st.error(f"Could not add vendor: {e}")
            st.dataframe(read_df("SELECT * FROM vendors ORDER BY name"),use_container_width=True,hide_index=True)
            de=st.selectbox("Deactivate vendor",[""]+options("SELECT name FROM vendors WHERE active=1 ORDER BY name","name"),key="de_vendor")
            if st.button("Deactivate selected vendor") and de:
                with engine.begin() as c: c.execute(text("UPDATE vendors SET active=0 WHERE name=:n"),{"n":de})
                audit(username,"DEACTIVATE","vendor",de); st.rerun()
        with mveh:
            trans=options("SELECT name FROM transporters WHERE active=1 ORDER BY name","name")
            with st.form("new_vehicle"):
                v=st.text_input("Vehicle Number"); tr=st.selectbox("Transporter",[""]+trans); sz=st.text_input("Vehicle Size"); rt=st.number_input("Fixed Rate (₹)",min_value=0.0,step=100.0); ok=st.form_submit_button("Add / Update Vehicle")
                if ok and v.strip() and tr:
                    with engine.begin() as c:
                        exists=c.execute(text("SELECT id FROM vehicles WHERE vehicle_no=:v"),{"v":v.strip().upper()}).first()
                        if exists: c.execute(text("UPDATE vehicles SET transporter=:t,vehicle_size=:s,fixed_rate=:r,active=1 WHERE vehicle_no=:v"),{"t":tr,"s":sz,"r":rt,"v":v.strip().upper()})
                        else: c.execute(text("INSERT INTO vehicles(vehicle_no,transporter,vehicle_size,fixed_rate) VALUES(:v,:t,:s,:r)"),{"v":v.strip().upper(),"t":tr,"s":sz,"r":rt})
                    audit(username,"UPSERT","vehicle",v.strip().upper(),f"transporter={tr};rate={rt}"); st.success("Vehicle/rate saved."); st.rerun()
            st.dataframe(read_df("SELECT * FROM vehicles ORDER BY vehicle_no"),use_container_width=True,hide_index=True)
    with pages[5]:
        st.subheader("Audit Log")
        adf=read_df("SELECT * FROM audit_log ORDER BY id DESC LIMIT 1000")
        st.dataframe(adf,use_container_width=True,hide_index=True)
        st.download_button("Download Audit CSV",adf.to_csv(index=False).encode("utf-8-sig"),"audit_log.csv","text/csv")

st.caption("Dates are stored as ISO dates and displayed by Streamlit date controls in DD-MM-YYYY format. Configure managed PostgreSQL for persistent online storage and scheduled backups.")
