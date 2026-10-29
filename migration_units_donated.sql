-- =============================================================================
-- migration_units_donated.sql  --  Blood Bank Management System
-- Database : blood_bank_db
--
-- DATA-PRESERVING migration for databases created before the "Units Donated"
-- feature. It converts the old Donation.Qty DECIMAL column into
-- Donation.Units_Donated INT (minimum 1) and keeps every existing donation row.
--
-- Historical values:
--   * a positive whole-unit Qty (for example 3.00) is copied exactly;
--   * any other old value falls back to 1, because a donation quantity must be
--     a whole number of units.
--
-- PARTIAL-MIGRATION RECOVERY
--   While Qty still exists it is treated as the source of truth. This means a
--   previous attempt that added Units_Donated with its DEFAULT 1 but did not
--   copy the old values is repaired: every row is copied from Qty again, not
--   only rows whose Units_Donated is still NULL.
--
-- CONSTRAINTS
--   Every CHECK constraint on Donation whose expression references Qty is
--   discovered from information_schema, whatever its name, and dropped before
--   the Qty column is removed. A CHECK on Units_Donated is then ensured.
--
-- VERIFICATION
--   Before Qty is dropped, every row is checked against the expected mapping
--   (valid whole Qty -> same integer, otherwise 1). If anything does not match,
--   the procedure raises an error and leaves Qty in place so the migration can
--   be corrected and re-run.
--
-- The script is safe to re-run and never drops the database or any table. It
-- never deletes donation rows. Run it during a maintenance window: while Qty
-- exists, this migration treats Qty as authoritative, so the application should
-- not be inserting new donations at the same time.
--
-- Usage (with a MySQL account that may ALTER tables):
--     mysql -u <admin_user> -p blood_bank_db < migration_units_donated.sql
--
-- Take a backup first:
--     mysqldump -u <admin_user> -p blood_bank_db Donation > Donation_backup.sql
-- =============================================================================

USE blood_bank_db;

DROP PROCEDURE IF EXISTS migrate_units_donated;

