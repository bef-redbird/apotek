-- F5 compact native registry. Earlier observations and frozen selections are unchanged.
CREATE SEQUENCE registry_row_sequence;
CREATE SEQUENCE registry_member_sequence;
CREATE TABLE registry_release (
 import_id UUID PRIMARY KEY REFERENCES import_batch(import_id),
 delivery_id UUID NOT NULL REFERENCES source_delivery(delivery_id),
 effective_date DATE NOT NULL,
 tariff_week VARCHAR NOT NULL,
 system_version VARCHAR NOT NULL,
 phase VARCHAR NOT NULL CHECK(phase IN ('final','preliminary')),
 profile VARCHAR NOT NULL,
 context JSON NOT NULL,
 system_parameters VARCHAR NOT NULL
);
CREATE TABLE registry_member (
 member_id BIGINT PRIMARY KEY,
 member_number INTEGER NOT NULL,
 profile VARCHAR NOT NULL,
 raw_sha256 VARCHAR NOT NULL,
 row_count BIGINT NOT NULL CHECK(row_count>=0),
 row_ids BIGINT[] NOT NULL,
 ordering_hash VARCHAR NOT NULL,
 UNIQUE(member_number,profile,raw_sha256),
 CHECK(array_length(row_ids)=row_count)
);
CREATE TABLE registry_release_member (
 import_id UUID NOT NULL REFERENCES registry_release(import_id),
 member_number INTEGER NOT NULL,
 member_id BIGINT NOT NULL REFERENCES registry_member(member_id),
 native_name VARCHAR NOT NULL,
 PRIMARY KEY(import_id,member_number)
);
CREATE TABLE registry_member_verification (
 member_id BIGINT NOT NULL REFERENCES registry_member(member_id),
 auditor_version VARCHAR NOT NULL,
 original_sha256 VARCHAR NOT NULL,
 typed_sha256 VARCHAR NOT NULL,
 report JSON NOT NULL,
 verified_at TIMESTAMPTZ NOT NULL,
 PRIMARY KEY(member_id,auditor_version)
);
CREATE TABLE registry_release_verification (
 import_id UUID NOT NULL REFERENCES registry_release(import_id),
 auditor_version VARCHAR NOT NULL,
 evidence_hash VARCHAR NOT NULL,
 report JSON NOT NULL,
 verified_at TIMESTAMPTZ NOT NULL,
 PRIMARY KEY(import_id,auditor_version)
);
CREATE TABLE analytical_dataset (
 dataset_id UUID PRIMARY KEY,
 policy_version VARCHAR NOT NULL,
 configuration JSON NOT NULL,
 configuration_hash VARCHAR NOT NULL UNIQUE,
 manifest JSON NOT NULL,
 evidence_hash VARCHAR NOT NULL,
 created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE registry_lms01 (
 row_id BIGINT PRIMARY KEY,
 profile VARCHAR NOT NULL,
 raw_sha256 VARCHAR NOT NULL,
 field_states JSON NOT NULL,
 drug_id VARCHAR,
 product_type VARCHAR,
 product_subtype VARCHAR,
 alphabetic_sequence VARCHAR,
 speciality_number VARCHAR,
 name VARCHAR,
 form VARCHAR,
 form_code VARCHAR,
 additional_form_token VARCHAR,
 strength VARCHAR,
 strength_numeric_token VARCHAR,
 strength_unit VARCHAR,
 authorisation_holder VARCHAR,
 drug_distributor VARCHAR,
 atc VARCHAR,
 administration_codes VARCHAR,
 traffic_warning VARCHAR,
 substitution_flag VARCHAR,
 renewal_token VARCHAR,
 legacy_20 VARCHAR,
 legacy_21 VARCHAR,
 drug_substitution_group VARCHAR,
 dose_suitable VARCHAR,
 deregistration_date DATE,
 quarantine_date DATE,
 UNIQUE(profile,raw_sha256)
);
CREATE TABLE registry_lms02 (
 row_id BIGINT PRIMARY KEY,
 profile VARCHAR NOT NULL,
 raw_sha256 VARCHAR NOT NULL,
 field_states JSON NOT NULL,
 drug_id VARCHAR,
 package_number VARCHAR,
 alphabetic_sequence VARCHAR,
 component_package VARCHAR,
 component_count_token VARCHAR,
 pack_description VARCHAR,
 pack_size_token VARCHAR,
 pack_unit VARCHAR,
 container_code VARCHAR,
 dispensing_code VARCHAR,
 speciality_code VARCHAR,
 reimbursement_code VARCHAR,
 reimbursement_clause VARCHAR,
 ddd_token VARCHAR,
 storage_time_token VARCHAR,
 storage_time_unit VARCHAR,
 storage_code VARCHAR,
 creation_date DATE,
 last_price_change_date DATE,
 exit_date DATE,
 calculation_code VARCHAR,
 reimbursement_group_flag VARCHAR,
 preparation_fee_flag VARCHAR,
 safety_features_token VARCHAR,
 package_distributor VARCHAR,
 UNIQUE(profile,raw_sha256)
);
CREATE TABLE registry_lms03 (
 row_id BIGINT PRIMARY KEY,
 profile VARCHAR NOT NULL,
 raw_sha256 VARCHAR NOT NULL,
 field_states JSON NOT NULL,
 package_number VARCHAR,
 aip_ore BIGINT,
 registered_ore BIGINT,
 consumer_ore BIGINT,
 legacy_05 VARCHAR,
 legacy_06 VARCHAR,
 reimbursement_ore BIGINT,
 hospital_slot_08 BIGINT,
 legacy_09 VARCHAR,
 non_reimbursable_ore BIGINT,
 legacy_11 VARCHAR,
 UNIQUE(profile,raw_sha256)
);
CREATE TABLE registry_lms04 (
 row_id BIGINT PRIMARY KEY,
 profile VARCHAR NOT NULL,
 raw_sha256 VARCHAR NOT NULL,
 field_states JSON NOT NULL,
 group_code VARCHAR,
 package_number VARCHAR,
 pack_size_token VARCHAR,
 alphabetic_sequence VARCHAR,
 legacy_05 VARCHAR,
 abc_code VARCHAR,
 cheapest_package VARCHAR,
 legacy_08 VARCHAR,
 UNIQUE(profile,raw_sha256)
);
CREATE TABLE registry_lms05 (
 row_id BIGINT PRIMARY KEY,
 profile VARCHAR NOT NULL,
 raw_sha256 VARCHAR NOT NULL,
 field_states JSON NOT NULL,
 group_code VARCHAR,
 package_number VARCHAR,
 UNIQUE(profile,raw_sha256)
);
CREATE TABLE registry_lms07 (
 row_id BIGINT PRIMARY KEY,
 profile VARCHAR NOT NULL,
 raw_sha256 VARCHAR NOT NULL,
 field_states JSON NOT NULL,
 group_code VARCHAR,
 package_number VARCHAR,
 legacy_03 VARCHAR,
 UNIQUE(profile,raw_sha256)
);
CREATE TABLE registry_lms09 (
 row_id BIGINT PRIMARY KEY,
 profile VARCHAR NOT NULL,
 raw_sha256 VARCHAR NOT NULL,
 field_states JSON NOT NULL,
 company_code VARCHAR,
 short_name VARCHAR,
 company_name VARCHAR,
 parallel_import_code VARCHAR,
 UNIQUE(profile,raw_sha256)
);
CREATE TABLE registry_lms10 (
 row_id BIGINT PRIMARY KEY,
 profile VARCHAR NOT NULL,
 raw_sha256 VARCHAR NOT NULL,
 field_states JSON NOT NULL,
 drug_id VARCHAR,
 change_date DATE,
 previous_name VARCHAR,
 UNIQUE(profile,raw_sha256)
);
CREATE TABLE registry_lms11 (
 row_id BIGINT PRIMARY KEY,
 profile VARCHAR NOT NULL,
 raw_sha256 VARCHAR NOT NULL,
 field_states JSON NOT NULL,
 code VARCHAR,
 short_text VARCHAR,
 description VARCHAR,
 legacy_04 VARCHAR,
 UNIQUE(profile,raw_sha256)
);
CREATE TABLE registry_lms12 (
 row_id BIGINT PRIMARY KEY,
 profile VARCHAR NOT NULL,
 raw_sha256 VARCHAR NOT NULL,
 field_states JSON NOT NULL,
 atc VARCHAR,
 description VARCHAR,
 legacy_07 VARCHAR,
 UNIQUE(profile,raw_sha256)
);
CREATE TABLE registry_lms13 (
 row_id BIGINT PRIMARY KEY,
 profile VARCHAR NOT NULL,
 raw_sha256 VARCHAR NOT NULL,
 field_states JSON NOT NULL,
 code VARCHAR,
 description VARCHAR,
 legacy_03 VARCHAR,
 UNIQUE(profile,raw_sha256)
);
CREATE TABLE registry_lms14 (
 row_id BIGINT PRIMARY KEY,
 profile VARCHAR NOT NULL,
 raw_sha256 VARCHAR NOT NULL,
 field_states JSON NOT NULL,
 code VARCHAR,
 short_text VARCHAR,
 description VARCHAR,
 legacy_04 VARCHAR,
 UNIQUE(profile,raw_sha256)
);
CREATE TABLE registry_lms15 (
 row_id BIGINT PRIMARY KEY,
 profile VARCHAR NOT NULL,
 raw_sha256 VARCHAR NOT NULL,
 field_states JSON NOT NULL,
 unit_type VARCHAR,
 code VARCHAR,
 short_text VARCHAR,
 description VARCHAR,
 legacy_05 VARCHAR,
 UNIQUE(profile,raw_sha256)
);
CREATE TABLE registry_lms16 (
 row_id BIGINT PRIMARY KEY,
 profile VARCHAR NOT NULL,
 raw_sha256 VARCHAR NOT NULL,
 field_states JSON NOT NULL,
 code VARCHAR,
 short_text VARCHAR,
 description VARCHAR,
 legacy_04 VARCHAR,
 UNIQUE(profile,raw_sha256)
);
CREATE TABLE registry_lms17 (
 row_id BIGINT PRIMARY KEY,
 profile VARCHAR NOT NULL,
 raw_sha256 VARCHAR NOT NULL,
 field_states JSON NOT NULL,
 code VARCHAR,
 short_text VARCHAR,
 description VARCHAR,
 UNIQUE(profile,raw_sha256)
);
CREATE TABLE registry_lms18 (
 row_id BIGINT PRIMARY KEY,
 profile VARCHAR NOT NULL,
 raw_sha256 VARCHAR NOT NULL,
 field_states JSON NOT NULL,
 code VARCHAR,
 dispensing_group VARCHAR,
 short_text VARCHAR,
 description VARCHAR,
 legacy_05 VARCHAR,
 UNIQUE(profile,raw_sha256)
);
CREATE TABLE registry_lms19 (
 row_id BIGINT PRIMARY KEY,
 profile VARCHAR NOT NULL,
 raw_sha256 VARCHAR NOT NULL,
 field_states JSON NOT NULL,
 code VARCHAR,
 short_text VARCHAR,
 description VARCHAR,
 UNIQUE(profile,raw_sha256)
);
CREATE TABLE registry_lms20 (
 row_id BIGINT PRIMARY KEY,
 profile VARCHAR NOT NULL,
 raw_sha256 VARCHAR NOT NULL,
 field_states JSON NOT NULL,
 code VARCHAR,
 short_text VARCHAR,
 description VARCHAR,
 UNIQUE(profile,raw_sha256)
);
CREATE TABLE registry_lms21 (
 row_id BIGINT PRIMARY KEY,
 profile VARCHAR NOT NULL,
 raw_sha256 VARCHAR NOT NULL,
 field_states JSON NOT NULL,
 drug_id VARCHAR,
 full_name VARCHAR,
 UNIQUE(profile,raw_sha256)
);
CREATE TABLE registry_lms22 (
 row_id BIGINT PRIMARY KEY,
 profile VARCHAR NOT NULL,
 raw_sha256 VARCHAR NOT NULL,
 field_states JSON NOT NULL,
 code VARCHAR,
 description VARCHAR,
 active_code VARCHAR,
 UNIQUE(profile,raw_sha256)
);
CREATE TABLE registry_lms23 (
 row_id BIGINT PRIMARY KEY,
 profile VARCHAR NOT NULL,
 raw_sha256 VARCHAR NOT NULL,
 field_states JSON NOT NULL,
 patient_type VARCHAR,
 level_code VARCHAR,
 lower_ore BIGINT,
 upper_ore BIGINT,
 percent_token VARCHAR,
 UNIQUE(profile,raw_sha256)
);
CREATE TABLE registry_lms24 (
 row_id BIGINT PRIMARY KEY,
 profile VARCHAR NOT NULL,
 raw_sha256 VARCHAR NOT NULL,
 field_states JSON NOT NULL,
 drug_id VARCHAR,
 package_number VARCHAR,
 drug_group VARCHAR,
 minimum_aip_unit_ore BIGINT,
 minimum_registered_unit_ore BIGINT,
 reimbursement_unit_ore BIGINT,
 abc_code VARCHAR,
 cheapest_drug VARCHAR,
 UNIQUE(profile,raw_sha256)
);
CREATE TABLE registry_lms25 (
 row_id BIGINT PRIMARY KEY,
 profile VARCHAR NOT NULL,
 raw_sha256 VARCHAR NOT NULL,
 field_states JSON NOT NULL,
 atc VARCHAR,
 indication_code VARCHAR,
 drug_id VARCHAR,
 UNIQUE(profile,raw_sha256)
);
CREATE TABLE registry_lms26 (
 row_id BIGINT PRIMARY KEY,
 profile VARCHAR NOT NULL,
 raw_sha256 VARCHAR NOT NULL,
 field_states JSON NOT NULL,
 code VARCHAR,
 description VARCHAR,
 line_1 VARCHAR,
 line_2 VARCHAR,
 line_3 VARCHAR,
 active_code VARCHAR,
 UNIQUE(profile,raw_sha256)
);
CREATE TABLE registry_lms27 (
 row_id BIGINT PRIMARY KEY,
 profile VARCHAR NOT NULL,
 raw_sha256 VARCHAR NOT NULL,
 field_states JSON NOT NULL,
 drug_id VARCHAR,
 dosage_code VARCHAR,
 UNIQUE(profile,raw_sha256)
);
CREATE TABLE registry_lms28 (
 row_id BIGINT PRIMARY KEY,
 profile VARCHAR NOT NULL,
 raw_sha256 VARCHAR NOT NULL,
 field_states JSON NOT NULL,
 code VARCHAR,
 short_text VARCHAR,
 description VARCHAR,
 units_per_day_token VARCHAR,
 line_1 VARCHAR,
 line_2 VARCHAR,
 line_3 VARCHAR,
 active_code VARCHAR,
 UNIQUE(profile,raw_sha256)
);
CREATE TABLE registry_lms29 (
 row_id BIGINT PRIMARY KEY,
 profile VARCHAR NOT NULL,
 raw_sha256 VARCHAR NOT NULL,
 field_states JSON NOT NULL,
 recommendation_group VARCHAR,
 drug_id VARCHAR,
 package_number VARCHAR,
 recommendation VARCHAR,
 UNIQUE(profile,raw_sha256)
);
CREATE TABLE registry_lms30 (
 row_id BIGINT PRIMARY KEY,
 profile VARCHAR NOT NULL,
 raw_sha256 VARCHAR NOT NULL,
 field_states JSON NOT NULL,
 drug_id VARCHAR,
 package_number VARCHAR,
 substance_class VARCHAR,
 substance_group VARCHAR,
 substance VARCHAR,
 UNIQUE(profile,raw_sha256)
);
CREATE TABLE registry_lms31 (
 row_id BIGINT PRIMARY KEY,
 profile VARCHAR NOT NULL,
 raw_sha256 VARCHAR NOT NULL,
 field_states JSON NOT NULL,
 drug_id VARCHAR,
 package_number VARCHAR,
 consumer_unit_token VARCHAR,
 consumer_ddd_token VARCHAR,
 cheapest_flag VARCHAR,
 UNIQUE(profile,raw_sha256)
);
CREATE TABLE registry_lms32 (
 row_id BIGINT PRIMARY KEY,
 profile VARCHAR NOT NULL,
 raw_sha256 VARCHAR NOT NULL,
 field_states JSON NOT NULL,
 ordered_package VARCHAR,
 substitute_package VARCHAR,
 alternative_package VARCHAR,
 package_count_token VARCHAR,
 combination_consumer_token VARCHAR,
 information_flag VARCHAR,
 UNIQUE(profile,raw_sha256)
);
CREATE TABLE registry_lms33 (
 row_id BIGINT PRIMARY KEY,
 profile VARCHAR NOT NULL,
 raw_sha256 VARCHAR NOT NULL,
 field_states JSON NOT NULL,
 ordered_package VARCHAR,
 substitute_package VARCHAR,
 alternative_package VARCHAR,
 package_count_token VARCHAR,
 reserved_price_token VARCHAR,
 information_flag VARCHAR,
 UNIQUE(profile,raw_sha256)
);
CREATE TABLE registry_lms34 (
 row_id BIGINT PRIMARY KEY,
 profile VARCHAR NOT NULL,
 raw_sha256 VARCHAR NOT NULL,
 field_states JSON NOT NULL,
 drug_id VARCHAR,
 package_number VARCHAR,
 patented_flag VARCHAR,
 indication_group VARCHAR,
 UNIQUE(profile,raw_sha256)
);
CREATE TABLE registry_lms35 (
 row_id BIGINT PRIMARY KEY,
 profile VARCHAR NOT NULL,
 raw_sha256 VARCHAR NOT NULL,
 field_states JSON NOT NULL,
 indication_group VARCHAR,
 description VARCHAR,
 UNIQUE(profile,raw_sha256)
);
CREATE TABLE registry_lms38 (
 row_id BIGINT PRIMARY KEY,
 profile VARCHAR NOT NULL,
 raw_sha256 VARCHAR NOT NULL,
 field_states JSON NOT NULL,
 drug_id VARCHAR,
 package_number VARCHAR,
 drug_group VARCHAR,
 reserved_minimum_aip_unit_ore VARCHAR,
 reserved_minimum_registered_unit_ore VARCHAR,
 reserved_reimbursement_unit_ore VARCHAR,
 abc_code VARCHAR,
 cheapest_drug VARCHAR,
 UNIQUE(profile,raw_sha256)
);
