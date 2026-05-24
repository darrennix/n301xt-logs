const state = {
  manifest: null,
  entityId: null,
  sourceId: null,
  page: 1,
  tab: "en",
  textPayload: null,
};

const SPLIT_STORAGE_KEY = "n301xt.viewer.pdfPaneWidth";
const DEFAULT_SPLIT = 70;
const MIN_SPLIT = 25;
const MAX_SPLIT = 75;
const PAGE_DATA = window.__N301XT_PAGE_DATA__ || {};
window.__N301XT_PAGE_DATA__ = PAGE_DATA;
const pendingPageScripts = new Map();

const els = {
  pageCount: document.getElementById("page-count"),
  entityNav: document.getElementById("entity-nav"),
  sourceSelect: document.getElementById("source-select"),
  sourceTitle: document.getElementById("source-title"),
  prevPage: document.getElementById("prev-page"),
  nextPage: document.getElementById("next-page"),
  pageInput: document.getElementById("page-input"),
  pageTotal: document.getElementById("page-total"),
  pdfFrame: document.getElementById("pdf-frame"),
  panes: document.querySelector(".panes"),
  paneResizer: document.getElementById("pane-resizer"),
  textMeta: document.getElementById("text-meta"),
  textContent: document.getElementById("text-content"),
  tabs: Array.from(document.querySelectorAll(".tab")),
};

function clamp(value, min, max) {
  return Math.min(Math.max(value, min), max);
}

function setPaneSplit(percent, persist = true) {
  const next = clamp(percent, MIN_SPLIT, MAX_SPLIT);
  document.documentElement.style.setProperty("--pdf-pane-width", `${next}%`);
  els.paneResizer.setAttribute("aria-valuenow", String(Math.round(next)));
  if (persist) {
    try {
      localStorage.setItem(SPLIT_STORAGE_KEY, String(next));
    } catch {
      // Some browsers restrict localStorage from file:// URLs.
    }
  }
}

function loadPaneSplit() {
  let storedValue = "";
  try {
    storedValue = localStorage.getItem(SPLIT_STORAGE_KEY) || "";
  } catch {
    storedValue = "";
  }
  const stored = Number.parseFloat(storedValue);
  if (Number.isFinite(stored)) {
    setPaneSplit(stored, false);
  } else {
    setPaneSplit(DEFAULT_SPLIT, false);
  }
}

function splitFromPointer(clientX) {
  const rect = els.panes.getBoundingClientRect();
  const relativeX = clamp(clientX - rect.left, 0, rect.width);
  return (relativeX / rect.width) * 100;
}

function initPaneResizer() {
  let resizing = false;

  els.paneResizer.addEventListener("pointerdown", (event) => {
    if (window.matchMedia("(max-width: 980px)").matches) return;
    resizing = true;
    els.paneResizer.setPointerCapture(event.pointerId);
    document.body.classList.add("pane-resizing");
    setPaneSplit(splitFromPointer(event.clientX));
  });

  els.paneResizer.addEventListener("pointermove", (event) => {
    if (!resizing) return;
    setPaneSplit(splitFromPointer(event.clientX));
  });

  function stopResize(event) {
    if (!resizing) return;
    resizing = false;
    document.body.classList.remove("pane-resizing");
    if (els.paneResizer.hasPointerCapture(event.pointerId)) {
      els.paneResizer.releasePointerCapture(event.pointerId);
    }
  }

  els.paneResizer.addEventListener("pointerup", stopResize);
  els.paneResizer.addEventListener("pointercancel", stopResize);

  els.paneResizer.addEventListener("keydown", (event) => {
    const current = Number.parseFloat(
      getComputedStyle(document.documentElement).getPropertyValue("--pdf-pane-width")
    );
    const step = event.shiftKey ? 10 : 2;
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      setPaneSplit(current - step);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      setPaneSplit(current + step);
    } else if (event.key === "Home") {
      event.preventDefault();
      setPaneSplit(MIN_SPLIT);
    } else if (event.key === "End") {
      event.preventDefault();
      setPaneSplit(MAX_SPLIT);
    }
  });
}

