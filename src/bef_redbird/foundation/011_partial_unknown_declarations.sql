-- An actual preliminary member can lack its own native SYSTEM declaration.
-- Unknown is NULL; source reconciliation permits this only for undeclared members.
ALTER TABLE registry_partial_member ALTER COLUMN declared_records DROP NOT NULL;
