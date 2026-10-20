CREATE TABLE price_period (
    period_start DATE PRIMARY KEY,
    period_end_exclusive DATE NOT NULL,
    CHECK (period_end_exclusive > period_start)
);

CREATE TABLE package (
    package_id UUID PRIMARY KEY
);

CREATE TABLE source_delivery (
    delivery_id UUID PRIMARY KEY,
    source VARCHAR NOT NULL CHECK (length(trim(source)) > 0),
    receipt_key VARCHAR NOT NULL CHECK (length(trim(receipt_key)) > 0),
    metadata_hash VARCHAR NOT NULL CHECK (regexp_full_match(metadata_hash, '[0-9a-f]{64}')),
    original_filename VARCHAR NOT NULL CHECK (length(original_filename) > 0),
    original_uri VARCHAR,
    content_hash VARCHAR NOT NULL CHECK (regexp_full_match(content_hash, '[0-9a-f]{64}')),
    archive_location VARCHAR NOT NULL,
    byte_count BIGINT NOT NULL CHECK (byte_count >= 0),
    received_lower TIMESTAMPTZ,
    received_upper TIMESTAMPTZ,
    received_precision VARCHAR NOT NULL,
    received_basis VARCHAR,
    received_timezone VARCHAR,
    published_lower TIMESTAMPTZ,
    published_upper TIMESTAMPTZ,
    published_precision VARCHAR NOT NULL,
    published_basis VARCHAR,
    published_timezone VARCHAR,
    edition VARCHAR,
    phase VARCHAR,
    current_period DATE REFERENCES price_period(period_start),
    target_period DATE REFERENCES price_period(period_start),
    coverage JSON NOT NULL CHECK (json_type(coverage) = 'OBJECT'),
    replacement JSON NOT NULL CHECK (json_type(replacement) = 'OBJECT'),
    registered_at TIMESTAMPTZ NOT NULL,
    UNIQUE (source, receipt_key),
    CHECK (archive_location = 'sha256/' || substr(content_hash, 1, 2) || '/' || content_hash),
    CHECK (
        (received_precision = 'unknown' AND received_lower IS NULL AND received_upper IS NULL)
        OR (received_precision IN ('instant', 'day', 'range')
            AND received_lower IS NOT NULL AND received_upper IS NOT NULL
            AND received_lower <= received_upper
            AND received_basis IS NOT NULL AND length(trim(received_basis)) > 0
            AND received_timezone IS NOT NULL AND length(trim(received_timezone)) > 0
            AND ((received_precision = 'instant' AND received_lower = received_upper)
                 OR (received_precision != 'instant' AND received_lower < received_upper)))
    ),
    CHECK (
        (published_precision = 'unknown' AND published_lower IS NULL AND published_upper IS NULL)
        OR (published_precision IN ('instant', 'day', 'range')
            AND published_lower IS NOT NULL AND published_upper IS NOT NULL
            AND published_lower <= published_upper
            AND published_basis IS NOT NULL AND length(trim(published_basis)) > 0
            AND published_timezone IS NOT NULL AND length(trim(published_timezone)) > 0
            AND ((published_precision = 'instant' AND published_lower = published_upper)
                 OR (published_precision != 'instant' AND published_lower < published_upper)))
    )
);

CREATE TABLE import_batch (
    import_id UUID PRIMARY KEY,
    delivery_id UUID NOT NULL REFERENCES source_delivery(delivery_id),
    parser_version VARCHAR NOT NULL CHECK (length(trim(parser_version)) > 0),
    schema_version VARCHAR NOT NULL CHECK (length(trim(schema_version)) > 0),
    mapping_version VARCHAR NOT NULL CHECK (length(trim(mapping_version)) > 0),
    interpretation_context JSON NOT NULL CHECK (json_type(interpretation_context) = 'OBJECT'),
    interpretation_context_hash VARCHAR NOT NULL
        CHECK (regexp_full_match(interpretation_context_hash, '[0-9a-f]{64}')),
    processing_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    validation_status VARCHAR NOT NULL
        CHECK (validation_status IN ('running', 'accepted', 'rejected', 'failed')),
    record_count BIGINT,
    accepted_count BIGINT,
    rejected_count BIGINT,
    UNIQUE (delivery_id, parser_version, schema_version, mapping_version, interpretation_context_hash),
    CHECK (
        (validation_status = 'running' AND completed_at IS NULL AND record_count IS NULL
            AND accepted_count IS NULL AND rejected_count IS NULL)
        OR (validation_status != 'running' AND completed_at IS NOT NULL
            AND completed_at >= processing_at
            AND record_count IS NOT NULL AND accepted_count IS NOT NULL AND rejected_count IS NOT NULL
            AND record_count >= 0 AND accepted_count >= 0 AND rejected_count >= 0
            AND accepted_count + rejected_count = record_count)
    )
);

