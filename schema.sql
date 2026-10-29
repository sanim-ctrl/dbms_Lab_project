-- =============================================================================
-- schema.sql  --  Blood Bank Management System
-- Database   : blood_bank_db
-- Engine     : InnoDB (required for transactions + row-level locking)
-- Charset    : utf8mb4
-- Purpose    : Creates the database and all six core tables.
--
-- NOTE: This script DROPS the database if it already exists, so it performs a
--       FULL RESET. Run schema.sql first, then seed.sql, on a fresh setup.
--
-- Design decision (Blood_Group): each complete blood group is stored as a
-- single value in Blood_Type (e.g. 'A+', 'O-'). We deliberately DO NOT keep a
-- separate Rh_Factor column, because 'A+' already encodes the A antigen and the
-- Rh factor together; storing both would duplicate the same information.
-- =============================================================================

DROP DATABASE IF EXISTS blood_bank_db;
CREATE DATABASE blood_bank_db
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;
USE blood_bank_db;

-- -----------------------------------------------------------------------------
-- Table 1: Blood_Group  (reference / lookup table)
--   One row per standard blood group. Acts as the parent for donors, donations,
--   requests and stock, so blood groups are never repeated as free text.
-- -----------------------------------------------------------------------------
CREATE TABLE Blood_Group (
    Bg_ID       INT          NOT NULL AUTO_INCREMENT,
    Blood_Type  VARCHAR(3)   NOT NULL,
    PRIMARY KEY (Bg_ID),
    -- Prevent duplicate blood groups such as two 'A+' rows.
    UNIQUE KEY uq_blood_group_type (Blood_Type),
    -- Keep the domain closed to the eight standard groups.
    CONSTRAINT chk_blood_type
        CHECK (Blood_Type IN ('A+','A-','B+','B-','AB+','AB-','O+','O-'))
) ENGINE=InnoDB;

-- -----------------------------------------------------------------------------
-- Table 2: Donor
--   People who donate blood. Phone is treated as the unique natural identifier
--   so the same person cannot be registered twice.
--   Age is bounded to the usual 18-65 donation range.
--   Reference action on Bg_ID is ON DELETE RESTRICT so reference data cannot be
--   removed while donors still point at it.
-- -----------------------------------------------------------------------------
CREATE TABLE Donor (
    Donor_ID  INT          NOT NULL AUTO_INCREMENT,
    Name      VARCHAR(100) NOT NULL,
    Age       INT          NOT NULL,
    Gender    ENUM('Male','Female','Other') NOT NULL,
    Phone     VARCHAR(15)  NOT NULL,
    Bg_ID     INT          NOT NULL,
    PRIMARY KEY (Donor_ID),
    UNIQUE KEY uq_donor_phone (Phone),
    CONSTRAINT chk_donor_age CHECK (Age BETWEEN 18 AND 65),
    KEY idx_donor_name (Name),
    KEY idx_donor_bg (Bg_ID),
    CONSTRAINT fk_donor_bg
        FOREIGN KEY (Bg_ID) REFERENCES Blood_Group(Bg_ID)
        ON UPDATE CASCADE ON DELETE RESTRICT
) ENGINE=InnoDB;

