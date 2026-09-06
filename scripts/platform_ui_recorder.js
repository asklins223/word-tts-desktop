(function installPlatformInputUiRecorder(config) {
  const options = config || {};
  const recorderVersion = 6;
  const sessionId = String(options.sessionId || "platform_input-demo");
  const storageKey = "__codex_platform_input_ui_ops_v5__" + sessionId;
  const metaKey = "__codex_platform_input_ui_meta_v5__" + sessionId;
  const outboxKey = "__codex_platform_input_ui_outbox_v6__" + sessionId;
  const endpoint = String(options.endpoint || "http://127.0.0.1:8765/event");
  const previewRe = /preview|预览|试听/i;
  const sensitiveRe = /password|passwd|token|secret|authorization|auth|otp|验证码|密码|密钥/i;
  const unsafeAttributeRe = /^(?:on[a-z]+|srcdoc)$/i;
  const maxText = 30000;
  const maxDescriptorText = 500;
  const maxAttributeValue = 1000;
  const maxLocalRecords = 20000;
  const maxOutboxRecords = 10000;
  const snapshotChunkSize = 200;
  const maxMutationElements = 2000;
  const maxMutationRecords = 1000;
  const postTimeoutMs = 10000;
  const maxRetryDelayMs = 30000;
  const rootDocument = document;
  const rootWindow = window;
  const previous = window.__codexPlatformInputRecorderV5;
  if (previous && typeof previous.stop === "function") previous.stop();

  const listeners = [];
  const observers = [];
  const trackedRoots = new WeakSet();
  const trackedDocuments = new WeakSet();
  const trackedFrames = new WeakSet();
  const contextByRoot = new WeakMap();
  const seenEvents = new WeakSet();
  const mutationTimers = new WeakMap();
  const throttleAt = new Map();
  const shadowPatches = [];
  const propertyPatches = [];
  let nextContextNumber = 0;
  let outbox = [];
  let sending = false;
  let retryTimer = null;
  let retryDelay = 500;
  let stopped = false;
  let abortController = null;

  const state = {
    recorderVersion,
    sessionId,
    storageKey,
    outboxKey,
    startedAt: new Date().toISOString(),
    sequence: 0,
    ignoredPreviewCount: 0,
    endpoint,
    localStorageWriteFailed: false,
    outboxWriteFailed: false,
    pendingCount: 0,
    sentCount: 0,
    retryCount: 0,
    droppedPostCount: 0,
    mutationCount: 0,
    snapshotCount: 0,
    attachedDocumentCount: 0,
    attachedShadowRootCount: 0,
    inaccessibleFrameCount: 0,
    throttledEventCount: 0,
  };

  const clean = (value, max = 240) => String(value ?? "").replace(/\s+/g, " ").trim().slice(0, max);
  const isElement = (value) => !!value && value.nodeType === 1;
  const isShadowRoot = (value) => !!value && value.nodeType === 11 && !!value.host;
  const isNode = (value) => !!value && typeof value.nodeType === "number";
  const rootView = (doc) => {
    try {
      return doc?.defaultView || rootWindow;
    } catch {
      return rootWindow;
    }
  };

  const readArray = (key) => {
    try {
      const stored = JSON.parse(localStorage.getItem(key) || "[]");
      return Array.isArray(stored) ? stored : [];
    } catch {
      return [];
    }
  };

  let records = readArray(storageKey);
  outbox = readArray(outboxKey).filter((item) => item && typeof item === "object" && item.eventId);
  state.pendingCount = outbox.length;
  for (const item of records) {
    if (item && item.sessionId === sessionId && Number.isFinite(Number(item.sequence))) {
      state.sequence = Math.max(state.sequence, Number(item.sequence));
    }
  }

  const randomPart = () => {
    try {
      if (rootWindow.crypto?.randomUUID) return rootWindow.crypto.randomUUID();
      if (rootWindow.crypto?.getRandomValues) {
        const bytes = new Uint32Array(3);
        rootWindow.crypto.getRandomValues(bytes);
        return Array.from(bytes).map((value) => value.toString(36)).join("");
      }
    } catch {}
    return Math.random().toString(36).slice(2);
  };

  const safeStorageSet = (key, value) => {
    try {
      localStorage.setItem(key, value);
      return true;
    } catch {
      return false;
    }
  };

  const persist = (record) => {
    records.push(record);
    if (records.length > maxLocalRecords) records = records.slice(-maxLocalRecords);

    // A long-lived session can hit localStorage quota. Keep the newest records
    // as a best-effort browser-side copy while the JSONL receiver remains the
    // durable store.
    let saved = false;
    for (const targetSize of [maxLocalRecords, 10000, 5000, 1000, 100]) {
      if (records.length > targetSize) records = records.slice(-targetSize);
      if (safeStorageSet(storageKey, JSON.stringify(records))) {
        saved = true;
        break;
      }
    }
    state.localStorageWriteFailed = !saved;
    window.__codexPlatformInputUiRecordsV5 = records;
  };

  const saveOutbox = () => {
    state.pendingCount = outbox.length;
    if (!outbox.length) {
      try {
        localStorage.removeItem(outboxKey);
        state.outboxWriteFailed = false;
      } catch {
        state.outboxWriteFailed = true;
      }
      return;
    }
    state.outboxWriteFailed = !safeStorageSet(outboxKey, JSON.stringify(outbox));
  };

  const schedulePump = (delay) => {
    if (stopped || retryTimer !== null || !outbox.length) return;
    retryTimer = setTimeout(() => {
      retryTimer = null;
      void pumpOutbox();
    }, Math.max(0, delay));
  };

  async function pumpOutbox() {
    if (stopped || sending || !outbox.length) return;
    sending = true;
    const current = outbox[0];
    let timeoutId = null;
    try {
      abortController = typeof AbortController === "function" ? new AbortController() : null;
      timeoutId = setTimeout(() => abortController?.abort(), postTimeoutMs);
      const response = await fetch(endpoint, {
        method: "POST",
        mode: "cors",
        cache: "no-store",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(current),
        signal: abortController?.signal,
      });
      if (!response.ok) {
        // A malformed or oversized event cannot be fixed by retrying. Keep
        // retrying network errors and rate limits, but unblock the queue for
        // permanent client errors.
        const permanentClientError = response.status >= 400 && response.status < 500 &&
          response.status !== 408 && response.status !== 429;
        if (permanentClientError) {
          outbox.shift();
          state.droppedPostCount += 1;
          state.lastPostStatus = response.status;
          saveOutbox();
        } else {
          throw new Error(`receiver_status_${response.status}`);
        }
      } else if (outbox[0]?.eventId === current.eventId) {
        outbox.shift();
        state.sentCount += 1;
        state.lastPostStatus = response.status;
        retryDelay = 500;
        saveOutbox();
      }
    } catch (error) {
      if (!stopped) {
        state.retryCount += 1;
        state.lastPostError = error?.name || "post_failed";
        schedulePump(retryDelay);
        retryDelay = Math.min(maxRetryDelayMs, retryDelay * 2);
      }
    } finally {
      if (timeoutId !== null) clearTimeout(timeoutId);
      abortController = null;
      sending = false;
      state.pendingCount = outbox.length;
      if (!stopped && outbox.length && retryTimer === null) schedulePump(0);
    }
  }

  const enqueue = (record) => {
    outbox.push(record);
    if (outbox.length > maxOutboxRecords) {
      outbox = outbox.slice(-maxOutboxRecords);
      state.outboxOverflowCount = (state.outboxOverflowCount || 0) + 1;
    }
    saveOutbox();
    void pumpOutbox();
  };

  const pageSnapshot = (doc) => {
    try {
      const location = rootView(doc).location;
      return {
        origin: clean(location?.origin, 300),
        pathname: clean(location?.pathname, 1000),
        hash: clean(location?.hash, 1000),
      };
    } catch {
      return { origin: "", pathname: "", hash: "" };
    }
  };

  const cssEscape = (value) => {
    try {
      if (rootWindow.CSS?.escape) return rootWindow.CSS.escape(String(value));
    } catch {}
    return String(value).replace(/[^a-zA-Z0-9_-]/g, (character) => `\\${character}`);
  };

  const cssPath = (element, depth = 0) => {
    if (!isElement(element) || depth > 20) return "";
    const parts = [];
    let current = element;
    let guard = 0;
    while (isElement(current) && guard++ < 20) {
      const tag = current.tagName.toLowerCase();
      if (current.id) {
        parts.unshift(`${tag}#${cssEscape(current.id)}`);
        break;
      }
      let part = tag;
      const parent = current.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter((child) => child.tagName === current.tagName);
        if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(current) + 1})`;
        current = parent;
      } else if (isShadowRoot(current.parentNode)) {
        parts.unshift(`:host(${cssPath(current.parentNode.host, depth + 1)})`);
        break;
      } else {
        current = null;
      }
      parts.unshift(part);
    }
    return parts.join(" > ");
  };

  const isSensitive = (element) => {
    if (!isElement(element)) return false;
    const type = String(element.type || "").toLowerCase();
    const identity = [
      type,
      element.name,
      element.id,
      element.placeholder,
      element.getAttribute?.("aria-label"),
      element.getAttribute?.("autocomplete"),
    ].join(" ");
    return type === "password" || sensitiveRe.test(identity);
  };

  const hasSensitiveAncestor = (element) => {
    let current = element;
    let guard = 0;
    while (isElement(current) && guard++ < 30) {
      if (isSensitive(current)) return true;
      current = current.parentElement;
    }
    return false;
  };

  const labelFor = (element) => {
    if (!isElement(element)) return "";
    const labelledBy = element.getAttribute && element.getAttribute("aria-labelledby");
    if (labelledBy) {
      try {
        const doc = element.ownerDocument;
        return clean(labelledBy.split(/\s+/).map((id) => doc.getElementById(id)?.innerText || "").join(" "));
      } catch {}
    }
    if (element.labels && element.labels.length) {
      return clean(Array.from(element.labels).map((label) => label.innerText).join(" "));
    }
    return clean(element.closest?.("label")?.innerText || "");
  };

  const fieldSnapshot = (element) => {
    if (!isElement(element)) return undefined;
    const tag = String(element.tagName || "").toLowerCase();
    const type = String(element.type || "").toLowerCase();
    const isField = tag === "input" || tag === "textarea" || tag === "select" || !!element.isContentEditable;
    if (!isField) return undefined;
    if (tag === "select") {
      return {
        kind: "select",
        multiple: !!element.multiple,
        selectedOptions: Array.from(element.selectedOptions || []).map((option) => clean(option.textContent, 500)),
      };
    }
    if (type === "file") {
      return {
        kind: "file",
        fileCount: element.files?.length || 0,
        fileNames: Array.from(element.files || []).map((file) => clean(file.name, 240)),
      };
    }
    if (tag === "input" && (type === "checkbox" || type === "radio")) {
      return { kind: type, checked: !!element.checked };
    }
    const rawValue = element.isContentEditable
      ? (element.innerText || element.textContent || "")
      : (element.value ?? "");
    if (isSensitive(element)) {
      return {
        kind: element.isContentEditable ? "contenteditable" : tag,
        redacted: true,
        length: String(rawValue).length,
      };
    }
    return {
      kind: element.isContentEditable ? "contenteditable" : tag,
      redacted: false,
      value: String(rawValue).slice(0, maxText),
      truncated: String(rawValue).length > maxText,
      length: String(rawValue).length,
    };
  };

  const rectSnapshot = (element) => {
    if (!isElement(element)) return undefined;
    try {
      const rect = element.getBoundingClientRect();
      const round = (value) => Math.round(Number(value || 0) * 100) / 100;
      const view = rootView(element.ownerDocument);
      const style = view.getComputedStyle ? view.getComputedStyle(element) : null;
      return {
        x: round(rect.x),
        y: round(rect.y),
        width: round(rect.width),
        height: round(rect.height),
        visible: !!(rect.width || rect.height) && style?.display !== "none" && style?.visibility !== "hidden",
      };
    } catch {
      return undefined;
    }
  };

  const safeAttribute = (element, name, value) => {
    const raw = String(value ?? "");
    if (unsafeAttributeRe.test(name) || (name.toLowerCase() === "value" && isSensitive(element)) || sensitiveRe.test(name)) {
      return { name: clean(name, 100), redacted: true, length: raw.length };
    }
    if (/^(?:href|src|action|formaction|poster|cite|background)$/i.test(name)) {
      try {
        const url = new URL(raw, element?.ownerDocument?.baseURI || rootWindow.location.href);
        return {
          name: clean(name, 100),
          redacted: false,
          value: clean(`${url.origin}${url.pathname}`, maxAttributeValue),
          queryAndHashOmitted: true,
        };
      } catch {}
    }
    return { name: clean(name, 100), redacted: false, value: clean(raw, maxAttributeValue) };
  };

  const attributesSnapshot = (element) => {
    if (!isElement(element)) return [];
    try {
      return Array.from(element.attributes || []).slice(0, 64).map((attribute) =>
        safeAttribute(element, attribute.name, attribute.value));
    } catch {
      return [];
    }
  };

  const elementText = (element, max = maxDescriptorText) => {
    if (!isElement(element)) return "";
    try {
      return clean(element.innerText || element.textContent || "", max);
    } catch {
      return "";
    }
  };

  const elementDescriptor = (element, descriptorOptions = {}) => {
    if (!isElement(element)) return undefined;
    const includeField = descriptorOptions.includeField !== false;
    const includeAttributes = descriptorOptions.includeAttributes === true;
    const includeRect = descriptorOptions.includeRect !== false;
    const text = elementText(element);
    const ariaLabel = clean(element.getAttribute?.("aria-label"), 300);
    const title = clean(element.getAttribute?.("title"), 300);
    const label = labelFor(element);
    const descriptor = {
      tag: element.tagName?.toLowerCase(),
      id: clean(element.id, 160),
      name: clean(element.getAttribute?.("name"), 160),
      type: clean(element.getAttribute?.("type"), 80),
      role: clean(element.getAttribute?.("role"), 80),
      placeholder: clean(element.getAttribute?.("placeholder"), 300),
      ariaLabel,
      title,
      label,
      text,
      path: cssPath(element),
      connected: !!element.isConnected,
      state: {
        disabled: !!element.disabled || element.hasAttribute?.("disabled"),
        hidden: !!element.hidden || element.hasAttribute?.("hidden"),
        checked: "checked" in element ? !!element.checked : undefined,
        selected: "selected" in element ? !!element.selected : undefined,
        required: !!element.required || element.hasAttribute?.("required"),
        readOnly: !!element.readOnly || element.hasAttribute?.("readonly"),
        ariaExpanded: clean(element.getAttribute?.("aria-expanded"), 40),
      },
    };
    if (includeField) descriptor.field = fieldSnapshot(element);
    if (includeAttributes) descriptor.attributes = attributesSnapshot(element);
    if (includeRect) descriptor.rect = rectSnapshot(element);
    if (element.shadowRoot) descriptor.shadowRoot = "open";
    return descriptor;
  };

  const isPreviewElement = (element) => {
    if (!isElement(element)) return false;
    const text = elementText(element);
    const ariaLabel = clean(element.getAttribute?.("aria-label"), 300);
    const title = clean(element.getAttribute?.("title"), 300);
    const label = labelFor(element);
    return previewRe.test([text, ariaLabel, title, label].join(" "));
  };

  const closestControl = (value, doc = rootDocument) => {
    const element = isElement(value) ? value : value && value.parentElement;
    if (!element) return doc?.activeElement || doc?.body || doc?.documentElement;
    try {
      return element.closest(
        "button,a,input,textarea,select,option,[role],[contenteditable=true],[tabindex]",
      ) || element;
    } catch {
      return element;
    }
  };

  const targetSnapshot = (rawTarget, context) => {
    const doc = context?.document || rootDocument;
    const element = closestControl(rawTarget, doc);
    const descriptor = elementDescriptor(element);
    return {
      element,
      descriptor,
      isPreview: isPreviewElement(element),
    };
  };

  const framePathFor = (doc) => {
    const frames = [];
    let current = doc;
    let guard = 0;
    while (current && guard++ < 10) {
      let frame = null;
      try {
        frame = current.defaultView?.frameElement;
      } catch {
        frame = null;
      }
      if (!isElement(frame)) break;
      frames.unshift(elementDescriptor(frame, { includeField: false, includeAttributes: false, includeRect: false }));
      try {
        current = frame.ownerDocument;
      } catch {
        break;
      }
    }
    return frames;
  };

  const contextSnapshot = (context) => {
    const framePath = framePathFor(context.document);
    return {
      contextId: context.contextId,
      rootType: context.rootType,
      frameDepth: framePath.length,
      framePath,
    };
  };

  const makeEventId = (sequence) => `${sessionId}:${Date.now().toString(36)}:${sequence}:${randomPart()}`;

  const emit = (recordType, body, context, kind = "ui") => {
    if (stopped) return;
    const sequence = ++state.sequence;
    const payload = {
      kind,
      recordType,
      recorderVersion,
      eventId: makeEventId(sequence),
      sessionId,
      sequence,
      recordedAt: new Date().toISOString(),
      page: pageSnapshot(context?.document || rootDocument),
      context: context ? contextSnapshot(context) : undefined,
      ...body,
    };
    persist(payload);
    enqueue(payload);
    try {
      console.debug("__CODEX_UI_EVENT_V5__" + JSON.stringify(payload));
    } catch {}
    return payload;
  };

  const eventPath = (event) => {
    try {
      return typeof event?.composedPath === "function" ? event.composedPath() : [];
    } catch {
      return [];
    }
  };

  const shouldHandleAtRoot = (root, event) => {
    const roots = eventPath(event).filter(isShadowRoot);
    if (!roots.length) return true;
    // A composed event also reaches the document and every containing shadow
    // root. Let only the deepest shadow root record it so its real target is
    // retained instead of the retargeted host element.
    return root === roots[0];
  };

  const throttleEvent = (eventType, context) => {
    const limits = {
      pointermove: 100,
      mousemove: 100,
      wheel: 100,
      touchmove: 100,
      drag: 100,
      dragover: 100,
      scroll: 250,
    };
    const limit = limits[eventType];
    if (!limit) return false;
    const key = `${context.contextId}:${eventType}`;
    const now = Date.now();
    const previousAt = throttleAt.get(key) || 0;
    if (now - previousAt < limit) {
      state.throttledEventCount += 1;
      return true;
    }
    throttleAt.set(key, now);
    return false;
  };

  const eventDetails = (eventType, event, element) => {
    const details = {};
    if (event && "button" in event) details.button = event.button;
    if (event && "clientX" in event) {
      details.clientX = event.clientX;
      details.clientY = event.clientY;
      details.screenX = event.screenX;
      details.screenY = event.screenY;
    }
    if (event && "deltaX" in event) {
      details.deltaX = event.deltaX;
      details.deltaY = event.deltaY;
      details.deltaMode = event.deltaMode;
    }
    if (event && "pointerId" in event) {
      details.pointerId = event.pointerId;
      details.pointerType = clean(event.pointerType, 40);
      details.pressure = event.pressure;
    }
    if (event && "touches" in event) {
      details.touchCount = event.touches?.length || 0;
      details.changedTouchCount = event.changedTouches?.length || 0;
    }
    if (event && "isTrusted" in event) details.isTrusted = !!event.isTrusted;
    if (event && (eventType === "keydown" || eventType === "keyup")) {
      details.code = clean(event.code, 80);
      details.repeat = !!event.repeat;
      details.ctrlKey = !!event.ctrlKey;
      details.altKey = !!event.altKey;
      details.shiftKey = !!event.shiftKey;
      details.metaKey = !!event.metaKey;
    }
    if (event && (eventType === "beforeinput" || eventType === "input" || eventType.startsWith("composition"))) {
      details.inputType = clean(event.inputType, 120);
      details.data = isSensitive(element) ? "[REDACTED]" : clean(event.data, 500);
    }
    if (eventType === "paste" || eventType === "copy" || eventType === "cut") {
      details.clipboardTextNotStored = true;
    }
    if (eventType === "drop") {
      details.fileNames = Array.from(event.dataTransfer?.files || []).map((file) => clean(file.name, 240));
    }
    if (eventType === "scroll") {
      const target = event.target?.nodeType === 9 ? event.target.scrollingElement : event.target;
      details.scrollTop = target?.scrollTop ?? 0;
      details.scrollLeft = target?.scrollLeft ?? 0;
    }
    return details;
  };

  const recordUi = (eventType, event, context) => {
    if (stopped || !shouldHandleAtRoot(context.root, event) || throttleEvent(eventType, context)) return;
    if (event && typeof event === "object") {
      if (seenEvents.has(event)) return;
      seenEvents.add(event);
    }
    const targetInfo = targetSnapshot(event?.target, context);
    if (targetInfo.isPreview) {
      state.ignoredPreviewCount += 1;
      return;
    }
    const targetSensitive = isSensitive(targetInfo.element);
    const details = eventDetails(eventType, event, targetInfo.element);
    if (event && (eventType === "keydown" || eventType === "keyup" || eventType === "beforeinput")) {
      details.key = targetSensitive ? "[REDACTED]" : clean(event.key, 80);
    }
    if (targetSensitive) details.sensitiveTarget = true;
    emit("ui_event", {
      eventType,
      target: targetInfo.descriptor,
      details,
    }, context, "ui");
  };

  const recordPageEvent = (eventType, details, context) => {
    emit("page_event", { eventType, details }, context, "browser");
  };

  const contextForElement = (element) => {
    if (!isElement(element)) return undefined;
    try {
      return contextByRoot.get(element.getRootNode?.()) || contextByRoot.get(element.ownerDocument);
    } catch {
      return contextByRoot.get(element.ownerDocument);
    }
  };

  const propertyValueSnapshot = (element, value) => {
    if (isSensitive(element)) {
      return { redacted: true, length: String(value ?? "").length };
    }
    if (typeof value === "boolean" || typeof value === "number") return value;
    const raw = String(value ?? "");
    return {
      redacted: false,
      value: raw.slice(0, maxText),
      truncated: raw.length > maxText,
      length: raw.length,
    };
  };

  const recordProgrammaticChange = (element, property, before, after) => {
    const context = contextForElement(element);
    if (!context || stopped) return;
    if (isPreviewElement(element)) {
      state.ignoredPreviewCount += 1;
      return;
    }
    emit("programmatic_change", {
      eventType: "property_set",
      target: elementDescriptor(element),
      details: {
        property,
        source: "dom_property_setter",
        valueBefore: propertyValueSnapshot(element, before),
        valueAfter: propertyValueSnapshot(element, after),
        sensitiveTarget: isSensitive(element) || undefined,
      },
    }, context, "ui");
  };

  const installFormPropertyPatches = (view = rootWindow) => {
    const prototypes = [
      [view.HTMLInputElement?.prototype, ["value", "checked", "indeterminate", "valueAsNumber"]],
      [view.HTMLTextAreaElement?.prototype, ["value"]],
      [view.HTMLSelectElement?.prototype, ["value", "selectedIndex"]],
      [view.HTMLOptionElement?.prototype, ["selected"]],
    ];
    for (const [prototype, properties] of prototypes) {
      if (!prototype) continue;
      for (const property of properties) {
        const descriptor = Object.getOwnPropertyDescriptor(prototype, property);
        if (!descriptor?.get || !descriptor.set || descriptor.set.__codexPlatformInputWrapped) continue;
        const wrappedSetter = function wrappedDomPropertySetter(value) {
          let before;
          try {
            before = descriptor.get.call(this);
          } catch {}
          descriptor.set.call(this, value);
          let after;
          try {
            after = descriptor.get.call(this);
          } catch {}
          if (!Object.is(before, after) && String(before) !== String(after)) {
            recordProgrammaticChange(this, property, before, after);
          }
        };
        try {
          Object.defineProperty(wrappedSetter, "__codexPlatformInputWrapped", { value: true });
          Object.defineProperty(prototype, property, { ...descriptor, set: wrappedSetter });
          propertyPatches.push({ prototype, property, descriptor, wrappedSetter });
        } catch {}
      }
    }
  };

  const addListener = (target, type, handler, listenerOptions = true) => {
    try {
      target.addEventListener(type, handler, listenerOptions);
      listeners.push(() => {
        try {
          target.removeEventListener(type, handler, listenerOptions);
        } catch {}
      });
    } catch {}
  };

  const interactionEvents = [
    "pointerdown", "pointerup", "pointermove", "pointerover", "pointerout", "pointercancel",
    "click", "dblclick", "auxclick", "contextmenu",
    "mousedown", "mouseup", "mousemove", "mouseover", "mouseout",
    "wheel",
    "keydown", "keyup", "beforeinput", "input", "change",
    "compositionstart", "compositionupdate", "compositionend",
    "focusin", "focusout", "submit", "reset", "invalid",
    "paste", "copy", "cut",
    "drop", "dragstart", "drag", "dragenter", "dragover", "dragleave", "dragend",
    "select", "selectstart",
    "touchstart", "touchmove", "touchend", "touchcancel",
    "slotchange",
    "scroll",
  ];

  const wireRootEvents = (root, context) => {
    if (trackedRoots.has(root)) return;
    trackedRoots.add(root);
    contextByRoot.set(root, context);
    for (const type of interactionEvents) {
      addListener(root, type, (event) => recordUi(type, event, context), true);
    }
  };

  const wirePageEvents = (doc, context) => {
    const view = rootView(doc);
    for (const type of ["pageshow", "pagehide", "hashchange", "popstate", "resize", "online", "offline"]) {
      addListener(view, type, (event) => recordPageEvent(type, {
        persisted: !!event.persisted,
        width: view.innerWidth,
        height: view.innerHeight,
        visibilityState: doc.visibilityState,
      }, context), false);
    }
    addListener(doc, "visibilitychange", () => recordPageEvent("visibilitychange", {
      visibilityState: doc.visibilityState,
    }, context), true);
  };

  const elementFromNode = (node) => {
    if (isElement(node)) return node;
    return isNode(node) && isElement(node.parentElement) ? node.parentElement : undefined;
  };

  const safeText = (node, parentElement) => {
    const raw = node?.nodeValue || "";
    const element = elementFromNode(node) || parentElement;
    if (hasSensitiveAncestor(element)) return { redacted: true, length: raw.length };
    return { redacted: false, value: clean(raw, maxDescriptorText), length: raw.length };
  };

  const elementListUnder = (root) => {
    try {
      if (isElement(root)) return [root, ...Array.from(root.querySelectorAll("*"))];
      return Array.from(root.querySelectorAll?.("*") || []);
    } catch {
      return [];
    }
  };

  const collectNodeElements = (node, remaining) => {
    const elements = [];
    const all = elementListUnder(node);
    for (const element of all) {
      if (elements.length >= remaining) break;
      if (!isPreviewElement(element)) {
        elements.push(elementDescriptor(element, {
          includeAttributes: true,
          includeRect: true,
        }));
      }
    }
    return elements;
  };

  const emitDomSnapshot = (root, context, reason) => {
    const elements = [];
    let skippedPreviewCount = 0;
    for (const element of elementListUnder(root)) {
      if (isPreviewElement(element)) {
        skippedPreviewCount += 1;
        continue;
      }
      elements.push(elementDescriptor(element, {
        includeAttributes: true,
        includeRect: true,
      }));
    }
    const snapshotId = `${sessionId}:snapshot:${Date.now().toString(36)}:${randomPart()}`;
    const chunkCount = Math.max(1, Math.ceil(elements.length / snapshotChunkSize));
    for (let chunkIndex = 0; chunkIndex < chunkCount; chunkIndex += 1) {
      emit("dom_snapshot", {
        snapshotId,
        reason,
        rootType: context.rootType,
        chunkIndex,
        chunkCount,
        elementCount: elements.length,
        skippedPreviewCount,
        elements: elements.slice(chunkIndex * snapshotChunkSize, (chunkIndex + 1) * snapshotChunkSize),
      }, context, "ui");
      state.snapshotCount += 1;
    }
  };

  const discoverInNode = (node, context) => {
    for (const element of elementListUnder(node)) {
      if (element.tagName === "IFRAME" || element.tagName === "FRAME") attachFrame(element, context);
      if (element.shadowRoot) attachShadowRoot(element.shadowRoot, context);
    }
  };

  const mutationSummary = (mutation) => {
    const targetElement = elementFromNode(mutation.target);
    if (targetElement && isPreviewElement(targetElement)) return undefined;
    const summary = {
      type: mutation.type,
      target: targetElement ? elementDescriptor(targetElement, {
        includeAttributes: true,
        includeRect: true,
      }) : undefined,
    };
    if (mutation.type === "attributes") {
      summary.attributeName = clean(mutation.attributeName, 120);
      const oldAttribute = safeAttribute(targetElement, mutation.attributeName || "", mutation.oldValue);
      summary.oldValue = oldAttribute.redacted ? oldAttribute : oldAttribute.value;
    } else if (mutation.type === "characterData") {
      summary.oldValue = safeText({ nodeValue: mutation.oldValue }, mutation.target.parentElement);
      summary.newValue = safeText(mutation.target);
    } else if (mutation.type === "childList") {
      summary.addedNodes = [];
      summary.removedNodes = [];
      let remaining = maxMutationElements;
      for (const node of Array.from(mutation.addedNodes || [])) {
        if (remaining <= 0) break;
        const chunk = collectNodeElements(node, remaining);
        summary.addedNodes.push(...chunk);
        remaining -= chunk.length;
      }
      for (const node of Array.from(mutation.removedNodes || [])) {
        if (remaining <= 0) break;
        const chunk = collectNodeElements(node, remaining);
        summary.removedNodes.push(...chunk);
        remaining -= chunk.length;
      }
      summary.addedNodeCount = mutation.addedNodes?.length || 0;
      summary.removedNodeCount = mutation.removedNodes?.length || 0;
    }
    return summary;
  };

  const flushMutations = (root, context, pending) => {
    mutationTimers.delete(root);
    if (stopped || !pending.length) return;
    const summaries = [];
    let truncated = false;
    for (const mutation of pending) {
      if (summaries.length >= maxMutationRecords) {
        truncated = true;
        break;
      }
      const summary = mutationSummary(mutation);
      if (summary) summaries.push(summary);
    }
    if (!summaries.length) return;
    state.mutationCount += summaries.length;
    emit("dom_mutation", {
      mutationCount: summaries.length,
      truncated,
      mutations: summaries,
    }, context, "ui");
  };

  const observeRoot = (root, context) => {
    wireRootEvents(root, context);
    const view = rootView(context.document);
    const MutationObserverClass = view.MutationObserver || rootWindow.MutationObserver;
    if (typeof MutationObserverClass !== "function") return;
    try {
      const observer = new MutationObserverClass((mutations) => {
        for (const mutation of mutations) {
          if (mutation.type === "childList") {
            for (const node of Array.from(mutation.addedNodes || [])) discoverInNode(node, context);
          }
        }
        const existing = mutationTimers.get(root) || [];
        existing.push(...mutations);
        if (existing.length > maxMutationRecords * 2) existing.splice(0, existing.length - maxMutationRecords * 2);
        mutationTimers.set(root, existing);
        setTimeout(() => {
          const pending = mutationTimers.get(root);
          if (pending) flushMutations(root, context, pending);
        }, 0);
      });
      observer.observe(root, {
        subtree: true,
        childList: true,
        attributes: true,
        attributeOldValue: true,
        characterData: true,
        characterDataOldValue: true,
      });
      observers.push(observer);
    } catch {}
  };

  function attachDocument(doc) {
    if (!doc || trackedDocuments.has(doc)) return;
    trackedDocuments.add(doc);
    const view = rootView(doc);
    installShadowPatch(view);
    installFormPropertyPatches(view);
    const context = {
      contextId: `document-${++nextContextNumber}`,
      document: doc,
      root: doc,
      rootType: "document",
    };
    contextByRoot.set(doc, context);
    observeRoot(doc, context);
    wirePageEvents(doc, context);
    state.attachedDocumentCount += 1;
    discoverInNode(doc, context);
    emitDomSnapshot(doc, context, "initial");
  }

  function attachShadowRoot(shadowRoot, parentContext) {
    if (!shadowRoot || trackedRoots.has(shadowRoot)) return;
    const context = {
      contextId: `shadow-${++nextContextNumber}`,
      document: parentContext?.document || rootDocument,
      root: shadowRoot,
      rootType: "shadow",
    };
    contextByRoot.set(shadowRoot, context);
    observeRoot(shadowRoot, context);
    state.attachedShadowRootCount += 1;
    discoverInNode(shadowRoot, context);
    emitDomSnapshot(shadowRoot, context, "shadow_attached");
  }

  function attachFrame(frame, parentContext) {
    if (!isElement(frame) || trackedFrames.has(frame)) return;
    trackedFrames.add(frame);
    const onLoad = () => {
      const childDocument = (() => {
        try {
          return frame.contentDocument;
        } catch {
          return null;
        }
      })();
      emit("frame_event", {
        eventType: "load",
        target: elementDescriptor(frame, { includeField: false, includeAttributes: true, includeRect: true }),
        details: { accessible: !!childDocument },
      }, parentContext, "browser");
      if (childDocument) attachDocument(childDocument);
      else state.inaccessibleFrameCount += 1;
    };
    addListener(frame, "load", onLoad, true);
    let childDocument = null;
    try {
      childDocument = frame.contentDocument;
    } catch {
      childDocument = null;
    }
    if (childDocument) attachDocument(childDocument);
    else if (frame.src || frame.srcdoc) state.inaccessibleFrameCount += 1;
  }

  const installShadowPatch = (view = rootWindow) => {
    const prototype = view.Element?.prototype;
    const original = prototype?.attachShadow;
    if (typeof original !== "function") return;
    if (original.__codexPlatformInputWrapped) return;
    const wrapped = function wrappedAttachShadow(init) {
      const shadowRoot = original.call(this, init);
      try {
        attachShadowRoot(shadowRoot, contextByRoot.get(this.ownerDocument) || {
          document: this.ownerDocument || rootDocument,
        });
      } catch {}
      return shadowRoot;
    };
    try {
      Object.defineProperty(wrapped, "__codexPlatformInputWrapped", { value: true });
      prototype.attachShadow = wrapped;
      shadowPatches.push({ prototype, original, wrapped });
    } catch {}
  };

  const meta = {
    recorderVersion,
    sessionId,
    storageKey,
    outboxKey,
    startedAt: state.startedAt,
    page: pageSnapshot(rootDocument),
    captures: interactionEvents,
    additionalRecords: ["dom_snapshot", "dom_mutation", "programmatic_change", "page_event", "frame_event"],
    snapshotPolicy: "initial_document_and_same_origin_iframe_and_shadow_root_chunks",
    mutationPolicy: "subtree_childList_attributes_characterData_coalesced",
    programmaticFieldPolicy: "form_value_checked_selected_property_setters",
    iframePolicy: "same_origin_only",
    shadowDomPolicy: "open_roots_and_attachShadow_roots_created_after_install",
    previewPolicy: "skip_preview",
    persistence: "localStorage_plus_retryable_local_jsonl_receiver",
    retryPolicy: {
      maxOutboxRecords,
      postTimeoutMs,
      maxRetryDelayMs,
      eventIdDeduplication: true,
    },
    rawPasswordsTokens: "never_stored",
    clipboardText: "never_stored",
  };

  safeStorageSet(metaKey, JSON.stringify(meta));
  window.__codexPlatformInputRecorderV5 = {
    sessionId,
    storageKey,
    outboxKey,
    getRecords: () => readArray(storageKey),
    getState: () => ({ ...state, persistedCount: readArray(storageKey).length, pendingCount: outbox.length }),
    flush: () => {
      retryDelay = 500;
      void pumpOutbox();
    },
    stop: () => {
      if (stopped) return;
      stopped = true;
      if (retryTimer !== null) clearTimeout(retryTimer);
      retryTimer = null;
      abortController?.abort();
      for (const observer of observers.splice(0)) {
        try {
          observer.disconnect();
        } catch {}
      }
      for (const cleanup of listeners.splice(0)) cleanup();
      for (const patch of shadowPatches.splice(0)) {
        try {
          if (patch.prototype.attachShadow === patch.wrapped) patch.prototype.attachShadow = patch.original;
        } catch {}
      }
      for (const patch of propertyPatches.splice(0)) {
        try {
          const current = Object.getOwnPropertyDescriptor(patch.prototype, patch.property);
          if (current?.set === patch.wrappedSetter) Object.defineProperty(patch.prototype, patch.property, patch.descriptor);
        } catch {}
      }
    },
  };
  window.__codexPlatformInputUiRecordsV5 = records;

  attachDocument(rootDocument);
  return {
    ...meta,
    persistedCount: records.length,
    pendingCount: outbox.length,
    ignoredPreviewCount: state.ignoredPreviewCount,
  };
})
