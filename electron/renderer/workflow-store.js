'use strict';

const STORAGE_PREFIX = 'wordtts.workflow.last-event.';
const SEQ_SUFFIX = '.seq';
const MAX_WORKSPACE_CACHE_ENTRIES = 8;

// Keep the renderer-facing projection deliberately scalar and bounded.  The
// durable snapshot may grow new server fields over time; the desktop store
// should not accidentally retain configuration blobs, credentials, paths, or
// an unbounded event payload as part of its reactive UI state.
const WORKFLOW_PROJECTION_FIELDS = Object.freeze([
    'workflow_id',
    'workflow_group_id',
    'parent_workflow_id',
    'status',
    'result_status',
    'execution_state',
    'control_state',
    'cleanup_state',
    'current_step_id',
    'state_version',
    'group_state_version',
    'draft_revision',
    'source_artifact_id',
    'item_count',
    'artifact_count',
    'latest_event_id',
    'latest_seq',
    'last_error_code',
    'last_error_message',
    'updated_at',
]);

function projectWorkflowSnapshot(workflow) {
    if (!workflow || typeof workflow !== 'object') return null;
    const projection = {};
    WORKFLOW_PROJECTION_FIELDS.forEach((field) => {
        if (workflow[field] === undefined) return;
        if (field === 'last_error_code') {
            projection[field] = workflow[field] === null
                ? null
                : String(workflow[field]).slice(0, 128);
        } else if (field === 'last_error_message') {
            projection[field] = workflow[field] === null
                ? null
                : String(workflow[field]).slice(0, 2000);
        } else {
            projection[field] = workflow[field];
        }
    });
    const runtime = {};
    const existingRuntime = workflow.runtime;
    if (existingRuntime && typeof existingRuntime === 'object' && !Array.isArray(existingRuntime)) {
        ['phase', 'status', 'message', 'item_id', 'segment_id', 'stage', 'error'].forEach((field) => {
            if (existingRuntime[field] !== undefined && existingRuntime[field] !== null) {
                runtime[field] = String(existingRuntime[field]).slice(0, 500);
            }
        });
        ['completed_segments', 'total_segments'].forEach((field) => {
            const value = Number(existingRuntime[field]);
            if (Number.isInteger(value) && value >= 0) runtime[field] = value;
        });
        ['submitted_works', 'downloaded_works', 'total_works', 'item_count'].forEach((field) => {
            const value = Number(existingRuntime[field]);
            if (Number.isInteger(value) && value >= 0) runtime[field] = value;
        });
        const runtimeElapsed = Number(existingRuntime.elapsed_seconds);
        if (Number.isFinite(runtimeElapsed) && runtimeElapsed >= 0) {
            runtime.elapsed_seconds = Math.round(runtimeElapsed * 10) / 10;
        }
    }
    const latest = workflow.latest_event;
    if (latest && typeof latest === 'object') {
        projection.latest_event_type = String(latest.event_type || '').slice(0, 128) || null;
        projection.latest_event_seq = Number.isInteger(latest.seq) ? latest.seq : null;
        const payload = latest.payload;
        if (payload && typeof payload === 'object') {
            ['phase', 'status', 'message', 'item_id', 'segment_id', 'stage', 'error'].forEach((field) => {
                if (payload[field] !== undefined && payload[field] !== null) {
                    runtime[field] = String(payload[field]).slice(0, 500);
                }
            });
            ['completed_segments', 'total_segments'].forEach((field) => {
                const value = Number(payload[field]);
                if (Number.isInteger(value) && value >= 0) runtime[field] = value;
            });
            ['submitted_works', 'downloaded_works', 'total_works', 'item_count'].forEach((field) => {
                const value = Number(payload[field]);
                if (Number.isInteger(value) && value >= 0) runtime[field] = value;
            });
            const payloadElapsed = Number(payload.elapsed_seconds);
            if (Number.isFinite(payloadElapsed) && payloadElapsed >= 0) {
                runtime.elapsed_seconds = Math.round(payloadElapsed * 10) / 10;
            }
        }
    }
    if (Object.keys(runtime).length > 0) projection.runtime = runtime;
    return projection;
}

function capWorkspaceText(value, limit = 500) {
    const text = String(value ?? '');
    return text ? text.slice(0, limit) : null;
}

function nonNegativeInteger(value) {
    const number = Number(value);
    return Number.isSafeInteger(number) && number >= 0 ? number : null;
}

function finiteNumber(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
}

function projectTarget(target) {
    if (!target || typeof target !== 'object' || Array.isArray(target)) return null;
    const projected = {};
    ['target_type', 'item_id', 'step_id', 'work_unit_id', 'artifact_id', 'attempt_id'].forEach((field) => {
        const value = capWorkspaceText(target[field], 256);
        if (value !== null) projected[field] = value;
    });
    return Object.keys(projected).length > 0 ? projected : null;
}

function projectAction(action) {
    if (!action || typeof action !== 'object' || Array.isArray(action)) return null;
    const projected = {};
    ['kind', 'type', 'reason', 'retry_scope'].forEach((field) => {
        const value = capWorkspaceText(action[field], 256);
        if (value !== null) projected[field] = value;
    });
    ['enabled', 'safe_to_retry'].forEach((field) => {
        if (action[field] !== undefined) projected[field] = action[field] === true;
    });
    ['expected_state_version', 'expected_target_state_version', 'expected_group_state_version'].forEach((field) => {
        const value = nonNegativeInteger(action[field]);
        if (value !== null) projected[field] = value;
    });
    const target = projectTarget(action.target);
    if (target) projected.target = target;
    return projected;
}

function projectProgress(progress) {
    if (!progress || typeof progress !== 'object' || Array.isArray(progress)) return null;
    const projected = {};
    ['total', 'completed', 'failed', 'cancelled', 'skipped', 'pending', 'deliverable', 'percent', 'deliverable_percent']
        .forEach((field) => {
            const value = nonNegativeInteger(progress[field]);
            if (value !== null) projected[field] = value;
        });
    return projected;
}

function projectBlocker(blocker) {
    if (!blocker || typeof blocker !== 'object' || Array.isArray(blocker)) return null;
    const projected = {};
    ['code', 'title', 'message', 'severity', 'retry_scope'].forEach((field) => {
        const value = capWorkspaceText(blocker[field], field === 'message' ? 2000 : 256);
        if (value !== null) projected[field] = value;
    });
    ['retryable', 'safe_to_retry', 'requires_reconcile'].forEach((field) => {
        if (blocker[field] !== undefined) projected[field] = blocker[field] === true;
    });
    if (Array.isArray(blocker.affected_item_ids)) {
        projected.affected_item_ids = blocker.affected_item_ids
            .slice(0, 2000)
            .map(value => capWorkspaceText(value, 256))
            .filter(Boolean);
    }
    const recoveryAction = projectAction(blocker.recovery_action);
    if (recoveryAction) projected.recovery_action = recoveryAction;
    return projected;
}

function projectMetadata(metadata) {
    if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata)) return {};
    const projected = {};
    Object.entries(metadata).slice(0, 64).forEach(([key, value]) => {
        const safeKey = String(key).slice(0, 128);
        if (value === null || typeof value === 'boolean' || (typeof value === 'number' && Number.isFinite(value))) {
            projected[safeKey] = value;
        } else if (typeof value === 'string') {
            projected[safeKey] = value.slice(0, 512);
        } else if (Array.isArray(value)) {
            projected[safeKey] = value.slice(0, 32).map(item => (
                item === null || typeof item === 'boolean' || (typeof item === 'number' && Number.isFinite(item))
                    ? item
                    : String(item).slice(0, 256)
            ));
        } else if (safeKey === 'capabilities' && typeof value === 'object') {
            const capabilities = {};
            ['parse', 'audio', 'normalize', 'external_input'].forEach((field) => {
                if (value[field] !== undefined) capabilities[field] = value[field] === true;
            });
            if (Object.keys(capabilities).length > 0) projected[safeKey] = capabilities;
        }
    });
    try {
        while (JSON.stringify(projected).length > 4096) {
            const lastKey = Object.keys(projected).pop();
            if (!lastKey) break;
            delete projected[lastKey];
        }
    } catch (_) { return {}; }
    return projected;
}

function projectItem(item) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) return null;
    const projected = {};
    ['item_id', 'item_identity_key', 'item_type', 'normalized_content', 'source_locator', 'skip_reason', 'content_hash', 'status', 'role', 'voice_key', 'error_code', 'user_message', 'retry_scope', 'updated_at']
        .forEach((field) => {
            const value = capWorkspaceText(item[field], field === 'user_message' ? 2000 : 512);
            if (value !== null) projected[field] = value;
        });
    if (item.normalized_content !== null && item.normalized_content !== undefined) {
        projected.normalized_content = String(item.normalized_content).slice(0, 16 * 1024);
    }
    if (item.normalized_content === null) projected.normalized_content = null;
    if (item.content_ref && typeof item.content_ref === 'object' && !Array.isArray(item.content_ref)) {
        const contentId = capWorkspaceText(item.content_ref.content_id, 128);
        const contentHash = capWorkspaceText(item.content_ref.content_hash, 128);
        const sizeBytes = nonNegativeInteger(item.content_ref.size_bytes);
        const maxResponseBytes = nonNegativeInteger(item.content_ref.max_response_bytes);
        if (contentId && contentHash && sizeBytes !== null && maxResponseBytes !== null) {
            projected.content_ref = {
                content_id: contentId,
                content_hash: contentHash,
                size_bytes: sizeBytes,
                max_response_bytes: maxResponseBytes,
            };
        }
    } else {
        projected.content_ref = null;
    }
    projected.metadata = projectMetadata(item.metadata);
    ['sequence', 'attempt_count'].forEach((field) => {
        const value = nonNegativeInteger(item[field]);
        if (value !== null) projected[field] = value;
    });
    if (item.requires_reconcile !== undefined) projected.requires_reconcile = item.requires_reconcile === true;
    if (Array.isArray(item.artifact_ids)) {
        projected.artifact_ids = item.artifact_ids
            .slice(0, 2000)
            .map(value => capWorkspaceText(value, 256))
            .filter(Boolean);
    }
    return projected;
}

