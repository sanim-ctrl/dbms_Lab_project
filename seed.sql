-- =============================================================================
-- seed.sql  --  Blood Bank Management System  (sample / demo data)
-- Database   : blood_bank_db
--
-- INTENDED USE: initial setup on a freshly created database. It assumes the
-- tables are EMPTY and are exactly the ones from schema.sql. Run it once,
-- immediately after schema.sql. To start over, re-run schema.sql (which drops
-- and recreates the database) and then seed.sql again. Do NOT run seed.sql
-- twice: the UNIQUE constraints on Blood_Group.Blood_Type, Donor.Phone and
-- Hospital.Phone will reject the duplicate rows.
--
-- All names, phone numbers and addresses below are FICTIONAL.
--
-- STOCK RECONCILIATION (how the numbers are kept consistent)
--   Final stock is defined as:  opening balance + donations - completed requests
--   Every Blood_Stock row at the end of this file uses that formula, and the
--   arithmetic is shown in the comment next to each value so it can be verified.
-- =============================================================================

USE blood_bank_db;

-- -----------------------------------------------------------------------------
-- 1. Blood groups (all eight standard groups). Bg_ID 1..8 in this fixed order.
-- -----------------------------------------------------------------------------
INSERT INTO Blood_Group (Bg_ID, Blood_Type) VALUES
    (1, 'A+'),
    (2, 'A-'),
    (3, 'B+'),
    (4, 'B-'),
    (5, 'AB+'),
    (6, 'AB-'),
    (7, 'O+'),
    (8, 'O-');

-- -----------------------------------------------------------------------------
-- 2. Donors (15 fictional donors).
-- -----------------------------------------------------------------------------
INSERT INTO Donor (Donor_ID, Name, Age, Gender, Phone, Bg_ID) VALUES
    (1,  'Arjun Mehta',   25, 'Male',   '9000000001', 1),  -- A+
    (2,  'Bilal Ahmed',   30, 'Male',   '9000000002', 7),  -- O+
    (3,  'Chitra Nair',   28, 'Female', '9000000003', 3),  -- B+
    (4,  'Deepak Verma',  35, 'Male',   '9000000004', 5),  -- AB+
    (5,  'Elena Dsouza',  22, 'Female', '9000000005', 2),  -- A-
    (6,  'Farhan Khan',   40, 'Male',   '9000000006', 8),  -- O-
    (7,  'Gita Sharma',   27, 'Female', '9000000007', 4),  -- B-
    (8,  'Harish Rao',    31, 'Male',   '9000000008', 6),  -- AB-
    (9,  'Iman Sultana',  24, 'Female', '9000000009', 1),  -- A+
    (10, 'Jatin Patel',   29, 'Male',   '9000000010', 7),  -- O+
    (11, 'Kavya Reddy',   33, 'Female', '9000000011', 3),  -- B+
    (12, 'Lalit Joshi',   45, 'Male',   '9000000012', 2),  -- A-
    (13, 'Meera Iyer',    26, 'Female', '9000000013', 5),  -- AB+
    (14, 'Nadeem Ali',    38, 'Male',   '9000000014', 8),  -- O-
    (15, 'Pooja Gupta',   21, 'Female', '9000000015', 4);  -- B-

-- -----------------------------------------------------------------------------
-- 3. Donations (12 records). Bg_ID must match the donor's registered group.
-- -----------------------------------------------------------------------------
INSERT INTO Donation (Donation_ID, Donor_ID, Bg_ID, Donation_Date, Qty) VALUES
    (1,  1,  1, '2026-01-05', 2.00),   -- Arjun  -> A+
    (2,  2,  7, '2026-01-12', 1.00),   -- Bilal  -> O+
    (3,  3,  3, '2026-01-20', 2.00),   -- Chitra -> B+
    (4,  5,  2, '2026-02-02', 1.00),   -- Elena  -> A-
    (5,  1,  1, '2026-02-15', 1.00),   -- Arjun  -> A+
    (6,  7,  4, '2026-02-20', 2.00),   -- Gita   -> B-
    (7,  6,  8, '2026-03-01', 1.00),   -- Farhan -> O-
    (8,  4,  5, '2026-03-05', 1.00),   -- Deepak -> AB+
    (9,  8,  6, '2026-03-10', 1.00),   -- Harish -> AB-
    (10, 2,  7, '2026-03-18', 2.00),   -- Bilal  -> O+
    (11, 11, 3, '2026-04-02', 1.00),   -- Kavya  -> B+
    (12, 9,  1, '2026-04-09', 3.00);   -- Iman   -> A+
