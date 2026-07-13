// ChatGPT-style zero-build frontend for FDAgent /ask conversations.
(() => {
  const STORE_KEY = "fdagent.chat.v1";
  const MAX_TITLE = 44;
  const SVG_NS = "http://www.w3.org/2000/svg";
  const TITLE_NEW = "new";
  const TITLE_PLACEHOLDER = "placeholder";
  const TITLE_AUTO = "auto";
  const TITLE_MANUAL = "manual";
  const TITLE_LEGACY = "legacy";
  const TITLE_SOURCES = [TITLE_NEW, TITLE_PLACEHOLDER, TITLE_AUTO, TITLE_MANUAL, TITLE_LEGACY];
  const EXAMPLES = [
    "How many Class I drug recalls have there been?",
    "Which firms had the most Class I recalls?",
    "What is the yearly trend of recalls in California?",
    "Show me a few sterility-related recalls.",
    "Find recalls involving pills that were too strong.",
  ];

  const els = {
    conversationList: document.getElementById("conversationList"),
    conversationTitle: document.getElementById("conversationTitle"),
    examples: document.getElementById("examples"),
    notice: document.getElementById("notice"),
    thread: document.getElementById("thread"),
    composer: document.getElementById("composer"),
    input: document.getElementById("questionInput"),
    send: document.getElementById("sendButton"),
    stop: document.getElementById("stopButton"),
    newChat: document.getElementById("newChat"),
  };

  let noticeText = "";
  let state = loadState();
  let activeRequest = null;
  let editState = null;

  ensureConversation();
  renderExamples();
  render();

  els.newChat.addEventListener("click", () => {
    const conversation = createConversation();
    state.conversations.unshift(conversation);
    state.activeId = conversation.id;
    editState = null;
    saveState();
    render();
    focusComposer();
  });

  els.composer.addEventListener("submit", (event) => {
    event.preventDefault();
    sendCurrentPrompt();
  });

  els.input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendCurrentPrompt();
    }
  });

  els.input.addEventListener("input", autoSizeComposer);
  els.stop.addEventListener("click", stopGeneration);

  function loadState() {
    try {
      const raw = localStorage.getItem(STORE_KEY);
      if (!raw) return { conversations: [], activeId: null };
      const parsed = JSON.parse(raw);
      if (!parsed || !Array.isArray(parsed.conversations)) {
        throw new Error("Invalid saved conversation format.");
      }

      const conversations = parsed.conversations
        .map(normalizeConversation)
        .filter(Boolean);
      return {
        conversations,
        activeId: conversations.some((conv) => conv.id === parsed.activeId)
          ? parsed.activeId
          : conversations[0]?.id ?? null,
      };
    } catch (error) {
      noticeText = `Saved conversations could not be loaded and were reset: ${error.message}`;
      return { conversations: [], activeId: null };
    }
  }

  function normalizeConversation(value) {
    if (!value || typeof value !== "object" || !Array.isArray(value.messages)) {
      return null;
    }
    const id = typeof value.id === "string" && value.id ? value.id : makeId("conv");
    const messages = value.messages
      .map(normalizeMessage)
      .filter(Boolean);
    const title = typeof value.title === "string" && value.title.trim()
      ? value.title.trim()
      : "New chat";
    const titleSource = normalizeTitleSource(value.titleSource, messages, title);
    const migratedWithoutTitleMetadata = typeof value.titleSource !== "string";
    return {
      id,
      title,
      titleSource,
      titleRequested: Boolean(value.titleRequested) || (migratedWithoutTitleMetadata && messages.length > 0),
      titlePromptMessageId: typeof value.titlePromptMessageId === "string" ? value.titlePromptMessageId : null,
      titlePromptQuestion: typeof value.titlePromptQuestion === "string" ? value.titlePromptQuestion : null,
      messages,
      createdAt: value.createdAt || new Date().toISOString(),
      updatedAt: value.updatedAt || new Date().toISOString(),
    };
  }

  function normalizeTitleSource(value, messages, title) {
    if (TITLE_SOURCES.includes(value)) return value;
    if (messages.length) return TITLE_LEGACY;
    return title === "New chat" ? TITLE_NEW : TITLE_MANUAL;
  }

  function normalizeMessage(value) {
    if (!value || typeof value !== "object") return null;
    if (value.role !== "user" && value.role !== "assistant") return null;
    return {
      id: typeof value.id === "string" && value.id ? value.id : makeId("msg"),
      role: value.role,
      content: typeof value.content === "string" ? value.content : "",
      status: value.status || (value.role === "assistant" ? "done" : undefined),
      result: value.result || null,
      error: value.error || null,
      createdAt: value.createdAt || new Date().toISOString(),
    };
  }

  function saveState() {
    localStorage.setItem(STORE_KEY, JSON.stringify({
      version: 2,
      activeId: state.activeId,
      conversations: state.conversations,
    }));
  }

  function ensureConversation() {
    if (!state.conversations.length) {
      const conversation = createConversation();
      state.conversations.push(conversation);
      state.activeId = conversation.id;
      saveState();
      return;
    }
    if (!getActiveConversation()) {
      state.activeId = state.conversations[0].id;
      saveState();
    }
  }

  function createConversation() {
    const now = new Date().toISOString();
    return {
      id: makeId("conv"),
      title: "New chat",
      titleSource: TITLE_NEW,
      titleRequested: false,
      titlePromptMessageId: null,
      titlePromptQuestion: null,
      messages: [],
      createdAt: now,
      updatedAt: now,
    };
  }

  function makeId(prefix) {
    if (globalThis.crypto?.randomUUID) return `${prefix}-${globalThis.crypto.randomUUID()}`;
    return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function getActiveConversation() {
    return state.conversations.find((conv) => conv.id === state.activeId) || null;
  }

  function touch(conversation) {
    conversation.updatedAt = new Date().toISOString();
    state.conversations.sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
  }

  function render() {
    ensureConversation();
    renderSidebar();
    renderThread();
    renderComposerState();
    autoSizeComposer();
  }

  function renderExamples() {
    els.examples.replaceChildren();
    for (const example of EXAMPLES) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "chip";
      button.textContent = example;
      button.addEventListener("click", () => {
        els.input.value = example;
        autoSizeComposer();
        sendCurrentPrompt();
      });
      els.examples.appendChild(button);
    }
  }

  function renderSidebar() {
    els.conversationList.replaceChildren();
    for (const conversation of state.conversations) {
      const row = document.createElement("div");
      row.className = `conversation-item${conversation.id === state.activeId ? " active" : ""}`;

      const select = document.createElement("button");
      select.type = "button";
      select.className = "conversation-select";
      select.textContent = conversation.title;
      select.title = conversation.title;
      select.addEventListener("click", () => {
        state.activeId = conversation.id;
        editState = null;
        saveState();
        render();
      });

      const rename = document.createElement("button");
      rename.type = "button";
      rename.className = "icon-button";
      rename.title = "Rename conversation";
      rename.setAttribute("aria-label", `Rename ${conversation.title}`);
      rename.appendChild(iconSvg("pencil"));
      rename.addEventListener("click", () => renameConversation(conversation.id));

      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "icon-button delete-button";
      remove.title = "Delete conversation";
      remove.setAttribute("aria-label", `Delete ${conversation.title}`);
      remove.appendChild(iconSvg("trash"));
      remove.addEventListener("click", () => deleteConversation(conversation.id));

      row.append(select, rename, remove);
      els.conversationList.appendChild(row);
    }
  }

  function renameConversation(conversationId) {
    const conversation = state.conversations.find((conv) => conv.id === conversationId);
    if (!conversation) return;
    const next = prompt("Rename conversation", conversation.title);
    if (next === null) return;
    const title = next.trim();
    if (!title) return;
    conversation.title = title.slice(0, MAX_TITLE);
    conversation.titleSource = TITLE_MANUAL;
    conversation.titleRequested = true;
    touch(conversation);
    saveState();
    render();
  }

  function deleteConversation(conversationId) {
    const conversation = state.conversations.find((conv) => conv.id === conversationId);
    if (!conversation) return;
    if (!confirm(`Delete "${conversation.title}"?`)) return;
    if (activeRequest?.conversationId === conversationId) {
      activeRequest.controller.abort();
    }
    state.conversations = state.conversations.filter((conv) => conv.id !== conversationId);
    if (!state.conversations.length) {
      const replacement = createConversation();
      state.conversations.push(replacement);
    }
    if (state.activeId === conversationId) {
      state.activeId = state.conversations[0].id;
    }
    editState = null;
    saveState();
    render();
  }

  function renderThread() {
    const conversation = getActiveConversation();
    els.conversationTitle.textContent = conversation.title;
    els.notice.textContent = noticeText;
    els.thread.replaceChildren();

    if (!conversation.messages.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      const title = document.createElement("strong");
      title.textContent = "Start a recall analysis";
      const text = document.createElement("span");
      text.textContent = "Ask for counts, trends, firm rankings, sample rows, or fuzzy semantic matches.";
      empty.append(title, text);
      els.thread.appendChild(empty);
      return;
    }

    for (const message of conversation.messages) {
      els.thread.appendChild(renderMessage(conversation, message));
    }
    requestAnimationFrame(() => {
      els.thread.scrollTop = els.thread.scrollHeight;
    });
  }

  function renderMessage(conversation, message) {
    const row = document.createElement("article");
    row.className = `message-row ${message.role}`;

    const bubble = document.createElement("div");
    bubble.className = "bubble";

    const meta = document.createElement("div");
    meta.className = "message-meta";
    const who = document.createElement("span");
    who.textContent = message.role === "user" ? "You" : "FDAgent";
    meta.appendChild(who);

    if (message.role === "user") {
      const actions = document.createElement("div");
      actions.className = "message-actions";
      const edit = document.createElement("button");
      edit.type = "button";
      edit.className = "link-button";
      edit.textContent = "Edit";
      edit.disabled = Boolean(activeRequest);
      edit.addEventListener("click", () => startEdit(conversation.id, message.id));
      actions.appendChild(edit);
      meta.appendChild(actions);
    }

    bubble.appendChild(meta);

    if (message.role === "user") {
      if (editState?.conversationId === conversation.id && editState.messageId === message.id) {
        bubble.appendChild(renderEditForm(conversation.id, message.id));
      } else {
        const text = document.createElement("div");
        text.className = "message-text";
        text.textContent = message.content;
        bubble.appendChild(text);
      }
    } else {
      bubble.appendChild(renderAssistantContent(message));
    }

    row.appendChild(bubble);
    return row;
  }

  function renderEditForm(conversationId, messageId) {
    const form = document.createElement("form");
    form.className = "edit-form";

    const textarea = document.createElement("textarea");
    textarea.value = editState.value;
    textarea.addEventListener("input", () => {
      editState.value = textarea.value;
    });

    const actions = document.createElement("div");
    actions.className = "edit-actions";

    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "secondary";
    cancel.textContent = "Cancel";
    cancel.addEventListener("click", () => {
      editState = null;
      render();
    });

    const save = document.createElement("button");
    save.type = "submit";
    save.textContent = "Save and submit";

    actions.append(cancel, save);
    form.append(textarea, actions);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      saveEdit(conversationId, messageId, textarea.value);
    });
    requestAnimationFrame(() => textarea.focus());
    return form;
  }

  function renderAssistantContent(message) {
    if (message.status === "loading") {
      const status = document.createElement("div");
      status.className = "status-text";
      status.textContent = "Thinking...";
      return status;
    }
    if (message.status === "aborted") {
      const status = document.createElement("div");
      status.className = "status-text aborted";
      status.textContent = "Generation stopped.";
      return status;
    }
    if (message.status === "error") {
      const status = document.createElement("div");
      status.className = "status-text error";
      status.textContent = message.error || "The request failed.";
      return status;
    }
    if (!message.result) {
      const status = document.createElement("div");
      status.className = "status-text";
      status.textContent = "No result.";
      return status;
    }
    return renderAskResult(message.result);
  }

  function renderAskResult(result) {
    const card = document.createElement("div");
    card.className = "result-card";

    const summary = document.createElement("div");
    summary.className = "summary";
    summary.textContent = result.summary || "Answered.";
    card.appendChild(summary);

    const data = result.data || {};
    const kind = data.kind;
    if (kind === "scalar") {
      renderScalar(data, card);
    } else if (kind === "distribution" || kind === "bar") {
      renderDistribution(data, card);
    } else if (kind === "series" || kind === "line") {
      renderSeries(data, card);
    } else if (kind === "rows" || kind === "table") {
      renderRows(data, card);
    } else if (kind === "retrieval" || kind === "semantic") {
      renderRetrieval(data, card);
    } else if (kind === "semantic_count" || kind === "semantic_distribution") {
      renderSemanticCount(data, card);
    } else if (kind === "taxonomy_explanation") {
      renderTaxonomyExplanation(data, card);
    } else if (kind === "message" || kind === "clarification") {
      renderAgentMessage(data, card);
    } else {
      const unknown = document.createElement("div");
      unknown.className = "status-text error";
      unknown.textContent = `Unsupported response kind: ${kind || "unknown"}`;
      card.appendChild(unknown);
    }

    const spec = document.createElement("div");
    spec.className = "spec";
    spec.textContent = `intent=${result.intent || "-"} | spec=${JSON.stringify(result.spec || {})}`;
    card.appendChild(spec);
    return card;
  }

  function renderScalar(data, card) {
    const value = document.createElement("div");
    value.className = "scalar";
    value.textContent = Number(data.value ?? 0).toLocaleString();
    card.appendChild(value);
  }

  function renderAgentMessage(data, card) {
    if (Array.isArray(data.suggestions) && data.suggestions.length) {
      const label = document.createElement("div");
      label.className = "muted";
      label.textContent = data.kind === "clarification" ? "Try a more specific question:" : "Examples:";
      card.appendChild(label);

      const examples = document.createElement("div");
      examples.className = "badge-row";
      for (const suggestion of data.suggestions.slice(0, 4)) {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "chip";
        chip.textContent = suggestion;
        chip.addEventListener("click", () => {
          promptInput.value = suggestion;
          promptInput.focus();
          autoResizePrompt();
        });
        examples.appendChild(chip);
      }
      card.appendChild(examples);
    }
  }

  function renderTaxonomyExplanation(data, card) {
    const metaParts = [
      data.label ? `Category: ${data.label}` : null,
      data.node_id ? `node ${data.node_id}` : null,
      data.source ? `source: ${data.source}` : null,
    ].filter(Boolean);
    if (metaParts.length) {
      const meta = document.createElement("div");
      meta.className = "muted";
      meta.textContent = metaParts.join(" | ");
      card.appendChild(meta);
    }

    if (data.definition) {
      const definition = document.createElement("div");
      definition.className = "muted";
      definition.textContent = `Taxonomy definition: ${data.definition}`;
      card.appendChild(definition);
    }

    const examples = Array.isArray(data.examples) ? data.examples : [];
    if (!examples.length) return;

    const label = document.createElement("div");
    label.className = "muted";
    label.textContent = "Example recall reasons:";
    card.appendChild(label);

    for (const item of examples.slice(0, 3)) {
      const hit = document.createElement("div");
      hit.className = "hit";

      const top = document.createElement("div");
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = item.recall_number || "-";
      top.appendChild(badge);

      const meta = document.createElement("span");
      meta.className = "hit-meta";
      meta.textContent = ` ${item.classification || "-"}`;
      top.appendChild(meta);

      const content = document.createElement("div");
      content.className = "hit-content";
      content.textContent = item.reason_for_recall || "";

      hit.append(top, content);
      card.appendChild(hit);
    }
  }

  function renderSemanticCount(data, card) {
    const value = document.createElement("div");
    value.className = "scalar";
    value.textContent = `~${Number(data.estimated_count ?? 0).toLocaleString()}`;
    card.appendChild(value);

    const interval = data.confidence_interval || {};
    const confidence = data.confidence || {};
    const meta = document.createElement("div");
    meta.className = "muted";
    meta.textContent = [
      "Estimated semantic count",
      `verified ${data.verified || `${data.verified_count ?? 0}/${data.candidate_count ?? 0}`}`,
      `${data.candidate_count ?? 0} retrieval candidates`,
      `avg confidence ${confidence.accepted_avg ?? "0.000"}`,
      `band ${interval.lower ?? 0}-${interval.upper ?? 0}`,
    ].join(" | ");
    card.appendChild(meta);

    if (data.kind === "semantic_distribution") {
      renderDistribution(data, card);
    } else if (Array.isArray(data.evidence) && data.evidence.length) {
      const label = document.createElement("div");
      label.className = "muted";
      label.textContent = "Evidence recall numbers:";
      card.appendChild(label);
      card.appendChild(renderBadges(data.evidence.slice(0, 18)));
    }

    const evidenceItems = Array.isArray(data.evidence_items) ? data.evidence_items.slice(0, 5) : [];
    for (const item of evidenceItems) {
      const hit = document.createElement("div");
      hit.className = "hit";

      const top = document.createElement("div");
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = item.recall_number || "-";
      top.appendChild(badge);

      const hitMeta = document.createElement("span");
      hitMeta.className = "hit-meta";
      hitMeta.textContent = ` validation ${item.validation_confidence ?? "-"} | sim ${item.similarity ?? "-"} | ${item.classification || "-"} | ${item.recalling_firm || "-"}`;
      top.appendChild(hitMeta);

      const snippet = document.createElement("div");
      snippet.className = "hit-content";
      snippet.textContent = item.supporting_snippet || item.content || "";

      const rationale = document.createElement("div");
      rationale.className = "muted";
      rationale.textContent = item.rationale || "";

      hit.append(top, snippet, rationale);
      card.appendChild(hit);
    }
  }

  function renderDistribution(data, card) {
    const items = Array.isArray(data.items) ? data.items.slice(0, 15) : [];
    if (!items.length) {
      const empty = document.createElement("div");
      empty.className = "muted";
      empty.textContent = "No distribution items returned.";
      card.appendChild(empty);
      return;
    }

    const chart = document.createElement("div");
    chart.className = "bar-chart";
    const max = Math.max(...items.map((item) => Number(item.count) || 0), 1);

    for (const item of items) {
      const count = Number(item.count) || 0;
      const row = document.createElement("div");
      row.className = "bar-row";

      const label = document.createElement("div");
      label.className = "bar-label";
      label.title = String(item.value ?? "-");
      label.textContent = String(item.value ?? "-");

      const track = document.createElement("div");
      track.className = "bar-track";
      const fill = document.createElement("div");
      fill.className = "bar-fill";
      fill.style.width = `${Math.max((count / max) * 100, count ? 3 : 0)}%`;
      track.appendChild(fill);

      const value = document.createElement("div");
      value.className = "bar-value";
      value.textContent = count.toLocaleString();

      row.append(label, track, value);
      chart.appendChild(row);
    }
    card.appendChild(chart);

    const evidence = items.flatMap((item) => Array.isArray(item.evidence) ? item.evidence : []);
    if (evidence.length) {
      const label = document.createElement("div");
      label.className = "muted";
      label.textContent = "Evidence recall numbers (samples):";
      card.appendChild(label);
      card.appendChild(renderBadges(evidence.slice(0, 18)));
    }
  }

  function renderSeries(data, card) {
    const points = Array.isArray(data.points) ? data.points : [];
    if (!points.length) {
      const empty = document.createElement("div");
      empty.className = "muted";
      empty.textContent = "No trend points returned.";
      card.appendChild(empty);
      return;
    }

    const width = 700;
    const height = 230;
    const pad = 34;
    const values = points.map((point) => Number(point.count) || 0);
    const max = Math.max(...values, 1);
    const min = 0;
    const xFor = (index) => pad + (points.length === 1 ? 0 : index * ((width - pad * 2) / (points.length - 1)));
    const yFor = (value) => height - pad - ((value - min) / (max - min || 1)) * (height - pad * 2);
    const coords = points.map((point, index) => `${xFor(index)},${yFor(Number(point.count) || 0)}`).join(" ");

    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", "line-chart");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", `Line chart of count by ${data.grain || "period"}`);

    const xAxis = svgLine(pad, height - pad, width - pad, height - pad, "axis");
    const yAxis = svgLine(pad, pad, pad, height - pad, "axis");
    svg.append(xAxis, yAxis);

    const polyline = document.createElementNS(SVG_NS, "polyline");
    polyline.setAttribute("class", "line");
    polyline.setAttribute("points", coords);
    svg.appendChild(polyline);

    points.forEach((point, index) => {
      const count = Number(point.count) || 0;
      const circle = document.createElementNS(SVG_NS, "circle");
      circle.setAttribute("class", "point");
      circle.setAttribute("cx", xFor(index));
      circle.setAttribute("cy", yFor(count));
      circle.setAttribute("r", "4");
      svg.appendChild(circle);

      if (index === 0 || index === points.length - 1 || points.length <= 8) {
        const label = document.createElementNS(SVG_NS, "text");
        label.setAttribute("x", xFor(index));
        label.setAttribute("y", height - 10);
        label.setAttribute("text-anchor", index === 0 ? "start" : index === points.length - 1 ? "end" : "middle");
        label.textContent = String(point.period ?? "");
        svg.appendChild(label);
      }
    });

    const maxLabel = document.createElementNS(SVG_NS, "text");
    maxLabel.setAttribute("x", pad + 6);
    maxLabel.setAttribute("y", pad + 4);
    maxLabel.textContent = max.toLocaleString();
    svg.appendChild(maxLabel);

    card.appendChild(svg);
  }

  function svgLine(x1, y1, x2, y2, className) {
    const line = document.createElementNS(SVG_NS, "line");
    line.setAttribute("class", className);
    line.setAttribute("x1", x1);
    line.setAttribute("y1", y1);
    line.setAttribute("x2", x2);
    line.setAttribute("y2", y2);
    return line;
  }

  function renderRows(data, card) {
    const rows = Array.isArray(data.rows) ? data.rows : [];
    if (!rows.length) {
      const empty = document.createElement("div");
      empty.className = "muted";
      empty.textContent = "No rows returned.";
      card.appendChild(empty);
      return;
    }

    const columns = Object.keys(rows[0]);
    const wrap = document.createElement("div");
    wrap.className = "table-wrap";
    const table = document.createElement("table");

    const thead = document.createElement("thead");
    const headerRow = document.createElement("tr");
    for (const column of columns) {
      const th = document.createElement("th");
      th.textContent = column;
      headerRow.appendChild(th);
    }
    thead.appendChild(headerRow);

    const tbody = document.createElement("tbody");
    for (const row of rows) {
      const tr = document.createElement("tr");
      for (const column of columns) {
        const td = document.createElement("td");
        td.textContent = stringifyCell(row[column]);
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }

    table.append(thead, tbody);
    wrap.appendChild(table);
    card.appendChild(wrap);
  }

  function renderRetrieval(data, card) {
    const items = Array.isArray(data.items) ? data.items : [];
    const intro = document.createElement("div");
    intro.className = "muted";
    intro.textContent = `Top ${items.length} semantic matches for "${data.query || ""}":`;
    card.appendChild(intro);

    for (const item of items) {
      const hit = document.createElement("div");
      hit.className = "hit";

      const top = document.createElement("div");
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = item.recall_number || "-";
      top.appendChild(badge);

      const meta = document.createElement("span");
      meta.className = "hit-meta";
      meta.textContent = ` sim ${item.similarity ?? "-"} | ${item.classification || "-"} | ${item.recalling_firm || "-"} | ${item.field || "-"}`;
      top.appendChild(meta);

      const content = document.createElement("div");
      content.className = "hit-content";
      content.textContent = item.content || "";

      hit.append(top, content);
      card.appendChild(hit);
    }
  }

  function renderBadges(values) {
    const row = document.createElement("div");
    row.className = "badge-row";
    for (const value of values) {
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = value;
      row.appendChild(badge);
    }
    return row;
  }

  function stringifyCell(value) {
    if (value === null || value === undefined) return "";
    if (typeof value === "object") return JSON.stringify(value);
    return String(value);
  }

  function startEdit(conversationId, messageId) {
    const conversation = state.conversations.find((conv) => conv.id === conversationId);
    const message = conversation?.messages.find((item) => item.id === messageId);
    if (!message || message.role !== "user" || activeRequest) return;
    editState = { conversationId, messageId, value: message.content };
    render();
  }

  function saveEdit(conversationId, messageId, value) {
    const question = value.trim();
    if (!question || activeRequest) return;
    const conversation = state.conversations.find((conv) => conv.id === conversationId);
    if (!conversation) return;
    const index = conversation.messages.findIndex((message) => message.id === messageId);
    if (index < 0) return;

    const message = conversation.messages[index];
    message.content = question;
    conversation.messages = conversation.messages.slice(0, index + 1);
    if (index === 0 && canUpdatePlaceholderTitle(conversation)) {
      conversation.title = titleFromQuestion(question);
      conversation.titleSource = TITLE_PLACEHOLDER;
      if (conversation.titlePromptMessageId === message.id) {
        conversation.titlePromptQuestion = question;
      }
    }
    editState = null;
    appendAssistantAndRequest(conversation, question);
  }

  function sendCurrentPrompt() {
    const question = els.input.value.trim();
    if (!question || activeRequest) return;
    const conversation = getActiveConversation();
    const userMessage = {
      id: makeId("msg"),
      role: "user",
      content: question,
      createdAt: new Date().toISOString(),
    };
    const shouldRequestTitle = prepareTitleRequest(conversation, userMessage, question);
    conversation.messages.push(userMessage);
    els.input.value = "";
    appendAssistantAndRequest(conversation, question);
    if (shouldRequestTitle) {
      requestTitle(conversation.id, userMessage.id, question);
    }
  }

  function appendAssistantAndRequest(conversation, question) {
    const assistantMessage = {
      id: makeId("msg"),
      role: "assistant",
      content: "",
      status: "loading",
      result: null,
      error: null,
      createdAt: new Date().toISOString(),
    };
    conversation.messages.push(assistantMessage);
    touch(conversation);
    saveState();
    render();
    requestAnswer(conversation.id, assistantMessage.id, question);
  }

  async function requestAnswer(conversationId, assistantId, question) {
    const controller = new AbortController();
    activeRequest = { controller, conversationId, assistantId };
    renderComposerState();

    try {
      const response = await fetch("/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question }),
        signal: controller.signal,
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.detail || `HTTP ${response.status}`);
      }
      updateAssistant(conversationId, assistantId, {
        status: "done",
        result: payload,
        error: null,
      });
    } catch (error) {
      updateAssistant(conversationId, assistantId, {
        status: error.name === "AbortError" ? "aborted" : "error",
        result: null,
        error: error.name === "AbortError" ? null : error.message,
      });
    } finally {
      if (activeRequest?.assistantId === assistantId) {
        activeRequest = null;
      }
      renderComposerState();
      render();
    }
  }

  async function requestTitle(conversationId, messageId, question) {
    try {
      const response = await fetch("/title", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.detail || `HTTP ${response.status}`);
      }
      const title = typeof payload.title === "string" ? payload.title.trim() : "";
      if (!title) {
        throw new Error("Empty title response");
      }
      applyGeneratedTitle(conversationId, messageId, question, title);
    } catch {
      keepPlaceholderTitle(conversationId, messageId, question);
    }
  }

  function prepareTitleRequest(conversation, userMessage, question) {
    if (conversation.messages.length || conversation.titleRequested || conversation.titleSource === TITLE_MANUAL) {
      return false;
    }
    conversation.title = titleFromQuestion(question);
    conversation.titleSource = TITLE_PLACEHOLDER;
    conversation.titleRequested = true;
    conversation.titlePromptMessageId = userMessage.id;
    conversation.titlePromptQuestion = question;
    return true;
  }

  function canUpdatePlaceholderTitle(conversation) {
    return conversation.titleSource === TITLE_NEW || conversation.titleSource === TITLE_PLACEHOLDER;
  }

  function isCurrentTitleRequest(conversation, messageId, question) {
    if (conversation.titleSource === TITLE_MANUAL) return false;
    if (!conversation.titleRequested || conversation.titlePromptMessageId !== messageId) return false;
    if (conversation.titlePromptQuestion !== question) return false;
    const firstUser = conversation.messages.find((message) => message.id === messageId);
    return Boolean(firstUser && firstUser.role === "user" && firstUser.content === question);
  }

  function applyGeneratedTitle(conversationId, messageId, question, title) {
    const conversation = state.conversations.find((conv) => conv.id === conversationId);
    if (!conversation || !isCurrentTitleRequest(conversation, messageId, question)) return;
    conversation.title = title.slice(0, MAX_TITLE);
    conversation.titleSource = TITLE_AUTO;
    saveState();
    render();
  }

  function keepPlaceholderTitle(conversationId, messageId, question) {
    const conversation = state.conversations.find((conv) => conv.id === conversationId);
    if (!conversation || !isCurrentTitleRequest(conversation, messageId, question)) return;
    conversation.titleSource = TITLE_PLACEHOLDER;
    saveState();
  }

  function updateAssistant(conversationId, assistantId, patch) {
    const conversation = state.conversations.find((conv) => conv.id === conversationId);
    const message = conversation?.messages.find((item) => item.id === assistantId);
    if (!conversation || !message) return;
    Object.assign(message, patch);
    touch(conversation);
    saveState();
  }

  function stopGeneration() {
    if (activeRequest) activeRequest.controller.abort();
  }

  function renderComposerState() {
    const busy = Boolean(activeRequest);
    els.send.disabled = busy || !els.input.value.trim();
    els.stop.disabled = !busy;
    els.stop.classList.toggle("is-active", busy);
  }

  function autoSizeComposer() {
    els.input.style.height = "auto";
    els.input.style.height = `${Math.min(180, Math.max(54, els.input.scrollHeight))}px`;
    renderComposerState();
  }

  function titleFromQuestion(question) {
    return question.length > MAX_TITLE ? `${question.slice(0, MAX_TITLE - 3)}...` : question;
  }

  function iconSvg(name) {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");

    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("fill", "currentColor");
    path.setAttribute("d", name === "trash"
      ? "M7 21a2 2 0 0 1-2-2V8h14v11a2 2 0 0 1-2 2H7Zm2-11v8h2v-8H9Zm4 0v8h2v-8h-2ZM4 6h16v2H4V6Zm5-3h6l1 2H8l1-2Z"
      : "M4 17.25V20h2.75L17.81 8.94l-2.75-2.75L4 17.25Zm15.71-10.04a1 1 0 0 0 0-1.41l-1.5-1.5a1 1 0 0 0-1.41 0l-1.08 1.08 2.75 2.75 1.24-.92Z");
    svg.appendChild(path);
    return svg;
  }

  function focusComposer() {
    requestAnimationFrame(() => els.input.focus());
  }
})();
