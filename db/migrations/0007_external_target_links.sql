-- v0007 external_target_links
-- 演进 0005 外部记录体系，接入原子小题目标（方案 D-EXT-001 冻结决策）：
-- * 新增 external_record_targets / external_operation_targets 保存
--   小题/材料/题组/聚合范围目标及提交快照；0005 的 local_item_id、
--   item_id 等旧列保留为历史兼容，只读不删；
-- * external_operations 增加可回填的 workflow_step_id / attempt_id
--   （历史行允许为空，新写入行由 repository 保证非空）；
-- * target_kind + target_id 的多态引用无法用普通外键约束（方案风险 17），
--   除 CHECK 枚举外，用按类型 trigger 校验目标真实存在；
-- * 本迁移不迁移历史数据、不改 0005 已有约束。

-- ============================================================
-- external_operations 回填统一执行链关联
-- ============================================================

ALTER TABLE external_operations ADD COLUMN workflow_step_id TEXT;
ALTER TABLE external_operations ADD COLUMN attempt_id TEXT;

CREATE INDEX idx_external_operations_step
    ON external_operations(workflow_step_id);
CREATE INDEX idx_external_operations_attempt
    ON external_operations(attempt_id);

-- ============================================================
-- 外部业务记录的目标关联（多对多、带版本与顺序）
-- ============================================================

CREATE TABLE external_record_targets (
    record_target_id    TEXT PRIMARY KEY,
    external_record_mapping_id TEXT NOT NULL,
    target_kind         TEXT NOT NULL CHECK (
        target_kind IN ('QUESTION', 'STIMULUS', 'CONTENT_UNIT', 'GROUP',
                        'MAJOR_SECTION', 'SCOPE')
    ),
    target_id           TEXT NOT NULL CHECK (length(target_id) > 0),
    target_revision_id  TEXT,
    ordinal             INTEGER NOT NULL CHECK (ordinal >= 0),
    relation_type       TEXT NOT NULL CHECK (
        relation_type IN ('PRIMARY', 'MEMBER', 'CONTEXT', 'MATERIAL')
    ),
    target_hash         TEXT,
    created_at          TEXT NOT NULL,
    UNIQUE (external_record_mapping_id, target_kind, target_id,
            target_revision_id, relation_type),
    FOREIGN KEY (external_record_mapping_id)
        REFERENCES external_records(external_record_mapping_id) ON DELETE RESTRICT
);

CREATE INDEX idx_external_record_targets_target
    ON external_record_targets(target_kind, target_id);

-- target_revision_id 允许为空（GROUP/SCOPE 等无版本目标），但防重复
-- 关联必须把 NULL 视为相等：表达式唯一索引兜底。
CREATE UNIQUE INDEX ux_external_record_targets_dedupe
    ON external_record_targets(external_record_mapping_id, target_kind,
                               target_id, IFNULL(target_revision_id, ''),
                               relation_type);

-- ============================================================
-- 外部操作提交时的目标成员快照（聚合提交部分成功的事实依据）
-- ============================================================

CREATE TABLE external_operation_targets (
    operation_target_id TEXT PRIMARY KEY,
    external_operation_id TEXT NOT NULL,
    target_kind         TEXT NOT NULL CHECK (
        target_kind IN ('QUESTION', 'STIMULUS', 'CONTENT_UNIT', 'GROUP',
                        'MAJOR_SECTION', 'SCOPE')
    ),
    target_id           TEXT NOT NULL CHECK (length(target_id) > 0),
    target_revision_id  TEXT,
    ordinal             INTEGER NOT NULL CHECK (ordinal >= 0),
    result_status       TEXT NOT NULL CHECK (
        result_status IN ('PENDING', 'SUCCEEDED', 'FAILED', 'SKIPPED',
                          'AMBIGUOUS')
    ),
    payload_fragment_hash TEXT,
    created_at          TEXT NOT NULL,
    UNIQUE (external_operation_id, target_kind, target_id,
            target_revision_id),
    FOREIGN KEY (external_operation_id)
        REFERENCES external_operations(external_operation_id) ON DELETE RESTRICT
);

CREATE INDEX idx_external_operation_targets_target
    ON external_operation_targets(target_kind, target_id);

CREATE UNIQUE INDEX ux_external_operation_targets_dedupe
    ON external_operation_targets(external_operation_id, target_kind,
                                  target_id, IFNULL(target_revision_id, ''));

-- ============================================================
-- 多态目标存在性校验（按类型 trigger，方案风险 17 的落地方案）
-- ============================================================

CREATE TRIGGER trg_ext_record_target_question
BEFORE INSERT ON external_record_targets
WHEN NEW.target_kind = 'QUESTION'
BEGIN
    SELECT RAISE(ABORT, 'external_record_targets: QUESTION target not found in question_items')
    WHERE NOT EXISTS (SELECT 1 FROM question_items q WHERE q.question_id = NEW.target_id);