DELIMITER $$
CREATE PROCEDURE migrate_units_donated()
BEGIN
    DECLARE has_qty INT DEFAULT 0;
    DECLARE has_units INT DEFAULT 0;
    DECLARE has_units_check INT DEFAULT 0;
    DECLARE mismatch_count BIGINT DEFAULT 0;
    DECLARE bad_units BIGINT DEFAULT 0;

    SELECT COUNT(*) INTO has_qty
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'Donation'
       AND COLUMN_NAME = 'Qty';

    SELECT COUNT(*) INTO has_units
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'Donation'
       AND COLUMN_NAME = 'Units_Donated';

    -- Add the new integer column.
    IF has_units = 0 THEN
        IF has_qty = 1 THEN
            -- Nullable at first so the copy below can be verified before the
            -- NOT NULL/default constraint is applied.
            ALTER TABLE Donation
                ADD COLUMN Units_Donated INT NULL AFTER Donation_Date;
        ELSE
            ALTER TABLE Donation
                ADD COLUMN Units_Donated INT NOT NULL DEFAULT 1 AFTER Donation_Date;
        END IF;
    END IF;

    -- While Qty is still present it is authoritative. Copy EVERY row, even if
    -- Units_Donated already exists with its default value of 1, so a partially
    -- completed migration cannot lose the original quantities.
    IF has_qty = 1 THEN
        UPDATE Donation
           SET Units_Donated = CASE
                 WHEN Qty >= 1 AND Qty = FLOOR(Qty)
                   THEN CAST(Qty AS UNSIGNED)
                 ELSE 1
               END
         WHERE Qty IS NOT NULL;
    END IF;

    -- Normalise rows with no usable legacy value.
    UPDATE Donation
       SET Units_Donated = 1
     WHERE Units_Donated IS NULL OR Units_Donated < 1;

    ALTER TABLE Donation
        MODIFY COLUMN Units_Donated INT NOT NULL DEFAULT 1;

    -- Verify the copy BEFORE dropping Qty or any constraint.
    IF has_qty = 1 THEN
        SELECT COUNT(*) INTO mismatch_count
          FROM Donation
         WHERE Qty IS NOT NULL
           AND Units_Donated <> CASE
                 WHEN Qty >= 1 AND Qty = FLOOR(Qty)
                   THEN CAST(Qty AS UNSIGNED)
                 ELSE 1
               END;

        SELECT COUNT(*) INTO bad_units
          FROM Donation
         WHERE Units_Donated IS NULL OR Units_Donated < 1;

        IF mismatch_count > 0 OR bad_units > 0 THEN
            SIGNAL SQLSTATE '45000'
               SET MESSAGE_TEXT = 'Units_Donated verification failed; Qty was NOT dropped.';
        END IF;
    END IF;

    -- Discover and drop ALL CHECK constraints on Donation that reference the
    -- Qty COLUMN, regardless of their names. MySQL 8 renders column references
    -- in CHECK_CLAUSE with backticks (for example (`Qty` > 0)), so matching the
    -- backticked identifier avoids both similarly-named columns (Qty_Note,
    -- Quantity) and string literals that merely contain the word 'qty'.
    IF has_qty = 1 THEN
        SET @drop_qty_checks = NULL;

        SELECT GROUP_CONCAT(
                   CONCAT('DROP CHECK `', REPLACE(tc.CONSTRAINT_NAME, '`', '``'), '`')
                   SEPARATOR ', '
               )
          INTO @drop_qty_checks
          FROM information_schema.TABLE_CONSTRAINTS tc
          JOIN information_schema.CHECK_CONSTRAINTS cc
            ON cc.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA
           AND cc.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
         WHERE tc.CONSTRAINT_SCHEMA = DATABASE()
           AND tc.TABLE_NAME = 'Donation'
           AND tc.CONSTRAINT_TYPE = 'CHECK'
           AND LOCATE('`qty`', LOWER(cc.CHECK_CLAUSE)) > 0;

        IF @drop_qty_checks IS NOT NULL THEN
            SET @drop_qty_checks_sql = CONCAT('ALTER TABLE Donation ', @drop_qty_checks);
            PREPARE drop_qty_checks_stmt FROM @drop_qty_checks_sql;
            EXECUTE drop_qty_checks_stmt;
            DEALLOCATE PREPARE drop_qty_checks_stmt;
        END IF;
    END IF;

    -- Ensure a validation constraint exists for the new column (without adding
    -- a duplicate if a previous run or another migration already created one).
    -- As above, match the backticked identifier so strings / similar names do
    -- not count as a Units_Donated constraint.
    SELECT COUNT(*) INTO has_units_check
      FROM information_schema.TABLE_CONSTRAINTS tc
      JOIN information_schema.CHECK_CONSTRAINTS cc
        ON cc.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA
       AND cc.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
     WHERE tc.CONSTRAINT_SCHEMA = DATABASE()
       AND tc.TABLE_NAME = 'Donation'
       AND tc.CONSTRAINT_TYPE = 'CHECK'
       AND LOCATE('`units_donated`', LOWER(cc.CHECK_CLAUSE)) > 0;

    IF has_units_check = 0 THEN
        ALTER TABLE Donation
            ADD CONSTRAINT chk_donation_units CHECK (Units_Donated >= 1);
    END IF;

    -- Only now, after the copy has been verified and Qty is no longer used by
    -- any CHECK constraint, remove the legacy column.
    IF has_qty = 1 THEN
        ALTER TABLE Donation DROP COLUMN Qty;
    END IF;
END$$
DELIMITER ;

CALL migrate_units_donated();
DROP PROCEDURE migrate_units_donated;

-- Verify: every donation now has a whole number of units (>= 1).
SELECT Donation_ID, Units_Donated
FROM Donation
ORDER BY Donation_ID;

-- =============================================================================
-- Rollback (only if you really need the old DECIMAL column back):
--
-- ALTER TABLE Donation
--     DROP CHECK chk_donation_units,
--     CHANGE COLUMN Units_Donated Qty DECIMAL(6,2) NOT NULL,
--     ADD CONSTRAINT chk_donation_qty CHECK (Qty > 0);
--
-- New donations are whole units, so converting them back to DECIMAL is lossless.
-- =============================================================================