function projectArtifact(artifact) {
    if (!artifact || typeof artifact !== 'object' || Array.isArray(artifact)) return null;
    const projected = {};
    ['artifact_id', 'workflow_id', 'item_id', 'step_id', 'work_unit_id', 'artifact_type', 'lifecycle_state', 'format', 'extension', 'mime_type', 'sha256', 'filename', 'producer', 'producer_version', 'created_at', 'updated_at']
        .forEach((field) => {
            const value = capWorkspaceText(artifact[field], field === 'filename' ? 512 : 256);
            if (value !== null) projected[field] = value;
        });
    ['size_bytes', 'duration_ms'].forEach((field) => {
        const value = nonNegativeInteger(artifact[field]);
        if (value !== null) projected[field] = value;
    });
    if (artifact.verified !== undefined) projected.verified = artifact.verified === true;
    return projected;
}

function projectConfiguration(configuration) {
    if (!configuration || typeof configuration !== 'object' || Array.isArray(configuration)) return {};
    const projected = {};
    const revision = nonNegativeInteger(configuration.configuration_revision);
    if (revision !== null) projected.configuration_revision = revision;
    const hash = capWorkspaceText(configuration.configuration_hash, 128);
    if (hash !== null) projected.configuration_hash = hash;

    const effectiveSource = configuration.effective;
    if (effectiveSource && typeof effectiveSource === 'object' && !Array.isArray(effectiveSource)) {
        const effective = {};
        ['provider', 'generation_mode', 'format', 'quality', 'default_female_voice', 'default_male_voice']
            .forEach((field) => {
                const value = capWorkspaceText(effectiveSource[field], 512);
                if (value !== null) effective[field] = value;
            });
        ['preview'].forEach((field) => {
            if (effectiveSource[field] !== undefined) effective[field] = effectiveSource[field] === true;
        });
        ['preview_limit'].forEach((field) => {
            const value = nonNegativeInteger(effectiveSource[field]);
            if (value !== null) effective[field] = value;
        });
        ['rate', 'pitch', 'volume'].forEach((field) => {
            const value = finiteNumber(effectiveSource[field]);
            if (value !== null) effective[field] = value;
        });
        if (effectiveSource.role_voices && typeof effectiveSource.role_voices === 'object' && !Array.isArray(effectiveSource.role_voices)) {
            effective.role_voices = {};
            Object.entries(effectiveSource.role_voices).slice(0, 256).forEach(([key, value]) => {
                effective.role_voices[String(key).slice(0, 256)] = capWorkspaceText(value, 512);
            });
        }
        if (effectiveSource.role_configs && typeof effectiveSource.role_configs === 'object' && !Array.isArray(effectiveSource.role_configs)) {
            effective.role_configs = {};
            Object.entries(effectiveSource.role_configs).slice(0, 256).forEach(([key, value]) => {
                if (!value || typeof value !== 'object' || Array.isArray(value)) return;
                const roleConfig = {};
                ['rate', 'pitch', 'volume'].forEach((field) => {
                    const number = finiteNumber(value[field]);
                    if (number !== null) roleConfig[field] = number;
                });
                effective.role_configs[String(key).slice(0, 256)] = roleConfig;
            });
        }
        projected.effective = effective;
    }
    if (configuration.source_priority && typeof configuration.source_priority === 'object' && !Array.isArray(configuration.source_priority)) {
        projected.source_priority = {};
        Object.entries(configuration.source_priority).slice(0, 256).forEach(([key, value]) => {
            const text = capWorkspaceText(value, 128);
            if (text !== null) projected.source_priority[String(key).slice(0, 256)] = text;
        });
    }
    if (Array.isArray(configuration.frozen_fields)) {
        projected.frozen_fields = configuration.frozen_fields.slice(0, 256)
            .map(value => capWorkspaceText(value, 256)).filter(Boolean);
    }
    return projected;
}

function projectDelivery(delivery) {
    if (!delivery || typeof delivery !== 'object' || Array.isArray(delivery)) return {};
    const projected = {};
    ['zip_artifact_id'].forEach((field) => {
        const value = capWorkspaceText(delivery[field], 256);
        if (value !== null) projected[field] = value;
    });
    if (delivery.zip_available !== undefined) projected.zip_available = delivery.zip_available === true;
    ['included_item_ids', 'excluded_item_ids'].forEach((field) => {
        projected[field] = Array.isArray(delivery[field])
            ? delivery[field].slice(0, 2000).map(value => capWorkspaceText(value, 256)).filter(Boolean)
            : [];
    });
    projected.exclusion_reasons = {};
    if (delivery.exclusion_reasons && typeof delivery.exclusion_reasons === 'object' && !Array.isArray(delivery.exclusion_reasons)) {
        Object.entries(delivery.exclusion_reasons).slice(0, 2000).forEach(([key, value]) => {
            const text = capWorkspaceText(value, 512);
            if (text !== null) projected.exclusion_reasons[String(key).slice(0, 256)] = text;
        });
    }
    return projected;
}