function padPage(page) {
  return String(page).padStart(4, "0");
}

function findEntity(id = state.entityId) {
  return state.manifest.entities.find((entity) => entity.id === id);
}

function findSource(sourceId = state.sourceId, entity = findEntity()) {
  if (!entity) return null;
  return entity.sources.find((source) => source.id === sourceId);
}

function parseHash() {
  const raw = window.location.hash.replace(/^#/, "");
  const params = new URLSearchParams(raw);
  return {
    entityId: params.get("entity") || null,
    sourceId: params.get("source") || null,
    page: Number.parseInt(params.get("page") || "1", 10),
    tab: params.get("tab") || "en",
  };
}

function writeHash() {
  const params = new URLSearchParams();
  params.set("entity", state.entityId);
  params.set("source", state.sourceId);
  params.set("page", String(state.page));
  params.set("tab", state.tab);
  const nextHash = `#${params.toString()}`;
  if (window.location.hash !== nextHash) {
    history.replaceState(null, "", nextHash);
  }
}

function normalizeStateFromHash() {
  const next = parseHash();
  const firstEntity = state.manifest.entities[0];
  let entity = state.manifest.entities.find((item) => item.id === next.entityId);
  if (!entity) entity = firstEntity;

  let source = entity.sources.find((item) => item.id === next.sourceId);
  if (!source) source = entity.sources[0];

  state.entityId = entity.id;
  state.sourceId = source.id;
  state.page = Number.isFinite(next.page) ? next.page : 1;
  state.page = Math.min(Math.max(state.page, 1), source.pageCount);
  state.tab = ["en", "original", "events"].includes(next.tab) ? next.tab : "en";
}

function pageUrl(source, page) {
  return source.pagePdfTemplate.replace("{pagePadded}", padPage(page));
}

function textUrl(source, page) {
  return source.textTemplate.replace("{pagePadded}", padPage(page));
}

function textScriptUrl(source, page) {
  const template = source.textScriptTemplate || source.textTemplate.replace(/\.json$/, ".js");
  return template.replace("{pagePadded}", padPage(page));
}

function canFetchLocalData() {
  return window.location.protocol !== "file:";
}

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const existing = Array.from(document.scripts).find((script) => script.dataset.src === src);
    if (existing?.dataset.loaded === "true") {
      resolve();
      return;
    }

    const script = existing || document.createElement("script");
    script.dataset.src = src;
    script.onload = () => {
      script.dataset.loaded = "true";
      resolve();
    };
    script.onerror = () => reject(new Error(`Could not load ${src}`));

    if (!existing) {
      script.src = src;
      document.head.appendChild(script);
    }
  });
}

async function loadManifest() {
  if (canFetchLocalData()) {
    const response = await fetch("data/manifest.json");
    if (!response.ok) throw new Error(`Could not load manifest: HTTP ${response.status}`);
    return response.json();
  }

  await loadScript("data/manifest.js");
  if (!window.__N301XT_MANIFEST__) {
    throw new Error("Could not load manifest script");
  }
  return window.__N301XT_MANIFEST__;
}

function pageDataKey(source, page) {
  return `${source.id}/${padPage(page)}`;
}

async function loadTextPayload(source, page) {
  if (canFetchLocalData()) {
    const response = await fetch(textUrl(source, page));
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }

  const key = pageDataKey(source, page);
  if (PAGE_DATA[key]) return PAGE_DATA[key];

  const scriptUrl = textScriptUrl(source, page);
  if (!pendingPageScripts.has(key)) {
    pendingPageScripts.set(
      key,
      new Promise((resolve, reject) => {
        const previousReady = window.__N301XT_PAGE_READY__;
        window.__N301XT_PAGE_READY__ = (readyKey) => {
          if (typeof previousReady === "function") previousReady(readyKey);
          if (readyKey === key) resolve();
        };
        loadScript(scriptUrl).then(() => {
          if (PAGE_DATA[key]) resolve();
        }).catch(reject);
      })
    );
  }

  await pendingPageScripts.get(key);
  if (!PAGE_DATA[key]) throw new Error(`No text payload registered for ${key}`);
  return PAGE_DATA[key];
}

