-- Compact frozen F5 choices; no expansion of native rows or changes to F3.
CREATE TABLE registry_dataset_release (
 dataset_id UUID NOT NULL REFERENCES analytical_dataset(dataset_id),
 import_id UUID NOT NULL REFERENCES registry_release(import_id),
 auditor_version VARCHAR NOT NULL,
 evidence_hash VARCHAR NOT NULL,
 input_hash VARCHAR NOT NULL,
 PRIMARY KEY(dataset_id,import_id)
);
CREATE TABLE registry_dataset_period (
 dataset_id UUID NOT NULL REFERENCES analytical_dataset(dataset_id),
 period_start DATE NOT NULL REFERENCES price_period(period_start),
 snapshot_date DATE NOT NULL,
 phase VARCHAR NOT NULL CHECK(phase IN ('final','preliminary')),
 period_ordinal INTEGER NOT NULL,
 PRIMARY KEY(dataset_id,period_start,phase)
);
CREATE TABLE registry_dataset_member (
 dataset_id UUID NOT NULL REFERENCES analytical_dataset(dataset_id),
 period_start DATE NOT NULL,
 phase VARCHAR NOT NULL,
 member_number INTEGER NOT NULL,
 state VARCHAR NOT NULL CHECK(state IN ('selected','missing','conflict','unsupported')),
 member_id BIGINT REFERENCES registry_member(member_id),
 import_id UUID REFERENCES registry_release(import_id),
 supporting_import_ids UUID[] NOT NULL,
 candidate_import_ids UUID[] NOT NULL,
 supported_fields VARCHAR[] NOT NULL,
 complete_snapshot BOOLEAN NOT NULL,
 monetary_context JSON NOT NULL,
 reason VARCHAR,
 PRIMARY KEY(dataset_id,period_start,phase,member_number),
 CHECK((state='selected')=(member_id IS NOT NULL AND import_id IS NOT NULL))
);
CREATE TABLE registry_dataset_legacy (
 dataset_id UUID NOT NULL REFERENCES analytical_dataset(dataset_id),
 selection_id UUID NOT NULL REFERENCES frozen_selection(selection_id),
 evidence_hash VARCHAR NOT NULL,
 PRIMARY KEY(dataset_id,selection_id)
);
