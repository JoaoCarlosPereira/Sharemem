-- Complete, registry-hosted Plugin bundles. Bytes remain deduplicated in the
-- shared artifacts table; this association pins one immutable bundle per
-- Plugin namespace/name/tag and follows the Plugin row lifecycle.
CREATE TABLE IF NOT EXISTS plugin_artifacts (
    namespace character varying(255) NOT NULL,
    name character varying(255) NOT NULL,
    tag character varying(255) NOT NULL,
    digest character(64) NOT NULL REFERENCES artifacts (digest),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    PRIMARY KEY (namespace, name, tag),
    FOREIGN KEY (namespace, name, tag)
        REFERENCES plugins (namespace, name, tag)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS plugin_artifacts_digest
    ON plugin_artifacts USING btree (digest);

CREATE OR REPLACE TRIGGER plugin_artifacts_set_updated_at
    BEFORE UPDATE ON plugin_artifacts
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
