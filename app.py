You are an expert full-stack software engineer. Build a complete, production-ready Web Application named "Multi-Plant Transport Management System" using Python with Streamlit and SQLite (or Node.js/React if preferred, but Streamlit is preferred for rapid, clean UI development). The system must support up to 5 concurrent users with authentication.

### Core Requirements to Implement:

1. Authentication & Roles:
   - Login page supporting 'Admin' and 'Standard User' roles.
   - Admins have exclusive rights to Master Data modifications and Trip Deletions.

2. Master Data Management (Admin Only for Add/Delete):
   - Transport Master Data: Transporter Name, Vehicle No., Vehicle Size, Plant/Base, Fixed Rate/Trip (Rs.), Status.
   - Vendor Master Data: Vendor Name, Address, Status.
   - Vehicle Size / Fixed Rate Master: Mapping sizes to rates.

3. Daily Trip Entry Section:
   - Dropdown menus for: Plant (1 to 5), Date, Transport Type (Local: Company-to-Company, Company-to-Vendor, Vendor-to-Company; or Vendor Dispatch), Transporter, Vehicle No., Vehicle Size, Purpose, From/To (fetching from Vendors/Plants), Challan No., Material/Remarks, and Auto Rate (fetched automatically from Master).
   - Ability to save trips.
   - Deletion feature: Restricted strictly to Admin, and mandatory input of a 'Deletion Reason' is required before confirmation.

4. Reporting & Bill Verification (Checklist Feature):
   - Transport Daily Report: Filterable by daily and monthly views, categorized by Transporter Name, Date, Vendor, and Vehicle.
   - Bill Verification Checklist: Transport bills arrive after 3 to 5 days. Link a verification checkbox to daily trip entries. If a user checks a bill that is already marked as verified, trigger an explicit warning message: "Warning: This bill has already been verified and processed previously!"

5. System Maintenance:
   - Provide a button/routine for Daily Database Backup (downloadable SQLite backup file).

Write clean, modular code with clear comments, embedded error handling, and an intuitive sidebar navigation menu.
