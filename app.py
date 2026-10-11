import streamlit as st

st.title("🚛 Multi-Plant Transport Management System")
st.write("Welcome to the Transport Management App for managing vehicle dispatches across plants.")

# Simple input form placeholder
transporter = st.text_input("Transporter Name")
vehicle_no = st.text_input("Vehicle No.")
plant = st.selectbox("Select Plant", ["Plant 1", "Plant 2", "Plant 3", "Plant 4", "Plant 5"])
rate = st.number_input("Fixed Rate (Rs.)", min_value=0.0)

if st.button("Save Trip"):
    st.success(f"Trip saved successfully for {transporter} ({vehicle_no}) at {plant}!")
