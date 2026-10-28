-- F3 freezes a selection, not a decision/review workflow. Existing facts are untouched.
CREATE TABLE frozen_selection (
    selection_id UUID PRIMARY KEY,
    policy_version VARCHAR NOT NULL,
    configuration JSON NOT NULL CHECK (json_type(configuration) = 'OBJECT'),
    configuration_hash VARCHAR NOT NULL CHECK (length(configuration_hash) = 64),
    evidence JSON NOT NULL CHECK (json_type(evidence) = 'OBJECT'),
    evidence_hash VARCHAR NOT NULL CHECK (length(evidence_hash) = 64),
    produced_at TIMESTAMPTZ NOT NULL,
    decision_cutoff TIMESTAMPTZ NOT NULL,
    availability_mode VARCHAR NOT NULL
        CHECK (availability_mode IN ('live', 'historical_replay', 'reconstructed_research'))
);
