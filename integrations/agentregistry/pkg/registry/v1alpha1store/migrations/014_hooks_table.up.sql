-- Hooks: standalone, publishable lifecycle hooks (the unit a Plugin bundles),
-- a content-registry kind keyed by (namespace, name, tag) and immutable by
-- tag. Shape mirrors the plugins/skills content tables (010) and wires the
-- same updated-at, status-notify, and control-plane-event triggers (009) so
-- the controller observes hook changes like every other kind.
--
-- hook_artifacts mirrors skill_artifacts (013): hooks that ship executable
-- files associate one immutable tar.gz per tag, reusing the shared,
-- content-addressed `artifacts` table.

CREATE TABLE IF NOT EXISTS hooks (
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

-- list by labels
CREATE INDEX IF NOT EXISTS hooks_labels_gin
    ON hooks USING gin (labels);

-- list live hook rows
CREATE INDEX IF NOT EXISTS hooks_list_alive
    ON hooks USING btree (namespace, name, tag, updated_at)
    WHERE deletion_timestamp IS NULL;

-- list tags for one hook
CREATE INDEX IF NOT EXISTS hooks_tags_alive
    ON hooks USING btree (namespace, name, updated_at DESC, tag DESC)
    WHERE deletion_timestamp IS NULL;

-- purge terminating rows
CREATE INDEX IF NOT EXISTS hooks_terminating
    ON hooks USING btree (deletion_timestamp)
    WHERE deletion_timestamp IS NOT NULL;

CREATE OR REPLACE TRIGGER hooks_set_updated_at
    BEFORE UPDATE ON hooks
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE OR REPLACE TRIGGER hooks_notify_status
    AFTER INSERT OR UPDATE OR DELETE ON hooks
    FOR EACH ROW EXECUTE FUNCTION notify_status_change('hooks_status');
CREATE OR REPLACE TRIGGER hooks_control_plane_event
    AFTER INSERT OR UPDATE OR DELETE ON hooks
    FOR EACH ROW EXECUTE FUNCTION record_control_plane_event('Hook');

CREATE TABLE IF NOT EXISTS hook_artifacts (
    namespace character varying(255) NOT NULL,
    name character varying(255) NOT NULL,
    tag character varying(255) NOT NULL,
    digest character(64) NOT NULL REFERENCES artifacts (digest),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    PRIMARY KEY (namespace, name, tag),
    FOREIGN KEY (namespace, name, tag)
        REFERENCES hooks (namespace, name, tag)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS hook_artifacts_digest
    ON hook_artifacts USING btree (digest);

CREATE OR REPLACE TRIGGER hook_artifacts_set_updated_at
    BEFORE UPDATE ON hook_artifacts
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
