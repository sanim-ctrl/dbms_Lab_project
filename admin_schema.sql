-- =============================================================================
-- admin_schema.sql  --  Blood Bank Management System (admin approval workflow)
-- Database   : blood_bank_db
-- Engine     : InnoDB
--
-- ADDITIVE ONLY. Unlike schema.sql this script never drops or rewrites existing
-- tables or data, so it is safe to run against a live database.
--
-- The application creates this table automatically (CREATE TABLE IF NOT EXISTS)
-- the first time an admin logs in. Run this script once if you prefer to manage
-- the schema by hand, or if the application's MySQL user cannot create tables.
--
-- Usage:
--     mysql -u <user> -p <database> < admin_schema.sql
-- =============================================================================

CREATE TABLE IF NOT EXISTS Admin_Audit_Log (
    Audit_ID      INT          NOT NULL AUTO_INCREMENT,
    -- Which kind of record the decision was about (currently 'Blood_Request').
    Proposal_Type VARCHAR(30)  NOT NULL,
    -- Identifier of the proposal that was decided (e.g. Blood_Request.Request_ID).
    Proposal_ID   INT          NOT NULL,
    Action        ENUM('Approved','Rejected') NOT NULL,
    -- Short human-readable summary of the decided record (optional).
    Detail        VARCHAR(255) NULL,
    Decided_At    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (Audit_ID),
    KEY idx_audit_proposal (Proposal_Type, Proposal_ID)
) ENGINE=InnoDB;