function projectSystemInput(systemInput) {
    if (!systemInput || typeof systemInput !== 'object' || Array.isArray(systemInput)) return null;
    const projected = {};
    ['delivery_mode', 'input_type', 'input_type_status', 'paper_category',
        'paper_category_status', 'parse_coverage_status', 'unit_count_status',
        'unit_count_override', 'input_status', 'error_code'].forEach((field) => {
        const value = capWorkspaceText(systemInput[field], 128);
        if (value !== null) projected[field] = value;
    });
    ['available', 'executor_available', 'supported_external_input'].forEach((field) => {
        if (systemInput[field] !== undefined) projected[field] = systemInput[field] === true;
    });
    if (systemInput.configuration_editable !== undefined) {
        projected.configuration_editable = systemInput.configuration_editable === true;
    }
    // 文档自动识别建议（录入类型/试卷分类/课文字段）。只透传安全字段，
    // 供录入目标抽屉预填与呈现识别结果。
    const suggested = systemInput.suggested_configuration;
    if (suggested && typeof suggested === 'object' && !Array.isArray(suggested)) {
        const projectedSuggested = {};
        ['input_type', 'input_type_status', 'paper_category', 'paper_category_status'].forEach((field) => {
            const value = capWorkspaceText(suggested[field], 128);
            if (value !== null) projectedSuggested[field] = value;
        });
        const textbook = suggested.textbook;
        if (textbook && typeof textbook === 'object' && !Array.isArray(textbook)) {
            const projectedTextbook = {};
            Object.entries(textbook).slice(0, 24).forEach(([key, value]) => {
                if (!/^textbook/.test(String(key)) || !value || typeof value !== 'object') return;
                projectedTextbook[String(key)] = {
                    value: capWorkspaceText(value.value, 256) || '',
                    source: capWorkspaceText(value.source, 32) || '',
                    confidence: capWorkspaceText(value.confidence, 16) || '',
                };
            });
            if (Object.keys(projectedTextbook).length) projectedSuggested.textbook = projectedTextbook;
        }
        if (projectedSuggested.input_type || projectedSuggested.paper_category) {
            projected.suggested_configuration = projectedSuggested;
        }
    }
    if (Array.isArray(systemInput.supported_external_input_types)) {
        projected.supported_external_input_types = systemInput.supported_external_input_types
            .slice(0, 16).map(value => capWorkspaceText(value, 64)).filter(Boolean);
    }
    if (Array.isArray(systemInput.input_type_capabilities)) {
        projected.input_type_capabilities = systemInput.input_type_capabilities
            .slice(0, 16)
            .filter(row => row && typeof row === 'object' && !Array.isArray(row))
            .map(row => {
                const capability = {};
                ['input_type', 'label', 'unit_label', 'platform', 'adapter_key',
                    'config_schema_version', 'status', 'reason'].forEach((field) => {
                    const value = capWorkspaceText(row[field], 256);
                    if (value !== null) capability[field] = value;
                });
                if (row.external_supported !== undefined) {
                    capability.external_supported = row.external_supported === true;
                }
                if (row.executor_available !== undefined) {
                    capability.executor_available = row.executor_available === true;
                }
                return capability;
            })
            .filter(row => row.input_type);
    }
    const projectSmallObject = (value, depth = 0) => {
        if (depth > 2 || value === null || value === undefined) return value ?? null;
        if (typeof value === 'string') return value.slice(0, 2000);
        if (typeof value === 'number' || typeof value === 'boolean') return value;
        if (Array.isArray(value)) return value.slice(0, 64).map(item => projectSmallObject(item, depth + 1));
        if (typeof value !== 'object') return null;
        const result = {};
        Object.entries(value).slice(0, 64).forEach(([key, item]) => {
            const safe = projectSmallObject(item, depth + 1);
            if (safe !== null && safe !== undefined) result[String(key).slice(0, 128)] = safe;
        });
        return result;
    };
    if (systemInput.document_entry_support
        && typeof systemInput.document_entry_support === 'object'
        && !Array.isArray(systemInput.document_entry_support)) {
        const entrySupport = {};
        ['schema_version', 'status', 'format', 'label', 'reason'].forEach((field) => {
            const value = capWorkspaceText(systemInput.document_entry_support[field], 512);
            if (value !== null) entrySupport[field] = value;
        });
        ['supported', 'structured_count', 'invalid_count',
            'major_section_profile_invalid_count', 'entry_profile_invalid_count', 'entry_capability_invalid_count'].forEach((field) => {
            if (systemInput.document_entry_support[field] !== undefined) {
                if (field === 'supported') {
                    entrySupport[field] = systemInput.document_entry_support[field] === true;
                } else {
                    const count = nonNegativeInteger(systemInput.document_entry_support[field]);
                    if (count !== null) entrySupport[field] = count;
                }
            }
        });
        ['detected_types', 'document_types', 'expected_types'].forEach((field) => {
            if (Array.isArray(systemInput.document_entry_support[field])) {
                entrySupport[field] = systemInput.document_entry_support[field]
                    .slice(0, 16)
                    .map(value => capWorkspaceText(value, 128))
                    .filter(Boolean);
            }
        });
        // Keep the renderer contract binary even when it receives a stale
        // projection written by an older backend. A missing/unknown status is
        // fail-closed and therefore belongs to “暂不支持”.
        // ``supported`` is the only authoritative gate.  An older/stale
        // projection that contains only ``status: supported`` must not leak a
        // third interpretation into the renderer or reopen the document flow.
        entrySupport.status = systemInput.document_entry_support.supported === true
            ? 'supported'
            : 'unsupported';
        projected.document_entry_support = entrySupport;
    }
    const projectPageInput = (pageInput) => {
        if (!pageInput || typeof pageInput !== 'object' || Array.isArray(pageInput)) return null;
        const projectedPageInput = {};
        ['schema_version', 'input_type', 'type'].forEach((field) => {
            const value = capWorkspaceText(pageInput[field], 128);
            if (value !== null) projectedPageInput[field] = value;
        });
        const projectQuestion = (question) => {
            if (!question || typeof question !== 'object' || Array.isArray(question)) return null;
            const projectedQuestion = {};
            ['number', 'prompt', 'listening_text', 'answer_prompt', 'prompt_audio_filename_stem', 'reference_answers_source'].forEach((field) => {
                const value = capWorkspaceText(question[field], 16 * 1024);
                if (value !== null) projectedQuestion[field] = value;
            });
            ['score', 'answer_time', 'times', 'gap'].forEach((field) => {
                const value = finiteNumber(question[field]);
                if (value !== null) projectedQuestion[field] = value;
            });
            if (question.answer !== undefined) {
                projectedQuestion.answer = projectSmallObject(question.answer);
            }
            if (Array.isArray(question.options)) {
                projectedQuestion.options = question.options.slice(0, 256).map((option) => {
                    if (!option || typeof option !== 'object' || Array.isArray(option)) return null;
                    const projectedOption = {};
                    ['option_id', 'text'].forEach((field) => {
                        const value = capWorkspaceText(option[field], 16 * 1024);
                        if (value !== null) projectedOption[field] = value;
                    });
                    return Object.keys(projectedOption).length > 0 ? projectedOption : null;
                }).filter(Boolean);
            }
            ['answers', 'reference_answers'].forEach((field) => {
                if (Array.isArray(question[field])) {
                    projectedQuestion[field] = question[field].slice(0, 256)
                        .map(value => capWorkspaceText(value, 16 * 1024)).filter(Boolean);
                }
            });
            return Object.keys(projectedQuestion).length > 0 ? projectedQuestion : null;
        };
        const projectQuestions = (questions) => Array.isArray(questions)
            ? questions.slice(0, 256).map(projectQuestion).filter(Boolean)
            : [];
        if (pageInput.type === '听后选择' && Array.isArray(pageInput.materials)) {
            projectedPageInput.materials = pageInput.materials.slice(0, 256).map((material) => {
                if (!material || typeof material !== 'object' || Array.isArray(material)) return null;
                const projectedMaterial = {};
                ['section', 'audio_filename_stem', 'listening_text'].forEach((field) => {
                    const value = capWorkspaceText(material[field], 16 * 1024);
                    if (value !== null) projectedMaterial[field] = value;
                });
                ['times', 'gap'].forEach((field) => {
                    const value = finiteNumber(material[field]);
                    if (value !== null) projectedMaterial[field] = value;
                });
                projectedMaterial.questions = projectQuestions(material.questions);
                return projectedMaterial;
            }).filter(Boolean);
        } else if (pageInput.type === '信息获取' && Array.isArray(pageInput.materials)) {
            projectedPageInput.materials = pageInput.materials.slice(0, 256).map((material) => {
                if (!material || typeof material !== 'object' || Array.isArray(material)) return null;
                const projectedMaterial = {};
                ['section', 'audio_filename_stem', 'listening_text'].forEach((field) => {
                    const value = capWorkspaceText(material[field], 16 * 1024);
                    if (value !== null) projectedMaterial[field] = value;
                });
                projectedMaterial.questions = projectQuestions(material.questions);
                return projectedMaterial;
            }).filter(Boolean);
        } else if (pageInput.type === '听后应答' || pageInput.type === '模仿朗读') {
            projectedPageInput.questions = projectQuestions(pageInput.questions);
        } else if (pageInput.type === '听后记录并转述信息' || pageInput.type === '信息转述及询问') {
            const recording = pageInput.recording;
            if (recording && typeof recording === 'object' && !Array.isArray(recording)) {
                const projectedRecording = {};
                const listeningText = capWorkspaceText(recording.listening_text, 16 * 1024);
                if (listeningText !== null) projectedRecording.listening_text = listeningText;
                [
                    'audio_filename_stem',
                    'instruction_text',
                    'instruction_audio_filename_stem',
                    // The asking-information section has its own guidance
                    // audio channel. Keep its page fact in the renderer
                    // projection; otherwise workspaceItemsToParseResults()
                    // replaces the parser metadata with a page_input object
                    // that silently loses this field before the document
                    // view renders it.
                    'asking_instruction_text',
                    'asking_instruction_audio_filename_stem',
                ].forEach((field) => {
                    const value = capWorkspaceText(recording[field], 16 * 1024);
                    if (value !== null) projectedRecording[field] = value;
                });
                const imageId = capWorkspaceText(recording.image_artifact_id, 256);
                if (imageId !== null) projectedRecording.image_artifact_id = imageId;
                if (recording.table_image_required === true) projectedRecording.table_image_required = true;
                if (recording.block_image_required === true) projectedRecording.block_image_required = true;
                if (recording.block_kind === 'table' || recording.block_kind === 'drawing_group') {
                    projectedRecording.block_kind = recording.block_kind;
                }
                const blockIndex = nonNegativeInteger(recording.block_index);
                if (blockIndex !== null) projectedRecording.block_index = blockIndex;
                const tableIndex = nonNegativeInteger(recording.table_index);
                if (tableIndex !== null) projectedRecording.table_index = tableIndex;
                const instructionOccurrence = nonNegativeInteger(recording.instruction_occurrence);
                if (instructionOccurrence !== null) projectedRecording.instruction_occurrence = instructionOccurrence;
                const askingInstructionOccurrence = nonNegativeInteger(recording.asking_instruction_occurrence);
                if (askingInstructionOccurrence !== null) {
                    projectedRecording.asking_instruction_occurrence = askingInstructionOccurrence;
                }
                projectedRecording.questions = projectQuestions(recording.questions);
                projectedPageInput.recording = projectedRecording;
            }
            if (pageInput.retelling && typeof pageInput.retelling === 'object' && !Array.isArray(pageInput.retelling)) {
                const retelling = {};
                ['prompt', 'prompt_audio_filename_stem'].forEach((field) => {
                    const value = capWorkspaceText(pageInput.retelling[field], 16 * 1024);
                    if (value !== null) retelling[field] = value;
                });
                ['score', 'answer_time'].forEach((field) => {
                    const value = finiteNumber(pageInput.retelling[field]);
                    if (value !== null) retelling[field] = value;
                });
                if (Array.isArray(pageInput.retelling.reference_answers)) {
                    retelling.reference_answers = pageInput.retelling.reference_answers.slice(0, 256)
                        .map(value => capWorkspaceText(value, 16 * 1024)).filter(Boolean);
                }
                projectedPageInput.retelling = retelling;
            }
            projectedPageInput.asking = projectQuestions(pageInput.asking);
        } else {
            const fallback = projectSmallObject(pageInput);
            if (fallback && typeof fallback === 'object' && !Array.isArray(fallback)) {
                Object.assign(projectedPageInput, fallback);
            }
        }
        return Object.keys(projectedPageInput).length > 0 ? projectedPageInput : null;
    };
    if (systemInput.input_capability && typeof systemInput.input_capability === 'object' && !Array.isArray(systemInput.input_capability)) {
        projected.input_capability = projectSmallObject(systemInput.input_capability);
    }
    if (systemInput.page_content_status
        && typeof systemInput.page_content_status === 'object'
        && !Array.isArray(systemInput.page_content_status)) {
        const pageContentStatus = {};
        ['status', 'reason'].forEach((field) => {
            const value = capWorkspaceText(systemInput.page_content_status[field], 512);
            if (value !== null) pageContentStatus[field] = value;
        });
        ['structured_count', 'expected_count'].forEach((field) => {
            const count = nonNegativeInteger(systemInput.page_content_status[field]);
            if (count !== null) pageContentStatus[field] = count;
        });
        projected.page_content_status = pageContentStatus;
    }
    if (systemInput.unit_boundary_decision
        && typeof systemInput.unit_boundary_decision === 'object'
        && !Array.isArray(systemInput.unit_boundary_decision)) {
        projected.unit_boundary_decision = projectSmallObject(systemInput.unit_boundary_decision);
    }
    if (systemInput.audio_batch
        && typeof systemInput.audio_batch === 'object'
        && !Array.isArray(systemInput.audio_batch)) {
        const audioBatch = {};
        ['audio_batch_id', 'manifest_hash', 'status'].forEach((field) => {
            const value = capWorkspaceText(systemInput.audio_batch[field], 256);
            if (value !== null) audioBatch[field] = value;
        });
        const revision = nonNegativeInteger(systemInput.audio_batch.audio_revision);
        if (revision !== null) audioBatch.audio_revision = revision;
        projected.audio_batch = audioBatch;
    }
    if (Array.isArray(systemInput.units)) {
        projected.units = systemInput.units.slice(0, 256).map((unit) => {
            if (!unit || typeof unit !== 'object') return null;
            const value = {};
            ['unit_id', 'label', 'input_type', 'unit_count_status', 'input_type_status',
                'paper_category', 'paper_category_status', 'parse_coverage_status',
                'ordinal', 'structure_revision'].forEach((field) => {
                if (unit[field] !== undefined) value[field] = typeof unit[field] === 'number'
                    ? unit[field] : capWorkspaceText(unit[field], 256);
            });
            if (unit.source_range && typeof unit.source_range === 'object') value.source_range = projectSmallObject(unit.source_range);
            if (unit.evidence && typeof unit.evidence === 'object') value.evidence = projectSmallObject(unit.evidence);
            if (unit.configuration && typeof unit.configuration === 'object') value.configuration = projectSmallObject(unit.configuration);
            return value;
        }).filter(Boolean);
    } else projected.units = [];
    if (Array.isArray(systemInput.structure_nodes)) {
        projected.structure_nodes = systemInput.structure_nodes.slice(0, 2000).map((node) => {
            if (!node || typeof node !== 'object') return null;
            const value = {};
            ['node_id', 'unit_id', 'parent_node_id', 'node_kind', 'label', 'source_locator'].forEach((field) => {
                const text = capWorkspaceText(node[field], 512);
                if (text !== null) value[field] = text;
            });
            ['ordinal', 'confidence'].forEach((field) => {
                const number = finiteNumber(node[field]);
                if (number !== null) value[field] = number;
            });
            if (Array.isArray(node.path)) value.path = node.path.slice(0, 16).map(item => capWorkspaceText(item, 256)).filter(Boolean);
            return value;
        }).filter(Boolean);
    } else projected.structure_nodes = [];
    if (Array.isArray(systemInput.content_segments)) {
        projected.content_segments = systemInput.content_segments.slice(0, 4000).map((segment) => {
            if (!segment || typeof segment !== 'object') return null;
            const value = {};
            ['segment_id', 'unit_id', 'node_id', 'item_id', 'content_item_id',
                'source_locator', 'audio_artifact_id', 'category', 'filename_stem',
                'audio_filename_stem'].forEach((field) => {
                const text = capWorkspaceText(segment[field], 512);
                if (text !== null) value[field] = text;
            });
            if (segment.audio_only_auxiliary !== undefined) {
                value.audio_only_auxiliary = segment.audio_only_auxiliary === true;
            }
            ['ordinal', 'score', 'audio_revision'].forEach((field) => {
                const number = finiteNumber(segment[field]);
                if (number !== null) value[field] = number;
            });
            ['raw_text', 'tts_text'].forEach((field) => {
                if (segment[field] !== undefined && segment[field] !== null) value[field] = String(segment[field]).slice(0, 16 * 1024);
            });
            if (segment.answer !== undefined) value.answer = projectSmallObject(segment.answer);
            if (segment.page_input_status !== undefined) {
                const status = capWorkspaceText(segment.page_input_status, 32);
                if (status !== null) value.page_input_status = status;
            }
            if (segment.page_input !== undefined) {
                const pageInput = projectPageInput(segment.page_input);
                if (pageInput !== null) value.page_input = pageInput;
            }
            return value;
        }).filter(Boolean);
    } else projected.content_segments = [];
    const gate = systemInput.audio_gate;
    if (gate && typeof gate === 'object' && !Array.isArray(gate)) {
        projected.audio_gate = {
            technical_status: capWorkspaceText(gate.technical_status, 32) || 'failed',
            audio_revision: nonNegativeInteger(gate.audio_revision) ?? 0,
            manifest_hash: capWorkspaceText(gate.manifest_hash, 128) || null,
            required_artifact_ids: Array.isArray(gate.required_artifact_ids)
                ? gate.required_artifact_ids.slice(0, 4000).map(value => capWorkspaceText(value, 256)).filter(Boolean) : [],
            missing_item_ids: Array.isArray(gate.missing_item_ids)
                ? gate.missing_item_ids.slice(0, 4000).map(value => capWorkspaceText(value, 256)).filter(Boolean) : [],
            conflict_item_ids: Array.isArray(gate.conflict_item_ids)
                ? gate.conflict_item_ids.slice(0, 4000).map(value => capWorkspaceText(value, 256)).filter(Boolean) : [],
        };
    }
    const acceptance = systemInput.audio_acceptance;
    if (acceptance && typeof acceptance === 'object' && !Array.isArray(acceptance)) {
        projected.audio_acceptance = {
            status: capWorkspaceText(acceptance.status, 32) || 'pending',
            acceptance_id: capWorkspaceText(acceptance.acceptance_id, 256),
            audio_revision: nonNegativeInteger(acceptance.audio_revision) ?? 0,
            manifest_hash: capWorkspaceText(acceptance.manifest_hash, 128),
            accepted_at: capWorkspaceText(acceptance.accepted_at, 128),
        };
    }
    if (Array.isArray(systemInput.entries)) {
        projected.entries = systemInput.entries.slice(0, 256).map((entry) => {
            if (!entry || typeof entry !== 'object') return null;
            const value = {};
            ['entry_id', 'unit_id', 'input_type', 'document_name', 'unit_label',
                'configuration_revision', 'structure_revision',
                'external_record_mapping_id', 'external_record_id', 'input_status',
                'external_status', 'review_url', 'review_url_status', 'review_url_source',
                'run_review_url'].forEach((field) => {
                const text = capWorkspaceText(entry[field], 1024);
                if (text !== null) value[field] = text;
            });
            if (entry.requires_reconcile !== undefined) value.requires_reconcile = entry.requires_reconcile === true;
            if (entry.configuration && typeof entry.configuration === 'object') value.configuration = projectSmallObject(entry.configuration);
            return value;
        }).filter(Boolean);
    } else projected.entries = [];
    if (systemInput.input_run && typeof systemInput.input_run === 'object') {
        projected.input_run = {};
        ['input_run_id', 'workflow_id', 'input_type', 'status', 'audio_revision',
            'artifact_manifest_hash', 'payload_hash', 'error_code', 'error_message',
            'started_at', 'finished_at', 'created_at', 'updated_at'].forEach((field) => {
            const text = capWorkspaceText(systemInput.input_run[field], 2000);
            if (text !== null) projected.input_run[field] = text;
        });
        if (systemInput.input_run.control
            && typeof systemInput.input_run.control === 'object'
            && !Array.isArray(systemInput.input_run.control)) {
            projected.input_run.control = {
                pause_requested: systemInput.input_run.control.pause_requested === true,
                stop_requested: systemInput.input_run.control.stop_requested === true,
            };
        }
        if (Array.isArray(systemInput.input_run.attempts)) {
            projected.input_run.attempts = systemInput.input_run.attempts.slice(0, 256).map((attempt) => {
                if (!attempt || typeof attempt !== 'object' || Array.isArray(attempt)) return null;
                const value = {};
                ['attempt_id', 'entry_id', 'external_operation_id', 'status', 'side_effect_state',
                    'error_code', 'error_message', 'started_at', 'finished_at', 'created_at', 'updated_at']
                    .forEach((field) => {
                        const text = capWorkspaceText(attempt[field], 2000);
                        if (text !== null) value[field] = text;
                    });
                if (attempt.evidence && typeof attempt.evidence === 'object' && !Array.isArray(attempt.evidence)) {
                    value.evidence = projectSmallObject(attempt.evidence);
                }
                return value;
            }).filter(Boolean);
        }
    }
    return projected;
}

