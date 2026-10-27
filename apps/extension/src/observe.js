// Structured page observation for the `observe` / `execute` commands: { url, title, text, nodes }
// plus the typed fill / select / click executors. Its consumers are the real-Chrome eval body
// (app/agent_eval/chrome_body.py) and the /operator/extension/observe grader; the live vision loop
// observes through screenshots instead. The role / label / required helpers below are transitional
// deterministic perception (see AGENTS.md) — do not expand them.
(function () {
  "use strict";

  const clean = (value) => (value || "").replace(/\s+/g, " ").trim();
  const GENERIC = new Set(["search", "select", "select...", "select an option", "choose", "choose...", "-", "—", "none"]);
  const isGeneric = (text) => !text || GENERIC.has(text.toLowerCase());
  // A custom dropdown's screen-reader announcement ("12 results available. Press up and down…") or
  // a framework-generated id ("WIK4XAMjd79Q", "css-1a2b3c", "field-99") is NOT a human label — using
  // it as the field's question makes the brain unable to answer. Treat these like generic and keep
  // searching for the real prompt.
  const isJunk = (text) => {
    if (!text) return true;
    if (/\d+\s+results?\s+available|press\s+(up|down)\s+and|use\s+(up|down)\s+and|arrow(s)?\s+to\s+navigate|navigate\s+and\s+(enter|select)|type\s+to\s+search|begin\s+typing/i.test(text)) return true;
    // A file drop-zone's instruction ("Drop or select (.doc / .docx / .pdf)") is NOT the field's label
    // and NOT a value — skip it so labelFor finds the real field label (e.g. "Résumé") above it.
    if (/\bdrop\b.*\bselect\b|\bdrag\b.*\bdrop\b|\bdrop\s+files?\b|\bchoose\s+a?\s*file\b|\bselect\s+a?\s*file\b|\bbrowse\s+files?\b/i.test(text)) return true;
    if (/^(field|react|select|css|mui|radix|headlessui|downshift)[-_:]?[A-Za-z0-9]*\d/i.test(text) && !/\s/.test(text)) return true;
    if (/^[A-Za-z0-9_:-]{8,}$/.test(text) && !/\s/.test(text) && /[0-9]/.test(text) && /[a-z]/i.test(text)) return true;
    return false;
  };
  const badLabel = (text) => isGeneric(text) || isJunk(text);

  const isVisible = (el) => {
    if (!el || !el.getBoundingClientRect) return false;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return false;
    const style = getComputedStyle(el);
    return style.visibility !== "hidden" && style.display !== "none" && style.opacity !== "0";
  };

  const roleFor = (el) => {
    const tag = (el.tagName || "").toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    const role = (el.getAttribute("role") || "").toLowerCase();
    if (type === "file") return "file_input";
    if (tag === "textarea") return "textarea";
    if (tag === "select") return "select";
    if (role === "combobox") return "combobox";
    if (role === "checkbox" || type === "checkbox") return "checkbox";
    if (role === "radio" || type === "radio") return "radio";
    if (tag === "button" || role === "button" || type === "submit" || type === "button") return "button";
    if (tag === "a") return "link";
    if (role === "option") return "option";
    return "textbox";
  };

  const sensitiveValueControl = (el) => {
    try {
      const tag = (el.tagName || "").toLowerCase();
      const inputType = clean(el.getAttribute("type") || "").toLowerCase();
      if (tag === "input" && inputType === "password") return true;
      const autocompleteTokens = clean(el.getAttribute("autocomplete") || "")
        .toLowerCase()
        .split(/\s+/)
        .filter(Boolean);
      return autocompleteTokens.some((token) => (
        token === "current-password"
        || token === "new-password"
        || token === "one-time-code"
        || token === "transaction-amount"
        || token === "transaction-currency"
        || token === "webauthn"
        || token.startsWith("cc-")
      ));
    } catch (_) {
      return false;
    }
  };

  const sensitiveOwnedTextValues = () => Array.from(
    document.querySelectorAll("input,textarea,[contenteditable='true'],[autocomplete]"),
  )
    .filter(sensitiveValueControl)
    .flatMap((node) => [
      clean(node.value),
      clean(node.getAttribute && node.getAttribute("value")),
      clean(node.innerText),
      clean(node.textContent),
    ])
    .filter(Boolean)
    .sort((a, b) => b.length - a.length);

  const redactSensitiveOwnedText = (value) => {
    let safe = clean(value);
    if (!safe) return safe;
    for (const secret of sensitiveOwnedTextValues()) {
      safe = clean(safe.split(secret).join(" [REDACTED] "));
    }
    return safe;
  };

  const sanitizedBodyText = () => {
    const body = document.body;
    if (!body) return "";
    try {
      const clone = body.cloneNode(true);
      // A detached clone is never rendered, so innerText degenerates to textContent and
      // would include script/style source, JSON-LD blobs, and hidden data - which then
      // drowns the real visible text inside the capped page-text budget. Strip
      // non-visible content structurally before reading.
      clone
        .querySelectorAll("script,style,noscript,template,[hidden]")
        .forEach((node) => node.remove());
      clone.querySelectorAll("input,textarea,[contenteditable='true'],[autocomplete]").forEach((node) => {
        if (!sensitiveValueControl(node)) return;
        node.textContent = "[REDACTED]";
        if (node.setAttribute) node.setAttribute("value", "[REDACTED]");
      });
      return redactSensitiveOwnedText(clone.innerText || clone.textContent || "");
    } catch (_) {
      return "";
    }
  };

  const refText = (el, attr) => {
    const ids = clean(el.getAttribute(attr));
    if (!ids) return "";
    for (const id of ids.split(/\s+/).filter(Boolean)) {
      const ref = document.getElementById(id);
      const text = ref ? clean(ref.innerText || ref.textContent) : "";
      if (text && !badLabel(text)) return text;
    }
    return "";
  };

  // Resolution order: own text (buttons) -> aria-labelledby -> aria-label -> <label for> ->
  // wrapping <label> -> field-wrapper label -> preceding sibling -> prompt block above ->
  // placeholder -> nearest text above -> name.
  const labelFor = (el) => {
    const tag = (el.tagName || "").toLowerCase();
    const role = (el.getAttribute("role") || "").toLowerCase();
    if (tag === "button" || tag === "a" || role === "button" || role === "option") {
      const own = sensitiveValueControl(el)
        ? clean(el.getAttribute("aria-label") || el.getAttribute("placeholder") || el.getAttribute("name"))
        : clean(el.innerText || el.textContent || el.value || el.getAttribute("aria-label"));
      if (own) return own;
    }
    const labelledby = refText(el, "aria-labelledby");
    if (labelledby) return labelledby;
    const aria = clean(el.getAttribute("aria-label"));
    if (aria && !badLabel(aria)) return aria;
    if (el.id) {
      const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      const text = lab ? clean(lab.innerText || lab.textContent) : "";
      if (text && !badLabel(text)) return text;
    }
    const wrap = el.closest("label");
    if (wrap) {
      const text = redactSensitiveOwnedText(wrap.innerText || wrap.textContent || "");
      if (text && !badLabel(text)) return text;
    }
    const fieldWrap = el.closest('[data-testid="field"],[class*="form-field"],fieldset,[role="group"],[class*="question"],[class*="field"]');
    if (fieldWrap) {
      // explicit label element on the wrapper
      const w = fieldWrap.querySelector('[id$="-label"],label,legend');
      const wText = w ? clean(w.innerText || w.textContent) : "";
      if (wText && !badLabel(wText)) return wText.slice(0, 180);
      // else the wrapper's leading text minus the control's own text (a prompt block above the
      // control, not an associated <label>).
      const own = clean(el.innerText || el.textContent || el.value || "");
      let body = clean(fieldWrap.innerText || fieldWrap.textContent || "");
      if (own) body = clean(body.split(own).join(" "));
      const first = body.split("\n").map((s) => s.trim()).find((s) => s && !badLabel(s));
      if (first && first.length <= 180) return first;
    }
    // a label-like preceding sibling (question text rendered just before the control)
    let prev = el.previousElementSibling;
    for (let hops = 0; prev && hops < 4; prev = prev.previousElementSibling, hops += 1) {
      const t = clean(prev.innerText || prev.textContent);
      if (t && !badLabel(t) && t.length <= 180) return t;
    }
    // Climb ancestors looking for a question-like prompt block ABOVE the control, for controls
    // whose field-wrapper is a tight container with no prompt inside it.
    let node = el;
    for (let up = 0; node && up < 6; up += 1, node = node.parentElement) {
      let sib = node.previousElementSibling;
      for (let h = 0; sib && h < 3; h += 1, sib = sib.previousElementSibling) {
        const t = clean(sib.innerText || sib.textContent);
        if (t && !badLabel(t) && t.length <= 200 && /\?|[?*:]\s*$/.test(t)) return t.slice(0, 180);
      }
    }
    const placeholder = clean(el.getAttribute("placeholder"));
    if (placeholder && !badLabel(placeholder)) return placeholder;
    const above = nearestTextAbove(el);
    if (above) return above;
    const fallback = clean(el.getAttribute("name") || el.id || "");
    // Never use a framework-generated id (random mixed-case token, no spaces) as a question label.
    if (isJunk(fallback) || (fallback.length >= 8 && !/\s/.test(fallback) && /[A-Z]/.test(fallback) && /[a-z]/.test(fallback))) return "";
    return fallback;
  };

  // The nearest visible text directly ABOVE a control — its question, found by geometry rather than
  // DOM nesting (robust when the prompt isn't an ancestor/sibling and the id is a random hash).
  const nearestTextAbove = (el) => {
    let rect;
    try { rect = el.getBoundingClientRect(); } catch (error) { return ""; }
    if (!rect || (rect.width === 0 && rect.height === 0)) return "";
    let best = "";
    let bestGap = 220;
    document.querySelectorAll('label,legend,p,h1,h2,h3,h4,h5,h6,dt,span,div,[id$="-label"],[class*="label"],[class*="question"],[class*="title"]').forEach((c) => {
      if (c.contains(el) || c.children.length > 4) return; // skip ancestors + big containers
      const t = clean(c.innerText || c.textContent);
      if (!t || badLabel(t) || t.length > 200) return;
      let r;
      try { r = c.getBoundingClientRect(); } catch (error) { return; }
      if (r.width === 0 && r.height === 0) return;
      const gap = rect.top - r.bottom; // c sits above el
      const overlapsX = r.left < rect.right && r.right > rect.left; // horizontally aligned with the field
      if (gap >= -6 && gap < bestGap && overlapsX) {
        best = t.slice(0, 180);
        bestGap = gap;
      }
    });
    return best;
  };

  // The value currently shown in a custom combobox (react-select etc.). Native <select> exposes
  // el.value, but a div-based combobox does not, so read the DISPLAYED selection.
  const comboboxDisplayValue = (el) => {
    const single = el.querySelector('[class*="single-value"],[class*="singleValue"],[class*="-selected"],[aria-selected="true"]');
    const fromSingle = single ? clean(single.innerText || single.textContent) : "";
    if (fromSingle && !isGeneric(fromSingle)) return fromSingle.slice(0, 120);
    const own = clean(el.innerText || el.textContent);
    return own && !isGeneric(own) ? own.slice(0, 120) : "";
  };

  const headingFor = (el) => {
    let current = el;
    for (let depth = 0; current && depth < 8; depth += 1, current = current.parentElement) {
      const h = current.querySelector && current.querySelector("h1,h2,h3,h4,legend,[role='heading']");
      if (h && clean(h.innerText || h.textContent)) return clean(h.innerText || h.textContent).slice(0, 80);
    }
    return "";
  };

  const elementRefFor = (el, label, role) => ({
    selector: stableSelector(el),
    label: label || "",
    name: clean(el.getAttribute("name") || ""),
    role,
  });

  const visibleMenuScopesFor = (el) => {
    const ids = [el.getAttribute("aria-controls"), el.getAttribute("aria-owns"), el.getAttribute("list")].filter(Boolean);
    const scopes = [];
    ids.forEach((id) => {
      const container = document.getElementById(id);
      if (container && isVisible(container)) scopes.push(container);
    });
    if (scopes.length) return scopes;
    const expanded = el.getAttribute("aria-expanded") === "true" || Boolean(el.closest('[aria-expanded="true"]'));
    if (!expanded) return [];
    let rect;
    try { rect = el.getBoundingClientRect(); } catch (error) { return []; }
    const candidates = Array.from(document.querySelectorAll('[role="listbox"],[class*="select__menu"],[class*="menu-list"],[class*="dropdown-menu"],[class*="-menu"],[class*="autocomplete"],[class*="suggestion"]')).filter(isVisible);
    const tied = candidates
      .map((node) => {
        const r = node.getBoundingClientRect();
        const overlapsX = r.left < rect.right && r.right > rect.left;
        const verticalGap = Math.abs(r.top - rect.bottom);
        return { node, score: overlapsX ? verticalGap : Number.POSITIVE_INFINITY };
      })
      .filter((item) => Number.isFinite(item.score) && item.score < 360)
      .sort((a, b) => a.score - b.score);
    return tied.slice(0, 1).map((item) => item.node);
  };

  const optionsFor = (el) => {
    if ((el.tagName || "").toLowerCase() === "select") {
      return Array.from(el.options)
        .map((o) => clean(o.text || o.label || o.value))
        .filter(Boolean)
        .slice(0, 40);
    }
    let labels = [];
    visibleMenuScopesFor(el).forEach((scope) => {
      const optionNodes = Array.from(scope.querySelectorAll('[role="option"]:not([aria-hidden="true"]),.select__option,li,button')).filter(isVisible);
      labels = labels.concat(optionNodes.map((option) => clean(option.innerText || option.textContent || option.getAttribute("aria-label") || option.value || "")));
    });
    return labels
      .filter(Boolean)
      .filter((label, index, list) => list.indexOf(label) === index)
      .slice(0, 40);
  };

  const stableSelector = (el) => {
    if (el.id) return `#${CSS.escape(el.id)}`;
    for (const attr of ["data-testid", "data-test", "data-qa", "name", "aria-label"]) {
      const value = el.getAttribute(attr);
      if (value) return `${el.tagName.toLowerCase()}[${attr}="${CSS.escape(value)}"]`;
    }
    return el.tagName.toLowerCase();
  };

  const INTERACTABLE = [
    'input:not([type="hidden"])', "textarea", "select",
    '[role="combobox"]', '[role="radio"]', '[role="checkbox"]', '[contenteditable="true"]',
    "button", 'input[type="submit"]', 'input[type="button"]', '[role="button"]', "a[href]", '[role="option"]',
  ].join(",");
  const NAV_RE = /apply|continue|next|review|submit|start|save|add|upload|sign in|log in|create account/i;

  // Many design systems render the required "*" with CSS `label::after { content: "*" }`, which
  // textContent NEVER sees. Read the pseudo-element content directly. Generic — any such marker.
  const pseudoHasStar = (el) => {
    if (!el || el.nodeType !== 1) return false;
    for (const pseudo of ["::after", "::before"]) {
      let content = "";
      try { content = getComputedStyle(el, pseudo).content || ""; } catch (error) { content = ""; }
      if (content && content !== "none" && content !== "normal" && content.includes("*")) return true;
    }
    return false;
  };

  // "Required" = what a HUMAN sees marks this field as required: the field's own DOM `required`; a
  // "*" in the captured label; a dedicated required-marker element (sup / [class*=required] /
  // aria-label "required"); a CSS ::after asterisk; or a literal "*" inside the BLOCK that holds this
  // field's label (its content subtracted so a long typed answer can't inflate it, size-guarded so a
  // sibling field's marker can't leak). aria-required is not trusted: component libraries set it on
  // voluntary fields. For radio/checkbox the logical field is the group.
  const detectRequired = (el, role, label) => {
    if (el.required) return true;
    if (/\*/.test(label)) return true;
    const labelFull = clean(label).replace(/\s*\*\s*$/, "");
    const controlOwn = clean(el.value || el.innerText || el.textContent || "");
    let node = el;
    for (let up = 0; node && up < 7; up += 1, node = node.parentElement) {
      const marker = node.querySelector && node.querySelector('[class*="required" i],[class*="asterisk" i],[class*="mandatory" i],[aria-label*="required" i],sup,[data-required="true"]');
      if (marker) {
        const mText = clean(marker.innerText || marker.textContent || marker.getAttribute("aria-label") || "");
        if (/\*/.test(mText) || /\brequired\b/i.test(mText) || pseudoHasStar(marker)) return true;
      }
      let blockText = clean(node.textContent || "");
      if (controlOwn && controlOwn.length > 2) blockText = clean(blockText.split(controlOwn).join(" "));
      if (labelFull && blockText.includes(labelFull)) {
        if (blockText.length <= 400 && /\*/.test(blockText)) return true;
        const labels = node.querySelectorAll('label,legend,[id$="-label"],[class*="label"]');
        for (let i = 0; i < labels.length && i < 8; i += 1) {
          if (pseudoHasStar(labels[i])) return true;
        }
        if (pseudoHasStar(node)) return true;
        break; // this is the field's question block and it carries no marker -> not required (don't climb on)
      }
      if (blockText.length > 900) break; // climbed into the whole-form container; stop
    }
    if (role === "radio" || role === "checkbox") {
      const group = el.closest('[role="radiogroup"],[role="group"],fieldset');
      if (group && group.querySelector("[required]")) return true;
    }
    return false;
  };

  const nodeFor = (el) => {
    const role = roleFor(el);
    const label = redactSensitiveOwnedText(labelFor(el)).slice(0, 180);
    const ref = elementRefFor(el, label, role);
    const checked = Boolean(el.checked || el.getAttribute("aria-checked") === "true");
    let rawValue = role === "checkbox" || role === "radio" ? (checked ? "true" : "") : clean(el.value || "");
    if (
      !rawValue
      && (el.isContentEditable || clean(el.getAttribute("contenteditable")).toLowerCase() === "true")
    ) rawValue = clean(el.innerText || el.textContent || "");
    if (!rawValue && role === "combobox") rawValue = comboboxDisplayValue(el);
    const valueRedacted = sensitiveValueControl(el);
    const node = {
      role,
      control_kind: role,
      label,
      resolved_label: label,
      placeholder: clean(el.getAttribute("placeholder") || ""),
      value: (valueRedacted && rawValue ? "[REDACTED]" : rawValue).slice(0, 220),
      required: detectRequired(el, role, label),
      checked,
      disabled: Boolean(el.disabled || el.getAttribute("aria-disabled") === "true"),
      readonly: Boolean(el.readOnly || el.getAttribute("readonly") !== null),
      options: optionsFor(el),
      field_ref: ref,
      element_ref: ref,
      locator_ref: ref,
      section_heading: redactSensitiveOwnedText(headingFor(el)),
    };
    if (valueRedacted) {
      node.value_redacted = true;
      node.value_redaction_reason = "sensitive_input_structure";
    }
    return node;
  };

  const coverageEvidence = () => {
    const doc = document.documentElement || document.body || {};
    const body = document.body || {};
    const scrollY = Math.max(window.scrollY || doc.scrollTop || body.scrollTop || 0, 0);
    const clientHeight = Math.max(window.innerHeight || doc.clientHeight || body.clientHeight || 0, 0);
    const scrollHeight = Math.max(doc.scrollHeight || 0, body.scrollHeight || 0, clientHeight);
    return {
      scroll_y: Math.round(scrollY),
      scroll_height: Math.round(scrollHeight),
      client_height: Math.round(clientHeight),
      at_bottom: scrollY + clientHeight >= scrollHeight - 4,
      fits_viewport: scrollHeight <= clientHeight + 4,
    };
  };

  function lumenObserve() {
    const elements = Array.from(document.querySelectorAll(INTERACTABLE)).filter((el) => {
      const type = (el.getAttribute("type") || "").toLowerCase();
      if (!isVisible(el) && type !== "file") return false;
      const role = roleFor(el);
      if (role === "link") {
        return NAV_RE.test(clean(el.innerText || el.textContent || el.getAttribute("aria-label")));
      }
      return true;
    });
    return {
      url: location.href,
      title: document.title,
      // Generic page text (capped) — the eval brain reasons over it and graders verify outcomes
      // like "page contains 'Download complete'" against it.
      text: sanitizedBodyText().slice(0, 4000),
      nodes: elements.map(nodeFor),
    };
  }

  const nodeMergeKey = (node) => [
    clean(node.label).toLowerCase(),
    clean(node.role).toLowerCase(),
    clean(node.element_ref.name).toLowerCase(),
    clean(node.element_ref.selector).toLowerCase(),
  ].join("|");

  // Scroll the page top to bottom, observing each viewport, so controls rendered only near the
  // viewport are captured; the scroll position is restored afterwards.
  async function lumenObserveFullPage() {
    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    const doc = document.documentElement || document.body || {};
    const originalX = window.scrollX || doc.scrollLeft || 0;
    const originalY = window.scrollY || doc.scrollTop || 0;
    const merged = new Map();
    let finalSnapshot = null;
    const scrollToY = (y) => {
      try {
        window.scrollTo(originalX, y);
      } catch (error) {
        try { doc.scrollTop = y; } catch (_) { /* ignore */ }
        try { if (document.body) document.body.scrollTop = y; } catch (_) { /* ignore */ }
      }
    };
    try {
      scrollToY(0);
      await sleep(140);
      let lastY = -1;
      for (let i = 0; i < 40; i += 1) {
        finalSnapshot = lumenObserve();
        for (const node of finalSnapshot.nodes) merged.set(nodeMergeKey(node), node);
        const evidence = coverageEvidence();
        const y = evidence.scroll_y;
        if (evidence.at_bottom || evidence.fits_viewport) break;
        if (y === lastY && i > 0) break;
        lastY = y;
        const step = Math.max(Math.floor((evidence.client_height || 600) * 0.82), 360);
        scrollToY(Math.min(evidence.scroll_height, y + step));
        await sleep(170);
      }
      return { ...finalSnapshot, nodes: Array.from(merged.values()) };
    } finally {
      scrollToY(originalY);
    }
  }

  // Visible suggestion/option items currently rendered (autocomplete menus, listboxes). Shared by
  // the typeahead fill path and the custom-combobox select path.
  const readVisibleOptions = () => {
    const scopes = Array.from(document.querySelectorAll('[role="listbox"],[class*="select__menu"],[class*="menu-list"],[class*="dropdown-menu"],[class*="-menu"],[class*="autocomplete"],[class*="suggestion"]')).filter(isVisible);
    let nodes = [];
    scopes.forEach((scope) => { nodes = nodes.concat(Array.from(scope.querySelectorAll('[role="option"],[class*="option"],li'))); });
    if (!nodes.length) nodes = Array.from(document.querySelectorAll('[role="option"]:not([aria-hidden="true"]),.select__option,[class*="option"]'));
    return nodes.filter(isVisible).map((node) => ({ text: clean(node.innerText || node.textContent || node.getAttribute("aria-label")), el: node })).filter((option) => option.text);
  };

  // --- Menu hygiene: a control's transient open menu must never leak onto the NEXT field. ---
  // Is a menu OWNED BY or adjacent to this control still open? Reuses the SAME owner/geometry scoping
  // observe.js binds options with, so we never mistake a different field's menu for this one.
  const ownedMenuOpen = (el) => {
    try { return Boolean(el) && visibleMenuScopesFor(el).length > 0; } catch (error) { return false; }
  };
  // Close THIS control's own open menu without disturbing the rest of the page: a SCOPED Escape on the
  // control (and its inner input) + a blur. NEVER a document-level Escape — that can close modals or
  // cancel unrelated UI. Bounded retries; gives up quietly if the page insists on keeping it open.
  const closeOwnedMenu = async (el) => {
    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    if (!el) return;
    const input = (el.tagName || "").toLowerCase() === "input" ? el : el.querySelector('input:not([type="hidden"])');
    for (let i = 0; i < 3 && ownedMenuOpen(el); i += 1) {
      try { el.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); } catch (error) { /* ignore */ }
      if (input) { try { input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); input.blur(); } catch (error) { /* ignore */ } }
      try { el.blur(); } catch (error) { /* ignore */ }
      await sleep(120);
    }
  };

  // Execute ONE typed `fill` intent the brain resolved. Dumb body: receives a selector + value and
  // types it; never decides what to fill. Native value setter + input/change so React registers it,
  // then reads back. For an AUTOCOMPLETE/typeahead input (location, company, school) typing opens a
  // suggestion list that must be CLICKED or the field stays unselected and the form rejects free
  // text — so it picks the matching suggestion (async).
  async function lumenExecuteFill(selector, value) {
    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    let el = null;
    try {
      el = selector ? document.querySelector(selector) : null;
    } catch (error) {
      return { ok: false, selector, error: "Bad selector: " + String(error) };
    }
    if (!el) return { ok: false, selector, error: "Target element not found." };
    try {
      el.scrollIntoView({ block: "center", inline: "center" });
      el.focus();
      // Snapshot the option menus visible BEFORE we type — taken NOW, before any value/input event — so a
      // menu a PREVIOUS field left open (e.g. a select's Yes/No list) is recorded as pre-existing. Only
      // options that appear AFTER typing count as THIS field's suggestions: a plain textarea reveals none
      // (so a stale menu can't make it read as a typeahead), while a real typeahead's menu (opened by
      // the input event below) is new and is still captured.
      const preExistingLabels = readVisibleOptions().map((o) => o.text).filter(Boolean);
      const preExisting = new Set(preExistingLabels.map((text) => normOpt(text)));
      const freshOptions = () => readVisibleOptions().filter((o) => !preExisting.has(normOpt(o.text)));
      const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, "value");
      if (setter && setter.set) setter.set.call(el, value);
      else el.value = value;
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      // Perceive a constrained control BEHAVIORALLY: if typing reveals an option list, it's a
      // typeahead/dropdown — even when it exposes no ARIA combobox attributes. We must then pick from
      // the options or ask; never leave typed free text.
      const ariaTypeahead = Boolean(
        el.getAttribute("aria-autocomplete") || el.getAttribute("aria-controls") || el.getAttribute("role") === "combobox" || el.closest('[role="combobox"]')
      );
      let picked = false;
      let sawOptions = false;
      if (value) {
        for (let i = 0; i < 6 && !picked; i += 1) {
          await sleep(150);
          const opts = freshOptions();
          if (opts.length) sawOptions = true;
          const match = matchOption(value, opts);
          if (match) {
            match.el.scrollIntoView({ block: "center" });
            ["pointerdown", "mousedown", "mouseup", "click"].forEach((t) => match.el.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true })));
            picked = true;
            break;
          }
          if (i >= 1 && !sawOptions && !ariaTypeahead) break;  // a plain text field never reveals options -> stop waiting
        }
      }
      const typeahead = ariaTypeahead || sawOptions;  // revealed an option list -> constrained
      if (typeahead && value && !picked) {
        // Constrained control: the typed value matched no suggestion. Clear the fabricated free text
        // and read the REAL suggestions so the loop can translate-or-ask — never leave a bad free-text
        // value in a typeahead.
        if (setter && setter.set) setter.set.call(el, "");
        else el.value = "";
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
        await sleep(220);
        const suggestions = freshOptions().map((o) => o.text).filter(Boolean).slice(0, 25);
        return { ok: false, action: "fill", selector, error: "No matching suggestion for " + value, needs_translation: true, options: suggestions, observed_value: "", value_present: false, newly_revealed_owned_options: suggestions, stray_options_ignored: preExistingLabels.slice(0, 25) };
      }
      if (!typeahead) el.dispatchEvent(new Event("blur", { bubbles: true }));
      const observed = clean(el.value || "");
      const revealed = freshOptions().map((o) => o.text).filter(Boolean).slice(0, 25);
      return { ok: true, action: "fill", selector, observed_value: observed, value_present: Boolean(observed) || picked, typeahead_selected: picked, newly_revealed_owned_options: revealed, stray_options_ignored: preExistingLabels.slice(0, 25) };
    } catch (error) {
      return { ok: false, selector, error: String(error) };
    }
  }

  // Execute ONE typed `click` intent (e.g. Apply / Continue) the brain resolved.
  function lumenExecuteClick(selector) {
    let el = null;
    try {
      el = selector ? document.querySelector(selector) : null;
    } catch (error) {
      return { ok: false, selector, error: "Bad selector: " + String(error) };
    }
    if (!el) return { ok: false, selector, error: "Target element not found." };
    try {
      el.scrollIntoView({ block: "center", inline: "center" });
      const before = location.href;
      el.click();
      return { ok: true, action: "click", selector, label: clean(el.innerText || el.textContent || el.value), url_before: before };
    } catch (error) {
      return { ok: false, selector, error: String(error) };
    }
  }

  // Option matching: exact -> contains -> semantic opt-out (the prefer-not-to-say / decline /
  // not-disclose family) -> fuzzy (Dice bigram overlap).
  const normOpt = (v) => clean(v).toLowerCase().replace(/[^a-z0-9+#./ -]+/g, "").trim();
  const OPT_OUT_RE = /(prefer not|choose not|do ?n'?t wish|i do ?n'?t wish|do ?n'?t want|decline|not disclose|no answer|not specified|not applicable|n\/a|opt out|rather not|wish not)/;
  const optIntent = (t) => (OPT_OUT_RE.test(t) ? "opt_out" : null);
  const diceScore = (a, b) => {
    if (a === b) return 1;
    if (a.length < 2 || b.length < 2) return 0;
    const grams = (s) => { const m = new Map(); for (let i = 0; i < s.length - 1; i += 1) { const g = s.slice(i, i + 2); m.set(g, (m.get(g) || 0) + 1); } return m; };
    const A = grams(a), B = grams(b); let inter = 0;
    A.forEach((count, g) => { if (B.has(g)) inter += Math.min(count, B.get(g)); });
    return (2 * inter) / ((a.length - 1) + (b.length - 1));
  };
  // candidates: [{text, el}] -> best {text, el, norm} or null.
  const matchOption = (value, candidates) => {
    const want = normOpt(value);
    const cands = candidates.map((c) => ({ ...c, norm: normOpt(c.text) })).filter((c) => c.norm);
    if (!want || !cands.length) return null;
    let hit = cands.find((c) => c.norm === want);
    if (hit) return hit;
    hit = cands.find((c) => want.length >= 5 && c.norm.length >= 5 && (c.norm.includes(want) || want.includes(c.norm)));
    if (hit) return hit;
    const wi = optIntent(want);
    if (wi) { hit = cands.find((c) => optIntent(c.norm) === wi); if (hit) return hit; }
    let best = null, bestScore = 0;
    for (const c of cands) { const s = diceScore(want, c.norm); if (s > bestScore) { best = c; bestScore = s; } }
    return bestScore >= 0.6 ? best : null;
  };

  // Execute ONE typed `select` intent: native <select>, or a custom combobox via robust
  // open (click / pointer events / keyboard) -> poll for rendered options -> match -> click.
  async function lumenExecuteSelect(selector, value) {
    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    let el = null;
    try {
      el = selector ? document.querySelector(selector) : null;
    } catch (error) {
      return { ok: false, selector, error: "Bad selector: " + String(error) };
    }
    if (!el) return { ok: false, selector, error: "Target element not found." };
    try {
      el.scrollIntoView({ block: "center", inline: "center" });
      const checkableRole = roleFor(el);
      if (checkableRole === "radio" || checkableRole === "checkbox") {
        const ownLabel = clean(labelFor(el) || el.getAttribute("aria-label") || el.innerText || el.textContent || el.value || "");
        const ownNorm = normOpt(ownLabel);
        const valueNorm = normOpt(value);
        const labelMatches = !valueNorm || Boolean(ownNorm && (ownNorm === valueNorm || ownNorm.includes(valueNorm) || valueNorm.includes(ownNorm)));
        if (!labelMatches) {
          return {
            ok: false,
            action: "select",
            selector,
            error: "Checkable option does not match " + value,
            needs_translation: true,
            observed_value: ownLabel,
            options: [ownLabel].filter(Boolean),
            value_present: false,
            observed_value_matches: false,
          };
        }
        ["pointerdown", "mousedown", "mouseup", "click"].forEach((t) => el.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true })));
        await sleep(80);
        const checkedAfter = Boolean(el.checked || el.getAttribute("aria-checked") === "true");
        return {
          ok: checkedAfter,
          action: "select",
          selector,
          observed_value: ownLabel,
          value_present: checkedAfter,
          observed_value_matches: checkedAfter && labelMatches,
          owned_menu_visible_after: false,
          transient_ui_settled: true,
          failure_reason: checkedAfter ? "" : "CHECKABLE_NOT_SELECTED",
        };
      }
      if ((el.tagName || "").toLowerCase() === "select") {
        const opts = Array.from(el.options).map((o) => ({ text: clean(o.text || o.label || o.value), el: o }));
        const m = matchOption(value, opts);
        if (!m) return { ok: false, selector, error: "No matching option for " + value, needs_translation: true, observed_value: clean(el.value), options: opts.slice(0, 25).map((o) => o.text) };
        const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value");
        if (setter && setter.set) setter.set.call(el, m.el.value);
        else el.value = m.el.value;
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
        return { ok: true, action: "select", selector, observed_value: clean(m.el.text || m.el.value), value_present: true, observed_value_matches: true, owned_menu_visible_after: false, transient_ui_settled: true };
      }
      // Custom combobox. Read options preferring an open menu/listbox; fall back to global.
      const readOptions = () => {
        const scopes = Array.from(document.querySelectorAll('[role="listbox"],[class*="select__menu"],[class*="menu-list"],[class*="dropdown-menu"],[class*="-menu"]')).filter(isVisible);
        let nodes = [];
        scopes.forEach((s) => { nodes = nodes.concat(Array.from(s.querySelectorAll('[role="option"],[class*="option"],li'))); });
        if (!nodes.length) nodes = Array.from(document.querySelectorAll('[role="option"]:not([aria-hidden="true"]),.select__option,[class*="option"]'));
        return nodes.filter(isVisible).map((n) => ({ text: clean(n.innerText || n.textContent || n.getAttribute("aria-label")), el: n })).filter((o) => o.text);
      };
      const openTries = [
        () => {
          // Typeahead: type the value into the combobox's search input so suggestions populate
          // (location/company/school fields show options only after typing, not on a bare click).
          const input = (el.tagName || "").toLowerCase() === "input" ? el : el.querySelector('input:not([type="hidden"])');
          if (input && value) {
            input.focus();
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
            if (setter && setter.set) setter.set.call(input, value);
            else input.value = value;
            input.dispatchEvent(new Event("input", { bubbles: true }));
          } else {
            el.focus();
            el.click();
          }
        },
        () => { el.focus(); el.click(); },
        () => { ["pointerdown", "mousedown", "mouseup", "click"].forEach((t) => el.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true }))); },
        () => { el.focus(); el.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true })); },
      ];
      let options = [];
      for (const open of openTries) {
        try { open(); } catch (e) { /* try the next opener */ }
        for (let i = 0; i < 8 && options.length === 0; i += 1) { await sleep(150); options = readOptions(); }
        if (options.length) break;
      }
      let match = options.length ? matchOption(value, options) : null;
      if (!match) {
        // Typing the value as a SEARCH may have filtered the list to nothing / no match. CLEAR the
        // search so the FULL option list shows, re-read it, and re-match — so we hand back the REAL
        // options (never an empty/"no results" list) and never leave the typed guess in the box.
        const input = (el.tagName || "").toLowerCase() === "input" ? el : el.querySelector('input:not([type="hidden"])');
        if (input && clean(input.value)) {
          const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
          if (setter && setter.set) setter.set.call(input, "");
          else input.value = "";
          input.dispatchEvent(new Event("input", { bubbles: true }));
          input.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
          let full = [];
          for (let i = 0; i < 8 && full.length === 0; i += 1) { await sleep(150); full = readOptions(); }
          if (full.length) {
            options = full;
            match = matchOption(value, full);
          }
        }
      }
      if (!match) {
        // The body matched locally (exact/contains/opt-out/fuzzy) and couldn't — hand the REAL
        // options back so the brain can translate the value to the exact one or ask, then retry.
        if (!options.length) return { ok: false, selector, error: "Dropdown did not open (no options) for " + value };
        await closeOwnedMenu(el);  // scoped close — never a global Escape that could disturb other UI
        return { ok: false, selector, error: "No matching option for " + value, needs_translation: true, options: options.slice(0, 25).map((o) => o.text) };
      }
      match.el.scrollIntoView({ block: "center" });
      ["pointerdown", "mousedown", "mouseup", "click"].forEach((t) => match.el.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true })));
      await sleep(220);
      // Close THIS control's menu so its options can't bleed onto the next field's fill, then verify
      // the owned menu is gone.
      await closeOwnedMenu(el);
      const ownedOpenAfter = ownedMenuOpen(el);
      if (ownedOpenAfter) {
        return { ok: false, action: "select", selector, observed_value: match.text, control_text: clean(el.innerText || el.value || match.text), value_present: true, observed_value_matches: true, owned_menu_visible_after: true, transient_ui_settled: false, failure_reason: "OWNED_MENU_STILL_OPEN" };
      }
      return { ok: true, action: "select", selector, observed_value: match.text, control_text: clean(el.innerText || el.value || match.text), value_present: true, observed_value_matches: true, owned_menu_visible_after: false, transient_ui_settled: true };
    } catch (error) {
      return { ok: false, selector, error: String(error) };
    }
  }

  // Shared isolated-world scope so the background worker can invoke these on demand.
  globalThis.__lumenObserveFullPage = lumenObserveFullPage;
  globalThis.__lumenExecuteFill = lumenExecuteFill;
  globalThis.__lumenExecuteClick = lumenExecuteClick;
  globalThis.__lumenExecuteSelect = lumenExecuteSelect;
})();
