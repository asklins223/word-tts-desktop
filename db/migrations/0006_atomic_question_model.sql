-- v0006 atomic_question_model
-- 原子小题规范化模型（方案 docs/atomic-question-model-plan.md 第 7.1 节）：
-- 文档身份/版本、章节/题组、题目及 revision、共享材料及 revision、
-- 题目部件与材料关系、学习内容、operation plan/scope/task 及目标关系、
-- revision match 裁决记录与 legacy 会话。
--
-- 约定：
-- * 逻辑身份（question_id/stimulus_id/content_unit_id）与内容版本
--   （*_revisions，内容寻址）严格分离；revision 行不可覆盖，正文变化
--   产生新 revision。
-- * target_kind + target_id 的多态引用无法用单个外键约束（方案风险 17），
--   除 CHECK 枚举外由 repository 与测试负责按类型校验。
-- * 本迁移只建表，不回填历史数据；回填与双写桥接按方案 7.1.1 顺序另做。

-- ============================================================
-- 文档身份与版本
-- ============================================================

CREATE TABLE source_documents (
    source_document_id TEXT PRIMARY KEY,
    logical_key        TEXT NOT NULL CHECK (length(logical_key) > 0),
    business_scope     TEXT NOT NULL DEFAULT 'local' CHECK (length(business_scope) > 0),
    source_type        TEXT NOT NULL CHECK (
        source_type IN ('exam_paper', 'textbook', 'worksheet', 'import', 'other')
    ),
    display_name       TEXT,
    created_at         TEXT NOT NULL,
    UNIQUE (business_scope, logical_key)
);

CREATE TABLE document_revisions (
    document_revision_id TEXT PRIMARY KEY,
    source_document_id   TEXT NOT NULL,
    source_artifact_id   TEXT,
    file_hash            TEXT NOT NULL CHECK (length(file_hash) > 0),
    parser_version       INTEGER NOT NULL CHECK (parser_version >= 0),
    schema_version       INTEGER NOT NULL CHECK (schema_version >= 0),
    created_at           TEXT NOT NULL,
    -- 同一文件 + 同一解析版本幂等重放
    UNIQUE (source_document_id, file_hash, parser_version, schema_version),
    FOREIGN KEY (source_document_id)
        REFERENCES source_documents(source_document_id) ON DELETE RESTRICT
);

-- ============================================================
-- 章节 / 题组
-- ============================================================

CREATE TABLE major_sections (
    major_section_id     TEXT PRIMARY KEY,
    document_revision_id TEXT NOT NULL,
    local_key            TEXT NOT NULL CHECK (length(local_key) > 0),
    ordinal              INTEGER NOT NULL CHECK (ordinal >= 0),
    title                TEXT,
    source_locator       TEXT NOT NULL,
    UNIQUE (document_revision_id, local_key),
    FOREIGN KEY (document_revision_id)
        REFERENCES document_revisions(document_revision_id) ON DELETE RESTRICT
);

CREATE TABLE question_groups (
    question_group_id    TEXT PRIMARY KEY,
    document_revision_id TEXT NOT NULL,
    major_section_id     TEXT,
    local_key            TEXT NOT NULL CHECK (length(local_key) > 0),
    ordinal              INTEGER NOT NULL CHECK (ordinal >= 0),
    source_locator       TEXT NOT NULL,
    UNIQUE (document_revision_id, local_key),
    FOREIGN KEY (document_revision_id)
        REFERENCES document_revisions(document_revision_id) ON DELETE RESTRICT,
    FOREIGN KEY (major_section_id)
        REFERENCES major_sections(major_section_id) ON DELETE RESTRICT
);

-- ============================================================
-- 原子小题及版本
-- ============================================================

CREATE TABLE question_items (
    question_id         TEXT PRIMARY KEY,
    source_document_id  TEXT NOT NULL,
    type_code           TEXT NOT NULL CHECK (length(type_code) > 0),
    current_revision_id TEXT,
    created_at          TEXT NOT NULL,
    FOREIGN KEY (source_document_id)
        REFERENCES source_documents(source_document_id) ON DELETE RESTRICT
);

