-- Located supplementary assertions, without guessed identities or supply facts.
CREATE TABLE supplement_import (
 import_id UUID PRIMARY KEY REFERENCES import_batch(import_id),
 delivery_id UUID NOT NULL REFERENCES source_delivery(delivery_id),
 kind VARCHAR NOT NULL CHECK(kind IN ('DOK','LMS36','LMS37')),
 original_sha256 VARCHAR NOT NULL,
 metadata JSON NOT NULL,
 coverage JSON NOT NULL,
 verification JSON NOT NULL
);
CREATE TABLE supplement_record (
 import_id UUID NOT NULL REFERENCES supplement_import(import_id),
 source_line BIGINT NOT NULL CHECK(source_line>0),
 byte_offset BIGINT NOT NULL CHECK(byte_offset>=0),
 byte_length BIGINT NOT NULL CHECK(byte_length>0),
 assertion VARCHAR NOT NULL CHECK(assertion IN (
  'conditional_reimbursement_exception','prescribing_permission','prescribing_reference'
 )),
 package_code VARCHAR,
 drug_id VARCHAR,
 extended_prescribing_code VARCHAR,
 issue_date DATE,
 effective_date DATE,
 valid_from DATE,
 valid_to DATE,
 payload JSON NOT NULL,
 PRIMARY KEY(import_id,source_line)
);
CREATE INDEX supplement_package_idx ON supplement_record(package_code,effective_date);
CREATE INDEX supplement_drug_idx ON supplement_record(drug_id,valid_from);
CREATE INDEX supplement_prescribing_code_idx ON supplement_record(extended_prescribing_code);