END;

CREATE TRIGGER trg_ext_record_target_stimulus
BEFORE INSERT ON external_record_targets
WHEN NEW.target_kind = 'STIMULUS'
BEGIN
    SELECT RAISE(ABORT, 'external_record_targets: STIMULUS target not found in stimuli')
    WHERE NOT EXISTS (SELECT 1 FROM stimuli s WHERE s.stimulus_id = NEW.target_id);
END;

CREATE TRIGGER trg_ext_record_target_content_unit
BEFORE INSERT ON external_record_targets
WHEN NEW.target_kind = 'CONTENT_UNIT'
BEGIN
    SELECT RAISE(ABORT, 'external_record_targets: CONTENT_UNIT target not found in content_units')
    WHERE NOT EXISTS (SELECT 1 FROM content_units c WHERE c.content_unit_id = NEW.target_id);
END;

CREATE TRIGGER trg_ext_record_target_group
BEFORE INSERT ON external_record_targets
WHEN NEW.target_kind = 'GROUP'
BEGIN
    SELECT RAISE(ABORT, 'external_record_targets: GROUP target not found in question_groups')
    WHERE NOT EXISTS (SELECT 1 FROM question_groups g WHERE g.question_group_id = NEW.target_id);
END;

CREATE TRIGGER trg_ext_record_target_major_section
BEFORE INSERT ON external_record_targets
WHEN NEW.target_kind = 'MAJOR_SECTION'
BEGIN
    SELECT RAISE(ABORT, 'external_record_targets: MAJOR_SECTION target not found in major_sections')
    WHERE NOT EXISTS (SELECT 1 FROM major_sections m WHERE m.major_section_id = NEW.target_id);
END;

CREATE TRIGGER trg_ext_record_target_scope
BEFORE INSERT ON external_record_targets
WHEN NEW.target_kind = 'SCOPE'
BEGIN
    SELECT RAISE(ABORT, 'external_record_targets: SCOPE target not found in operation_scopes')
    WHERE NOT EXISTS (SELECT 1 FROM operation_scopes os WHERE os.scope_row_id = NEW.target_id);
END;

CREATE TRIGGER trg_ext_operation_target_question
BEFORE INSERT ON external_operation_targets
WHEN NEW.target_kind = 'QUESTION'
BEGIN
    SELECT RAISE(ABORT, 'external_operation_targets: QUESTION target not found in question_items')
    WHERE NOT EXISTS (SELECT 1 FROM question_items q WHERE q.question_id = NEW.target_id);
END;

CREATE TRIGGER trg_ext_operation_target_stimulus
BEFORE INSERT ON external_operation_targets
WHEN NEW.target_kind = 'STIMULUS'
BEGIN
    SELECT RAISE(ABORT, 'external_operation_targets: STIMULUS target not found in stimuli')
    WHERE NOT EXISTS (SELECT 1 FROM stimuli s WHERE s.stimulus_id = NEW.target_id);
END;

CREATE TRIGGER trg_ext_operation_target_content_unit
BEFORE INSERT ON external_operation_targets
WHEN NEW.target_kind = 'CONTENT_UNIT'
BEGIN
    SELECT RAISE(ABORT, 'external_operation_targets: CONTENT_UNIT target not found in content_units')
    WHERE NOT EXISTS (SELECT 1 FROM content_units c WHERE c.content_unit_id = NEW.target_id);
END;

CREATE TRIGGER trg_ext_operation_target_group
BEFORE INSERT ON external_operation_targets
WHEN NEW.target_kind = 'GROUP'
BEGIN
    SELECT RAISE(ABORT, 'external_operation_targets: GROUP target not found in question_groups')
    WHERE NOT EXISTS (SELECT 1 FROM question_groups g WHERE g.question_group_id = NEW.target_id);
END;

CREATE TRIGGER trg_ext_operation_target_major_section
BEFORE INSERT ON external_operation_targets
WHEN NEW.target_kind = 'MAJOR_SECTION'
BEGIN
    SELECT RAISE(ABORT, 'external_operation_targets: MAJOR_SECTION target not found in major_sections')
    WHERE NOT EXISTS (SELECT 1 FROM major_sections m WHERE m.major_section_id = NEW.target_id);
END;

CREATE TRIGGER trg_ext_operation_target_scope
BEFORE INSERT ON external_operation_targets
WHEN NEW.target_kind = 'SCOPE'
BEGIN
    SELECT RAISE(ABORT, 'external_operation_targets: SCOPE target not found in operation_scopes')
    WHERE NOT EXISTS (SELECT 1 FROM operation_scopes os WHERE os.scope_row_id = NEW.target_id);
END;