function projectSync(sync) {
    if (!sync || typeof sync !== 'object' || Array.isArray(sync)) return {};
    const projected = {};
    const stateVersion = nonNegativeInteger(sync.state_version);
    if (stateVersion !== null) projected.state_version = stateVersion;
    const eventId = capWorkspaceText(sync.last_event_id, 256);
    if (eventId !== null) projected.last_event_id = eventId;
    if (sync.requires_resync !== undefined) projected.requires_resync = sync.requires_resync === true;
    return projected;
}

function projectProvider(provider) {
    if (!provider || typeof provider !== 'object' || Array.isArray(provider)) {
        return {
            provider: 'UNKNOWN',
            status: 'UNKNOWN',
            ready: false,
            can_generate: false,
            can_start_generation: false,
            reason: 'Provider 状态未知',
        };
    }
    const allowedStatuses = new Set(['UNKNOWN', 'READY', 'LOGIN_REQUIRED', 'EXPIRED', 'UNAVAILABLE', 'DISABLED']);
    const status = String(provider.status || '').toUpperCase();
    const normalizedStatus = allowedStatuses.has(status) ? status : 'UNKNOWN';
    const ready = normalizedStatus === 'READY' && provider.ready === true;
    return {
        provider: capWorkspaceText(provider.provider, 128) || 'UNKNOWN',
        status: normalizedStatus,
        ready,
        can_generate: ready && provider.can_generate === true,
        can_start_generation: provider.can_start_generation === true
            && !['UNAVAILABLE', 'DISABLED'].includes(normalizedStatus),
        reason: capWorkspaceText(provider.reason, 500) || 'Provider 状态未知',
    };
}

