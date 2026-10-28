-- Explicitly partial native references; never complete registry releases.
CREATE TABLE registry_partial_group (
 import_id UUID PRIMARY KEY REFERENCES import_batch(import_id),
 manifest_delivery_id UUID NOT NULL REFERENCES source_delivery(delivery_id),
 system_delivery_id UUID NOT NULL REFERENCES source_delivery(delivery_id),
 effective_date DATE NOT NULL,
 tariff_week VARCHAR NOT NULL,
 system_version VARCHAR NOT NULL,
 phase VARCHAR NOT NULL CHECK(phase IN ('final','preliminary')),
 profile VARCHAR NOT NULL,
 context JSON NOT NULL,
 system_data JSON NOT NULL,
 coverage JSON NOT NULL,
 verification_report JSON NOT NULL,
 evidence_hash VARCHAR NOT NULL,
 source_role VARCHAR NOT NULL CHECK(source_role='registry_partial'),
 canonical_eligible BOOLEAN NOT NULL CHECK(canonical_eligible=FALSE)
);
CREATE TABLE registry_partial_member (
 import_id UUID NOT NULL REFERENCES registry_partial_group(import_id),
 member_number INTEGER NOT NULL CHECK(member_number>=0 AND member_number<100),
 member_id BIGINT REFERENCES registry_member(member_id),
 source_delivery_id UUID NOT NULL REFERENCES source_delivery(delivery_id),
 native_name VARCHAR NOT NULL,
 declared_records BIGINT NOT NULL CHECK(declared_records>=0),
 available_records BIGINT NOT NULL CHECK(available_records>=0),
 raw_sha256 VARCHAR NOT NULL,
 status VARCHAR NOT NULL CHECK(status IN ('system','available','available_empty','empty_unavailable')),
 PRIMARY KEY(import_id,member_number),
 CHECK((member_number=0 AND status='system' AND member_id IS NULL)
    OR (member_number>0 AND status='empty_unavailable' AND member_id IS NULL
        AND available_records=0 AND declared_records>0)
    OR (member_number>0 AND status IN ('available','available_empty') AND member_id IS NOT NULL))
);
