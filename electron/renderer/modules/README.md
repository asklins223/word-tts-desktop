# Renderer modules

`index.html` is the renderer composition root. It loads the modules in an
explicit dependency order, and `app.js` only starts `init()` after that graph
has been assembled.

## Boundaries

- `core/` contains DOM helpers, runtime state, module registration, and the
  shared context.
- `source/` owns file intake, parsing transport, and source feedback.
- `review/` owns document/outline models and review rendering/actions.
- `voice/` owns voice configuration, asset loading, and voice UI.
- `generation/` owns task startup, SSE, progress, logs, recovery, and runtime
  presentation.
- `delivery/` owns artifact selection, playback, transfer, and result pages.
- `system-input/` owns platform templates, region pickers, unit configuration,
  disclosures, and delivery actions.
- `history/`, `workflow/`, `updates/`, and `app/` contain their corresponding
  application services and orchestration.

Each file should have one reason to change. Pure normalization, presentation,
and model functions should stay independent of DOM side effects so they can be
reused by another page or tested in isolation.

### System-input cascades

`system-input/cascade.js` is the single source of truth for dependency graphs,
parent readiness, descendant resets, batch-field prerequisites, and
catalog-backed child options. The target editor, batch model, region form, and
textbook catalogue all consume this API; add or change a relationship there
instead of maintaining a second parent map in a feature module.

## Public surface

Feature modules publish a small API with:

```js
registerRendererModule('review.model', {
    buildReviewOutlineModel,
});
```

New code can consume it through `WORDTTS_RENDERER.modules['review.model']` or
`WORDTTS_RENDERER.getModule('review.model')`. Shared environment/services and
cross-feature state are available from `WORDTTS_RENDERER.getContext()`:

```js
const context = WORDTTS_RENDERER.getContext();
const { api, adapter } = context.services;
const workspace = context.state.currentWorkspace;
```

The direct global function aliases are a temporary compatibility bridge for
the existing renderer. The bridge rejects duplicate module names and export
aliases, so a new module cannot silently replace another feature's API. New
modules must not add more global state; when an old module is changed, migrate
its dependencies to the namespaced module surface or runtime context.

When adding a module, add its `<script>` tag beside its domain peers in
`index.html`, keep the dependency order explicit, and add a focused test for
its pure public functions or lifecycle boundary.