function createWorkspaceState() {
    return {
        // items 计数来自服务端条目事实（item_count）；segments 计数来自
        // 讯飞运行时的分段进度；works 计数来自合并生成的作品阶段事件。
        // 三者单位不同，UI 按可用性择优展示。
        items: { total: null, completed: 0, failed: 0, cancelled: 0, skipped: 0 },
        segments: { completed: 0, total: 0 },
        works: { total: 0, submitted: 0, downloaded: 0, items: 0 },
        phase: null,
        runtime: { status: null, stage: null, message: null, itemId: null, elapsedSeconds: null, updatedAt: null },
        executionState: null,
        controlState: null,
        resultStatus: null,
        updatedAt: null,
    };
}

// The rich workspace is kept in memory for the active task only.  It is never
// persisted to localStorage and the snapshot is reduced before entering the
// store so a latest_event payload cannot accidentally carry diagnostics or
// credentials into reactive UI state.
function projectWorkspaceData(workspace) {
    if (!workspace || typeof workspace !== 'object') return null;
    const snapshot = workspace.snapshot && typeof workspace.snapshot === 'object'
        ? projectWorkflowSnapshot(workspace.snapshot)
        : null;
    return {
        schema_version: nonNegativeInteger(workspace.schema_version),
        source_filename: capWorkspaceText(workspace.source_filename, 256) || '未命名文档.docx',
        snapshot,
        progress: projectProgress(workspace.progress),
        blockers: Array.isArray(workspace.blockers)
            ? workspace.blockers.slice(0, 256).map(projectBlocker).filter(Boolean)
            : [],
        available_actions: Array.isArray(workspace.available_actions)
            ? workspace.available_actions.slice(0, 256).map(projectAction).filter(Boolean)
            : [],
        current_target: workspace.current_target && typeof workspace.current_target === 'object'
            ? {
                item_id: capWorkspaceText(workspace.current_target.item_id, 256),
                label: capWorkspaceText(workspace.current_target.label, 512),
                started_at: capWorkspaceText(workspace.current_target.started_at, 128),
            }
            : null,
        items: Array.isArray(workspace.items) ? workspace.items.slice(0, 2000).map(projectItem).filter(Boolean) : [],
        artifacts: Array.isArray(workspace.artifacts) ? workspace.artifacts.slice(0, 4000).map(projectArtifact).filter(Boolean) : [],
        configuration: projectConfiguration(workspace.configuration),
        provider: projectProvider(workspace.provider),
        delivery: projectDelivery(workspace.delivery),
        system_input: projectSystemInput(workspace.system_input),
        sync: projectSync(workspace.sync),
    };
}

function workspaceProgressState(workspace, scalarState) {
    const progress = workspace?.progress;
    const snapshot = workspace?.snapshot && typeof workspace.snapshot === 'object'
        ? workspace.snapshot
        : {};
    let nextState = {
        ...scalarState,
        executionState: snapshot.execution_state
            ? String(snapshot.execution_state)
            : scalarState.executionState,
        controlState: snapshot.control_state
            ? String(snapshot.control_state)
            : scalarState.controlState,
        resultStatus: snapshot.result_status
            ? String(snapshot.result_status)
            : scalarState.resultStatus,
        updatedAt: snapshot.updated_at || scalarState.updatedAt,
    };
    if (progress && typeof progress === 'object') {
        nextState = {
            ...nextState,
            items: {
                ...nextState.items,
                total: Number.isFinite(Number(progress.total)) ? Number(progress.total) : nextState.items.total,
                completed: Number.isFinite(Number(progress.completed)) ? Number(progress.completed) : nextState.items.completed,
                failed: Number.isFinite(Number(progress.failed)) ? Number(progress.failed) : nextState.items.failed,
                skipped: Number.isFinite(Number(progress.skipped)) ? Number(progress.skipped) : nextState.items.skipped,
                cancelled: Number.isFinite(Number(progress.cancelled)) ? Number(progress.cancelled) : nextState.items.cancelled,
            },
        };
    }

    // Reconnects can legitimately receive only a snapshot (or no new event at
    // all when Last-Event-ID is still current). Restore the scalar runtime
    // projection from that snapshot so a single-item run does not fall back to
    // 0% until the next provider event happens to arrive.
    const projectedSnapshot = projectWorkflowSnapshot(snapshot);
    const runtime = projectedSnapshot?.runtime;
    if (!runtime || typeof runtime !== 'object') return nextState;
    const nextRuntime = { ...nextState.runtime };
    ['status', 'stage', 'message'].forEach((field) => {
        if (runtime[field] !== undefined && runtime[field] !== null) {
            nextRuntime[field] = String(runtime[field]);
        }
    });
    if (runtime.item_id !== undefined && runtime.item_id !== null) {
        nextRuntime.itemId = String(runtime.item_id);
    }
    if (Number.isFinite(Number(runtime.elapsed_seconds)) && Number(runtime.elapsed_seconds) >= 0) {
        nextRuntime.elapsedSeconds = Number(runtime.elapsed_seconds);
    }
    if (snapshot.updated_at) nextRuntime.updatedAt = snapshot.updated_at;

    const completedSegments = Number(runtime.completed_segments);
    const totalSegments = Number(runtime.total_segments);
    const nextSegments = { ...nextState.segments };
    if (Number.isFinite(completedSegments) && Number.isFinite(totalSegments) && totalSegments > 0) {
        nextSegments.completed = Math.max(0, completedSegments);
        nextSegments.total = Math.max(0, totalSegments);
    }

    const totalWorks = Number(runtime.total_works);
    const nextWorks = { ...nextState.works };
    if (Number.isFinite(totalWorks) && totalWorks > 0) {
        nextWorks.total = Math.max(0, Math.round(totalWorks));
        ['submitted_works', 'downloaded_works'].forEach((field) => {
            const value = Number(runtime[field]);
            if (Number.isFinite(value) && value >= 0) {
                const target = field === 'submitted_works' ? 'submitted' : 'downloaded';
                nextWorks[target] = Math.min(nextWorks.total, Math.round(value));
            }
        });
        const itemCount = Number(runtime.item_count);
        if (Number.isFinite(itemCount) && itemCount >= 0) nextWorks.items = Math.round(itemCount);
    }

    const latestEventType = String(
        snapshot.latest_event_type || projectedSnapshot.latest_event_type || '',
    ).toUpperCase();
    let phase = nextState.phase;
    const runtimePhase = String(runtime.phase || '').toLowerCase();
    const runtimeUiPhases = new Set([
        'preparing', 'running', 'submitting', 'downloading', 'verifying', 'attention',
    ]);
    if (runtimeUiPhases.has(runtimePhase)) phase = runtimePhase;
    else if (latestEventType === 'TTS_PLAN_PREPARED') phase = 'preparing';
    else if (latestEventType === 'TTS_RUNTIME_STATUS' || latestEventType === 'TTS_RUNTIME_PROGRESS') phase = 'running';
    else if (latestEventType === 'TTS_SUBMISSION_IN_FLIGHT') phase = 'submitting';
    else if (latestEventType === 'PROVIDER_RECEIPT_OBSERVED') phase = 'downloading';
    else if (latestEventType === 'TTS_OUTPUT_VERIFIED') phase = 'verifying';
    else if (latestEventType === 'TTS_SUBMISSION_AMBIGUOUS'
        || latestEventType === 'TTS_SUBMISSION_REJECTED'
        || latestEventType === 'GENERATION_TASK_FAILED') phase = 'attention';

    return {
        ...nextState,
        phase,
        runtime: nextRuntime,
        segments: nextSegments,
        works: nextWorks,
    };
}

