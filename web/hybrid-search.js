// Zero-build frontend for the FDAgent hybrid retrieval lab.
(() => {
  const ALIAS_DEBOUNCE_MS = 700;
  const PROGRESS_STEPS = [
    ["preparing query", "Validating field, k, filters, aliases, and weighted RRF settings."],
    ["embedding query", "Requesting a safe query embedding when the provider is configured."],
    ["running vector search", "Searching pgvector candidates from the embeddings table."],
    ["running FTS search", "Searching Postgres full-text candidates and aliases."],
    ["fusing results with weighted RRF", "Combining vector and keyword ranks with configured weights."],
    ["writing debug log", "Recording safe trace metadata in hybrid_search_log."],
    ["rendering results", "Rendering metadata, exports, and the scrollable results table."],
  ];
  const PROGRESS_DELAYS_MS = [0, 450, 1200, 2000, 3000, 3800, 4600];
  const TABLE_COLUMNS = [
    ["rank", "Rank"],
    ["recall_number", "Recall #"],
    ["retrieval_mode", "Mode"],
    ["rrf_score", "RRF"],
    ["vector_rrf_contribution", "Vector contrib"],
    ["fts_rrf_contribution", "FTS contrib"],
    ["vector_contribution_percent", "Vector %"],
    ["fts_contribution_percent", "FTS %"],
    ["vector_rank", "Vector rank"],
    ["vector_distance", "Vector dist"],
    ["vector_similarity", "Vector sim"],
    ["fts_rank", "FTS rank"],
    ["fts_score", "FTS score"],
    ["matched_fts_queries", "Matched FTS queries"],
    ["field", "Field"],
    ["classification", "Class"],
    ["status", "Status"],
    ["recalling_firm", "Recalling firm"],
    ["product_description", "Product"],
    ["reason_for_recall", "Reason"],
    ["report_date", "Report date"],
    ["evidence_link", "Evidence link"],
  ];
  const CSV_METADATA_COLUMNS = [
    "run_timestamp",
    "query",
    "field",
    "k",
    "vector_weight_percent",
    "retrieval_mode",
    "embedding_provider",
    "embedding_model",
    "fallback_reason",
    "vector_hit_count",
    "fts_hit_count",
    "fused_hit_count",
    "returned_count",
    "log_id",
  ];

  const els = {
    form: document.getElementById("hybridSearchForm"),
    query: document.getElementById("queryInput"),
    field: document.getElementById("fieldSelect"),
    k: document.getElementById("kInput"),
    aliases: document.getElementById("aliasesInput"),
    aliasStatus: document.getElementById("aliasStatus"),
    vectorWeight: document.getElementById("vectorWeightInput"),
    weightStatus: document.getElementById("weightStatus"),
    filters: document.getElementById("filtersInput"),
    search: document.getElementById("searchButton"),
    example: document.getElementById("exampleButton"),
    status: document.getElementById("statusText"),
    progress: document.getElementById("progressList"),
    metadataCard: document.getElementById("metadataCard"),
    metadata: document.getElementById("metadataGrid"),
    resultsCard: document.getElementById("resultsCard"),
    resultsSummary: document.getElementById("resultsSummary"),
    table: document.getElementById("resultsTable"),
    downloadCsv: document.getElementById("downloadCsv"),
    downloadJson: document.getElementById("downloadJson"),
    refreshHistory: document.getElementById("refreshHistory"),
    historyList: document.getElementById("historyList"),
  };

  let progressTimers = [];
  let currentResult = null;
  let aliasTimer = null;
  let aliasController = null;
  let aliasRequestPromise = null;
  let aliasState = { status: "idle", query: "", field: "both", aliases: [], manual: false };

  renderProgress(-1, "idle");
  renderWeight();
  loadHistory();

  els.form.addEventListener("submit", (event) => {
    event.preventDefault();
    runSearch();
  });
  els.query.addEventListener("input", handleQueryChange);
  els.field.addEventListener("change", handleQueryChange);
  els.aliases.addEventListener("input", () => {
    aliasState = {
      ...aliasState,
      status: "manual",
      aliases: splitAliases(els.aliases.value).slice(0, 8),
      manual: true,
    };
    renderAliasStatus();
  });
  els.vectorWeight.addEventListener("input", renderWeight);
  els.example.addEventListener("click", () => {
    els.query.value = "pills too strong";
    els.field.value = "both";
    els.k.value = "20";
    els.aliases.value = "";
    els.filters.value = JSON.stringify({ classification: "Class II" }, null, 2);
    aliasState = { status: "idle", query: "", field: els.field.value, aliases: [], manual: false };
    handleQueryChange();
    els.query.focus();
  });
  els.downloadCsv.addEventListener("click", () => {
    if (currentResult) downloadCsv(currentResult);
  });
  els.downloadJson.addEventListener("click", () => {
    if (currentResult) {
      downloadBlob(
        JSON.stringify(currentResult, null, 2),
        "application/json",
        fileName(currentResult, "json"),
      );
    }
  });
  els.refreshHistory.addEventListener("click", loadHistory);

  function handleQueryChange() {
    cancelAliasTimer();
    if (aliasController) aliasController.abort();
    const query = els.query.value.trim();
    aliasState = {
      ...aliasState,
      status: query ? "stale" : "idle",
      query,
      field: els.field.value,
      manual: false,
    };
    renderAliasStatus();
    if (!query) return;
    aliasTimer = globalThis.setTimeout(() => requestAliases(query, els.field.value), ALIAS_DEBOUNCE_MS);
  }

  function cancelAliasTimer() {
    if (aliasTimer) globalThis.clearTimeout(aliasTimer);
    aliasTimer = null;
  }

  function requestAliases(query, field) {
    if (aliasController) aliasController.abort();
    aliasController = new AbortController();
    aliasState = { ...aliasState, status: "generating", query, field, manual: false };
    renderAliasStatus();
    aliasRequestPromise = fetch("/hybrid-search/aliases", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, field }),
      signal: aliasController.signal,
    })
      .then(async (response) => {
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
        if (els.query.value.trim() !== query || els.field.value !== field || aliasState.manual) {
          aliasState = { ...aliasState, status: "stale" };
          renderAliasStatus();
          return payload;
        }
        const aliases = Array.isArray(payload.aliases) ? payload.aliases : [];
        els.aliases.value = aliases.join(", ");
        aliasState = {
          status: payload.status || "generated",
          query,
          field,
          aliases,
          manual: false,
          provider: payload.provider,
          model: payload.model,
          message: payload.message,
        };
        renderAliasStatus();
        return payload;
      })
      .catch((error) => {
        if (error.name === "AbortError") return null;
        aliasState = {
          status: "error",
          query,
          field,
          aliases: [],
          manual: false,
          message: error.message || String(error),
        };
        els.aliases.value = "";
        renderAliasStatus();
        return null;
      })
      .finally(() => {
        aliasController = null;
        aliasRequestPromise = null;
      });
  }

  function renderAliasStatus() {
    const aliases = splitAliases(els.aliases.value);
    const status = aliasState.status;
    const prefix = {
      idle: "Aliases auto-generate after typing stops.",
      stale: "Aliases are stale for the current query.",
      generating: "Generating aliases...",
      generated: "Aliases generated for the current query.",
      manual: "Aliases manually edited.",
      degraded: "Alias provider degraded; search will use the query only.",
      unavailable: "Alias provider unavailable; search will use the query only.",
      error: "Alias generation failed; search will use the query only.",
    }[status] || `Alias status: ${status}`;
    els.aliasStatus.textContent = aliases.length
      ? `${prefix} ${aliases.length} aliases.`
      : prefix;
  }

  function renderWeight() {
    const vector = Number.parseInt(els.vectorWeight.value, 10);
    const bounded = Number.isInteger(vector) ? Math.min(100, Math.max(0, vector)) : 50;
    els.vectorWeight.value = String(bounded);
    els.weightStatus.textContent = `Vector ${bounded}% / FTS ${100 - bounded}%`;
  }

  async function runSearch() {
    if (aliasRequestPromise && !aliasState.manual) {
      els.status.textContent = "Waiting for current alias generation...";
      await aliasRequestPromise;
    }
    let request;
    try {
      request = buildRequest();
    } catch (error) {
      showError(error.message);
      return;
    }

    currentResult = null;
    els.search.disabled = true;
    els.downloadCsv.disabled = true;
    els.downloadJson.disabled = true;
    els.metadataCard.hidden = true;
    els.resultsCard.hidden = true;
    els.status.textContent = "Starting hybrid search...";
    startProgressTimers();

    try {
      const response = await fetch("/hybrid-search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(request),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.detail || `HTTP ${response.status}`);
      }
      currentResult = {
        ...payload,
        run_timestamp: new Date().toISOString(),
      };
      finishProgress("done");
      renderResult(currentResult);
      loadHistory();
    } catch (error) {
      finishProgress("failed");
      showError(error.message || String(error));
      loadHistory();
    } finally {
      els.search.disabled = false;
    }
  }

  function buildRequest() {
    const query = els.query.value.trim();
    if (!query) throw new Error("Enter a retrieval query.");
    const k = Number.parseInt(els.k.value, 10);
    if (!Number.isInteger(k) || k < 1 || k > 100) {
      throw new Error("k must be an integer between 1 and 100.");
    }
    const vectorWeight = Number.parseInt(els.vectorWeight.value, 10);
    return {
      query,
      field: els.field.value,
      k,
      aliases: splitAliases(els.aliases.value),
      alias_generation: {
        status: aliasState.status,
        source: aliasState.manual ? "manual" : "generated",
        provider: aliasState.provider,
        model: aliasState.model,
        aliases: splitAliases(els.aliases.value).slice(0, 8),
      },
      vector_weight_percent: Number.isInteger(vectorWeight) ? vectorWeight : 50,
      filters: parseFilters(els.filters.value),
    };
  }

  function splitAliases(value) {
    return value
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean)
      .slice(0, 20);
  }

  function parseFilters(value) {
    const text = value.trim();
    if (!text) return {};
    const parsed = JSON.parse(text);
    if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
      throw new Error("Filters JSON must be an object.");
    }
    return parsed;
  }

  function startProgressTimers() {
    clearProgressTimers();
    for (const [index, delay] of PROGRESS_DELAYS_MS.entries()) {
      progressTimers.push(globalThis.setTimeout(() => {
        renderProgress(index, "working");
      }, delay));
    }
  }

  function clearProgressTimers() {
    for (const timer of progressTimers) {
      globalThis.clearTimeout(timer);
    }
    progressTimers = [];
  }

  function finishProgress(state) {
    clearProgressTimers();
    renderProgress(PROGRESS_STEPS.length - 1, state);
    els.status.textContent = state === "done"
      ? "Search complete."
      : "Search failed. See the error below.";
  }

  function renderProgress(activeIndex, state) {
    els.progress.replaceChildren();
    PROGRESS_STEPS.forEach(([label, detail], index) => {
      const item = document.createElement("li");
      let className = "lab-progress-step";
      if (state === "idle") className += " pending";
      else if (state === "done" || index < activeIndex) className += " done";
      else if (state === "failed" && index === activeIndex) className += " failed";
      else if (index === activeIndex) className += " active";
      else className += " pending";
      item.className = className;
      const marker = document.createElement("span");
      marker.className = "lab-progress-marker";
      marker.textContent = index + 1;
      const body = document.createElement("span");
      body.className = "lab-progress-body";
      const title = document.createElement("strong");
      title.textContent = label;
      const note = document.createElement("small");
      note.textContent = detail;
      body.append(title, note);
      item.append(marker, body);
      els.progress.appendChild(item);
    });
  }

  function renderResult(result) {
    renderMetadata(result);
    renderTable(result.rows || result.fused_trace || []);
    const counts = result.counts || {};
    const returned = counts.returned_count ?? result.rows?.length ?? result.fused_trace?.length ?? 0;
    els.resultsSummary.textContent = `${returned} rows returned; `
      + `${counts.fused_hit_count ?? result.fused_hit_count ?? 0} fused candidates `
      + `(${counts.vector_hit_count ?? result.vector_hit_count ?? 0} vector, `
      + `${counts.fts_hit_count ?? result.fts_hit_count ?? 0} FTS).`;
    els.metadataCard.hidden = false;
    els.resultsCard.hidden = false;
    els.downloadCsv.disabled = false;
    els.downloadJson.disabled = false;
  }

  function renderMetadata(result) {
    const counts = result.counts || {};
    const fusion = result.fusion_config || {};
    const aliasGeneration = result.alias_generation || {};
    const items = [
      ["Query", result.query],
      ["Field", result.field],
      ["k", result.k],
      ["Retrieval mode", result.retrieval_mode],
      ["RRF formula", fusion.formula || "—"],
      ["RRF k", fusion.rrf_k ?? "—"],
      ["Weights", `Vector ${fusion.vector_weight_percent ?? result.vector_weight_percent ?? 50}% / FTS ${fusion.fts_weight_percent ?? 50}%`],
      ["Candidate limit", fusion.candidate_limit ?? "—"],
      ["Alias status", aliasGeneration.status || "—"],
      ["Embedding provider", result.embedding_provider],
      ["Embedding model", result.embedding_model],
      ["Embedding available", String(Boolean(result.embedding_available))],
      ["Fallback reason", result.fallback_reason || "—"],
      ["Vector hits", counts.vector_hit_count ?? result.vector_hit_count ?? 0],
      ["FTS hits", counts.fts_hit_count ?? result.fts_hit_count ?? 0],
      ["Fused hits", counts.fused_hit_count ?? result.fused_hit_count ?? 0],
      ["Returned", counts.returned_count ?? result.rows?.length ?? result.fused_trace?.length ?? 0],
      ["FTS queries", (result.fts_queries || result.response_metadata?.fts_queries || []).join(" | ") || "—"],
      ["Aliases", (result.aliases || aliasGeneration.aliases || []).join(" | ") || "—"],
      ["Top recalls", (result.top_recall_numbers || []).join(", ") || "—"],
      ["Timings ms", JSON.stringify(result.timings_ms || {})],
      ["Log id", result.log_id ?? result.id ?? "—"],
    ];
    els.metadata.replaceChildren();
    for (const [label, value] of items) {
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.textContent = value == null || value === "" ? "—" : String(value);
      els.metadata.append(dt, dd);
    }
  }

  function renderTable(rows) {
    els.table.replaceChildren();
    const thead = document.createElement("thead");
    const headRow = document.createElement("tr");
    for (const [, label] of TABLE_COLUMNS) {
      const th = document.createElement("th");
      th.scope = "col";
      th.textContent = label;
      headRow.appendChild(th);
    }
    thead.appendChild(headRow);

    const tbody = document.createElement("tbody");
    if (!rows.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = TABLE_COLUMNS.length;
      td.textContent = "No rows returned. Check retrieval_mode and fallback_reason before treating this as semantic evidence.";
      tr.appendChild(td);
      tbody.appendChild(tr);
    } else {
      for (const row of rows) {
        const tr = document.createElement("tr");
        for (const [key] of TABLE_COLUMNS) {
          const td = document.createElement("td");
          if (["product_description", "reason_for_recall", "matched_fts_queries"].includes(key)) {
            td.className = "lab-long-cell";
          }
          appendCellValue(td, key, row);
          tr.appendChild(td);
        }
        tbody.appendChild(tr);
      }
    }
    els.table.append(thead, tbody);
  }

  function appendCellValue(td, key, row) {
    const value = row[key];
    if (key === "recall_number" && row.url) {
      const link = document.createElement("a");
      link.href = row.url;
      link.textContent = value || "open";
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      td.appendChild(link);
      return;
    }
    if (key === "evidence_link" && value) {
      const link = document.createElement("a");
      link.href = value;
      link.textContent = "detail";
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      td.appendChild(link);
      return;
    }
    td.textContent = value == null || value === ""
      ? "—"
      : (Array.isArray(value) ? value.join(" | ") : String(value));
  }

  async function loadHistory() {
    try {
      const response = await fetch("/hybrid-search/logs?limit=25");
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
      renderHistory(payload.runs || []);
    } catch (error) {
      els.historyList.textContent = `Could not load history: ${error.message || error}`;
    }
  }

  function renderHistory(runs) {
    els.historyList.replaceChildren();
    if (!runs.length) {
      els.historyList.textContent = "No saved runs yet.";
      return;
    }
    for (const run of runs) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "lab-history-item";
      button.textContent = `#${run.id} ${run.query} · ${run.retrieval_mode} · ${run.field}`;
      button.addEventListener("click", () => loadHistoryDetail(run.id));
      els.historyList.appendChild(button);
    }
  }

  async function loadHistoryDetail(id) {
    try {
      const response = await fetch(`/hybrid-search/logs/${encodeURIComponent(id)}`);
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
      currentResult = {
        ...payload,
        log_id: payload.id,
        rows: payload.fused_trace || [],
        counts: {
          vector_hit_count: payload.vector_hit_count,
          fts_hit_count: payload.fts_hit_count,
          fused_hit_count: payload.fused_hit_count,
          returned_count: payload.fused_trace?.length || 0,
        },
        run_timestamp: payload.created_at,
      };
      finishProgress("done");
      renderResult(currentResult);
    } catch (error) {
      showError(error.message || String(error));
    }
  }

  function showError(message) {
    currentResult = null;
    els.metadataCard.hidden = false;
    els.resultsCard.hidden = true;
    els.metadata.replaceChildren();
    const dt = document.createElement("dt");
    dt.textContent = "Error";
    const dd = document.createElement("dd");
    dd.textContent = message;
    els.metadata.append(dt, dd);
    els.downloadCsv.disabled = true;
    els.downloadJson.disabled = true;
  }

  function downloadCsv(result) {
    const rows = result.rows || result.fused_trace || [];
    const metadata = csvMetadata(result);
    const columns = [...CSV_METADATA_COLUMNS, ...TABLE_COLUMNS.map(([key]) => key)];
    const dataRows = rows.length ? rows : [{}];
    const lines = [
      columns.map(csvEscape).join(","),
      ...dataRows.map((row) => columns.map((column) => {
        if (Object.prototype.hasOwnProperty.call(metadata, column)) {
          return csvEscape(metadata[column]);
        }
        return csvEscape(Array.isArray(row[column]) ? row[column].join(" | ") : row[column]);
      }).join(",")),
    ];
    downloadBlob(lines.join("\n"), "text/csv;charset=utf-8", fileName(result, "csv"));
  }

  function csvMetadata(result) {
    const counts = result.counts || {};
    return {
      run_timestamp: result.run_timestamp,
      query: result.query,
      field: result.field,
      k: result.k,
      vector_weight_percent: result.fusion_config?.vector_weight_percent ?? result.vector_weight_percent ?? "",
      retrieval_mode: result.retrieval_mode,
      embedding_provider: result.embedding_provider,
      embedding_model: result.embedding_model,
      fallback_reason: result.fallback_reason || "",
      vector_hit_count: counts.vector_hit_count ?? result.vector_hit_count ?? 0,
      fts_hit_count: counts.fts_hit_count ?? result.fts_hit_count ?? 0,
      fused_hit_count: counts.fused_hit_count ?? result.fused_hit_count ?? 0,
      returned_count: counts.returned_count ?? 0,
      log_id: result.log_id ?? result.id ?? "",
    };
  }

  function csvEscape(value) {
    if (value == null) return "";
    const text = String(value);
    if (/[",\n\r]/.test(text)) {
      return `"${text.replaceAll("\"", "\"\"")}"`;
    }
    return text;
  }

  function downloadBlob(text, type, name) {
    const blob = new Blob([text], { type });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = name;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }

  function fileName(result, extension) {
    const stem = (result.query || "hybrid-search")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 48) || "hybrid-search";
    return `${stem}-${new Date().toISOString().slice(0, 10)}.${extension}`;
  }
})();