CREATE TABLE question_revisions (
    question_revision_id TEXT PRIMARY KEY,
    question_id          TEXT NOT NULL,
    document_revision_id TEXT NOT NULL,
    stem                 TEXT NOT NULL CHECK (length(stem) > 0),
    options_json         TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(options_json)),
    answer_json          TEXT CHECK (answer_json IS NULL OR json_valid(answer_json)),
    question_number      INTEGER,
    number_inferred      INTEGER NOT NULL DEFAULT 0 CHECK (number_inferred IN (0, 1)),
    section              TEXT,
    source_locator       TEXT NOT NULL,
    resolution_state     TEXT NOT NULL CHECK (
        resolution_state IN ('DRAFT', 'CANDIDATE', 'AMBIGUOUS', 'UNRESOLVED',
                             'CONFIRMED', 'REJECTED')
    ),
    content_hash         TEXT NOT NULL CHECK (length(content_hash) = 64),
    created_at           TEXT NOT NULL,
    -- 同一题目的 revision 不可覆盖；同内容幂等重放
    UNIQUE (question_id, content_hash),
    FOREIGN KEY (question_id)
        REFERENCES question_items(question_id) ON DELETE RESTRICT,
    FOREIGN KEY (document_revision_id)
        REFERENCES document_revisions(document_revision_id) ON DELETE RESTRICT
);

CREATE TABLE question_parts (
    part_id              TEXT PRIMARY KEY,
    question_revision_id TEXT NOT NULL,
    part_key             TEXT NOT NULL CHECK (length(part_key) > 0),
    ordinal              INTEGER NOT NULL CHECK (ordinal >= 0),
    stem                 TEXT,
    options_json         TEXT CHECK (options_json IS NULL OR json_valid(options_json)),
    answer_json          TEXT CHECK (answer_json IS NULL OR json_valid(answer_json)),
    UNIQUE (question_revision_id, part_key),
    FOREIGN KEY (question_revision_id)
        REFERENCES question_revisions(question_revision_id) ON DELETE RESTRICT
);

-- ============================================================
-- 共享材料及版本
-- ============================================================

CREATE TABLE stimuli (
    stimulus_id         TEXT PRIMARY KEY,
    source_document_id  TEXT NOT NULL,
    stimulus_type       TEXT NOT NULL CHECK (length(stimulus_type) > 0),
    current_revision_id TEXT,
    created_at          TEXT NOT NULL,
    FOREIGN KEY (source_document_id)
        REFERENCES source_documents(source_document_id) ON DELETE RESTRICT
);

CREATE TABLE stimulus_revisions (
    stimulus_revision_id TEXT PRIMARY KEY,
    stimulus_id          TEXT NOT NULL,
    document_revision_id TEXT NOT NULL,
    text                 TEXT NOT NULL CHECK (length(text) > 0),
    section              TEXT,
    source_locator       TEXT NOT NULL,
    content_hash         TEXT NOT NULL CHECK (length(content_hash) = 64),
    created_at           TEXT NOT NULL,
    UNIQUE (stimulus_id, content_hash),
    FOREIGN KEY (stimulus_id)
        REFERENCES stimuli(stimulus_id) ON DELETE RESTRICT,
    FOREIGN KEY (document_revision_id)
        REFERENCES document_revisions(document_revision_id) ON DELETE RESTRICT
);

CREATE TABLE question_stimuli (
    link_id              TEXT PRIMARY KEY,
    question_revision_id TEXT NOT NULL,
    stimulus_revision_id TEXT NOT NULL,
    relation_type        TEXT NOT NULL CHECK (
        relation_type IN ('references', 'context', 'answer_material')
    ),
    ordinal              INTEGER NOT NULL DEFAULT 0 CHECK (ordinal >= 0),
    fragment_locator     TEXT,
    UNIQUE (question_revision_id, stimulus_revision_id, relation_type),
    FOREIGN KEY (question_revision_id)
        REFERENCES question_revisions(question_revision_id) ON DELETE RESTRICT,
    FOREIGN KEY (stimulus_revision_id)
        REFERENCES stimulus_revisions(stimulus_revision_id) ON DELETE RESTRICT
);

-- ============================================================
-- 非考试学习内容
-- ============================================================

CREATE TABLE content_units (
    content_unit_id     TEXT PRIMARY KEY,
    source_document_id  TEXT NOT NULL,
    content_kind        TEXT NOT NULL CHECK (length(content_kind) > 0),
    unit_kind           TEXT NOT NULL DEFAULT 'LEARNING_CONTENT' CHECK (
        unit_kind IN ('LEARNING_CONTENT', 'QUESTION', 'STIMULUS', 'OTHER')
    ),
    current_revision_id TEXT,
    created_at          TEXT NOT NULL,
    FOREIGN KEY (source_document_id)
        REFERENCES source_documents(source_document_id) ON DELETE RESTRICT
);

