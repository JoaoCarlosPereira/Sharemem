CREATE TABLE IF NOT EXISTS scripts (
    namespace character varying(255) NOT NULL,
    name character varying(255) NOT NULL,
    tag character varying(255) NOT NULL,
    uid uuid DEFAULT gen_random_uuid() NOT NULL,
    generation bigint DEFAULT 1 NOT NULL,
    labels jsonb DEFAULT '{}'::jsonb NOT NULL,
    annotations jsonb DEFAULT '{}'::jsonb NOT NULL,
    spec jsonb NOT NULL,
    content_hash character(64) NOT NULL,
    status jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deletion_timestamp timestamp with time zone,
    PRIMARY KEY (namespace, name, tag)
);

CREATE INDEX IF NOT EXISTS scripts_labels_gin
    ON scripts USING gin (labels);

CREATE INDEX IF NOT EXISTS scripts_list_alive
    ON scripts USING btree (namespace, name, tag, updated_at)
    WHERE deletion_timestamp IS NULL;

CREATE INDEX IF NOT EXISTS scripts_tags_alive
    ON scripts USING btree (namespace, name, updated_at DESC, tag DESC)
    WHERE deletion_timestamp IS NULL;

CREATE INDEX IF NOT EXISTS scripts_terminating
    ON scripts USING btree (deletion_timestamp)
    WHERE deletion_timestamp IS NOT NULL;

CREATE OR REPLACE TRIGGER scripts_set_updated_at
    BEFORE UPDATE ON scripts
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE OR REPLACE TRIGGER scripts_notify_status
    AFTER INSERT OR UPDATE OR DELETE ON scripts
    FOR EACH ROW EXECUTE FUNCTION notify_status_change('scripts_status');
CREATE OR REPLACE TRIGGER scripts_control_plane_event
    AFTER INSERT OR UPDATE OR DELETE ON scripts
    FOR EACH ROW EXECUTE FUNCTION record_control_plane_event('Script');
