-- =============================================================================
-- queries.sql  --  Blood Bank Management System
-- Database : blood_bank_db
--
-- A collection of practical queries used for learning and for the DBMS Lab
-- demonstration. Each query has a comment explaining WHAT it returns and WHICH
-- SQL concept it demonstrates. Run them with:
--     mysql -u <user> -p blood_bank_db < queries.sql
-- =============================================================================

USE blood_bank_db;

-- -----------------------------------------------------------------------------
-- 1) SELECT: list every donor (basic projection + ORDER BY).
-- -----------------------------------------------------------------------------
SELECT Donor_ID, Name, Age, Gender, Phone, Bg_ID
FROM Donor
ORDER BY Donor_ID;

-- -----------------------------------------------------------------------------
-- 2) Filtering: donors belonging to one blood group.
--    We look up the Bg_ID for 'O+' first, then filter on it.
-- -----------------------------------------------------------------------------
SELECT d.Donor_ID, d.Name, d.Phone
FROM Donor d
JOIN Blood_Group bg ON d.Bg_ID = bg.Bg_ID
WHERE bg.Blood_Type = 'O+'
ORDER BY d.Name;

-- -----------------------------------------------------------------------------
-- 3) Search: donors whose name matches a pattern (LIKE, case-insensitive).
-- -----------------------------------------------------------------------------
SELECT Donor_ID, Name, Phone
FROM Donor
WHERE Name LIKE '%a%'
ORDER BY Name;

-- -----------------------------------------------------------------------------
-- 4) INNER JOIN: donors together with the human-readable blood group name.
--    INNER JOIN keeps only rows that have a match in both tables.
-- -----------------------------------------------------------------------------
SELECT d.Donor_ID, d.Name, bg.Blood_Type
FROM Donor d
INNER JOIN Blood_Group bg ON d.Bg_ID = bg.Bg_ID
ORDER BY d.Donor_ID;

-- -----------------------------------------------------------------------------
-- 5) LEFT JOIN: every donor, even those with NO donations (they show NULL).
--    A LEFT JOIN preserves unmatched rows from the left table.
-- -----------------------------------------------------------------------------
SELECT d.Donor_ID, d.Name, COUNT(dn.Donation_ID) AS donation_count
FROM Donor d
LEFT JOIN Donation dn ON d.Donor_ID = dn.Donor_ID
GROUP BY d.Donor_ID, d.Name
ORDER BY donation_count DESC, d.Name;

-- -----------------------------------------------------------------------------
-- 6) Aggregation (SUM): total quantity donated by each donor.
--    COALESCE turns the NULL of a donor with no donations into 0.
-- -----------------------------------------------------------------------------
SELECT d.Donor_ID, d.Name, COALESCE(SUM(dn.Qty), 0) AS total_donated
FROM Donor d
LEFT JOIN Donation dn ON d.Donor_ID = dn.Donor_ID
GROUP BY d.Donor_ID, d.Name
ORDER BY total_donated DESC, d.Name;

-- -----------------------------------------------------------------------------
-- 7) COUNT + GROUP BY: how many donors belong to each blood group.
--    LEFT JOIN keeps blood groups that currently have zero donors visible.
-- -----------------------------------------------------------------------------
SELECT bg.Blood_Type, COUNT(d.Donor_ID) AS donor_count
FROM Blood_Group bg
LEFT JOIN Donor d ON bg.Bg_ID = d.Bg_ID
GROUP BY bg.Bg_ID, bg.Blood_Type
ORDER BY bg.Bg_ID;

-- -----------------------------------------------------------------------------
-- 8) Current blood stock for all blood groups (INNER JOIN for the label).
-- -----------------------------------------------------------------------------
SELECT bg.Blood_Type, s.Total_Qty, s.Last_Updated
FROM Blood_Stock s
INNER JOIN Blood_Group bg ON s.Bg_ID = bg.Bg_ID
ORDER BY bg.Bg_ID;

-- -----------------------------------------------------------------------------
-- 9) Low stock: blood groups whose stock is below a threshold (here 10 units).
-- -----------------------------------------------------------------------------
SELECT bg.Blood_Type, s.Total_Qty
FROM Blood_Stock s
JOIN Blood_Group bg ON s.Bg_ID = bg.Bg_ID
WHERE s.Total_Qty < 10
ORDER BY s.Total_Qty ASC;

-- -----------------------------------------------------------------------------
-- 10) All pending hospital requests (simple status filter).
-- -----------------------------------------------------------------------------
SELECT Request_ID, Hospital_ID, Bg_ID, Date, Qty, Status
FROM Blood_Request
WHERE Status = 'Pending'
ORDER BY Date;

