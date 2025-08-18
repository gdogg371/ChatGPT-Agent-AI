-- Version: 2.1
-- Created: 2025-08-12
-- Description: Dense vector storage for introspection_index; optional VSS if available.
BEGIN;

CREATE TABLE IF NOT EXISTS introspection_index_embeddings (
  id         INTEGER PRIMARY KEY,
  item_id    INTEGER NOT NULL,
  model      TEXT    NOT NULL,
  dim        INTEGER NOT NULL,
  embedding  BLOB    NOT NULL,
  note       TEXT,
  created_at TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT fk_ix_embed_item FOREIGN KEY (item_id)
    REFERENCES introspection_index(id) ON DELETE CASCADE,
  CONSTRAINT uq_ix_embed_item_model UNIQUE (item_id, model)
);

CREATE INDEX IF NOT EXISTS idx_ix_embed_model ON introspection_index_embeddings(model);
CREATE INDEX IF NOT EXISTS idx_ix_embed_dim   ON introspection_index_embeddings(dim);

-- Optional VSS (requires sqlite-vss extension):
-- CREATE VIRTUAL TABLE IF NOT EXISTS introspection_index_embeddings_vss USING vss0(embedding(dim=1536));
-- INSERT INTO introspection_index_embeddings_vss(rowid, embedding)
--   SELECT id, embedding FROM introspection_index_embeddings;

COMMIT;
