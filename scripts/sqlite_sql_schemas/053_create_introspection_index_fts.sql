-- Version: 1.1
-- Created: 2025-08-12
-- Description: FTS5 mirror over introspection_index with change-tracking triggers.
BEGIN;

CREATE VIRTUAL TABLE IF NOT EXISTS introspection_index_fts
USING fts5(
  filepath,
  name,
  symbol_type,
  description,
  ag_tag,
  route_method,
  route_path,
  relation_type,
  target_symbol,
  content='introspection_index',
  content_rowid='id',
  tokenize='unicode61'
);

DROP TRIGGER IF EXISTS trg_ix_fts_ai;
DROP TRIGGER IF EXISTS trg_ix_fts_au;
DROP TRIGGER IF EXISTS trg_ix_fts_ad;

CREATE TRIGGER trg_ix_fts_ai AFTER INSERT ON introspection_index BEGIN
  INSERT INTO introspection_index_fts(rowid, filepath, name, symbol_type, description, ag_tag, route_method, route_path, relation_type, target_symbol)
  VALUES (new.id, new.filepath, new.name, new.symbol_type, new.description, new.ag_tag, new.route_method, new.route_path, new.relation_type, new.target_symbol);
END;

CREATE TRIGGER trg_ix_fts_au AFTER UPDATE ON introspection_index BEGIN
  DELETE FROM introspection_index_fts WHERE rowid = old.id;
  INSERT INTO introspection_index_fts(rowid, filepath, name, symbol_type, description, ag_tag, route_method, route_path, relation_type, target_symbol)
  VALUES (new.id, new.filepath, new.name, new.symbol_type, new.description, new.ag_tag, new.route_method, new.route_path, new.relation_type, new.target_symbol);
END;

CREATE TRIGGER trg_ix_fts_ad AFTER DELETE ON introspection_index BEGIN
  DELETE FROM introspection_index_fts WHERE rowid = old.id;
END;

COMMIT;
