-- The official PDF distinguishes a reimbursement basis (TSP) from its dispensing
-- price (ESP). Preserve existing rows/IDs while adding that concrete price type.
-- DuckDB cannot alter a CHECK constraint in place.
CREATE TABLE price_observation_archive (
    observation_id UUID PRIMARY KEY,
    import_id UUID NOT NULL REFERENCES import_batch(import_id),
    delivery_id UUID NOT NULL REFERENCES source_delivery(delivery_id),
    package_id UUID NOT NULL REFERENCES package(package_id),
    mapping_id UUID NOT NULL REFERENCES package_identifier(mapping_id),
    source_locator VARCHAR NOT NULL CHECK (length(trim(source_locator)) > 0),
    identifier_namespace VARCHAR NOT NULL CHECK (identifier_namespace = 'dk:varenummer'),
    source_identifier VARCHAR NOT NULL CHECK (length(trim(source_identifier)) > 0),
    canonical_identifier VARCHAR NOT NULL CHECK (regexp_full_match(canonical_identifier, '[0-9]{6}')),
    effective_period DATE REFERENCES price_period(period_start),
    report_period DATE REFERENCES price_period(period_start),
    effective_date DATE,
    quality_state VARCHAR NOT NULL CHECK (quality_state IN ('valid', 'incomplete', 'invalid', 'conflicting')),
    field_presence JSON NOT NULL CHECK (json_type(field_presence) = 'OBJECT'),
    source_values JSON NOT NULL CHECK (json_type(source_values) = 'OBJECT'),
    price_type VARCHAR NOT NULL CHECK (price_type IN ('aip', 'registered', 'consumer', 'reimbursement_basis')),
    amount_ore BIGINT CHECK (amount_ore >= 0),
    currency VARCHAR NOT NULL CHECK (regexp_full_match(currency, '[A-Z]{3}')),
    price_basis VARCHAR NOT NULL CHECK (price_basis IN ('per_package', 'per_unit')),
    tax_basis VARCHAR NOT NULL CHECK (length(trim(tax_basis)) > 0),
    fee_basis VARCHAR NOT NULL CHECK (length(trim(fee_basis)) > 0),
    price_role VARCHAR NOT NULL CHECK (price_role IN ('effective_price', 'reported_previous_price')),
    CHECK (amount_ore IS NOT NULL OR (json_extract_string(field_presence, '$.amount_ore') IS NOT NULL AND json_extract_string(field_presence, '$.amount_ore') IN ('blank', 'unreported', 'missing', 'withdrawn', 'invalid', 'conflicting')))
);
INSERT INTO price_observation_archive SELECT * FROM price_observation;
DROP TABLE price_observation;
ALTER TABLE price_observation_archive RENAME TO price_observation;