CREATE TABLE content_unit_revisions (
    content_unit_revision_id TEXT PRIMARY KEY,
    content_unit_id          TEXT NOT NULL,
    document_revision_id     TEXT NOT NULL,
    text                     TEXT NOT NULL CHECK (length(text) > 0),
    section                  TEXT,
    discourse_number         INTEGER,
    sentence_number          INTEGER,
    entry_number             INTEGER,
    source_locator           TEXT NOT NULL,
    content_hash             TEXT NOT NULL CHECK (length(content_hash) = 64),
    created_at               TEXT NOT NULL,
    UNIQUE (content_unit_id, content_hash),
    FOREIGN KEY (content_unit_id)
        REFERENCES content_units(content_unit_id) ON DELETE RESTRICT,
    FOREIGN KEY (document_revision_id)
        REFERENCES document_revisions(document_revision_id) ON DELETE RESTRICT
);

-- ============================================================
-- 操作计划 / 不可变范围 / 统一任务
-- ============================================================

CREATE TABLE operation_plans (
    plan_id              TEXT PRIMARY KEY,
    source_document_id   TEXT NOT NULL,
    document_revision_id TEXT NOT NULL,
    workflow_group_id    TEXT,
    workflow_id          TEXT,
    configuration_json   TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(configuration_json)),
    created_at           TEXT NOT NULL,
    FOREIGN KEY (source_document_id)
        REFERENCES source_documents(source_document_id) ON DELETE RESTRICT,
    FOREIGN KEY (document_revision_id)
        REFERENCES document_revisions(document_revision_id) ON DELETE RESTRICT
);

CREATE TABLE operation_scopes (
    scope_row_id   TEXT PRIMARY KEY,
    scope_id       TEXT NOT NULL CHECK (length(scope_id) > 0),
    scope_revision INTEGER NOT NULL CHECK (scope_revision >= 1),
    scope_kind     TEXT NOT NULL CHECK (
        scope_kind IN ('QUESTION', 'GROUP', 'STIMULUS', 'MAJOR_SECTION',
                       'DOCUMENT', 'CONTENT_UNIT')
    ),
    plan_id        TEXT,
    member_hash    TEXT,
    payload_hash   TEXT,
    created_at     TEXT NOT NULL,
    UNIQUE (scope_id, scope_revision),
    FOREIGN KEY (plan_id) REFERENCES operation_plans(plan_id) ON DELETE RESTRICT
);

CREATE TABLE operation_scope_members (
    member_id          TEXT PRIMARY KEY,
    scope_row_id       TEXT NOT NULL,
    target_kind        TEXT NOT NULL CHECK (
        target_kind IN ('QUESTION', 'STIMULUS', 'CONTENT_UNIT', 'GROUP',
                        'MAJOR_SECTION')
    ),
    target_id          TEXT NOT NULL CHECK (length(target_id) > 0),
    target_revision_id TEXT,
    ordinal            INTEGER NOT NULL CHECK (ordinal >= 0),
    UNIQUE (scope_row_id, target_kind, target_id, target_revision_id, ordinal),
    FOREIGN KEY (scope_row_id)
        REFERENCES operation_scopes(scope_row_id) ON DELETE RESTRICT
);

CREATE TABLE operation_tasks (
    operation_id       TEXT PRIMARY KEY,
    plan_id            TEXT NOT NULL,
    operation_type     TEXT NOT NULL CHECK (
        operation_type IN ('AUDIO_GENERATE', 'EXTERNAL_UPSERT', 'VALIDATE')
    ),
    scope_row_id       TEXT,
    workflow_step_id   TEXT UNIQUE,
    input_hash         TEXT,
    configuration_hash TEXT,
    adapter_version    TEXT,
    created_at         TEXT NOT NULL,
    FOREIGN KEY (plan_id)
        REFERENCES operation_plans(plan_id) ON DELETE RESTRICT,
    FOREIGN KEY (scope_row_id)
        REFERENCES operation_scopes(scope_row_id) ON DELETE RESTRICT
);

CREATE TABLE operation_task_targets (
    target_row_id      TEXT PRIMARY KEY,
    operation_id       TEXT NOT NULL,
    target_kind        TEXT NOT NULL CHECK (
        target_kind IN ('QUESTION', 'STIMULUS', 'CONTENT_UNIT', 'GROUP',
                        'MAJOR_SECTION')
    ),
    target_id          TEXT NOT NULL CHECK (length(target_id) > 0),
    target_revision_id TEXT,
    ordinal            INTEGER NOT NULL CHECK (ordinal >= 0),
    role               TEXT NOT NULL DEFAULT 'primary',
    UNIQUE (operation_id, target_kind, target_id, target_revision_id, role),
    FOREIGN KEY (operation_id)
        REFERENCES operation_tasks(operation_id) ON DELETE RESTRICT
);

