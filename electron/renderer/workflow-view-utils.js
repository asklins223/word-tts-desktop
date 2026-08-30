/*
 * 小猪wordTTS · shared workflow view ordering helpers
 *
 * Reducer and adapter must present the same blocker when a workspace contains
 * more than one severity.  Keep the ranking in one small, dependency-free
 * module so adding a severity does not require two renderer implementations.
 */
(function attachWorkflowViewUtils(root) {
    'use strict';

    const BLOCKER_SEVERITY_RANK = Object.freeze({
        BLOCKING: 0,
        ERROR: 1,
        WARNING: 2,
        INFO: 3,
    });

    function blockerSeverityRank(value) {
        return BLOCKER_SEVERITY_RANK[String(value || '').toUpperCase()] ?? 4;
    }

    function compareBlockers(left, right) {
        return blockerSeverityRank(left?.severity) - blockerSeverityRank(right?.severity);
    }

    function firstBlocker(workspace) {
        return (Array.isArray(workspace?.blockers) ? workspace.blockers : [])
            .slice()
            .sort(compareBlockers)[0] || null;
    }

    const exports = {
        BLOCKER_SEVERITY_RANK,
        blockerSeverityRank,
        compareBlockers,
        firstBlocker,
    };
    if (typeof module !== 'undefined' && module.exports) module.exports = exports;
    if (root) root.WORDTTS_WORKFLOW_VIEW_UTILS = exports;
})(typeof globalThis !== 'undefined' ? globalThis : null);
