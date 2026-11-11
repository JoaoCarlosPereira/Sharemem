CREATE TABLE IF NOT EXISTS local_apps (
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

CREATE INDEX IF NOT EXISTS local_apps_labels_gin
    ON local_apps USING gin (labels);

CREATE INDEX IF NOT EXISTS local_apps_list_alive
    ON local_apps USING btree (namespace, name, tag, updated_at)
    WHERE deletion_timestamp IS NULL;

CREATE INDEX IF NOT EXISTS local_apps_tags_alive
    ON local_apps USING btree (namespace, name, updated_at DESC, tag DESC)
    WHERE deletion_timestamp IS NULL;

CREATE INDEX IF NOT EXISTS local_apps_terminating
    ON local_apps USING btree (deletion_timestamp)
    WHERE deletion_timestamp IS NOT NULL;

CREATE OR REPLACE TRIGGER local_apps_set_updated_at
    BEFORE UPDATE ON local_apps
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE OR REPLACE TRIGGER local_apps_notify_status
    AFTER INSERT OR UPDATE OR DELETE ON local_apps
    FOR EACH ROW EXECUTE FUNCTION notify_status_change('local_apps_status');
CREATE OR REPLACE TRIGGER local_apps_control_plane_event
    AFTER INSERT OR UPDATE OR DELETE ON local_apps
    FOR EACH ROW EXECUTE FUNCTION record_control_plane_event('LocalApp');

CREATE TABLE IF NOT EXISTS localapp_artifacts (
    namespace character varying(255) NOT NULL,
    name character varying(255) NOT NULL,
    tag character varying(255) NOT NULL,
    digest character(64) NOT NULL REFERENCES artifacts (digest),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    PRIMARY KEY (namespace, name, tag),
    FOREIGN KEY (namespace, name, tag)
        REFERENCES local_apps (namespace, name, tag)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS localapp_artifacts_digest
    ON localapp_artifacts USING btree (digest);

CREATE OR REPLACE TRIGGER localapp_artifacts_set_updated_at
    BEFORE UPDATE ON localapp_artifacts
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
