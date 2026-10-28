-- Add separately verified existing supplier observations to the frozen dataset.
CREATE TABLE registry_dataset_supply (
 dataset_id UUID NOT NULL REFERENCES analytical_dataset(dataset_id),
 import_id UUID NOT NULL REFERENCES import_batch(import_id),
 verification_delivery_id UUID NOT NULL REFERENCES source_delivery(delivery_id),
 input_hash VARCHAR NOT NULL,
 rows_hash VARCHAR NOT NULL,
 row_count BIGINT NOT NULL,
 selected BOOLEAN NOT NULL,
 reason VARCHAR,
 PRIMARY KEY(dataset_id,import_id)
);
