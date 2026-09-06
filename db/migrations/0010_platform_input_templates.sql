-- v0010 platform input template catalog
--
-- These rows are local references used by the renderer's platform-template
-- picker.  They do not mirror or mutate a third-party platform catalog.

CREATE TABLE platform_input_templates (
    platform_template_key     TEXT PRIMARY KEY,
    input_type                TEXT NOT NULL CHECK (input_type IN ('paper', 'textbook', 'vocabulary')),
    name                      TEXT NOT NULL CHECK (length(name) > 0),
    platform_template_id      TEXT,
    platform_template_version TEXT,
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL,
    archived_at               TEXT
);

CREATE UNIQUE INDEX ux_platform_input_templates_active_name
    ON platform_input_templates(input_type, name)
    WHERE archived_at IS NULL;

CREATE INDEX idx_platform_input_templates_type_active
    ON platform_input_templates(input_type, archived_at, updated_at DESC, platform_template_key);