CREATE TABLE package_identifier (
    mapping_id UUID PRIMARY KEY,
    package_id UUID REFERENCES package(package_id),
    namespace VARCHAR NOT NULL CHECK (length(trim(namespace)) > 0),
    identifier_value VARCHAR NOT NULL CHECK (length(trim(identifier_value)) > 0),
    canonical_value VARCHAR NOT NULL CHECK (length(trim(canonical_value)) > 0),
    validity_kind VARCHAR NOT NULL CHECK (validity_kind IN ('unknown', 'interval', 'scope')),
    valid_from DATE,
    valid_to DATE,
    scope_key VARCHAR,
    evidence_import_id UUID NOT NULL REFERENCES import_batch(import_id),
    source_locator VARCHAR NOT NULL CHECK (length(trim(source_locator)) > 0),
    mapping_version VARCHAR NOT NULL CHECK (length(trim(mapping_version)) > 0),
    available_lower TIMESTAMPTZ,
    available_upper TIMESTAMPTZ,
    available_precision VARCHAR NOT NULL,
    available_basis VARCHAR,
    available_timezone VARCHAR,
    CHECK (namespace != 'dk:varenummer' OR
        (regexp_full_match(identifier_value, '[0-9]{1,6}')
         AND regexp_full_match(canonical_value, '[0-9]{6}')
         AND canonical_value = lpad(identifier_value, 6, '0'))),
    CHECK (
        (validity_kind = 'unknown' AND valid_from IS NULL AND valid_to IS NULL AND scope_key IS NULL)
        OR (validity_kind = 'interval' AND valid_from IS NOT NULL AND valid_to IS NOT NULL
            AND valid_from < valid_to AND scope_key IS NULL)
        OR (validity_kind = 'scope' AND valid_from IS NULL AND valid_to IS NULL
            AND scope_key IS NOT NULL AND length(trim(scope_key)) > 0)
    ),
    CHECK (
        (available_precision = 'unknown' AND available_lower IS NULL AND available_upper IS NULL)
        OR (available_precision IN ('instant', 'day', 'range')
            AND available_lower IS NOT NULL AND available_upper IS NOT NULL
            AND available_lower <= available_upper
            AND available_basis IS NOT NULL AND length(trim(available_basis)) > 0
            AND available_timezone IS NOT NULL AND length(trim(available_timezone)) > 0
            AND ((available_precision = 'instant' AND available_lower = available_upper)
                 OR (available_precision != 'instant' AND available_lower < available_upper)))
    )
);

CREATE TABLE import_issue (
    issue_id UUID PRIMARY KEY,
    import_id UUID NOT NULL REFERENCES import_batch(import_id),
    source_locator VARCHAR NOT NULL CHECK (length(trim(source_locator)) > 0),
    identifier VARCHAR,
    field VARCHAR,
    period DATE REFERENCES price_period(period_start),
    code VARCHAR NOT NULL CHECK (length(trim(code)) > 0),
    severity VARCHAR NOT NULL CHECK (severity IN ('info', 'warning', 'error')),
    blocking_scope VARCHAR NOT NULL
        CHECK (blocking_scope IN ('none', 'field', 'identifier', 'period', 'import')),
    CHECK (blocking_scope = 'none' OR severity = 'error'),
    CHECK (blocking_scope != 'field' OR (field IS NOT NULL AND length(trim(field)) > 0)),
    CHECK (blocking_scope != 'identifier' OR
        (identifier IS NOT NULL AND length(trim(identifier)) > 0)),
    CHECK (blocking_scope != 'period' OR period IS NOT NULL)
);