// 只投影 UI 需要的有界标量；事件 payload 中的大字段（progress 快照、
// 诊断详情、错误 details）一律不进入响应式状态。
function advanceWorkspaceWithEvent(workspace, event) {
    const type = String(event.event_type || '');
    const payload = event.payload && typeof event.payload === 'object' ? event.payload : {};
    // 心跳事件（TTS_RUNTIME_STATUS waiting）不携带 stage/item_id；保留上
    // 一次的值，否则合并阶段的阶段信号会被每 2 秒的心跳抹掉。
    const runtimePatch = {
        status: capWorkspaceText(payload.status),
        stage: payload.stage === undefined
            ? workspace.runtime.stage
            : capWorkspaceText(payload.stage),
        message: capWorkspaceText(payload.message || payload.error),
        itemId: payload.item_id === undefined
            ? workspace.runtime.itemId
            : capWorkspaceText(payload.item_id),
        elapsedSeconds: payload.elapsed_seconds === undefined
            ? workspace.runtime.elapsedSeconds
            : finiteNumber(payload.elapsed_seconds),
        updatedAt: event.created_at || workspace.runtime.updatedAt,
    };
    const touch = (phase) => {
        workspace.phase = phase || workspace.phase;
        workspace.runtime = { ...workspace.runtime, ...runtimePatch };
        workspace.updatedAt = event.created_at || workspace.updatedAt;
    };
    const controlPatch = {};
    const setControl = (controlState, {
        executionState = null,
        resultStatus = null,
        phase = null,
        message = null,
        lastErrorCode = undefined,
        lastErrorMessage = undefined,
    } = {}) => {
        workspace.controlState = controlState;
        controlPatch.control_state = controlState;
        if (executionState) {
            workspace.executionState = executionState;
            controlPatch.execution_state = executionState;
        }
        if (resultStatus) {
            workspace.resultStatus = resultStatus;
            controlPatch.result_status = resultStatus;
        }
        if (phase || message) {
            workspace.phase = phase || workspace.phase;
            workspace.runtime = {
                ...workspace.runtime,
                status: controlState.toLowerCase(),
                message: message || workspace.runtime.message,
                updatedAt: event.created_at || workspace.runtime.updatedAt,
            };
        }
        if (lastErrorCode !== undefined) {
            const value = lastErrorCode === null ? null : capWorkspaceText(lastErrorCode, 128);
            controlPatch.last_error_code = value;
            workspace.lastErrorCode = value;
        }
        if (lastErrorMessage !== undefined) {
            const value = lastErrorMessage === null ? null : capWorkspaceText(lastErrorMessage, 2000);
            controlPatch.last_error_message = value;
            workspace.lastErrorMessage = value;
        }
        workspace.updatedAt = event.created_at || workspace.updatedAt;
    };
    if (type === 'TTS_PLAN_PREPARED') {
        const count = Number(payload.item_count);
        if (Number.isInteger(count) && count > 0) workspace.items.total = count;
        touch('preparing');
    } else if (type === 'TTS_RUNTIME_STATUS' || type === 'TTS_RUNTIME_PROGRESS') {
        const completed = Number(payload.completed_segments);
        const total = Number(payload.total_segments);
        if (Number.isFinite(completed) && Number.isFinite(total) && total > 0) {
            workspace.segments = { completed: Math.max(0, completed), total: Math.max(0, total) };
        }
        // 合并生成的作品阶段事件带作品计数；total_works 是合并模式的
        // 标志字段，逐条模式的事件没有它，不会误入这里。
        const totalWorks = Number(payload.total_works);
        if (Number.isFinite(totalWorks) && totalWorks > 0) {
            const submittedWorks = Number(payload.submitted_works);
            const downloadedWorks = Number(payload.downloaded_works);
            const workItemCount = Number(payload.item_count);
            workspace.works = {
                total: Math.max(0, Math.round(totalWorks)),
                submitted: Number.isFinite(submittedWorks)
                    ? Math.max(0, Math.round(submittedWorks))
                    : workspace.works.submitted,
                downloaded: Number.isFinite(downloadedWorks)
                    ? Math.max(0, Math.round(downloadedWorks))
                    : workspace.works.downloaded,
                items: Number.isFinite(workItemCount)
                    ? Math.max(0, Math.round(workItemCount))
                    : workspace.works.items,
            };
        }
        touch('running');
    } else if (type === 'TTS_SUBMISSION_IN_FLIGHT') {
        touch('submitting');
    } else if (type === 'PROVIDER_RECEIPT_OBSERVED') {
        touch('downloading');
    } else if (type === 'TTS_OUTPUT_VERIFIED') {
        touch('verifying');
    } else if (type === 'TTS_SUBMISSION_AMBIGUOUS' || type === 'TTS_SUBMISSION_REJECTED' || type === 'GENERATION_TASK_FAILED') {
        touch('attention');
        const errorCode = capWorkspaceText(payload.error_code, 128);
        const errorMessage = capWorkspaceText(payload.message || payload.error, 2000);
        if (errorCode !== null) {
            workspace.lastErrorCode = errorCode;
            controlPatch.last_error_code = errorCode;
        }
        if (errorMessage !== null) {
            workspace.lastErrorMessage = errorMessage;
            controlPatch.last_error_message = errorMessage;
        }
    } else if (type === 'WORKFLOW_PAUSE') {
        setControl('PAUSE_REQUESTED', { phase: 'pausing', message: '正在暂停任务…' });
    } else if (type === 'WORKFLOW_PAUSED') {
        setControl('PAUSED', { phase: 'paused', message: '任务已暂停，可恢复执行' });
    } else if (type === 'WORKFLOW_RESUME') {
        setControl('RUNNING', { phase: 'running', message: '正在恢复任务…' });
    } else if (type === 'WORKFLOW_CANCEL') {
        setControl('TERMINATING', { executionState: 'BLOCKED', phase: 'stopping', message: '正在停止生成任务…' });
    } else if (type === 'WORKFLOW_CANCELLED') {
        setControl('TERMINATED', {
            executionState: 'TERMINAL',
            resultStatus: payload.result_status || 'CANCELLED',
            phase: 'attention',
            message: '任务已取消',
            lastErrorCode: 'WORKFLOW_CANCELLED',
            lastErrorMessage: payload.reason || payload.message || '任务已取消',
        });
    } else if (type.startsWith('SYSTEM_INPUT_')) {
        // System-input progress is projected through polling; these events
        // still mark the runtime dirty so the shell re-renders (and the
        // delivery card's scheduled refresh runs) without waiting a full
        // poll cycle after a run starts or an external record is repaired.
        const inputPhase = ['SYSTEM_INPUT_RUN_CREATED', 'SYSTEM_INPUT_RUN_RETRY_CREATED'].includes(type)
            ? 'running'
            : workspace.phase;
        const message = capWorkspaceText(
            payload.message
            || (['SYSTEM_INPUT_RUN_CREATED', 'SYSTEM_INPUT_RUN_RETRY_CREATED'].includes(type)
                ? '系统录入运行已创建，正在执行页面录入'
                : null),
            2000,
        );
        touch(inputPhase);
        if (message !== null) {
            workspace.runtime = { ...workspace.runtime, message, updatedAt: event.created_at || workspace.runtime.updatedAt };
        }
        const errorCode = capWorkspaceText(payload.error_code, 128);
        if (errorCode !== null) {
            workspace.lastErrorCode = errorCode;
            controlPatch.last_error_code = errorCode;
        }
    }
    return controlPatch;
}

