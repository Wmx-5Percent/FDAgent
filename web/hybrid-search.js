// Zero-build frontend for the FDAgent hybrid retrieval lab.
(() => {
  const PROGRESS_STEPS = [
    ["preparing query", "Validating field, k, filters, and optional aliases."],
    ["embedding query", "Requesting a safe query embedding when the provider is configured."],
    ["running vector search", "Searching pgvector candidates from the embeddings table."],
    ["running FTS search", "Searching Postgres full-text candidates and aliases."],
    ["fusing results with RRF", "Combining vector and keyword ranks with reciprocal rank fusion."],
    ["writing debug log", "Recording safe run metadata in hybrid_search_log."],
    ["rendering results", "Rendering metadata, exports, and the scrollable results table."],
  ];
  const PROGRESS_DELAYS_MS = [0, 450, 1200, 2000, 3000, 3800, 4600];
  const TABLE_COLUMNS = [
    ["rank", "Rank"],
    ["recall_number", "Recall #"],
    ["retrieval_mode", "Mode"],
    ["rrf_score", "RRF"],
    ["vector_rank", "Vector rank"],
    ["vector_distance", "Vector dist"],
    ["vector_similarity", "Vector sim"],
    ["fts_rank", "FTS rank"],
    ["fts_score", "FTS score"],
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
  };

  let progressTimers = [];
  let currentResult = null;

  renderProgress(-1, "idle");
  els.form.addEventListener("submit", (event) => {
    event.preventDefault();
    runSearch();
  });
  els.example.addEventListener("click", () => {
    els.query.value = "pills too strong";
    els.field.value = "both";
    els.k.value = "20";
    els.aliases.value = "too strong, over strength, high assay, superpotent";
    els.filters.value = JSON.stringify({ classification: "Class II" }, null, 2);
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

  async function runSearch() {
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
    } catch (error) {
      finishProgress("failed");
      showError(error.message || String(error));
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
    return {
      query,
      field: els.field.value,
      k,
      aliases: splitAliases(els.aliases.value),
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
    renderTable(result.rows || []);
    const counts = result.counts || {};
    els.resultsSummary.textContent = `${counts.returned_count ?? result.rows?.length ?? 0} rows returned; `
      + `${counts.fused_hit_count ?? 0} fused candidates `
      + `(${counts.vector_hit_count ?? 0} vector, ${counts.fts_hit_count ?? 0} FTS).`;
    els.metadataCard.hidden = false;
    els.resultsCard.hidden = false;
    els.downloadCsv.disabled = false;
    els.downloadJson.disabled = false;
  }

  function renderMetadata(result) {
    const counts = result.counts || {};
    const items = [
      ["Query", result.query],
      ["Field", result.field],
      ["k", result.k],
      ["Retrieval mode", result.retrieval_mode],
      ["Embedding provider", result.embedding_provider],
      ["Embedding model", result.embedding_model],
      ["Embedding available", String(Boolean(result.embedding_available))],
      ["Fallback reason", result.fallback_reason || "—"],
      ["Vector hits", counts.vector_hit_count ?? 0],
      ["FTS hits", counts.fts_hit_count ?? 0],
      ["Fused hits", counts.fused_hit_count ?? 0],
      ["Returned", counts.returned_count ?? 0],
      ["FTS queries", (result.fts_queries || []).join(" | ") || "—"],
      ["Aliases", (result.aliases || []).join(" | ") || "—"],
      ["Top recalls", (result.top_recall_numbers || []).join(", ") || "—"],
      ["Timings ms", JSON.stringify(result.timings_ms || {})],
      ["Log id", result.log_id ?? "—"],
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
          if (["product_description", "reason_for_recall"].includes(key)) {
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
    td.textContent = value == null || value === "" ? "—" : String(value);
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
    const rows = result.rows || [];
    const metadata = csvMetadata(result);
    const columns = [...CSV_METADATA_COLUMNS, ...TABLE_COLUMNS.map(([key]) => key)];
    const dataRows = rows.length ? rows : [{}];
    const lines = [
      columns.map(csvEscape).join(","),
      ...dataRows.map((row) => columns.map((column) => {
        if (Object.prototype.hasOwnProperty.call(metadata, column)) {
          return csvEscape(metadata[column]);
        }
        return csvEscape(row[column]);
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
      retrieval_mode: result.retrieval_mode,
      embedding_provider: result.embedding_provider,
      embedding_model: result.embedding_model,
      fallback_reason: result.fallback_reason || "",
      vector_hit_count: counts.vector_hit_count ?? 0,
      fts_hit_count: counts.fts_hit_count ?? 0,
      fused_hit_count: counts.fused_hit_count ?? 0,
      returned_count: counts.returned_count ?? 0,
      log_id: result.log_id ?? "",
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
