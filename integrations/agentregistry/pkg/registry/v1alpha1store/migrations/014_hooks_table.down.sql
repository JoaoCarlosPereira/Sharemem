-- Reverses 014_hooks_table.up.sql. Dropping the tables removes their indexes
-- and triggers; the shared trigger functions (set_updated_at,
-- notify_status_change, record_control_plane_event) and the content-addressed
-- `artifacts` table are owned by earlier migrations and left in place.
DROP INDEX IF EXISTS hook_artifacts_digest;
DROP TABLE IF EXISTS hook_artifacts;

DROP INDEX IF EXISTS hooks_terminating;
DROP INDEX IF EXISTS hooks_tags_alive;
DROP INDEX IF EXISTS hooks_list_alive;
DROP INDEX IF EXISTS hooks_labels_gin;

DROP TABLE IF EXISTS hooks;