function createWorkflowStore({ storage = null, keyPrefix = STORAGE_PREFIX, workspaceLoader = null } = {}) {
    let state = {
        workflow: null,
        workflowId: null,
        lastEventId: null,
        lastSeq: 0,
        needsCatchup: false,
        connected: false,
        error: null,
        workspace: createWorkspaceState(),
        workspaceData: null,
        workspaceByWorkflow: {},
        workspaceSyncByWorkflow: {},
        activeCandidates: [],
        pendingCommands: {},
    };
    const listeners = new Set();
    let stream = null;
    let detachFrame = null;
    let detachError = null;

    function rememberWorkflowEntry(map, workflowId, value, protectedWorkflowId = state.workflowId) {
        const key = String(workflowId || '');
        if (!key) return map;
        const next = { ...map };
        // Delete before re-inserting so a refreshed workflow becomes the most
        // recently used entry while preserving the active workflow on eviction.
        delete next[key];
        next[key] = value;
        while (Object.keys(next).length > MAX_WORKSPACE_CACHE_ENTRIES) {
            const oldest = Object.keys(next).find(candidate => candidate !== String(protectedWorkflowId || ''))
                || Object.keys(next)[0];
            if (!oldest) break;
            delete next[oldest];
        }
        return next;
    }

    const notify = () => {
        const snapshot = getState();
        listeners.forEach((listener) => listener(snapshot));
    };
    const persist = () => {
        if (!storage || !state.workflowId) return;
        try {
            const cursorKey = `${keyPrefix}${state.workflowId}`;
            const seqKey = `${cursorKey}${SEQ_SUFFIX}`;
            if (state.lastEventId) storage.setItem(cursorKey, state.lastEventId);
            else storage.removeItem(cursorKey);
            if (state.lastEventId && Number.isInteger(state.lastSeq) && state.lastSeq > 0) {
                storage.setItem(seqKey, String(state.lastSeq));
            } else {
                storage.removeItem(seqKey);
            }
        } catch (_) {
            // A full localStorage must not turn an already-reduced event into
            // a false failure; the server cursor remains authoritative.
        }
    };
    const storedCursor = (workflowId) => {
        if (!storage) return null;
        try { return storage.getItem(`${keyPrefix}${workflowId}`); } catch (_) { return null; }
    };
    const storedSeq = (workflowId) => {
        if (!storage) return 0;
        try {
            const value = Number.parseInt(storage.getItem(`${keyPrefix}${workflowId}${SEQ_SUFFIX}`) || '0', 10);
            return Number.isInteger(value) && value > 0 ? value : 0;
        } catch (_) {
            return 0;
        }
    };

    function getState() {
        const workspace = state.workspace;
        return {
            ...state,
            workflow: state.workflow ? { ...state.workflow } : null,
            workflowProjection: projectWorkflowSnapshot(state.workflow),
            workspaceData: state.workspaceData ? projectWorkspaceData(state.workspaceData) : null,
            workspaceByWorkflow: Object.fromEntries(Object.entries(state.workspaceByWorkflow).map(([workflowId, workspaceData]) => [
                workflowId,
                workspaceData ? projectWorkspaceData(workspaceData) : null,
            ])),
            workspaceSyncByWorkflow: Object.fromEntries(Object.entries(state.workspaceSyncByWorkflow).map(([workflowId, sync]) => [
                workflowId,
                sync ? { ...sync } : sync,
            ])),
            activeCandidates: state.activeCandidates.map(candidate => ({
                ...candidate,
                workflow: candidate.workflow ? { ...candidate.workflow } : null,
                workspace: candidate.workspace ? projectWorkspaceData(candidate.workspace) : null,
            })),
            pendingCommands: { ...state.pendingCommands },
            workspace: {
                ...workspace,
                items: { ...workspace.items },
                segments: { ...workspace.segments },
                works: { ...workspace.works },
                runtime: { ...workspace.runtime },
            },
        };
    }

    function subscribe(listener) {
        if (typeof listener !== 'function') return () => {};
        listeners.add(listener);
        return () => listeners.delete(listener);
    }

    function prepare(workflowId, initial = null) {
        const normalizedWorkflowId = String(workflowId || '');
        const persistedCursor = storedCursor(workflowId);
        const persistedSeq = storedSeq(workflowId);
        const initialCursor = initial?.lastEventId || initial?.workflow?.latest_event_id || null;
        const initialSeq = Number(initial?.lastSeq ?? initial?.workflow?.latest_seq ?? 0);
        const useInitial = !persistedCursor && initialCursor && Number.isInteger(initialSeq) && initialSeq > 0;
        const cachedWorkspace = state.workspaceByWorkflow[normalizedWorkflowId] || null;
        const cachedSnapshot = cachedWorkspace?.snapshot || null;
        const cachedWorkflow = state.workflowId === normalizedWorkflowId ? state.workflow : null;
        const seedSnapshot = cachedSnapshot
            ? { snapshot: cachedSnapshot }
            : (cachedWorkflow
                ? { snapshot: cachedWorkflow }
                : (useInitial && initial?.workflow ? { snapshot: initial.workflow } : null));
        state = {
            ...state,
            workflow: cachedSnapshot
                ? projectWorkflowSnapshot(cachedSnapshot)
                : (cachedWorkflow
                    ? projectWorkflowSnapshot(cachedWorkflow)
                    : (useInitial && initial?.workflow ? projectWorkflowSnapshot(initial.workflow) : null)),
            workflowId: normalizedWorkflowId,
            lastEventId: persistedCursor || (useInitial ? initialCursor : null),
            lastSeq: persistedSeq || (useInitial ? initialSeq : 0),
            needsCatchup: false,
            connected: false,
            error: null,
            // 切换 run 时 workspace 必须重置；旧 run 的分段进度绝不能
            // 泄漏成新任务的初始显示。重连同一个 run 则从已缓存的快照
            // 恢复运行时标量，避免 Last-Event-ID 没有新事件时回到 0%。
            workspace: workspaceProgressState(seedSnapshot, createWorkspaceState()),
            workspaceData: cachedWorkspace,
            workspaceSyncByWorkflow: rememberWorkflowEntry(
                state.workspaceSyncByWorkflow,
                normalizedWorkflowId,
                { state: cachedWorkspace ? 'ready' : 'idle', reason: null, updatedAt: Date.now() },
                normalizedWorkflowId,
            ),
            pendingCommands: {},
        };
        notify();
        return getState();
    }

    function setWorkspace(workspace, { snapshot = null } = {}) {
        if (!workspace || typeof workspace !== 'object') return getState();
        const data = projectWorkspaceData({
            ...workspace,
            snapshot: workspace.snapshot || snapshot || state.workflow || null,
        });
        const projectedSnapshot = data?.snapshot;
        const workflowId = String(projectedSnapshot?.workflow_id || state.workflowId || '');
        const isActiveWorkspace = !state.workflowId || workflowId === String(state.workflowId);
        const previous = workflowId ? state.workspaceByWorkflow[workflowId] : null;
        const previousSnapshot = previous?.snapshot || (isActiveWorkspace ? state.workflow : null);
        const previousVersion = Number(previousSnapshot?.state_version);
        const nextVersion = Number(projectedSnapshot?.state_version);
        const previousSeq = Number(previousSnapshot?.latest_seq);
        const nextSeq = Number(projectedSnapshot?.latest_seq);
        const hasPreviousFreshness = (
            (Number.isInteger(previousVersion) && previousVersion >= 0)
            || (Number.isInteger(previousSeq) && previousSeq >= 0)
        );
        const hasNextFreshness = (
            (Number.isInteger(nextVersion) && nextVersion >= 0)
            || (Number.isInteger(nextSeq) && nextSeq >= 0)
        );
        const olderByVersion = Number.isInteger(previousVersion)
            && Number.isInteger(nextVersion)
            && nextVersion < previousVersion;
        const olderBySequence = Number.isInteger(previousSeq)
            && Number.isInteger(nextSeq)
            && nextSeq < previousSeq;
        if (
            previousSnapshot
            && (olderByVersion || olderBySequence || (hasPreviousFreshness && !hasNextFreshness))
        ) {
            // The request that produced this snapshot is still complete even
            // when its payload lost a race with a newer workspace refresh.
            // Leaving the per-workflow sync record in `loading` here makes a
            // late, stale response spin the UI forever.
            const previousSync = state.workspaceSyncByWorkflow[workflowId] || {};
            state = {
                ...state,
                workspaceSyncByWorkflow: rememberWorkflowEntry(
                    state.workspaceSyncByWorkflow,
                    workflowId,
                    {
                        ...previousSync,
                        state: 'ready',
                        reason: null,
                        updatedAt: Date.now(),
                    },
                    isActiveWorkspace ? workflowId : state.workflowId,
                ),
                error: null,
            };
            notify();
            return getState();
        }
        const nextWorkspace = workspaceProgressState(workspace, {
            ...state.workspace,
            executionState: projectedSnapshot?.execution_state || state.workspace.executionState,
            controlState: projectedSnapshot?.control_state || state.workspace.controlState,
            resultStatus: projectedSnapshot?.result_status || state.workspace.resultStatus,
            updatedAt: projectedSnapshot?.updated_at || state.workspace.updatedAt,
        });
        state = {
            ...state,
            workflow: isActiveWorkspace && projectedSnapshot ? { ...projectedSnapshot } : state.workflow,
            workflowId: isActiveWorkspace ? (workflowId || state.workflowId) : state.workflowId,
            workspace: isActiveWorkspace ? nextWorkspace : state.workspace,
            workspaceData: isActiveWorkspace ? data : state.workspaceData,
            workspaceByWorkflow: workflowId
                ? rememberWorkflowEntry(
                    state.workspaceByWorkflow,
                    workflowId,
                    data,
                    isActiveWorkspace ? workflowId : state.workflowId,
                )
                : state.workspaceByWorkflow,
            workspaceSyncByWorkflow: workflowId
                ? rememberWorkflowEntry(
                    state.workspaceSyncByWorkflow,
                    workflowId,
                    { state: 'ready', reason: null, updatedAt: Date.now() },
                    isActiveWorkspace ? workflowId : state.workflowId,
                )
                : state.workspaceSyncByWorkflow,
            error: null,
        };
        notify();
        return getState();
    }

    function hydrate(workspaceOrWorkflowId, options = {}) {
        if (workspaceOrWorkflowId && typeof workspaceOrWorkflowId === 'object') {
            return setWorkspace(workspaceOrWorkflowId, options);
        }
        const workflowId = String(workspaceOrWorkflowId || '');
        const loader = options?.loader || workspaceLoader;
        if (!workflowId || typeof loader !== 'function') return Promise.resolve(getState());
        const requestToken = `${Date.now()}:${Math.random()}`;
        state = {
            ...state,
            workspaceSyncByWorkflow: rememberWorkflowEntry(state.workspaceSyncByWorkflow, workflowId, {
                    state: 'loading',
                    reason: options?.reason || null,
                    requestToken,
                    updatedAt: Date.now(),
                }, workflowId),
        };
        notify();
        return Promise.resolve()
            .then(() => loader(workflowId))
            .then((workspace) => {
                const sync = state.workspaceSyncByWorkflow[workflowId];
                if (sync?.requestToken !== requestToken) return getState();
                if (!workspace) throw new Error('workspace is empty');
                return setWorkspace(workspace, { snapshot: workspace.snapshot });
            })
            .catch((error) => {
                const sync = state.workspaceSyncByWorkflow[workflowId];
                if (sync?.requestToken !== requestToken) return getState();
                state = {
                    ...state,
                    workspaceSyncByWorkflow: rememberWorkflowEntry(state.workspaceSyncByWorkflow, workflowId, {
                            state: 'error',
                            reason: String(error?.message || error || 'workspace sync failed').slice(0, 500),
                            requestToken,
                            updatedAt: Date.now(),
                        }, workflowId),
                };
                notify();
                return getState();
            });
    }

    function setActiveCandidates(candidates) {
        const values = Array.isArray(candidates) ? candidates : [];
        let workspaceByWorkflow = { ...state.workspaceByWorkflow };
        values.forEach((candidate) => {
            const workflowId = String(candidate?.workflow?.workflow_id || candidate?.workspace?.snapshot?.workflow_id || '');
            if (workflowId && candidate?.workspace) {
                workspaceByWorkflow = rememberWorkflowEntry(
                    workspaceByWorkflow,
                    workflowId,
                    candidate.workspace,
                    state.workflowId,
                );
            }
        });
        state = {
            ...state,
            workspaceByWorkflow,
            activeCandidates: values.slice(0, 32).map(candidate => ({
                ...candidate,
                workflow: candidate?.workflow ? projectWorkflowSnapshot(candidate.workflow) : null,
                workspace: candidate?.workspace ? projectWorkspaceData(candidate.workspace) : null,
            })),
        };
        notify();
        return getState();
    }

    function markCommandPending(commandKey, pending = true) {
        const key = String(commandKey || '');
        if (!key) return getState();
        const pendingCommands = { ...state.pendingCommands };
        if (pending) pendingCommands[key] = true;
        else delete pendingCommands[key];
        state = { ...state, pendingCommands };
        notify();
        return getState();
    }

    function setError(error) {
        state = { ...state, error: error ? String(error) : null };
        notify();
        return getState();
    }

    function lastEventIdFor(workflowId) {
        if (state.workflowId === workflowId && state.lastEventId) return state.lastEventId;
        return storedCursor(workflowId);
    }

    function resetCursor(workflowId) {
        const normalizedWorkflowId = String(workflowId || '');
        if (!normalizedWorkflowId) return getState();
        if (storage) {
            try {
                const cursorKey = `${keyPrefix}${normalizedWorkflowId}`;
                storage.removeItem(cursorKey);
                storage.removeItem(`${cursorKey}${SEQ_SUFFIX}`);
            } catch (_) {
                // A storage failure must not prevent the server snapshot from
                // becoming the recovery source on the next connection.
            }
        }
        if (state.workflowId !== normalizedWorkflowId) return getState();
        state = {
            ...state,
            lastEventId: null,
            lastSeq: 0,
            needsCatchup: false,
            error: null,
        };
        notify();
        return getState();
    }

    function consume(frame) {
        if (!frame || (frame.kind !== 'event' && frame.kind !== 'snapshot')) {
            return { accepted: false, reason: 'invalid-frame' };
        }
        if (frame.kind === 'snapshot') {
            const snapshot = frame.snapshot;
            if (!snapshot || !snapshot.state || !snapshot.workflow_id) {
                return { accepted: false, reason: 'invalid-snapshot' };
            }
            const snapshotSeq = Number(snapshot.snapshot_seq ?? snapshot.state.latest_seq ?? 0);
            if (!Number.isInteger(snapshotSeq) || snapshotSeq < 0) {
                return { accepted: false, reason: 'invalid-snapshot' };
            }
            if (state.workflowId && snapshot.workflow_id !== state.workflowId) {
                return { accepted: false, reason: 'workflow-mismatch' };
            }
            if (snapshotSeq < state.lastSeq) {
                return { accepted: false, reason: 'stale-snapshot' };
            }
            const workspace = state.workspace;
            const snapshotState = snapshot.state;
            const itemCount = Number(snapshotState.item_count);
            if (Number.isInteger(itemCount) && itemCount > 0) {
                workspace.items.total = itemCount;
            }
            const projectedSnapshot = projectWorkflowSnapshot(snapshotState);
            const hydratedWorkspace = workspaceProgressState(
                { snapshot: projectedSnapshot },
                workspace,
            );
            if (Number.isInteger(itemCount) && itemCount > 0) {
                hydratedWorkspace.items.total = itemCount;
            }
            hydratedWorkspace.executionState = snapshotState.execution_state
                ? String(snapshotState.execution_state)
                : hydratedWorkspace.executionState;
            hydratedWorkspace.controlState = snapshotState.control_state
                ? String(snapshotState.control_state)
                : hydratedWorkspace.controlState;
            hydratedWorkspace.resultStatus = snapshotState.result_status
                ? String(snapshotState.result_status)
                : hydratedWorkspace.resultStatus;
            hydratedWorkspace.updatedAt = snapshotState.updated_at || hydratedWorkspace.updatedAt;
            state = {
                ...state,
                workflow: projectedSnapshot,
                workflowId: snapshot.workflow_id,
                workspace: hydratedWorkspace,
                lastEventId: snapshot.snapshot_event_id || snapshot.state.latest_event_id || null,
                lastSeq: snapshotSeq,
                needsCatchup: false,
                error: null,
            };
            if (state.workspaceData) {
                state.workspaceData = {
                    ...state.workspaceData,
                    snapshot: projectedSnapshot,
                };
                state.workspaceByWorkflow = rememberWorkflowEntry(
                    state.workspaceByWorkflow,
                    state.workflowId,
                    state.workspaceData,
                    state.workflowId,
                );
            }
            persist();
            notify();
            return { accepted: true, kind: 'snapshot' };
        }

        const event = frame.event;
        if (!event || !event.workflow_id || !Number.isInteger(event.seq) || event.seq < 1 || !event.event_id) {
            return { accepted: false, reason: 'invalid-event' };
        }
        if (state.workflowId && event.workflow_id !== state.workflowId) {
            return { accepted: false, reason: 'workflow-mismatch' };
        }
        if (event.seq <= state.lastSeq) {
            return { accepted: false, reason: 'duplicate' };
        }
        if (state.lastSeq > 0 && event.seq !== state.lastSeq + 1) {
            state = { ...state, needsCatchup: true, error: `event gap: expected ${state.lastSeq + 1}, got ${event.seq}` };
            notify();
            return { accepted: false, reason: 'gap', expectedSeq: state.lastSeq + 1, actualSeq: event.seq };
        }
        const eventStatePatch = advanceWorkspaceWithEvent(state.workspace, event);
        const projectedWorkflow = projectWorkflowSnapshot({
            ...(state.workflow || {}),
            ...eventStatePatch,
            workflow_id: event.workflow_id,
            latest_event_id: event.event_id,
            latest_seq: event.seq,
            latest_event: event,
        });
        state = {
            ...state,
            workflowId: event.workflow_id,
            lastEventId: event.event_id,
            lastSeq: event.seq,
            workflow: projectedWorkflow,
            error: null,
        };
        if (state.workspaceData) {
            state.workspaceData = {
                ...state.workspaceData,
                snapshot: projectedWorkflow,
            };
            state.workspaceByWorkflow = rememberWorkflowEntry(
                state.workspaceByWorkflow,
                state.workflowId,
                state.workspaceData,
                state.workflowId,
            );
        }
        persist();
        notify();
        return { accepted: true, kind: 'event' };
    }

    async function connect(workflowId, api) {
        await close();
        prepare(workflowId);
        stream = await api.openWorkflowEvents(workflowId, state.lastEventId);
        detachFrame = stream.onFrame((frame) => consume(frame));
        detachError = stream.onError((error) => {
            state = { ...state, connected: false, error: error?.message || 'workflow stream failed' };
            notify();
        });
        state = { ...state, connected: true };
        notify();
        return getState();
    }

    async function close() {
        detachFrame?.();
        detachError?.();
        detachFrame = null;
        detachError = null;
        if (stream) await stream.close();
        stream = null;
        if (state.connected) {
            state = { ...state, connected: false };
            notify();
        }
    }

    return Object.freeze({
        getState,
        subscribe,
        consume,
        prepare,
        setWorkspace,
        hydrate,
        setActiveCandidates,
        markCommandPending,
        setError,
        lastEventIdFor,
        resetCursor,
        connect,
        close,
    });
}

const workflowStoreExports = { createWorkflowStore, projectWorkflowSnapshot, projectWorkspaceData, projectSystemInput, STORAGE_PREFIX };
if (typeof module !== 'undefined' && module.exports) {
    module.exports = workflowStoreExports;
} else if (typeof globalThis !== 'undefined') {
    globalThis.createWorkflowStore = createWorkflowStore;
    globalThis.WORDTTS_WORKFLOW_STORE_PREFIX = STORAGE_PREFIX;
}