CREATE TABLE operation_task_dependencies (
    dependency_id           TEXT PRIMARY KEY,
    operation_id            TEXT NOT NULL,
    depends_on_operation_id TEXT NOT NULL,
    dependency_type         TEXT NOT NULL CHECK (
        dependency_type IN ('AUDIO_ARTIFACT', 'SCOPE_INPUT', 'MANUAL')
    ),
    failure_policy          TEXT NOT NULL CHECK (
        failure_policy IN ('BLOCK', 'SKIP', 'CONTINUE')
    ),
    UNIQUE (operation_id, depends_on_operation_id),
    FOREIGN KEY (operation_id)
        REFERENCES operation_tasks(operation_id) ON DELETE RESTRICT,
    FOREIGN KEY (depends_on_operation_id)
        REFERENCES operation_tasks(operation_id) ON DELETE RESTRICT
);

-- ============================================================
-- revision 匹配裁决与 legacy 迁移
-- ============================================================

CREATE TABLE revision_match_decisions (
    decision_id              TEXT PRIMARY KEY,
    source_document_id       TEXT NOT NULL,
    from_document_revision_id TEXT,
    to_document_revision_id  TEXT NOT NULL,
    question_id              TEXT,
    decision                 TEXT NOT NULL CHECK (
        decision IN ('MATCHED', 'NEW', 'REMOVED', 'CHANGED', 'AMBIGUOUS')
    ),
    algorithm_version        TEXT NOT NULL CHECK (length(algorithm_version) > 0),
    candidates_json          TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(candidates_json)),
    resolved_by              TEXT,
    resolved_at              TEXT NOT NULL,
    UNIQUE (from_document_revision_id, to_document_revision_id, question_id,
            algorithm_version, decision),
    FOREIGN KEY (source_document_id)
        REFERENCES source_documents(source_document_id) ON DELETE RESTRICT
);

CREATE TABLE legacy_aliases (
    alias_id           TEXT PRIMARY KEY,
    alias_kind         TEXT NOT NULL CHECK (
        alias_kind IN ('WORK_ITEM', 'PROGRESS_ITEM', 'WORKS_ID', 'FILENAME', 'ITEM_ID')
    ),
    alias_value        TEXT NOT NULL CHECK (length(alias_value) > 0),
    target_kind        TEXT NOT NULL CHECK (
        target_kind IN ('QUESTION', 'STIMULUS', 'CONTENT_UNIT', 'WORK_ITEM', 'SCOPE')
    ),
    target_id          TEXT NOT NULL CHECK (length(target_id) > 0),
    target_revision_id TEXT,
    created_at         TEXT NOT NULL,
    UNIQUE (alias_kind, alias_value, target_kind, target_id)
);

CREATE TABLE legacy_execution_sessions (
    session_id           TEXT PRIMARY KEY,
    source_classification TEXT NOT NULL CHECK (
        source_classification IN ('LEGACY_OUT_OF_BAND', 'LEGACY_BRIDGED', 'IMPORTED')
    ),
    legacy_source        TEXT,
    bridge_version       TEXT,
    import_state         TEXT NOT NULL CHECK (
        import_state IN ('PENDING', 'IMPORTED', 'SKIPPED')
    ),
    recorded_at          TEXT NOT NULL
);

-- ============================================================
-- 索引
-- ============================================================

CREATE INDEX idx_document_revisions_source
    ON document_revisions(source_document_id);
CREATE INDEX idx_major_sections_revision
    ON major_sections(document_revision_id);
CREATE INDEX idx_question_items_source
    ON question_items(source_document_id, type_code);
CREATE INDEX idx_question_revisions_question
    ON question_revisions(question_id);
CREATE INDEX idx_question_revisions_revision_doc
    ON question_revisions(document_revision_id);
CREATE INDEX idx_stimuli_source
    ON stimuli(source_document_id);
CREATE INDEX idx_stimulus_revisions_doc
    ON stimulus_revisions(document_revision_id);
CREATE INDEX idx_question_stimuli_stimulus
    ON question_stimuli(stimulus_revision_id);
CREATE INDEX idx_content_units_source
    ON content_units(source_document_id, content_kind);
CREATE INDEX idx_content_unit_revisions_doc
    ON content_unit_revisions(document_revision_id);
CREATE INDEX idx_operation_scopes_plan
    ON operation_scopes(plan_id);
CREATE INDEX idx_operation_scope_members_scope
    ON operation_scope_members(scope_row_id);
CREATE INDEX idx_operation_tasks_plan
    ON operation_tasks(plan_id, operation_type);
CREATE INDEX idx_operation_task_targets_operation
    ON operation_task_targets(operation_id);
CREATE INDEX idx_operation_task_deps_upstream
    ON operation_task_dependencies(depends_on_operation_id);
CREATE INDEX idx_legacy_aliases_value
    ON legacy_aliases(alias_kind, alias_value);
