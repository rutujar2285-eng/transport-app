import streamlit as st

st.title("🚛 Multi-Plant Transport Management System")
st.write("Welcome to the Transport Management App for managing vehicle dispatches across plants.")
Authentication & Roles:
   - Login page supporting 'Admin' and 'Standard User' roles.
   - Admins have exclusive rights to Master Data modifications and Trip Deletions.
       Master Data Management (Admin Only for Add/Delete):
   - Transport Master Data: Transporter Name, Vehicle No., Vehicle Size, Plant/Base, Fixed Rate/Trip (INR), Status.
   - Vendor Master Data: Vendor Name, Address, Status.
   - Vehicle Size / Fixed Rate Master: Mapping sizes to rates.
# Simple input form placeholder
transporter = st.text_input("Transporter Name")
vehicle_no = st.text_input("Vehicle No.")
plant = st.selectbox("Select Plant", ["Plant 1 = J BLOCK", "Plant 2 = F-2 BLOCK", "Plant 3 = T BLOCK", "Plant 4 = S BLOCK", "Plant 5 = GAURI BLOCK"])
rate = st.number_input("Fixed Rate (Rs.)", min_value=0.0)

if st.button("Save Trip"):
    st.success(f"Trip saved successfully for {transporter} ({vehicle_no}) at {plant}!")