function renderNav() {
  const entity = findEntity();
  els.entityNav.innerHTML = "";
  for (const item of state.manifest.entities) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `entity-button${item.id === state.entityId ? " active" : ""}`;
    button.textContent = item.label;
    button.addEventListener("click", () => {
      state.entityId = item.id;
      state.sourceId = item.sources[0].id;
      state.page = 1;
      state.textPayload = null;
      render();
    });
    els.entityNav.appendChild(button);
  }

  els.sourceSelect.innerHTML = "";
  for (const source of entity.sources) {
    const option = document.createElement("option");
    option.value = source.id;
    option.textContent = `${source.label} (${source.pageCount.toLocaleString()} pages)`;
    option.selected = source.id === state.sourceId;
    els.sourceSelect.appendChild(option);
  }
}

function renderChrome() {
  const entity = findEntity();
  const source = findSource();
  els.pageCount.textContent = `${state.manifest.pageCount.toLocaleString()} pages`;
  els.sourceTitle.textContent = `${entity.label} / ${source.label} / page ${state.page.toLocaleString()} of ${source.pageCount.toLocaleString()}`;
  els.pageInput.min = "1";
  els.pageInput.max = String(source.pageCount);
  els.pageInput.value = String(state.page);
  els.pageTotal.textContent = `/ ${source.pageCount.toLocaleString()}`;

  const prev = getAdjacentPage(-1);
  const next = getAdjacentPage(1);
  els.prevPage.disabled = !prev;
  els.nextPage.disabled = !next;

  for (const tab of els.tabs) {
    tab.classList.toggle("active", tab.dataset.tab === state.tab);
  }
}

function getAdjacentPage(direction) {
  const entity = findEntity();
  const sourceIndex = entity.sources.findIndex((source) => source.id === state.sourceId);
  if (sourceIndex < 0) return null;
  const source = entity.sources[sourceIndex];
  const nextPage = state.page + direction;

  if (nextPage >= 1 && nextPage <= source.pageCount) {
    return { sourceId: source.id, page: nextPage };
  }

  const nextSource = entity.sources[sourceIndex + direction];
  if (!nextSource) return null;
  return {
    sourceId: nextSource.id,
    page: direction > 0 ? 1 : nextSource.pageCount,
  };
}

function goAdjacent(direction) {
  const target = getAdjacentPage(direction);
  if (!target) return;
  state.sourceId = target.sourceId;
  state.page = target.page;
  state.textPayload = null;
  render();
}

function shouldIgnorePageKey(event) {
  const target = event.target;
  if (!target || !(target instanceof HTMLElement)) return false;
  if (target === els.paneResizer) return true;
  return Boolean(target.closest("input, select, textarea"));
}

function handlePageKeys(event) {
  if (shouldIgnorePageKey(event)) return;
  if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;

  if (event.key === "ArrowLeft") {
    event.preventDefault();
    goAdjacent(-1);
  } else if (event.key === "ArrowRight") {
    event.preventDefault();
    goAdjacent(1);
  }
}

