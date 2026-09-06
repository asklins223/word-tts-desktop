/** Renderer module: systemInput.pages */
(function attachRendererFeature_systemInput_pages(root) {
    'use strict';

function systemInputRunIsComplete(systemInput = {}) {
    const status = String(systemInput?.input_status || '').trim().toLowerCase();
    const runStatus = String(systemInput?.input_run?.status || '').trim().toUpperCase();
    const control = systemInput?.input_run?.control;
    if (control && typeof control === 'object' && !Array.isArray(control)
        && control.stop_requested === true) return false;
    const entries = Array.isArray(systemInput?.entries) ? systemInput.entries : [];
    const units = Array.isArray(systemInput?.units) ? systemInput.units : [];
    const hasSucceededSummary = status === 'succeeded' || runStatus === 'SUCCEEDED';
    if (!hasSucceededSummary || (runStatus && runStatus !== 'SUCCEEDED')) return false;
    // A summary is not enough to claim a complete delivery. Require one
    // explicit succeeded result for every projected unit; missing or stale
    // unit rows must remain visible as an unresolved reconciliation state.
    if (!entries.length) return false;
    if (!units.length) {
        return entries.every(entry => String(entry?.input_status || '').trim().toLowerCase() === 'succeeded');
    }
    const unitIds = units.map(unit => String(unit?.unit_id || '').trim());
    if (unitIds.some(unitId => !unitId) || new Set(unitIds).size !== units.length) return false;
    const entriesByUnit = new Map();
    for (const entry of entries) {
        const unitId = String(entry?.unit_id || '').trim();
        if (!unitId || entriesByUnit.has(unitId)) return false;
        entriesByUnit.set(unitId, entry);
    }
    return entriesByUnit.size === units.length
        && units.every(unit => String(entriesByUnit.get(String(unit.unit_id))?.input_status || '').trim().toLowerCase() === 'succeeded');
}

function systemInputEntryStatus(systemInput = {}, entry = null) {
    const explicit = String(entry?.input_status || '').trim().toLowerCase();
    if (explicit) return explicit;
    const overall = String(systemInput?.input_status || '').trim().toLowerCase();
    const runStatus = String(systemInput?.input_run?.status || '').trim().toUpperCase();
    // A run or terminal summary without a per-unit result is unknown, never
    // successful. This keeps partial projections from being painted green.
    if (systemInput?.input_run
        || ['succeeded', 'running', 'failed_retryable', 'needs_reconcile', 'partial_success', 'ambiguous'].includes(overall)
        || ['RUNNING', 'SUCCEEDED', 'PARTIAL_SUCCESS', 'AMBIGUOUS'].includes(runStatus)) {
        return 'needs_reconcile';
    }
    return overall === 'pending_execute' ? 'pending_execute' : 'pending_config';
}

function systemInputPageRunSnapshot(workspace = currentWorkspace) {
    const systemInput = workspace?.system_input || {};
    const rows = systemInputUnitRows(workspace);
    const runStatus = String(systemInput.input_run?.status || '').trim().toUpperCase();
    const rawInputStatus = String(systemInput.input_status || '').trim().toLowerCase();
    const counts = {
        total: rows.length,
        complete: 0,
        running: 0,
        pending: 0,
        retryable: 0,
        reconcile: 0,
    };
    rows.forEach(row => {
        const status = systemInputEntryStatus(systemInput, row.entry);
        if (status === 'succeeded') counts.complete += 1;
        else if (status === 'running') counts.running += 1;
        else if (['failed_retryable', 'failed'].includes(status)) counts.retryable += 1;
        else if (['needs_reconcile', 'ambiguous'].includes(status)) counts.reconcile += 1;
        else counts.pending += 1;
    });
    if (counts.total === 0 && runStatus === 'SUCCEEDED') counts.complete = 0;
    const status = rawInputStatus || (
        runStatus === 'RUNNING'
            ? 'running'
            : runStatus === 'PENDING'
                ? 'pending_execute'
                : runStatus === 'SUCCEEDED'
                    ? 'succeeded'
                    : runStatus === 'AMBIGUOUS'
                        ? 'needs_reconcile'
                        : 'pending_execute'
    );
    const percent = counts.total > 0
        ? Math.round((Math.min(counts.complete, counts.total) / counts.total) * 100)
        : systemInputRunIsComplete(systemInput) ? 100 : 0;
    return { systemInput, rows, runStatus, status, counts, percent };
}

function systemInputRunControlState(systemInput = {}) {
    const run = systemInput?.input_run;
    const control = run?.control && typeof run.control === 'object' && !Array.isArray(run.control)
        ? run.control
        : {};
    const runStatus = String(run?.status || '').trim().toUpperCase();
    const runErrorCode = String(run?.error_code || '').trim().toUpperCase();
    const browserClosed = ['INPUT_RUN_STOPPED', 'INPUT_BROWSER_CLOSED'].includes(runErrorCode);
    // A terminal-looking success with a retained stop marker is the short
    // handoff window before the backend finalizer rolls it to FAILED. Keep
    // the controls in the stopping state during that window instead of
    // briefly painting a completed run and hiding the button.
    const stopping = control.stop_requested === true;
    const active = Boolean(run?.input_run_id)
        && !browserClosed
        && (['PENDING', 'RUNNING'].includes(runStatus) || stopping);
    return {
        active,
        paused: active && !stopping && control.pause_requested === true,
        stopping: active && stopping,
    };
}

function systemInputRunWasStopped(systemInput = {}) {
    const run = systemInput?.input_run || {};
    const errorCode = String(run.error_code || '').trim().toUpperCase();
    return run?.control?.stop_requested === true
        || errorCode === 'INPUT_RUN_STOPPED'
        || errorCode === 'INPUT_BROWSER_CLOSED';
}

function renderSystemInputRunControls(systemInput = null, { forceHidden = false } = {}) {
    const pauseButton = $('system-input-page-pause-btn');
    const resumeButton = $('system-input-page-resume-btn');
    const stopButton = $('system-input-page-stop-btn');
    if (!pauseButton && !resumeButton && !stopButton) return;
    const state = forceHidden ? { active: false, paused: false, stopping: false } : systemInputRunControlState(systemInput || {});
    const busy = systemInputRunControlBusy;
    const setButton = (button, { hidden, disabled, text, title }) => {
        if (!button) return;
        button.hidden = hidden;
        button.disabled = disabled;
        button.textContent = text;
        button.title = title;
        button.setAttribute('aria-busy', busy && !hidden ? 'true' : 'false');
    };
    setButton(pauseButton, {
        hidden: !state.active || state.paused || state.stopping,
        disabled: busy || state.paused || state.stopping,
        text: busy ? '处理中…' : '暂停录入',
        title: '在安全页面检查点暂停，不会中断正在提交的页面动作',
    });
    setButton(resumeButton, {
        hidden: !state.active || !state.paused || state.stopping,
        disabled: busy || state.stopping,
        text: busy ? '处理中…' : '恢复录入',
        title: '继续执行尚未完成的录入单元',
    });
    setButton(stopButton, {
        hidden: !state.active,
        disabled: busy || state.stopping,
        text: state.stopping ? '正在停止…' : busy ? '处理中…' : '停止录入',
        title: state.stopping
            ? '已收到停止请求，当前页面操作结束后不会再打开新的浏览器窗口'
            : '停止本次系统录入；当前页面操作会先结束，未处理单元保留为可重试',
    });
}

function systemInputPageTone(status, { audioReady = true } = {}) {
    const key = String(status || '').trim().toLowerCase();
    if (['succeeded'].includes(key)) return 'success';
    if (['needs_reconcile', 'ambiguous', 'failed_retryable', 'failed'].includes(key)) return 'warning';
    if (!audioReady) return 'danger';
    return 'info';
}

function systemInputReconciliationTarget(systemInput = {}) {
    const run = systemInput?.input_run;
    if (!run || typeof run !== 'object') return null;
    const attempts = Array.isArray(run.attempts) ? run.attempts : [];
    const attempt = [...attempts].reverse().find(candidate => (
        candidate
        && ['AMBIGUOUS', 'NEEDS_RECONCILE'].includes(String(candidate.status || '').trim().toUpperCase())
    ));
    if (!attempt?.attempt_id) return null;
    return {
        inputRunId: String(run.input_run_id || '').trim(),
        attemptId: String(attempt.attempt_id || '').trim(),
        entryId: String(attempt.entry_id || '').trim(),
        diagnostic: typeof systemInputDiagnosticText === 'function'
            ? systemInputDiagnosticText(systemInput)
            : String(attempt.error_message || run.error_message || '').trim(),
    };
}

function systemInputNeedsReconciliation(systemInput = {}, displayStatus = '') {
    const status = String(displayStatus || systemInput?.input_status || '').trim().toLowerCase();
    const runStatus = String(systemInput?.input_run?.status || '').trim().toUpperCase();
    return ['needs_reconcile', 'ambiguous'].includes(status)
        || runStatus === 'AMBIGUOUS'
        || Boolean(systemInputReconciliationTarget(systemInput));
}

function systemInputVerificationOutcome(systemInput = {}, attemptId = '') {
    const run = systemInput?.input_run;
    const attempts = Array.isArray(run?.attempts) ? run.attempts : [];
    const targetAttemptId = String(attemptId || '').trim();
    for (let index = attempts.length - 1; index >= 0; index -= 1) {
        const attempt = attempts[index];
        if (targetAttemptId && String(attempt?.attempt_id || '').trim() !== targetAttemptId) continue;
        const outcome = attempt?.evidence?.external_record_verification;
        if (outcome && typeof outcome === 'object') return outcome;
    }
    return null;
}

function renderSystemInputReconciliationPanel(workspace, displayStatus) {
    const panel = $('system-input-reconcile-panel');
    if (!panel) return;
    const systemInput = workspace?.system_input || {};
    const runStopped = systemInputRunWasStopped(systemInput);
    const target = systemInputReconciliationTarget(systemInput);
    const visible = systemInputNeedsReconciliation(systemInput, displayStatus) && Boolean(target);
    panel.hidden = !visible;
    if (!visible) return;
    const outcome = systemInputVerificationOutcome(systemInput, target.attemptId);
    const message = $('system-input-reconcile-message');
    const statusBadge = $('system-input-reconcile-status');
    const diagnostic = target.diagnostic;
    if (message) {
        if (runStopped) {
            message.textContent = '本次录入已停止；不会自动重新打开浏览器。待核验结果请手动核对，确认后再决定是否安全重试。';
        } else if (String(outcome?.status || '') === 'needs_manual_resolution') {
            message.textContent = String(outcome?.message || '平台存在多条同名记录，需要先处理具体问题。');
        } else if (String(outcome?.status || '') === 'failed') {
            message.textContent = `自动核验没有完成：${String(outcome?.message || '原因未知')}。可以重试核验，或使用下方的手动处理。`;
        } else if (systemInput.executor_available === false) {
            message.textContent = '页面录入执行器尚未连接，暂时只能用下方的手动处理；重新打开应用恢复执行器后会提供自动核验。';
        } else if (diagnostic) {
            message.textContent = `${diagnostic}。服务端不会自动重试，避免重复创建外部记录；点击下方按钮即可自动只读核验。`;
        } else {
            message.textContent = '页面执行结果暂时无法确认。点击下方按钮即可自动只读核验，无需填写任何平台 ID。';
        }
    }
    if (statusBadge) {
        const busy = systemInputReconciliationBusy || systemInputVerificationBusy;
        statusBadge.textContent = busy
            ? '核验中'
            : runStopped
                ? '已停止，待核验'
                : String(outcome?.status || '') === 'needs_manual_resolution'
                ? '需处理'
                : String(outcome?.status || '') === 'failed'
                    ? '核验失败'
                    : '待核验';
    }
    const verifyButton = $('system-input-reconcile-verify-btn');
    const notSubmittedButton = $('system-input-reconcile-not-submitted-btn');
    const busy = systemInputReconciliationBusy || systemInputVerificationBusy;
    if (verifyButton) {
        verifyButton.hidden = systemInput.executor_available === false;
        verifyButton.disabled = busy;
        verifyButton.setAttribute('aria-busy', systemInputVerificationBusy ? 'true' : 'false');
        verifyButton.textContent = systemInputVerificationBusy
            ? '正在只读核验…'
            : runStopped
                ? '手动只读核验平台记录'
                : '自动核验平台记录';
    }
    if (notSubmittedButton) notSubmittedButton.disabled = busy;
    panel.setAttribute('aria-busy', busy ? 'true' : 'false');
}

function systemInputEvidenceHash(payload) {
    const source = JSON.stringify(payload || {});
    // Keep the evidence hash stable for the same reconciliation command. The
    // idempotency key is intentionally reused after a network retry; a random
    // hash would make the retried request look like a different body and be
    // rejected before the server can replay the original result.
    let hash = 2166136261;
    for (let index = 0; index < source.length; index += 1) {
        hash ^= source.charCodeAt(index);
        hash = Math.imul(hash, 16777619);
    }
    return `desktop-${source.length}-${(hash >>> 0).toString(16).padStart(8, '0')}`;
}

async function resolveSystemInputAmbiguity(decision) {
    if (systemInputReconciliationBusy || systemInputVerificationBusy) return false;
    const resolution = String(decision || '').trim().toUpperCase();
    // The renderer no longer collects a platform record ID: users cannot
    // reliably find one. "已在平台生成" is answered by the automated
    // read-only verification; this manual path only ever declares that the
    // platform has no record.
    if (resolution !== 'NOT_SUBMITTED') return false;
    const workspace = systemInputInteractionWorkspace();
    const systemInput = workspace?.system_input || {};
    const target = systemInputReconciliationTarget(systemInput);
    if (!workspace || !target?.inputRunId || !target?.attemptId) {
        showToast('当前没有可处理的待核验录入结果', 'warning');
        return false;
    }
    const workflowId = isHistoryResultView()
        ? historyResultWorkflowId(activeResultContext)
        : String(currentSession?.session_id || '');
    if (!workflowApi?.resolveSystemInputExternalOperation || !workflowId) {
        showToast('当前版本缺少录入结果核验接口', 'error');
        return false;
    }
    const evidence = {
        source: 'desktop-system-input',
        reference: target.attemptId,
        summary: '用户核对后确认平台未生成录入记录',
        evidence_hash: systemInputEvidenceHash({
            workflowId,
            inputRunId: target.inputRunId,
            attemptId: target.attemptId,
            decision: resolution,
        }),
    };
    systemInputReconciliationBusy = true;
    renderSystemInputReconciliationPanel(workspace, 'needs_reconcile');
    try {
        const response = await workflowApi.resolveSystemInputExternalOperation(workflowId, {
            input_run_id: target.inputRunId,
            attempt_id: target.attemptId,
            decision: resolution,
            external_record_id: null,
            evidence,
            resolved_by: 'desktop',
        }, {
            idempotencyKey: `renderer-system-input-resolve-${workflowId}-${target.inputRunId}-${target.attemptId}-${resolution}`,
        });
        const updated = isHistoryResultView()
            ? updateHistoryResultWorkspace(response?.workspace, activeResultContext)
            : applySystemInputWorkspaceResponse(response);
        if (!updated) throw new Error('服务端未返回更新后的工作区');
        showToast('已确认平台未生成记录，当前单元可以安全重试', 'success');
        showSystemInputProgressPage({ workspace: updated, refresh: false });
        return true;
    } catch (error) {
        showToast(workflowAdapter.issueMessage?.(error)?.message || `录入结果核验失败：${error.message || '请稍后重试'}`, 'error');
        if (isHistoryResultView()) await refreshHistoryResultWorkspace(activeResultContext, { silent: true });
        else await hydrateWorkflowWorkspace(workflowId, { silent: true });
        return false;
    } finally {
        systemInputReconciliationBusy = false;
        renderSystemInputReconciliationPanel(systemInputInteractionWorkspace(), 'needs_reconcile');
    }
}

const SYSTEM_INPUT_VERIFY_POLL_MS = 2500;
const SYSTEM_INPUT_VERIFY_TIMEOUT_MS = 6 * 60 * 1000;

function systemInputVerificationTerminal(systemInput = {}, attemptId = '') {
    // The verified attempt left its ambiguous state: this round is done even
    // when other units of the same run still need reconciliation. Checking
    // the target first also keeps a stale failed outcome from an earlier
    // verification of the same attempt from masking a successful re-run.
    if (attemptId) {
        const attempts = Array.isArray(systemInput?.input_run?.attempts) ? systemInput.input_run.attempts : [];
        const target = attempts.find(item => String(item?.attempt_id || '') === String(attemptId));
        if (target && !['AMBIGUOUS', 'NEEDS_RECONCILE'].includes(String(target.status || '').trim().toUpperCase())) {
            return 'resolved';
        }
    }
    const status = String(systemInput?.input_status || '').trim().toLowerCase();
    if (['failed_retryable', 'succeeded', 'partial_success'].includes(status)) return 'resolved';
    const outcome = systemInputVerificationOutcome(systemInput, attemptId);
    if (outcome && ['needs_manual_resolution', 'failed'].includes(String(outcome.status || ''))) {
        return String(outcome.status || '');
    }
    return '';
}

function systemInputRemainingAmbiguousCount(systemInput = {}) {
    const run = systemInput?.input_run;
    const attempts = Array.isArray(run?.attempts) ? run.attempts : [];
    // Mirror the server's aggregation: only the latest attempt per entry
    // counts towards the run's reconciliation state.
    const latestByEntry = new Map();
    attempts.forEach(item => {
        const entryId = String(item?.entry_id || '');
        if (!entryId || !item || typeof item !== 'object') return;
        const previous = latestByEntry.get(entryId);
        if (!previous || String(item.created_at || '') >= String(previous.created_at || '')) {
            latestByEntry.set(entryId, item);
        }
    });
    let count = 0;
    latestByEntry.forEach(item => {
        if (['AMBIGUOUS', 'NEEDS_RECONCILE'].includes(String(item.status || '').trim().toUpperCase())) count += 1;
    });
    return count;
}

function systemInputResolvedToast(systemInput = {}) {
    const run = systemInput?.input_run || {};
    const attempts = Array.isArray(run.attempts) ? run.attempts : [];
    const attempt = [...attempts].reverse().find(candidate => (
        candidate && ['FAILED'].includes(String(candidate.status || '').trim().toUpperCase())
        && String(candidate.error_code || '') === 'INPUT_PARTIAL_EXTERNAL_RECORD'
    ));
    if (attempt) return '已核对到平台记录，本次录入可以安全重试（将接续既有记录）';
    return '平台没有发现同名记录，已开放安全重试';
}

async function runSystemInputVerification({ automatic = false } = {}) {
    if (systemInputReconciliationBusy || systemInputVerificationBusy) return false;
    const workspace = systemInputInteractionWorkspace();
    const systemInput = workspace?.system_input || {};
    if (automatic && systemInputRunWasStopped(systemInput)) return false;
    const target = systemInputReconciliationTarget(systemInput);
    if (!workspace || !target?.inputRunId || !target?.attemptId) {
        if (!automatic) showToast('当前没有可处理的待核验录入结果', 'warning');
        return false;
    }
    if (systemInput.executor_available === false) {
        if (!automatic) showToast('页面录入执行器尚未连接，无法自动核验', 'warning');
        return false;
    }
    const workflowId = isHistoryResultView()
        ? historyResultWorkflowId(activeResultContext)
        : String(currentSession?.session_id || '');
    if (!workflowApi?.verifySystemInputExternalRecord || !workflowId) {
        if (!automatic) showToast('当前版本缺少只读核验接口', 'error');
        return false;
    }
    systemInputVerificationBusy = true;
    renderSystemInputReconciliationPanel(workspace, 'needs_reconcile');
    let latest = null;
    let terminal = '';
    try {
        await workflowApi.verifySystemInputExternalRecord(workflowId, {
            input_run_id: target.inputRunId,
            attempt_id: target.attemptId,
            automatic,
        }, {
            // A verification may legitimately be re-run after a failure (the
            // browser check is an observation, not a state transition), so
            // each click gets a fresh key instead of replaying the previous
            // "verification_started" response.
            idempotencyKey: `renderer-system-input-verify-${workflowId}-${target.inputRunId}-${target.attemptId}-${Date.now()}`,
        });
        // The 202 only starts the read-only browser check. Its outcome is
        // durable on the attempt evidence, so poll the authoritative
        // workspace until the attempt leaves the ambiguous state or the
        // verification result lands.
        const deadline = Date.now() + SYSTEM_INPUT_VERIFY_TIMEOUT_MS;
        while (Date.now() < deadline) {
            await new Promise(resolve => setTimeout(resolve, SYSTEM_INPUT_VERIFY_POLL_MS));
            // The user moved to another task; leave the verification running
            // server-side and stop polling for this one.
            if (!isHistoryResultView() && currentSession?.session_id !== workflowId) return false;
            latest = isHistoryResultView()
                ? await refreshHistoryResultWorkspace(activeResultContext, { silent: true })
                : await hydrateWorkflowWorkspace(workflowId, { silent: true });
            if (!latest) continue;
            const latestSystemInput = latest.system_input || {};
            if (automatic && systemInputRunWasStopped(latestSystemInput)) return false;
            terminal = systemInputVerificationTerminal(latestSystemInput, target.attemptId);
            if (terminal) break;
        }
        if (!latest) {
            if (!automatic) showToast('任务状态暂时无法读取，请稍后重试核验', 'warning');
            return false;
        }
        if (terminal === 'resolved') {
            const remaining = systemInputRemainingAmbiguousCount(latest.system_input || {});
            showToast(systemInputResolvedToast(latest.system_input || {})
                + (remaining > 0 ? `；还有 ${remaining} 个单元待核验` : ''), 'success');
            return true;
        }
        const outcome = systemInputVerificationOutcome(latest.system_input || {}, target.attemptId) || {};
        if (terminal === 'needs_manual_resolution') {
            showToast(String(outcome.message || '平台存在多条同名记录，需要先处理具体问题'), 'warning');
            return false;
        }
        if (terminal === 'failed') {
            showToast(`自动核验没有完成：${String(outcome.message || '原因未知')}`, 'error');
            return false;
        }
        if (!automatic) showToast('自动核验超时，请稍后重试或使用下方手动处理', 'warning');
        return false;
    } catch (error) {
        showToast(workflowAdapter.issueMessage?.(error)?.message || `自动核验失败：${error.message || '请稍后重试'}`, 'error');
        if (isHistoryResultView()) await refreshHistoryResultWorkspace(activeResultContext, { silent: true });
        else await hydrateWorkflowWorkspace(workflowId, { silent: true });
        return false;
    } finally {
        systemInputVerificationBusy = false;
        // Re-render the whole system-input surface, not just the panel: with
        // several ambiguous units the next one must become the reconciliation
        // target and its auto-verification has to be able to start now that
        // the busy flag is released.
        renderSystemInputSurface(systemInputInteractionWorkspace());
    }
}

function maybeAutoVerifySystemInput(workspace = currentWorkspace, displayStatus = '') {
    // The user asked for the verification to run by itself right after a
    // page write ends unresolved. Only the live task view auto-triggers;
    // opening an old task from history must not launch a browser session.
    if (isHistoryResultView()) return;
    if (displayStatus !== 'needs_reconcile') return;
    const systemInput = workspace?.system_input || {};
    // A manually closed browser is an explicit stop. Keep the reconciliation
    // target visible, but never turn the next render into a new browser run.
    // The user can still start a read-only check explicitly from the panel.
    if (systemInputRunWasStopped(systemInput)) return;
    if (systemInput.executor_available === false) return;
    const target = systemInputReconciliationTarget(systemInput);
    if (!target?.inputRunId || !target?.attemptId) return;
    if (systemInputVerificationOutcome(systemInput, target.attemptId)) return;
    const key = `${target.inputRunId}:${target.attemptId}`;
    if (systemInputAutoVerifiedRuns.has(key)) return;
    if (systemInputReconciliationBusy || systemInputVerificationBusy) return;
    systemInputAutoVerifiedRuns.add(key);
    void runSystemInputVerification({ automatic: true });
}

function systemInputPageGate(label, state, detail, tone = 'info') {
    const row = document.createElement('article');
    row.className = `system-input-page-gate is-${tone}`;
    const mark = document.createElement('span');
    mark.className = 'system-input-page-gate-mark';
    mark.setAttribute('aria-hidden', 'true');
    const copy = document.createElement('div');
    copy.className = 'system-input-page-gate-copy';
    const title = document.createElement('strong');
    title.textContent = label;
    const description = document.createElement('small');
    description.textContent = detail || '状态待同步';
    copy.append(title, description);
    const badge = document.createElement('span');
    badge.className = 'system-input-page-gate-state';
    badge.textContent = state;
    row.append(mark, copy, badge);
    return row;
}

function ensureTaskActionCopy(actionBar, { label, title, noteId } = {}) {
    if (!actionBar) return null;
    let copy = actionBar.querySelector('.task-action-copy');
    if (!copy) {
        copy = document.createElement('div');
        copy.className = 'task-action-copy';
        const labelNode = document.createElement('span');
        labelNode.className = 'task-action-label';
        const titleNode = document.createElement('strong');
        titleNode.className = 'task-action-title';
        const note = actionBar.querySelector(`#${noteId}`);
        if (note) {
            copy.append(labelNode, titleNode, note);
        } else {
            copy.append(labelNode, titleNode);
        }
        actionBar.insertBefore(copy, actionBar.firstChild);
    }
    const labelNode = copy.querySelector('.task-action-label');
    const titleNode = copy.querySelector('.task-action-title');
    if (labelNode) labelNode.textContent = label || '';
    if (titleNode) titleNode.textContent = title || '';
    return copy;
}

function renderSystemInputSubflow(systemInput = {}, rows = []) {
    const subflow = document.querySelector('#system-input-subflow');
    const gatePassed = systemInput.audio_gate?.technical_status === 'passed';
    const accepted = systemInput.audio_acceptance?.status === 'accepted';
    const boundaryData = systemInputBoundaryReviewData(systemInput);
    const boundaryNeedsReview = ['multiple_candidate'].includes(String(systemInput.unit_count_status || ''))
        || boundaryData.grouping?.strategy === 'user_override_stale';
    const inputStatus = String(systemInput.input_status || 'not_enabled').trim().toLowerCase();
    const runStarted = Boolean(systemInput.input_run)
        || ['running', 'succeeded', 'failed_retryable', 'needs_reconcile'].includes(inputStatus);
    const runComplete = systemInputRunIsComplete(systemInput);
    const stageState = {
        audio: gatePassed ? ['complete', '已通过'] : ['active', '待核验'],
        acceptance: accepted ? ['complete', '已确认'] : [gatePassed ? 'active' : 'waiting', '待确认'],
        input: runComplete
            ? ['complete', '已完成']
            : runStarted
                ? [inputStatus === 'running' ? 'active' : 'attention', systemInputStatusLabel(inputStatus)]
                : [accepted && !boundaryNeedsReview ? 'active' : 'waiting', '待开始'],
        // The platform has no review URL. The stage becomes complete only
        // after every unit is confirmed; the document name itself is shown in
        // each unit row and can be copied there or from the final page.
        review: runComplete
            ? ['complete', '文档名可搜索']
            : [runStarted ? 'active' : 'waiting', runStarted ? '等待回读' : '待录入'],
    };
    Object.entries(stageState).forEach(([stage, [state, label]]) => {
        const item = document.querySelector(`[data-system-input-stage="${stage}"]`);
        if (!item) return;
        item.classList.remove('is-complete', 'is-active', 'is-attention', 'is-waiting');
        item.classList.add(`is-${state}`);
        item.setAttribute('aria-current', state === 'active' || state === 'attention' ? 'step' : 'false');
        const detail = item.querySelector('small');
        if (detail) detail.textContent = label;
        item.setAttribute('aria-label', `${item.querySelector('strong')?.textContent || stage}：${label}`);
    });
}

function renderSystemInputPageFacts(workspace, snapshot) {
    const container = $('system-input-page-facts');
    if (!container) return;
    const systemInput = snapshot.systemInput || {};
    const run = systemInput.input_run || {};
    const unitRevisions = snapshot.rows
        .map(row => Number(row.unit?.structure_revision || row.entry?.structure_revision))
        .filter(value => Number.isInteger(value) && value > 0);
    const configurationRevision = Number(workspace?.configuration?.configuration_revision
        ?? systemInput.entries?.[0]?.configuration_revision);
    const audioRevision = Number(run.audio_revision || systemInput.audio_gate?.audio_revision);
    // audio_revision is a hash-derived integer (v4722694099933-style). Show
    // the short manifest hash instead — stable, and readable to the user.
    const audioVersionLabel = systemInput.audio_gate?.manifest_hash
        ? `v${String(systemInput.audio_gate.manifest_hash).slice(0, 8)}`
        : Number.isInteger(audioRevision) && audioRevision > 0 ? `v${audioRevision}` : '待同步';
    const facts = [
        ['录入运行 ID', run.input_run_id || '尚未创建'],
        ['录入类型', systemInput.input_type === 'textbook' ? '课文' : systemInput.input_type === 'vocabulary' ? '词汇' : '试卷'],
        ['音频版本', audioVersionLabel],
        ['配置版本', Number.isInteger(configurationRevision) && configurationRevision > 0 ? `v${configurationRevision}` : '待设置'],
        ['结构版本', unitRevisions.length ? `v${Math.max(...unitRevisions)}` : '待确认'],
    ];
    container.replaceChildren();
    facts.forEach(([label, value]) => {
        const row = document.createElement('div');
        const key = document.createElement('dt');
        key.textContent = label;
        const content = document.createElement('dd');
        content.textContent = systemInputDisplayValue(value, '—');
        if (String(label).includes('ID') || String(label).includes('版本')) content.classList.add('is-mono');
        row.append(key, content);
        container.appendChild(row);
    });
    const note = $('system-input-page-facts-note');
    if (note) {
        note.textContent = run.input_run_id
            ? (run.updated_at ? `最近同步：${new Date(run.updated_at).toLocaleString('zh-CN', { hour12: false })}` : '运行状态来自服务端只读投影。')
            : '尚未创建系统录入运行。保存配置并通过前置检查后，才会创建运行。';
    }
}

function renderSystemInputPageUnits(snapshot) {
    const container = $('system-input-page-units');
    if (!container) return;
    container.replaceChildren();
    if (!snapshot.rows.length) {
        const empty = document.createElement('div');
        empty.className = 'system-input-page-empty';
        empty.textContent = '还没有可展示的录入单元；请先完成录入目标配置。';
        container.appendChild(empty);
        return;
    }
    snapshot.rows.forEach((row, index) => {
        const entryStatus = systemInputEntryStatus(snapshot.systemInput, row.entry);
        const article = document.createElement('article');
        article.className = `system-input-page-unit is-${entryStatus}`;
        const heading = document.createElement('div');
        heading.className = 'system-input-page-unit-heading';
        const marker = document.createElement('span');
        marker.className = 'system-input-page-unit-marker';
        marker.textContent = String(index + 1).padStart(2, '0');
        const copy = document.createElement('div');
        copy.className = 'system-input-page-unit-copy';
        const title = document.createElement('h3');
        title.textContent = systemInputUnitRowLabel(row, index);
        const meta = document.createElement('p');
        const config = row.unit?.configuration || row.entry?.configuration || {};
        const inputType = String(
            row.unit?.input_type || row.entry?.input_type || snapshot.systemInput.input_type || '',
        ).trim().toLowerCase();
        const configuredName = inputType === 'textbook'
            ? (config.textbookNameZh || config.textbook_name_zh || config.textbookNameEn || config.textbook_name_en)
            : (config.paperName || config.paper_name || config.platformTemplateName || config.platformTemplateId);
        meta.textContent = [
            `${systemInputUnitRowContentCount(row, snapshot.systemInput)} 条内容`,
            systemInputDisplayValue(configuredName, '配置待完成'),
        ].join(' · ');
        copy.append(title, meta);
        const badge = document.createElement('span');
        badge.className = 'system-input-page-unit-badge';
        badge.textContent = systemInputStatusLabel(entryStatus);
        heading.append(marker, copy, badge);

        const detail = document.createElement('div');
        detail.className = 'system-input-page-unit-detail';
        const ids = document.createElement('div');
        ids.className = 'system-input-page-unit-ids';
        const entryId = document.createElement('span');
        entryId.textContent = `录入 ID：${row.entry?.entry_id || '暂未生成'}`;
        ids.appendChild(entryId);
        if (row.entry?.external_record_id) {
            const externalId = document.createElement('span');
            externalId.textContent = `外部 ID：${row.entry.external_record_id}`;
            ids.appendChild(externalId);
        }
        const review = document.createElement('div');
        review.className = 'system-input-page-unit-review';
        const reviewLabel = document.createElement('span');
        reviewLabel.textContent = '审阅文档名';
        review.appendChild(reviewLabel);
        const reviewPresentation = systemInputReviewPresentation(row.entry, entryStatus);
        const reviewName = document.createElement('strong');
        reviewName.textContent = reviewPresentation.name || reviewPresentation.label || '文档名待配置';
        reviewName.title = reviewPresentation.detail || reviewPresentation.name || '';
        review.appendChild(reviewName);
        if (reviewPresentation.name) {
            review.appendChild(createSystemInputDocumentCopyButton(reviewPresentation.name));
        }
        detail.append(ids, review);
        article.append(heading, detail);
        container.appendChild(article);
    });
}
function renderSystemInputProgressPage(workspace = currentWorkspace) {
    if (currentView !== 'system-input-progress') return;
    const page = $('page-system-input');
    if (!page) return;
    renderSystemInputRunControls(null, { forceHidden: true });
    const progressActions = page.querySelector('.system-input-page-action-bar');
    ensureTaskActionCopy(progressActions, {
        label: '下一步操作',
        title: '完成前置检查后开始',
        noteId: 'system-input-page-action-note',
    });
    const snapshot = systemInputPageRunSnapshot(workspace);
    const systemInput = snapshot.systemInput;
    if (!workspace || !systemInput || systemInput.available === false) {
        $('system-input-page-status')?.replaceChildren(document.createTextNode('等待任务'));
        if ($('system-input-page-summary')) $('system-input-page-summary').textContent = '当前没有可继续录入的任务。';
        if ($('system-input-progress-title')) $('system-input-progress-title').textContent = '等待任务同步';
        if ($('system-input-progress-message')) $('system-input-progress-message').textContent = '返回音频交付中心后，可以从当前任务继续。';
        return;
    }
    if (!systemInputDocumentEntryIsSupported(workspace)) {
        $('system-input-page-status')?.replaceChildren(document.createTextNode('暂不支持'));
        if ($('system-input-page-summary')) $('system-input-page-summary').textContent = '当前文档没有匹配已接入的录入脚本。';
        if ($('system-input-progress-title')) $('system-input-progress-title').textContent = '当前文档暂不支持系统录入';
        if ($('system-input-progress-message')) $('system-input-progress-message').textContent = systemInput.document_entry_support?.reason || '请返回音频交付中心继续。';
        $('system-input-page-accept-btn')?.setAttribute('hidden', 'true');
        $('system-input-page-start-btn')?.setAttribute('hidden', 'true');
        return;
    }
    if (systemInputRunIsComplete(systemInput)) {
        showFinalDeliveryPage({ workspace, refresh: false });
        return;
    }
    const status = snapshot.status;
    const runControl = systemInputRunControlState(systemInput);
    const runStopped = systemInputRunWasStopped(systemInput);
    const runComplete = systemInputRunIsComplete(systemInput);
    const summarySucceeded = status === 'succeeded' || snapshot.runStatus === 'SUCCEEDED';
    const displayStatus = summarySucceeded && !runComplete ? 'needs_reconcile' : status;
    // The generic "完成前置检查后开始" heading is wrong once a run exists:
    // after an ambiguous page write the user's job is reconciliation, not
    // precheck completion. Keep the heading in sync with the actual state.
    const actionHeadingTitle = displayStatus === 'needs_reconcile'
        ? '处理待核验的录入结果'
        : displayStatus === 'failed_retryable'
            ? '继续完成系统录入'
            : ['RUNNING', 'PENDING'].includes(snapshot.runStatus)
                ? '系统录入进行中'
                : '完成前置检查后开始';
    const actionHeadingNode = progressActions?.querySelector('.task-action-title');
    if (actionHeadingNode) actionHeadingNode.textContent = actionHeadingTitle;
    const tone = systemInputPageTone(displayStatus, { audioReady: systemInput.audio_gate?.technical_status === 'passed' });
    const statusPill = $('system-input-page-status');
    if (statusPill) {
        const controlTone = runControl.stopping || runControl.paused || runStopped ? 'warning' : tone;
        statusPill.className = `system-input-page-status is-${controlTone}`;
        statusPill.textContent = runControl.stopping
            ? '停止中'
            : runControl.paused
                ? '已暂停'
                : runStopped
                    ? '已停止'
                    : systemInputStatusLabel(displayStatus);
    }
    const sourceName = activeResultContext?.sourceFilename || currentSession?.source_filename || workspace.source_filename || '当前文档';
    if ($('system-input-page-summary')) {
        const summaryText = `${sourceName} · 沿用已验证音频，不重新生成；按录入单元持续读取外部系统结果。`;
        const summaryNode = $('system-input-page-summary');
        summaryNode.textContent = summaryText;
        // The subtitle is single-line truncated by CSS; expose the full text
        // on hover instead of leaving it cut off with no way to read it.
        summaryNode.setAttribute('title', summaryText);
    }
    const runCopy = runControl.stopping
        ? '正在停止系统录入'
        : runControl.paused
            ? '系统录入已暂停，等待恢复'
            : runStopped
                ? '系统录入已停止'
            : snapshot.runStatus === 'RUNNING'
                ? (snapshot.counts.running > 0 ? `系统录入（当前有 ${snapshot.counts.running} 个单元正在执行）` : '系统录入正在执行')
                : snapshot.runStatus === 'PENDING'
                    ? '系统录入已排队，等待执行'
                    : displayStatus === 'pending_config'
                        ? '等待录入目标配置'
                        : displayStatus === 'needs_reconcile'
                            ? '录入结果需要核验'
                            : displayStatus === 'failed_retryable'
                                ? '录入中断，可安全重试'
                                : '准备开始系统录入';
    if ($('system-input-progress-title')) $('system-input-progress-title').textContent = runCopy;
    const statusParts = [`${snapshot.counts.complete} 个已完成`];
    if (snapshot.counts.running) statusParts.push(`${snapshot.counts.running} 个录入中`);
    if (snapshot.counts.reconcile) statusParts.push(`${snapshot.counts.reconcile} 个录入结果待确认`);
    if (snapshot.counts.retryable) statusParts.push(`${snapshot.counts.retryable} 个可重试`);
    if (snapshot.counts.pending) statusParts.push(`${snapshot.counts.pending} 个待处理`);
    const startAction = workspaceAction('START_INPUT', workspace);
    let message = statusParts.join('，');
    if (runControl.stopping) {
        message = `${message}。已收到停止请求；当前页面操作结束后不会再打开新的浏览器窗口，未处理单元保留为可重试。`;
    } else if (runControl.paused) {
        message = `${message}。录入已暂停；点击“恢复录入”后会从未完成的单元继续。`;
    } else if (runStopped) {
        message = `${message}。本次录入已停止，不会自动重新打开浏览器；未处理单元保留为可重试，待核验结果需手动处理。`;
    } else if (snapshot.runStatus === 'RUNNING' || snapshot.runStatus === 'PENDING') {
        message = `${message}。页面会自动刷新服务端状态，运行中的外部副作用不会重复提交。`;
    } else if (displayStatus === 'needs_reconcile') {
        // The reconciliation panel explains what to do; the diagnostic line
        // explains why — otherwise the user only sees a generic warning.
        const diagnostic = typeof systemInputDiagnosticText === 'function'
            ? systemInputDiagnosticText(systemInput)
            : '';
        message = `${message}。存在无法确认的外部结果，请先完成对账；当前不会直接重试。${diagnostic ? ` 最近状态：${diagnostic}` : ''}`;
    } else if (displayStatus === 'failed_retryable') {
        const diagnostic = typeof systemInputDiagnosticText === 'function'
            ? systemInputDiagnosticText(systemInput)
            : '';
        message = `${message}。${diagnostic || systemInput.input_run?.error_message || '可以沿用同一配置安全重试。'}`;
    } else if (displayStatus === 'pending_config') {
        message = '还需补充录入目标或平台字段；请返回音频交付中心打开配置，保存后不会重新生成音频。';
    } else if (startAction?.enabled !== true && startAction?.reason) {
        message = startAction.reason;
    }
    if ($('system-input-progress-message')) $('system-input-progress-message').textContent = message;

    const percent = Math.max(0, Math.min(100, snapshot.percent));
    if ($('system-input-progress-percent')) $('system-input-progress-percent').textContent = `${percent}%`;
    const track = $('system-input-progress-track');
    if (track) {
        track.setAttribute('aria-valuenow', String(percent));
        track.setAttribute('aria-valuetext', `${percent}%，${statusParts.join('，')}`);
        track.dataset.progressState = percent >= 100 ? 'complete' : percent > 0 ? 'active' : 'pending';
    }
    if ($('system-input-progress-fill')) {
        const fill = $('system-input-progress-fill');
        fill.style.width = `${percent}%`;
        fill.style.transform = 'none';
    }
    if ($('system-input-progress-complete')) $('system-input-progress-complete').textContent = `${snapshot.counts.complete} 个已完成`;
    if ($('system-input-progress-running')) $('system-input-progress-running').textContent = `${snapshot.counts.running} 个录入中`;
    if ($('system-input-progress-pending')) $('system-input-progress-pending').textContent = `${snapshot.counts.pending + snapshot.counts.retryable + snapshot.counts.reconcile} 个待处理`;

    const accepted = systemInput.audio_acceptance?.status === 'accepted';
    const gateList = $('system-input-page-gates');
    if (gateList) {
        const boundaryData = systemInputBoundaryReviewData(systemInput);
        const boundaryNeedsReview = String(systemInput.unit_count_status || '') === 'multiple_candidate'
            || boundaryData.grouping?.strategy === 'user_override_stale';
        const configured = snapshot.rows.length > 0
            && snapshot.rows.every(row => row.entry && String(row.entry?.input_status || '').toLowerCase() !== 'pending_config');
        const gatePassed = systemInput.audio_gate?.technical_status === 'passed';
        gateList.replaceChildren(
            systemInputPageGate('音频技术闸门', gatePassed ? '已通过' : '待核验', gatePassed ? '已验证音频产物满足录入前置要求。' : '音频产物尚未通过服务端技术核验。', gatePassed ? 'success' : 'warning'),
            systemInputPageGate('整批音频验收', accepted ? '已确认' : '待确认', accepted ? '当前音频批次已被用户明确验收。' : '先确认整批音频，才允许创建系统录入运行。', accepted ? 'success' : 'warning'),
            systemInputPageGate('录入单元边界', boundaryNeedsReview ? '待确认' : '已确认', boundaryNeedsReview ? '发现多个候选范围，请在录入目标设置中确认。' : `${snapshot.rows.length || '待解析'} 个录入单元已进入当前投影。`, boundaryNeedsReview ? 'warning' : 'success'),
            systemInputPageGate('平台字段', configured ? '已保存' : '待设置', configured ? '平台配置已保存，运行时会按只读配置执行。' : '还需填写平台模板、名称、分类等字段。', configured ? 'success' : 'warning'),
        );
    }
    renderSystemInputSubflow(systemInput, snapshot.rows);
    renderSystemInputPageFacts(workspace, snapshot);
    renderSystemInputPageUnits(snapshot);
    const unitsCount = $('system-input-page-units-count');
    if (unitsCount) unitsCount.textContent = `${snapshot.rows.length} 个单元`;
    const acceptanceAction = workspaceAction('ACCEPT_AUDIO', workspace);
    const acceptButton = $('system-input-page-accept-btn');
    if (acceptButton) {
        acceptButton.hidden = accepted || Boolean(systemInput.input_run);
        acceptButton.disabled = acceptanceAction?.enabled !== true;
        acceptButton.title = acceptanceAction?.enabled === true ? '确认当前整批已验证音频' : (acceptanceAction?.reason || '当前不能验收音频');
    }
    const startButton = $('system-input-page-start-btn');
    if (startButton) {
        const isWaitingRun = ['PENDING', 'RUNNING'].includes(snapshot.runStatus);
        const isSucceeded = runComplete;
        // In the reconciliation state the panel below owns every action; a
        // disabled "结果待核验" button next to it only reads as a dead end.
        startButton.hidden = isSucceeded || displayStatus === 'needs_reconcile';
        startButton.disabled = isWaitingRun || startAction?.enabled !== true;
        startButton.textContent = displayStatus === 'failed_retryable'
            ? '安全重试录入'
            : isWaitingRun
                ? '录入进行中'
                : '开始录入系统';
        startButton.title = startButton.disabled
            ? (isWaitingRun ? '系统录入正在执行，页面会自动刷新' : (startAction?.reason || '当前不能开始系统录入'))
            : '按已确认的录入单元开始系统录入';
    }
    renderSystemInputRunControls(systemInput);
    const actionNote = $('system-input-page-action-note');
    if (actionNote) {
        actionNote.textContent = runControl.stopping
            ? '正在停止本次录入；当前页面动作结束后不会再启动新的浏览器窗口。'
            : runControl.paused
                ? '录入已暂停；点击“恢复录入”后继续，关闭本页不会改变暂停状态。'
                : runStopped
                    ? '本次录入已停止；不会自动重新打开浏览器。需要继续时，请手动处理待核验结果或发起安全重试。'
            : snapshot.runStatus === 'RUNNING' || snapshot.runStatus === 'PENDING'
                    ? '状态每次都从服务端刷新；暂停、恢复和停止请求都以服务端状态为准。'
                    : displayStatus === 'needs_reconcile'
                        ? (!systemInputReconciliationTarget(systemInput)
                            ? '服务端汇总与逐单元明细暂不一致，请稍后重开本页刷新；如持续出现请反馈。'
                            : systemInput.executor_available === false
                                ? '页面录入执行器未连接，无法自动核验；恢复后重新打开本页即可继续。'
                                : '正在自动核验平台记录（只读）；核验后会自动开放安全重试或给出具体问题。')
                        : displayStatus === 'failed_retryable'
                            ? '重试会沿用当前配置和录入单元，不会重新生成音频。'
                            : '前置条件由服务端动作状态决定；需要修改录入目标时，请返回音频交付中心打开配置。';
    }
    renderSystemInputReconciliationPanel(workspace, displayStatus);
    maybeAutoVerifySystemInput(workspace, displayStatus);
}
function finalDeliveryContextWorkspace(workspace = null) {
    if (workspace) return workspace;
    if (isHistoryResultView()) return activeResultContext?.workspace || null;
    return currentWorkspace || activeResultContext?.workspace || null;
}

function renderFinalDeliveryFacts(container, facts) {
    if (!container) return;
    container.replaceChildren();
    facts.forEach(([label, value]) => {
        const row = document.createElement('div');
        const key = document.createElement('dt');
        key.textContent = label;
        const content = document.createElement('dd');
        content.textContent = systemInputDisplayValue(value, '—');
        if (String(label).includes('ID') || String(label).includes('版本')) content.classList.add('is-mono');
        row.append(key, content);
        container.appendChild(row);
    });
}

function finalDeliveryPresentation(context, workspace, systemInput, audioCount, unresolved) {
    const enabled = systemInput?.delivery_mode === 'audio_and_input';
    const status = String(systemInput?.input_status || 'not_enabled').trim().toLowerCase();
    const runStatus = String(systemInput?.input_run?.status || '').trim().toUpperCase();
    const summarySucceeded = status === 'succeeded' || runStatus === 'SUCCEEDED';
    if (!enabled) {
        if (audioCount > 0 && unresolved === 0) return {
            tone: 'success', title: '音频交付完成', message: '本次只生成音频，系统录入未启用；已验证音频和交付包入口都保留在本页。', label: '音频已完成',
        };
        return {
            tone: audioCount > 0 ? 'warning' : 'danger', title: audioCount > 0 ? '音频部分完成' : '交付需要处理', message: audioCount > 0 ? `已有 ${audioCount} 个已验证音频，仍有内容未纳入交付。` : '当前没有可交付的已验证音频。', label: '需要处理',
        };
    }
    if (summarySucceeded && !systemInputRunIsComplete(systemInput)) return {
        tone: 'warning', title: '录入结果待确认', message: '服务端汇总为已完成，但逐单元结果尚未全部回读；请以单元明细为准。', label: '待核验',
    };
    if (summarySucceeded) {
        if (audioCount > 0 && unresolved === 0) return {
            tone: 'success', title: '全部完成', message: '音频已通过交付核验，所有录入单元也已回读明确的完成状态。', label: '全部完成',
        };
        return {
            tone: audioCount > 0 ? 'warning' : 'danger', title: audioCount > 0 ? '录入已完成，音频需处理' : '音频交付需要处理', message: audioCount > 0 ? '系统录入已完成，但仍有音频内容未进入完整交付范围。' : '系统录入已完成，但当前没有可交付的已验证音频。', label: '需要处理',
        };
    }
    if (status === 'needs_reconcile') return {
        tone: 'warning', title: '录入结果待确认', message: '音频可以交付，但至少有一个外部录入结果无法确认；请打开录入进度页，按处理步骤核验结果。', label: '待核验',
    };
    if (status === 'failed_retryable') return {
        tone: 'warning', title: '音频已交付，录入可重试', message: '系统录入未全部完成；当前页面保留失败单元、录入 ID 和重试入口。', label: '可重试',
    };
    if (status === 'pending_execute' && !systemInput?.input_run) return {
        tone: 'info', title: '录入已准备好，尚未开始', message: '录入目标已保存。打开录入准备，检查提交清单后开始。', label: '待开始',
    };
    if (['running', 'pending_execute'].includes(status)) return {
        tone: 'info', title: '系统录入仍在进行', message: '音频已经准备好，系统录入结果尚未全部回读；请回到录入进度页查看运行状态。', label: systemInputStatusLabel(status),
    };
    return {
        tone: 'warning', title: '录入配置尚未完成', message: '音频已经准备好，但系统录入目标还没有满足执行条件。', label: systemInputStatusLabel(status),
    };
}

function renderFinalDeliveryEntries(context, workspace, systemInput) {
    const container = $('final-delivery-entries');
    if (!container) return;
    container.replaceChildren();
    const enabled = systemInput?.delivery_mode === 'audio_and_input';
    const rows = systemInputUnitRows(workspace);
    const count = $('final-delivery-entries-count');
    const copyAllButton = $('final-delivery-copy-all-names-btn');
    const syncCopyAllButton = (names = []) => {
        if (!copyAllButton) return;
        copyAllButton.disabled = !enabled || names.length === 0;
        copyAllButton.title = names.length
            ? `复制 ${names.length} 个审阅文档名，每行一个`
            : '当前没有可复制的审阅文档名';
        copyAllButton.setAttribute('aria-label', names.length
            ? `复制全部审阅文档名，共 ${names.length} 个`
            : '复制全部审阅文档名');
    };
    syncCopyAllButton();
    if (count) count.textContent = enabled ? `${rows.length} 个单元` : '未启用';
    if (!enabled) {
        const empty = document.createElement('div');
        empty.className = 'final-delivery-empty';
        empty.textContent = '本次未启用系统录入；音频交付作为唯一交付物。';
        container.appendChild(empty);
        return;
    }
    if (!rows.length) {
        const empty = document.createElement('div');
        empty.className = 'final-delivery-empty';
        empty.textContent = '系统录入已开启，但服务端暂未返回单元结果。';
        container.appendChild(empty);
        return;
    }
    const runError = systemInput?.input_run?.error_message || systemInput?.input_run?.error_code || '';
    const copyableNames = [];
    rows.forEach((row, index) => {
        const status = systemInputEntryStatus(systemInput, row.entry);
        const card = document.createElement('article');
        card.className = `final-delivery-entry is-${status}`;
        const heading = document.createElement('div');
        heading.className = 'final-delivery-entry-heading';
        const marker = document.createElement('span');
        marker.className = 'final-delivery-entry-marker';
        marker.textContent = String(index + 1).padStart(2, '0');
        const copy = document.createElement('div');
        copy.className = 'final-delivery-entry-copy';
        const title = document.createElement('h3');
        title.textContent = systemInputUnitRowLabel(row, index);
        const documentName = document.createElement('p');
        documentName.textContent = row.entry?.document_name || context?.sourceFilename || '未命名文档';
        documentName.title = documentName.textContent;
        copy.append(title, documentName);
        const badge = document.createElement('span');
        badge.className = 'final-delivery-entry-status';
        badge.textContent = systemInputStatusLabel(status);
        heading.append(marker, copy, badge);
        const fields = document.createElement('div');
        fields.className = 'final-delivery-entry-fields';
        const appendField = (label, value, { mono = false, node = null } = {}) => {
            const field = document.createElement('div');
            field.className = 'final-delivery-entry-field';
            const key = document.createElement('span');
            key.textContent = label;
            const content = node || document.createElement('strong');
            if (!node) content.textContent = systemInputDisplayValue(value, '—');
            if (mono) content.classList.add('is-mono');
            field.append(key, content);
            fields.appendChild(field);
        };
        appendField('录入 ID', row.entry?.entry_id || '暂未生成', { mono: true });
        appendField('文档名称', row.entry?.document_name || context?.sourceFilename || '未命名文档');
        appendField('录入单元', `${systemInputUnitRowLabel(row, index)} · ${systemInputUnitRowContentCount(row, systemInput)} 条内容`);
        const external = row.entry?.external_record_id
            ? `${row.entry.external_record_id}${row.entry.external_status ? ` · ${row.entry.external_status}` : ''}`
            : '暂未获取';
        appendField('外部记录', external, { mono: Boolean(row.entry?.external_record_id) });
        const reviewPresentation = systemInputReviewPresentation(row.entry, status);
        const reviewName = reviewPresentation.name || reviewPresentation.label || '文档名待配置';
        if (reviewPresentation.name) copyableNames.push(reviewPresentation.name);
        const reviewNode = document.createElement('div');
        reviewNode.className = 'system-input-document-value';
        const reviewValue = document.createElement('strong');
        reviewValue.textContent = reviewName;
        reviewValue.title = reviewPresentation.detail || reviewName;
        reviewNode.appendChild(reviewValue);
        if (reviewPresentation.name) {
            reviewNode.appendChild(createSystemInputDocumentCopyButton(reviewPresentation.name));
        }
        appendField('审阅文档名', reviewName, { node: reviewNode });
        if (['failed', 'failed_retryable', 'needs_reconcile', 'ambiguous'].includes(status) && runError) {
            appendField('处理说明', runError);
        }
        card.append(heading, fields);
        container.appendChild(card);
    });
    syncCopyAllButton(copyableNames);
}
function renderFinalDeliveryPage(workspace = null) {
    if (currentView !== 'final-delivery') return;
    const finalFrame = document.querySelector('.final-delivery-frame');
    const finalActions = finalFrame?.querySelector('.final-delivery-action-bar');
    ensureTaskActionCopy(finalActions, {
        label: '交付去向',
        title: '选择下一步',
        noteId: 'final-delivery-action-note',
    });
    const context = activeResultContext || {};
    const current = finalDeliveryContextWorkspace(workspace);
    const systemInput = current?.system_input || null;
    const files = Array.isArray(context.files) ? context.files : (current ? resultFilesFromArtifacts(current.items, current.artifacts, current) : generatedFiles);
    const audioCount = files.length;
    const workspaceCounts = current ? workspaceProgress(current) : {};
    const deliveryBlockers = (Array.isArray(current?.blockers) ? current.blockers : []).filter(blocker => (
        ['BLOCKING', 'ERROR'].includes(String(blocker?.severity || '').toUpperCase())
        && ['ARTIFACT_MISSING_OR_UNVERIFIED', 'ARTIFACT_FORMAT_UNSUPPORTED', 'ARTIFACT_METADATA_CONFLICT'].includes(String(blocker?.code || '').toUpperCase())
    ));
    const deliveryAffectedItemIds = new Set(
        deliveryBlockers.flatMap(blocker => Array.isArray(blocker?.affected_item_ids) ? blocker.affected_item_ids.map(String) : []),
    );
    const deliveryIssueCount = deliveryBlockers.length > 0
        ? Math.max(1, deliveryAffectedItemIds.size)
        : 0;
    const summaryCounts = resultSummaryCounts(context, audioCount, workspaceCounts, deliveryIssueCount);
    const unresolved = summaryCounts.unresolved;
    const presentation = finalDeliveryPresentation(context, current, systemInput, audioCount, unresolved);
    const sourceName = context.sourceFilename || currentSession?.source_filename || current?.source_filename || '未命名文档';
    const rows = systemInput ? systemInputUnitRows(current) : [];
    const run = systemInput?.input_run || {};
    const title = $('final-delivery-title');
    if (title) title.textContent = '最终结果';
    const statusCard = $('final-delivery-status-card');
    if (statusCard) {
        statusCard.className = `final-delivery-status-card is-${presentation.tone}`;
        statusCard.dataset.deliveryStatus = presentation.tone;
    }
    const headerStatus = $('final-delivery-header-status');
    if (headerStatus) {
        headerStatus.className = `system-input-page-status is-${presentation.tone}`;
        headerStatus.textContent = presentation.label;
    }
    if ($('final-delivery-summary')) $('final-delivery-summary').textContent = sourceName;
    const inputEnabled = systemInput?.delivery_mode === 'audio_and_input';
    if (finalFrame) {
        finalFrame.dataset.deliveryMode = inputEnabled ? 'audio-and-input' : 'audio-only';
    }
    const inputPanel = document.querySelector('.final-delivery-input-panel');
    if (inputPanel) inputPanel.hidden = !inputEnabled;
    const entriesPanel = document.querySelector('.final-delivery-entries-panel');
    if (entriesPanel) entriesPanel.hidden = !inputEnabled;
    ['final-input-unit-count'].forEach(id => { if ($(id)?.parentElement) $(id).parentElement.hidden = !inputEnabled; });
    if ($('final-delivery-status-title')) $('final-delivery-status-title').textContent = presentation.title;
    if ($('final-delivery-status-message')) $('final-delivery-status-message').textContent = presentation.message;
    if ($('final-audio-count')) $('final-audio-count').textContent = String(audioCount);
    if ($('final-input-unit-count')) $('final-input-unit-count').textContent = systemInput?.delivery_mode === 'audio_and_input' ? String(rows.length) : '—';

    const delivery = current?.delivery || context.delivery || {};
    const format = files[0]?.format || context.format || current?.configuration?.effective?.format || '待同步';
    const includedCount = Array.isArray(delivery.included_item_ids) ? delivery.included_item_ids.length : audioCount;
    renderFinalDeliveryFacts($('final-delivery-audio-facts'), [
        ['源文档', sourceName],
        ['已验证音频', `${audioCount} 个`],
        ['交付范围', `${includedCount} 个已纳入交付`],
        ['输出格式', String(format).toUpperCase()],
        ['交付包', delivery.zip_available === true ? '已准备，可下载' : '点击时由服务端整理'],
    ]);
    const inputStatus = systemInput?.input_status || 'not_enabled';
    const configurationRevision = Number(current?.configuration?.configuration_revision ?? systemInput?.entries?.[0]?.configuration_revision);
    const audioRevision = Number(run.audio_revision || systemInput?.audio_gate?.audio_revision);
    const audioVersionLabel = systemInput?.audio_gate?.manifest_hash
        ? `v${String(systemInput.audio_gate.manifest_hash).slice(0, 8)}`
        : Number.isInteger(audioRevision) && audioRevision > 0 ? `v${audioRevision}` : '—';
    renderFinalDeliveryFacts($('final-delivery-input-facts'), [
        ['录入状态', systemInput ? systemInputStatusLabel(inputStatus) : '未启用'],
        ['录入运行 ID', run.input_run_id || '尚未创建'],
        ['录入类型', systemInput?.input_type === 'textbook' ? '课文' : systemInput?.input_type === 'vocabulary' ? '词汇' : systemInput?.delivery_mode === 'audio_and_input' ? '试卷' : '—'],
        ['音频版本', audioVersionLabel],
        ['配置版本', Number.isInteger(configurationRevision) && configurationRevision > 0 ? `v${configurationRevision}` : '—'],
    ]);
    const inputNote = $('final-delivery-input-note');
    if (inputNote) inputNote.textContent = systemInput?.delivery_mode === 'audio_and_input'
        ? (systemInputRunIsComplete(systemInput) ? '所有录入单元都已回读明确状态；审阅文档名按单元展示，可直接复制到平台搜索。平台不提供审阅链接时，不会将其标记为失败。' : '系统录入尚未全部完成；请根据逐单元结果处理，不会把不确定状态标记为完成。')
        : '本次未启用系统录入；音频交付作为唯一交付物。';
    renderFinalDeliveryEntries(context, current, systemInput || {});
    const actionNote = $('final-delivery-action-note');
    if (actionNote) actionNote.textContent = presentation.tone === 'success'
        ? '交付事实已收齐；之后仍可从历史记录重新打开本页。'
        : '此页只汇总服务端已返回的事实；未明确的外部状态会保留为待核验。';
    const downloadButton = $('final-download-audio-btn');
    if (downloadButton) {
        downloadButton.disabled = audioCount === 0;
        downloadButton.textContent = delivery.zip_available === true ? '下载交付包' : '准备交付包';
        downloadButton.title = audioCount > 0 ? '由服务端按当前交付范围整理并下载 ZIP' : '当前没有可下载的已验证音频';
    }
}

function renderSystemInputSubpage(workspace = null) {
    if (!isSystemInputSubpageView()) return;
    const current = finalDeliveryContextWorkspace(workspace);
    if (currentView === 'system-input-progress') renderSystemInputProgressPage(current);
    else renderFinalDeliveryPage(current);
}

function renderSystemInputSurface(workspace = currentWorkspace) {
    renderSystemInputDeliveryChoice(workspace);
    renderSystemInputConfigEntry(workspace);
    renderSystemInputBoundaryReview(workspace?.system_input, workspace);
    renderSystemInputDelivery(workspace);
    renderSystemInputSubpage(systemInputInteractionWorkspace(workspace));
}

function routeAfterSystemInputAudioAcceptance(workspace) {
    const systemInput = workspace?.system_input;
    if (!workspace || !systemInput) return false;
    if (!systemInputDocumentEntryIsSupported(workspace)) return false;
    // A pure-audio task has no input run yet. After the user confirms the
    // audio batch, continue from the delivery center into the shared target
    // drawer instead of leaving them on a progress page whose status is still
    // “未启用”. Configuring the target does not regenerate audio.
    if (systemInput.delivery_mode !== 'audio_and_input' && !systemInput.input_run) {
        if (isSystemInputSubpageView()) returnFromTaskSubpage();
        return openSystemInputConfigDrawer(workspace, { deliveryMode: 'audio_and_input' });
    }
    return showSystemInputProgressPage({ workspace, refresh: false });
}

async function performSystemInputRunControl(actionType) {
    const action = String(actionType || '').trim().toLowerCase();
    const actionLabels = {
        pause: '暂停录入',
        resume: '恢复录入',
        stop: '停止录入',
    };
    const actionLabel = actionLabels[action];
    if (!actionLabel || systemInputRunControlBusy) return false;

    const workspace = systemInputInteractionWorkspace();
    const systemInput = workspace?.system_input;
    const state = systemInputRunControlState(systemInput);
    if (!state.active) {
        showToast('当前录入运行已经结束，不能再控制', 'warning');
        return false;
    }
    if ((action === 'pause' && (state.paused || state.stopping))
        || (action === 'resume' && (!state.paused || state.stopping))
        || (action === 'stop' && state.stopping)) {
        showToast('任务状态已刷新，当前不能执行该操作', 'warning');
        return false;
    }
    const serverActionType = {
        pause: 'INPUT_RUN_PAUSE',
        resume: 'INPUT_RUN_RESUME',
        stop: 'INPUT_RUN_STOP',
    }[action];
    const serverAction = typeof workspaceAction === 'function'
        ? workspaceAction(serverActionType, workspace)
        : null;
    if (serverAction && serverAction.enabled !== true) {
        showToast(serverAction.reason || '当前不能控制系统录入', 'warning');
        return false;
    }

    const historyView = isHistoryResultView();
    const historyContext = activeResultContext;
    const workflowId = String(
        historyView
            ? historyResultWorkflowId(historyContext)
            : (currentSession?.session_id || workspace?.snapshot?.workflow_id || ''),
    ).trim();
    const inputRunId = String(systemInput?.input_run?.input_run_id || '').trim();
    if (!workflowApi?.controlSystemInputRun || !workflowId || !inputRunId) {
        showToast('系统录入控制服务暂不可用', 'error');
        return false;
    }

    const stateVersion = Number(workspace?.snapshot?.state_version || 0);
    const idempotencyKey = `renderer-system-input-control-${workflowId}-${inputRunId}-${action}-${Number.isInteger(stateVersion) ? stateVersion : 0}`;
    systemInputRunControlBusy = true;
    renderSystemInputProgressPage(workspace);
    try {
        const response = await workflowApi.controlSystemInputRun(workflowId, {
            input_run_id: inputRunId,
            expected_state_version: Number.isInteger(stateVersion) ? stateVersion : null,
            action,
            reason: action === 'stop' ? '用户停止系统录入' : `用户${actionLabel}请求`,
            requested_by: 'desktop',
        }, { idempotencyKey });
        let updated = null;
        if (response?.workspace) {
            updated = historyView
                ? updateHistoryResultWorkspace(response.workspace, historyContext)
                : applySystemInputWorkspaceResponse(response);
        } else if (historyView) {
            updated = await refreshHistoryResultWorkspace(historyContext, { silent: false });
        } else {
            updated = await hydrateWorkflowWorkspace(workflowId, { silent: false });
        }
        if (!updated) throw new Error('服务端未返回更新后的工作区');
        if (currentView === 'system-input-progress') {
            renderSystemInputProgressPage(updated);
        }
        showToast(
            action === 'pause'
                ? '已请求暂停录入，当前页面动作完成后会停在安全检查点'
                : action === 'resume'
                    ? '已恢复系统录入，任务将从未完成单元继续'
                    : '已请求停止录入，之后不会自动重新打开浏览器',
            'success',
        );
        return true;
    } catch (error) {
        showToast(workflowAdapter.issueMessage?.(error)?.message || `${actionLabel}失败：${error.message || '请稍后重试'}`, 'error');
        if (historyView) {
            await refreshHistoryResultWorkspace(historyContext, { silent: true });
        } else {
            await hydrateWorkflowWorkspace(workflowId, { silent: true });
        }
        return false;
    } finally {
        systemInputRunControlBusy = false;
        if (currentView === 'system-input-progress') {
            renderSystemInputProgressPage(systemInputInteractionWorkspace());
        }
    }
}

async function performHistorySystemInputAction(actionType) {
    const context = activeResultContext;
    const workflowId = historyResultWorkflowId(context);
    if (!isHistoryResultView(context) || !workflowApi || !workflowId) return false;
    const workspace = await refreshHistoryResultWorkspace(context, { silent: false });
    if (!workspace) return false;
    const action = workspaceAction(actionType, workspace);
    if (!action || action.enabled !== true) {
        showToast(action?.reason || '任务状态已刷新，当前不可执行该操作。', 'warning');
        return false;
    }
    const expectedStateVersion = Number(action.expected_state_version ?? workspace.snapshot?.state_version);
    if (!Number.isInteger(expectedStateVersion) || expectedStateVersion < 0) {
        showToast('任务版本缺失，暂时不能执行系统录入操作', 'error');
        return false;
    }
    try {
        let response;
        if (actionType === 'ACCEPT_AUDIO' && workflowApi.acceptAudio) {
            response = await workflowApi.acceptAudio(workflowId, {
                expected_state_version: expectedStateVersion,
            }, { idempotencyKey: `renderer-audio-accept-${workflowId}-${expectedStateVersion}` });
        } else if (actionType === 'START_INPUT' && workflowApi.startSystemInput) {
            const inputRunId = String(workspace.system_input?.input_run?.input_run_id || 'new');
            response = await workflowApi.startSystemInput(workflowId, {
                expected_state_version: expectedStateVersion,
            }, { idempotencyKey: `renderer-system-input-${workflowId}-${expectedStateVersion}-${inputRunId}` });
        } else {
            showToast('当前版本暂时不能执行系统录入操作', 'warning');
            return false;
        }
        const updated = response?.workspace
            ? updateHistoryResultWorkspace(response.workspace, context)
            : await refreshHistoryResultWorkspace(context, { silent: false });
        if (!updated) throw new Error('服务端未返回更新后的工作区');
        if (actionType === 'START_INPUT') {
            showSystemInputProgressPage({ workspace: updated, refresh: false });
        } else if (actionType === 'ACCEPT_AUDIO') {
            routeAfterSystemInputAudioAcceptance(updated);
        }
        showToast(actionType === 'ACCEPT_AUDIO' ? '已确认整批音频，可以继续录入系统' : '已启动系统录入，页面会持续显示各单元状态', 'success');
        return true;
    } catch (error) {
        showToast(workflowAdapter.issueMessage?.(error)?.message || `${actionType === 'ACCEPT_AUDIO' ? '音频验收' : '启动系统录入'}失败：${error.message || '请稍后重试'}`, 'error');
        await refreshHistoryResultWorkspace(context, { silent: true });
        return false;
    }
}

async function performSystemInputAcceptanceAndStart() {
    if (isHistoryResultView()) return performHistorySystemInputAcceptanceAndStart();

    const workflowId = String(currentSession?.session_id || '').trim();
    if (!workflowApi?.acceptAudio || !workflowApi?.startSystemInput || !workflowId) {
        showToast('当前版本暂时不能确认并开始系统录入', 'warning');
        return false;
    }

    let workspace = await hydrateWorkflowWorkspace(workflowId, { silent: false });
    if (!workspace) return false;
    const systemInput = workspace.system_input || {};

    try {
        if (systemInput.audio_acceptance?.status !== 'accepted') {
            const acceptAction = workspaceAction('ACCEPT_AUDIO', workspace);
            if (!acceptAction || acceptAction.enabled !== true) {
                showToast(acceptAction?.reason || '当前不能确认整批音频', 'warning');
                return false;
            }
            const expectedStateVersion = Number(
                acceptAction.expected_state_version
                ?? workspace.snapshot?.state_version
                ?? currentSession.state_version
                ?? 0,
            );
            if (!Number.isInteger(expectedStateVersion) || expectedStateVersion < 0) {
                showToast('任务版本缺失，暂时不能确认整批音频', 'error');
                return false;
            }
            const response = await workflowApi.acceptAudio(workflowId, {
                expected_state_version: expectedStateVersion,
            }, { idempotencyKey: `renderer-audio-accept-${workflowId}-${expectedStateVersion}` });
            workspace = applySystemInputWorkspaceResponse(response);
            if (!workspace) throw new Error('服务端未返回更新后的工作区');
            // Fetch the authoritative action projection after acceptance. The
            // start action is intentionally disabled before acceptance, so it
            // cannot be reused from the previous workspace snapshot.
            workspace = await hydrateWorkflowWorkspace(workflowId, { silent: false }) || workspace;
        }

        const startAction = workspaceAction('START_INPUT', workspace);
        if (!startAction || startAction.enabled !== true) {
            showSystemInputProgressPage({ workspace, refresh: false });
            showToast(startAction?.reason || '音频已确认，请先完成录入目标配置', 'warning');
            return false;
        }
        return performSystemInputStart(startAction);
    } catch (error) {
        showToast(workflowAdapter.issueMessage?.(error)?.message || `确认并开始录入失败：${error.message || '请稍后重试'}`, 'error');
        await hydrateWorkflowWorkspace(workflowId, { silent: true });
        return false;
    }
}

async function performHistorySystemInputAcceptanceAndStart() {
    const context = activeResultContext;
    const workflowId = historyResultWorkflowId(context);
    if (!isHistoryResultView(context) || !workflowApi?.acceptAudio || !workflowApi?.startSystemInput || !workflowId) {
        showToast('当前版本暂时不能确认并开始系统录入', 'warning');
        return false;
    }

    let workspace = await refreshHistoryResultWorkspace(context, { silent: false });
    if (!workspace) return false;

    try {
        if (workspace.system_input?.audio_acceptance?.status !== 'accepted') {
            const acceptAction = workspaceAction('ACCEPT_AUDIO', workspace);
            if (!acceptAction || acceptAction.enabled !== true) {
                showToast(acceptAction?.reason || '当前不能确认整批音频', 'warning');
                return false;
            }
            const expectedStateVersion = Number(acceptAction.expected_state_version ?? workspace.snapshot?.state_version ?? 0);
            if (!Number.isInteger(expectedStateVersion) || expectedStateVersion < 0) {
                showToast('任务版本缺失，暂时不能确认整批音频', 'error');
                return false;
            }
            const response = await workflowApi.acceptAudio(workflowId, {
                expected_state_version: expectedStateVersion,
            }, { idempotencyKey: `renderer-audio-accept-${workflowId}-${expectedStateVersion}` });
            workspace = response?.workspace
                ? updateHistoryResultWorkspace(response.workspace, context)
                : await refreshHistoryResultWorkspace(context, { silent: false });
            if (!workspace) throw new Error('服务端未返回更新后的工作区');
            workspace = await refreshHistoryResultWorkspace(context, { silent: false }) || workspace;
        }

        const startAction = workspaceAction('START_INPUT', workspace);
        if (!startAction || startAction.enabled !== true) {
            showSystemInputProgressPage({ workspace, refresh: false });
            showToast(startAction?.reason || '音频已确认，请先完成录入目标配置', 'warning');
            return false;
        }
        const expectedStateVersion = Number(startAction.expected_state_version ?? workspace.snapshot?.state_version ?? 0);
        if (!Number.isInteger(expectedStateVersion) || expectedStateVersion < 0) {
            showToast('任务版本缺失，暂时不能开始系统录入', 'error');
            return false;
        }
        const inputRunId = String(workspace.system_input?.input_run?.input_run_id || 'new');
        const response = await workflowApi.startSystemInput(workflowId, {
            expected_state_version: expectedStateVersion,
        }, { idempotencyKey: `renderer-system-input-${workflowId}-${expectedStateVersion}-${inputRunId}` });
        const updated = response?.workspace
            ? updateHistoryResultWorkspace(response.workspace, context)
            : await refreshHistoryResultWorkspace(context, { silent: false });
        if (!updated) throw new Error('服务端未返回更新后的工作区');
        showSystemInputProgressPage({ workspace: updated, refresh: false });
        showToast('已确认整批音频并启动系统录入，页面会持续显示各单元状态', 'success');
        return true;
    } catch (error) {
        showToast(workflowAdapter.issueMessage?.(error)?.message || `确认并开始录入失败：${error.message || '请稍后重试'}`, 'error');
        await refreshHistoryResultWorkspace(context, { silent: true });
        return false;
    }
}

function runSystemInputDeliveryAction(actionType) {
    if (actionType === 'ACCEPT_AUDIO_AND_START') return performSystemInputAcceptanceAndStart();
    return isHistoryResultView() ? performHistorySystemInputAction(actionType) : runFreshWorkspaceAction(actionType);
}

async function performSystemInputAudioAcceptance(action) {
    if (!action?.enabled || !workflowApi?.acceptAudio || !currentSession?.session_id) return false;
    const workflowId = currentSession.session_id;
    try {
        const response = await workflowApi.acceptAudio(workflowId, {
            expected_state_version: Number(action.expected_state_version ?? currentWorkspace?.snapshot?.state_version ?? currentSession.state_version ?? 0),
        }, { idempotencyKey: `renderer-audio-accept-${workflowId}-${Number(action.expected_state_version || 0)}` });
        const updated = applySystemInputWorkspaceResponse(response);
        if (!updated) throw new Error('服务端未返回更新后的工作区');
        routeAfterSystemInputAudioAcceptance(updated);
        showToast('已确认整批音频，可以继续录入系统', 'success');
        return true;
    } catch (error) {
        showToast(workflowAdapter.issueMessage?.(error)?.message || `音频验收失败：${error.message || '请稍后重试'}`, 'error');
        await hydrateWorkflowWorkspace(workflowId, { silent: true });
        return false;
    }
}

async function performSystemInputStart(action) {
    if (!action?.enabled || !workflowApi?.startSystemInput || !currentSession?.session_id) return false;
    const workflowId = currentSession.session_id;
    const inputRunId = String(currentWorkspace?.system_input?.input_run?.input_run_id || 'new');
    const expectedStateVersion = Number(action.expected_state_version ?? currentWorkspace?.snapshot?.state_version ?? currentSession.state_version ?? 0);
    try {
        const response = await workflowApi.startSystemInput(workflowId, {
            expected_state_version: expectedStateVersion,
        }, { idempotencyKey: `renderer-system-input-${workflowId}-${expectedStateVersion}-${inputRunId}` });
        const updated = applySystemInputWorkspaceResponse(response);
        if (!updated) throw new Error('服务端未返回更新后的工作区');
        showSystemInputProgressPage({ workspace: updated, refresh: false });
        showToast('已启动系统录入，页面会持续显示各单元状态', 'success');
        return true;
    } catch (error) {
        showToast(workflowAdapter.issueMessage?.(error)?.message || `启动系统录入失败：${error.message || '请稍后重试'}`, 'error');
        await hydrateWorkflowWorkspace(workflowId, { silent: true });
        return false;
    }
}


registerRendererModule("systemInput.pages", {
    systemInputRunIsComplete,
    systemInputEntryStatus,
    systemInputPageRunSnapshot,
    systemInputPageTone,
    systemInputEvidenceHash,
    systemInputReconciliationTarget,
    systemInputNeedsReconciliation,
    renderSystemInputReconciliationPanel,
    resolveSystemInputAmbiguity,
    runSystemInputVerification,
    maybeAutoVerifySystemInput,
    systemInputVerificationOutcome,
    systemInputVerificationTerminal,
    systemInputPageGate,
    renderSystemInputSubflow,
    renderSystemInputPageFacts,
    renderSystemInputPageUnits,
    renderSystemInputProgressPage,
    finalDeliveryContextWorkspace,
    renderFinalDeliveryFacts,
    finalDeliveryPresentation,
    renderFinalDeliveryEntries,
    renderFinalDeliveryPage,
    renderSystemInputSubpage,
    renderSystemInputSurface,
    systemInputRunControlState,
    systemInputRunWasStopped,
    renderSystemInputRunControls,
    performSystemInputRunControl,
    performHistorySystemInputAction,
    performSystemInputAcceptanceAndStart,
    performHistorySystemInputAcceptanceAndStart,
    routeAfterSystemInputAudioAcceptance,
    runSystemInputDeliveryAction,
    performSystemInputAudioAcceptance,
    performSystemInputStart,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