-- -----------------------------------------------------------------------------
-- Table 3: Donation
--   One row per donation event. Bg_ID is stored (denormalised on purpose so a
--   historic donation keeps the group it was given for) but the application
--   enforces that it matches the donor's registered group.
--   Units_Donated is the number of whole blood units collected and must be >= 1.
--   RESTRICT on Donor_ID protects donation history (auditability): a donor with
--   donations cannot simply be deleted.
-- -----------------------------------------------------------------------------
CREATE TABLE Donation (
    Donation_ID    INT          NOT NULL AUTO_INCREMENT,
    Donor_ID       INT          NOT NULL,
    Bg_ID          INT          NOT NULL,
    Donation_Date  DATE         NOT NULL,
    Units_Donated  INT          NOT NULL DEFAULT 1,
    PRIMARY KEY (Donation_ID),
    CONSTRAINT chk_donation_units CHECK (Units_Donated >= 1),
    KEY idx_donation_date (Donation_Date),
    KEY idx_donation_donor (Donor_ID),
    KEY idx_donation_bg (Bg_ID),
    CONSTRAINT fk_donation_donor
        FOREIGN KEY (Donor_ID) REFERENCES Donor(Donor_ID)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT fk_donation_bg
        FOREIGN KEY (Bg_ID) REFERENCES Blood_Group(Bg_ID)
        ON UPDATE CASCADE ON DELETE RESTRICT
) ENGINE=InnoDB;

-- -----------------------------------------------------------------------------
-- Table 4: Hospital
--   Hospitals that can raise blood requests. Phone is unique.
-- -----------------------------------------------------------------------------
CREATE TABLE Hospital (
    Hospital_ID  INT          NOT NULL AUTO_INCREMENT,
    Name         VARCHAR(150) NOT NULL,
    Address      VARCHAR(255) NULL,
    Phone        VARCHAR(15)  NOT NULL,
    PRIMARY KEY (Hospital_ID),
    UNIQUE KEY uq_hospital_phone (Phone),
    KEY idx_hospital_name (Name)
) ENGINE=InnoDB;

-- -----------------------------------------------------------------------------
-- Table 5: Blood_Request
--   A hospital's request for a quantity of a blood group. Status follows the
--   workflow: Pending -> Approved -> Completed, or Pending -> Rejected.
--   Stock is deducted ONLY when a request becomes Completed (see app.py).
--   RESTRICT on Hospital_ID keeps request history for auditability.
-- -----------------------------------------------------------------------------
CREATE TABLE Blood_Request (
    Request_ID   INT          NOT NULL AUTO_INCREMENT,
    Hospital_ID  INT          NOT NULL,
    Bg_ID        INT          NOT NULL,
    Date         DATE         NOT NULL,
    Qty          DECIMAL(6,2) NOT NULL,
    Status       ENUM('Pending','Approved','Completed','Rejected')
                              NOT NULL DEFAULT 'Pending',
    PRIMARY KEY (Request_ID),
    CONSTRAINT chk_request_qty CHECK (Qty > 0),
    KEY idx_request_status (Status),
    KEY idx_request_date (Date),
    KEY idx_request_hospital (Hospital_ID),
    KEY idx_request_bg (Bg_ID),
    CONSTRAINT fk_request_hospital
        FOREIGN KEY (Hospital_ID) REFERENCES Hospital(Hospital_ID)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT fk_request_bg
        FOREIGN KEY (Bg_ID) REFERENCES Blood_Group(Bg_ID)
        ON UPDATE CASCADE ON DELETE RESTRICT
) ENGINE=InnoDB;

-- -----------------------------------------------------------------------------
-- Table 6: Blood_Stock
--   Exactly one row per blood group (UNIQUE on Bg_ID). Total_Qty can never go
--   negative (CHECK). Last_Updated is maintained automatically.
-- -----------------------------------------------------------------------------
CREATE TABLE Blood_Stock (
    Stock_ID      INT          NOT NULL AUTO_INCREMENT,
    Bg_ID         INT          NOT NULL,
    Total_Qty     DECIMAL(8,2) NOT NULL DEFAULT 0,
    Last_Updated  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                               ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (Stock_ID),
    UNIQUE KEY uq_stock_bg (Bg_ID),
    CONSTRAINT chk_stock_non_negative CHECK (Total_Qty >= 0),
    CONSTRAINT fk_stock_bg
        FOREIGN KEY (Bg_ID) REFERENCES Blood_Group(Bg_ID)
        ON UPDATE CASCADE ON DELETE RESTRICT
) ENGINE=InnoDB;