--
-- Totals donated per group (used by the stock reconciliation below):
--   A+  = 2+1+3 = 6      A-  = 1
--   B+  = 2+1   = 3      B-  = 2
--   AB+ = 1              AB- = 1
--   O+  = 1+2   = 3      O-  = 1

-- -----------------------------------------------------------------------------
-- 4. Hospitals (5 fictional hospitals).
-- -----------------------------------------------------------------------------
INSERT INTO Hospital (Hospital_ID, Name, Address, Phone) VALUES
    (1, 'City Care Hospital',      '12 MG Road, Bengaluru',      '9111100001'),
    (2, 'Sunrise Medical Center',  '45 Park Street, Kolkata',    '9111100002'),
    (3, 'Apollo General Hospital', '7 Anna Salai, Chennai',      '9111100003'),
    (4, 'Lakeside Multispeciality','3 Lake View Road, Hyderabad','9111100004'),
    (5, 'Green Valley Hospital',   '88 Ring Road, Pune',         '9111100005');

-- -----------------------------------------------------------------------------
-- 5. Blood requests (10 records across every status).
--    Only the Completed requests have already deducted stock.
-- -----------------------------------------------------------------------------
INSERT INTO Blood_Request (Request_ID, Hospital_ID, Bg_ID, Date, Qty, Status) VALUES
    (1,  1, 1, '2026-02-01', 2.00, 'Completed'),  -- City Care   A+    -> deduct
    (2,  2, 7, '2026-02-05', 1.00, 'Completed'),  -- Sunrise     O+    -> deduct
    (3,  3, 4, '2026-03-02', 1.00, 'Completed'),  -- Apollo      B-    -> deduct
    (4,  4, 5, '2026-03-12', 2.00, 'Pending'),    -- Lakeside    AB+
    (5,  1, 8, '2026-03-15', 1.00, 'Approved'),   -- City Care   O-
    (6,  5, 1, '2026-04-01', 5.00, 'Pending'),    -- Green Valley A+
    (7,  2, 3, '2026-04-05', 1.00, 'Rejected'),   -- Sunrise     B+
    (8,  3, 2, '2026-04-08', 1.00, 'Completed'),  -- Apollo      A-    -> deduct
    (9,  4, 6, '2026-04-10', 1.00, 'Pending'),    -- Lakeside    AB-
    (10, 5, 7, '2026-04-12', 2.00, 'Approved');   -- Green Valley O+
--
-- Completed requests deducted per group:
--   A+ = 2      O+ = 1      B- = 1      A- = 1

-- -----------------------------------------------------------------------------
-- 6. Blood stock -- ONE row per blood group with the reconciled quantity.
--    Formula: opening + donated - completed
-- -----------------------------------------------------------------------------
INSERT INTO Blood_Stock (Stock_ID, Bg_ID, Total_Qty, Last_Updated) VALUES
    (1, 1, 24.00, '2026-04-09 10:00:00'),  -- A+  : 20 + 6 - 2 = 24
    (2, 2, 15.00, '2026-04-08 10:00:00'),  -- A-  : 15 + 1 - 1 = 15
    (3, 3, 21.00, '2026-04-02 10:00:00'),  -- B+  : 18 + 3 - 0 = 21
    (4, 4, 11.00, '2026-03-02 10:00:00'),  -- B-  : 10 + 2 - 1 = 11
    (5, 5,  9.00, '2026-03-05 10:00:00'),  -- AB+ :  8 + 1 - 0 = 9
    (6, 6,  6.00, '2026-03-10 10:00:00'),  -- AB- :  5 + 1 - 0 = 6
    (7, 7, 27.00, '2026-03-18 10:00:00'),  -- O+  : 25 + 3 - 1 = 27
    (8, 8,  8.00, '2026-03-01 10:00:00');  -- O-  :  7 + 1 - 0 = 8