-- -----------------------------------------------------------------------------
-- 11) Request detail: hospital name, requested blood group, qty and status.
--     Demonstrates joining three tables in one query.
-- -----------------------------------------------------------------------------
SELECT br.Request_ID, h.Name AS hospital, bg.Blood_Type,
       br.Qty, br.Date, br.Status
FROM Blood_Request br
JOIN Hospital h    ON br.Hospital_ID = h.Hospital_ID
JOIN Blood_Group bg ON br.Bg_ID = bg.Bg_ID
ORDER BY br.Request_ID;

-- -----------------------------------------------------------------------------
-- 12) Range filter: donations made within a date range (BETWEEN).
-- -----------------------------------------------------------------------------
SELECT Donation_ID, Donor_ID, Bg_ID, Donation_Date, Qty
FROM Donation
WHERE Donation_Date BETWEEN '2026-02-01' AND '2026-03-31'
ORDER BY Donation_Date;

-- -----------------------------------------------------------------------------
-- 13) Monthly totals: total quantity donated per calendar month.
--     DATE_FORMAT groups rows by year-month.
-- -----------------------------------------------------------------------------
SELECT DATE_FORMAT(Donation_Date, '%Y-%m') AS month,
       SUM(Qty) AS total_qty,
       COUNT(*) AS donations
FROM Donation
GROUP BY DATE_FORMAT(Donation_Date, '%Y-%m')
ORDER BY month;

-- -----------------------------------------------------------------------------
-- 14) Request counts grouped by status.
-- -----------------------------------------------------------------------------
SELECT Status, COUNT(*) AS request_count
FROM Blood_Request
GROUP BY Status
ORDER BY request_count DESC;

-- -----------------------------------------------------------------------------
-- 15) Most frequently requested blood groups (SUM of requested quantity).
-- -----------------------------------------------------------------------------
SELECT bg.Blood_Type,
       COUNT(br.Request_ID)      AS times_requested,
       COALESCE(SUM(br.Qty), 0)  AS total_requested
FROM Blood_Group bg
LEFT JOIN Blood_Request br ON bg.Bg_ID = br.Bg_ID
GROUP BY bg.Bg_ID, bg.Blood_Type
ORDER BY total_requested DESC, bg.Blood_Type;

-- -----------------------------------------------------------------------------
-- 16) Safe UPDATE: correct a donor's phone number using a parameterised,
--     targeted WHERE on the primary key. (Wrap in a transaction when testing.)
-- -----------------------------------------------------------------------------
START TRANSACTION;
UPDATE Donor
SET Phone = '9000000099'
WHERE Donor_ID = 2;
-- COMMIT;   -- keep COMMIT commented so the sample data is not permanently changed
ROLLBACK;

-- -----------------------------------------------------------------------------
-- 17) Safe DELETE: remove a specific sample record inside a transaction.
--     The Donor row that has donations cannot be deleted while FK RESTRICT is
--     in force, which protects donation history (auditability).
-- -----------------------------------------------------------------------------
START TRANSACTION;
-- Create a throw-away donor with no donations, then delete just that row.
INSERT INTO Donor (Name, Age, Gender, Phone, Bg_ID)
VALUES ('Temp Delete Demo', 30, 'Other', '9000000123', 1);
DELETE FROM Donor WHERE Phone = '9000000123';
ROLLBACK;   -- roll back so the demonstration leaves no trace

-- -----------------------------------------------------------------------------
-- 18) TRANSACTION example: fulfil request #4 (Lakeside, AB+, 2 units) atomically.
--     Locks the stock row, checks availability, deducts and marks Completed.
--     Commented COMMIT so it can be reviewed safely; see app.py for the live
--     implementation used by the web application.
-- -----------------------------------------------------------------------------
START TRANSACTION;
-- (a) lock the stock row for the requested blood group (AB+ = Bg_ID 5)
SELECT Total_Qty
FROM Blood_Stock
WHERE Bg_ID = 5
FOR UPDATE;
-- (b) deduct only if enough stock exists; the WHERE clause enforces no-negative
UPDATE Blood_Stock
SET Total_Qty = Total_Qty - 2.00
WHERE Bg_ID = 5 AND Total_Qty >= 2.00;
-- (c) mark the request completed
UPDATE Blood_Request
SET Status = 'Completed'
WHERE Request_ID = 4 AND Status IN ('Approved','Pending');
ROLLBACK;   -- replace with COMMIT to actually apply the change