function renderPdf() {
  const source = findSource();
  els.pdfFrame.src = pageUrl(source, state.page);
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function renderTextPayload() {
  const payload = state.textPayload;
  if (!payload) return;

  const langs = (payload.sourceLang || []).join(", ") || "unknown";
  els.textMeta.textContent = `${payload.sourceLabel} - page ${payload.page} - source language: ${langs}`;

  if (state.tab === "en") {
    els.textContent.className = "text-content";
    els.textContent.textContent = payload.textEnglish || "No translated text extracted for this page.";
    return;
  }

  if (state.tab === "original") {
    els.textContent.className = "text-content";
    els.textContent.textContent = payload.textOriginal || "No original OCR text extracted for this page.";
    return;
  }

  els.textContent.className = "text-content event-list";
  const events = payload.events || [];
  if (!events.length) {
    els.textContent.innerHTML = '<div class="empty-state">No structured events extracted for this page.</div>';
    return;
  }

  els.textContent.innerHTML = events
    .map((event) => {
      const date = event.date ? escapeHtml(event.date) : "Undated";
      const category = escapeHtml(event.category || "event");
      const damageClass = event.isDamage ? " damage" : "";
      const severity = event.damageSeverity ? ` - ${escapeHtml(event.damageSeverity)}` : "";
      const detailParts = [
        event.details,
        event.shop ? `Shop: ${event.shop}` : null,
        event.location ? `Location: ${event.location}` : null,
        event.aircraftTotalTimeH != null ? `TAT: ${event.aircraftTotalTimeH}` : null,
        event.aircraftCycles != null ? `TAC: ${event.aircraftCycles}` : null,
        event.engineTotalTimeH != null ? `ETT: ${event.engineTotalTimeH}` : null,
        event.engineCycles != null ? `ECYC: ${event.engineCycles}` : null,
      ].filter(Boolean);
      return `
        <section class="event-item">
          <div class="event-heading">
            <span>${date}</span>
            <span class="event-pill${damageClass}">${category}${severity}</span>
          </div>
          <div>${escapeHtml(event.summary || "")}</div>
          <div class="event-detail">${escapeHtml(detailParts.join("\n"))}</div>
        </section>
      `;
    })
    .join("");
}

async function loadText() {
  const source = findSource();
  const requestedSourceId = state.sourceId;
  const requestedPage = state.page;
  els.textMeta.textContent = "Loading text";
  els.textContent.className = "text-content";
  els.textContent.textContent = "";

  try {
    const payload = await loadTextPayload(source, requestedPage);
    if (requestedSourceId !== state.sourceId || requestedPage !== state.page) {
      return;
    }
    state.textPayload = payload;
    renderTextPayload();
  } catch (error) {
    els.textMeta.textContent = "Text unavailable";
    els.textContent.className = "text-content error-state";
    els.textContent.textContent = `Could not load extracted text for this page: ${error.message}`;
  }
}

function render() {
  renderNav();
  renderChrome();
  writeHash();
  renderPdf();
  loadText();
}

async function boot() {
  loadPaneSplit();
  initPaneResizer();
  state.manifest = await loadManifest();
  normalizeStateFromHash();
  render();
}

els.prevPage.addEventListener("click", () => goAdjacent(-1));
els.nextPage.addEventListener("click", () => goAdjacent(1));

els.sourceSelect.addEventListener("change", () => {
  state.sourceId = els.sourceSelect.value;
  state.page = 1;
  state.textPayload = null;
  render();
});

els.pageInput.addEventListener("change", () => {
  const source = findSource();
  const page = Number.parseInt(els.pageInput.value, 10);
  state.page = Math.min(Math.max(Number.isFinite(page) ? page : 1, 1), source.pageCount);
  state.textPayload = null;
  render();
});

for (const tab of els.tabs) {
  tab.addEventListener("click", () => {
    state.tab = tab.dataset.tab;
    writeHash();
    renderChrome();
    renderTextPayload();
  });
}

window.addEventListener("hashchange", () => {
  normalizeStateFromHash();
  state.textPayload = null;
  render();
});

document.addEventListener("keydown", handlePageKeys);

boot().catch((error) => {
  els.sourceTitle.textContent = `Viewer failed to load: ${error.message}`;
});
