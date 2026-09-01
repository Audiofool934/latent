"use strict";

const elements = {
  appShell: document.querySelector("#appShell"),
  archivePath: document.querySelector("#archivePath"),
  cachedAssetCount: document.querySelector("#cachedAssetCount"),
  contactGrid: document.querySelector("#contactGrid"),
  contactStage: document.querySelector(".contact-stage"),
  copyPathButton: document.querySelector("#copyPathButton"),
  dateBreadcrumb: document.querySelector("#dateBreadcrumb"),
  dateList: document.querySelector("#dateList"),
  dateTitle: document.querySelector("#dateTitle"),
  embeddingReadyCount: document.querySelector("#embeddingReadyCount"),
  emptyState: document.querySelector("#emptyState"),
  findSimilarButton: document.querySelector("#findSimilarButton"),
  frameCount: document.querySelector("#frameCount"),
  inspectorClose: document.querySelector("#inspectorClose"),
  inspectorContent: document.querySelector("#inspectorContent"),
  inspectorEmpty: document.querySelector("#inspectorEmpty"),
  inspectorImage: document.querySelector("#inspectorImage"),
  inspectorTitle: document.querySelector("#inspectorTitle"),
  librarySearch: document.querySelector("#librarySearch"),
  loadMoreButton: document.querySelector("#loadMoreButton"),
  loadingState: document.querySelector("#loadingState"),
  metaCamera: document.querySelector("#metaCamera"),
  metaLens: document.querySelector("#metaLens"),
  metaSize: document.querySelector("#metaSize"),
  metaTaken: document.querySelector("#metaTaken"),
  mobileDateSelect: document.querySelector("#mobileDateSelect"),
  paginationProgress: document.querySelector("#paginationProgress"),
  paginationStatus: document.querySelector("#paginationStatus"),
  previewDimensions: document.querySelector("#previewDimensions"),
  queuedJobCount: document.querySelector("#queuedJobCount"),
  readyJobCount: document.querySelector("#readyJobCount"),
  relationEvidence: document.querySelector("#relationEvidence"),
  resultModeLabel: document.querySelector("#resultModeLabel"),
};

const state = {
  assets: [],
  controller: null,
  currentDate: null,
  dates: [],
  hasMore: false,
  embeddingCount: 0,
  loading: false,
  mode: "date",
  query: "",
  relationBasis: null,
  requestSerial: 0,
  selectedIndex: -1,
  total: 0,
};

const PAGE_SIZE = 250;
const SEARCH_DELAY_MS = 180;
let searchTimer = null;
let paginationObserver = null;

async function requestJSON(url, signal = undefined) {
  const response = await fetch(url, {
    headers: { Accept: "application/json" },
    signal,
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.message || `Local index request failed with ${response.status}`);
  }
  return payload;
}

async function boot() {
  try {
    const library = await requestJSON("/api/library");
    state.dates = library.dates;
    elements.cachedAssetCount.textContent = String(library.cached_assets);
    elements.readyJobCount.textContent = String(library.preview_jobs.succeeded);
    elements.queuedJobCount.textContent = String(library.preview_jobs.pending);
    state.embeddingCount = library.embedding_index?.indexed_assets || 0;
    elements.embeddingReadyCount.textContent = String(state.embeddingCount);
    renderDates();
    setupPaginationObserver();
    const parameters = new URLSearchParams(window.location.search);
    const requested = parameters.get("date");
    const requestedQuery = (parameters.get("q") || "").trim();
    const initialDate = state.dates.some((item) => item.capture_date === requested)
      ? requested
      : state.dates[0]?.capture_date;
    if (initialDate) {
      state.currentDate = initialDate;
      elements.librarySearch.value = requestedQuery;
      updateDateChrome();
      await resetResults(requestedQuery);
    } else {
      showEmpty("NO CACHED FRAMES", "Run the preview worker to populate the local index.");
    }
  } catch (error) {
    showEmpty("LOCAL INDEX UNAVAILABLE", error.message);
  } finally {
    elements.loadingState.hidden = true;
  }
}

function renderDates() {
  elements.dateList.replaceChildren();
  elements.mobileDateSelect.replaceChildren();
  for (const item of state.dates) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "date-button";
    button.dataset.date = item.capture_date;
    button.addEventListener("click", () => loadDate(item.capture_date));

    const label = document.createElement("span");
    label.textContent = item.capture_date;
    const count = document.createElement("span");
    count.className = "date-count";
    count.textContent = String(item.asset_count).padStart(2, "0");
    button.append(label, count);
    elements.dateList.append(button);

    const option = document.createElement("option");
    option.value = item.capture_date;
    option.textContent = `${item.capture_date} (${item.asset_count})`;
    elements.mobileDateSelect.append(option);
  }
}

async function loadDate(captureDate) {
  if (!captureDate || (captureDate === state.currentDate && state.mode === "date")) {
    return;
  }
  state.currentDate = captureDate;
  elements.librarySearch.value = "";
  updateDateChrome();
  const url = new URL(window.location.href);
  url.searchParams.set("date", captureDate);
  url.searchParams.delete("q");
  window.history.replaceState({}, "", url);
  await resetResults("");
}

function updateDateChrome() {
  const date = parseArchiveDate(state.currentDate);
  elements.dateBreadcrumb.textContent = state.currentDate || "EMPTY";
  elements.dateTitle.textContent = date
    ? new Intl.DateTimeFormat("en", { day: "2-digit", month: "long", year: "numeric" }).format(date)
    : "Contact Sheet";
  elements.resultModeLabel.textContent = "Embedded RAW previews";
  for (const button of elements.dateList.querySelectorAll(".date-button")) {
    if (button.dataset.date === state.currentDate) {
      button.setAttribute("aria-current", "date");
    } else {
      button.removeAttribute("aria-current");
    }
  }
  elements.mobileDateSelect.value = state.currentDate || "";
}

function appendCards(startIndex) {
  const fragment = document.createDocumentFragment();
  state.assets.slice(startIndex).forEach((asset, relativeIndex) => {
    const index = startIndex + relativeIndex;
    const card = document.createElement("button");
    card.type = "button";
    card.className = "photo-card";
    if (state.mode !== "date") {
      card.classList.add("ranked-card");
    }
    card.dataset.index = String(index);
    card.setAttribute("aria-label", `Inspect ${asset.name}`);
    card.setAttribute("aria-selected", "false");
    card.addEventListener("click", () => selectAsset(index, true));

    const imageWell = document.createElement("span");
    imageWell.className = "image-well";
    const image = document.createElement("img");
    image.src = asset.contact_url;
    image.alt = asset.name;
    image.loading = index < 12 ? "eager" : "lazy";
    image.decoding = "async";
    image.addEventListener("load", () => image.classList.add("loaded"));
    const frameIndex = document.createElement("span");
    frameIndex.className = "frame-index";
    frameIndex.textContent = String(asset.rank || index + 1).padStart(2, "0");
    imageWell.append(image, frameIndex);

    const caption = document.createElement("span");
    caption.className = "card-caption";
    const name = document.createElement("span");
    name.className = "card-name";
    name.textContent = asset.name.replace(/\.ARW$/i, "");
    const time = document.createElement("span");
    time.className = "card-time";
    time.textContent =
      state.mode === "date"
        ? captureTime(asset.capture_at)
        : `${captureDate(asset.capture_at)} · ${formatSimilarity(asset.similarity)}`;
    caption.append(name, time);
    card.append(imageWell, caption);
    fragment.append(card);
  });
  elements.contactGrid.append(fragment);
}

function setupPaginationObserver() {
  if (!("IntersectionObserver" in window)) {
    return;
  }
  paginationObserver = new IntersectionObserver(
    (entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        void loadNextPage();
      }
    },
    { root: elements.contactStage, rootMargin: "480px 0px" },
  );
  paginationObserver.observe(elements.paginationStatus);
}

async function resetResults(query) {
  const mode = query ? "semantic" : "date";
  prepareResults(mode, query);
  const url = new URL(window.location.href);
  if (query) {
    url.searchParams.set("q", query);
    updateSemanticChrome(query);
  } else {
    url.searchParams.delete("q");
    updateDateChrome();
  }
  window.history.replaceState({}, "", url);
  try {
    if (mode === "semantic") {
      await loadSemanticResults();
    } else {
      await loadNextPage();
    }
  } catch (error) {
    if (error.name !== "AbortError") {
      showEmpty(
        mode === "semantic" ? "SEMANTIC SEARCH UNAVAILABLE" : "LOCAL INDEX UNAVAILABLE",
        error.message,
      );
    }
  }
}

function prepareResults(mode, query) {
  state.controller?.abort();
  state.controller = new AbortController();
  state.requestSerial += 1;
  state.assets = [];
  state.hasMore = mode === "date";
  state.loading = false;
  state.mode = mode;
  state.query = query;
  state.relationBasis = null;
  state.selectedIndex = -1;
  state.total = 0;
  elements.contactGrid.replaceChildren();
  elements.contactStage.scrollTop = 0;
  elements.emptyState.hidden = true;
  elements.loadingState.hidden = false;
  elements.paginationStatus.hidden = true;
  closeInspector(false);
  clearInspector();
  updateResultCounts();
}

function updateSemanticChrome(query) {
  elements.dateBreadcrumb.textContent = "SEMANTIC / ALL DATES";
  elements.dateTitle.textContent = `“${query}”`;
  elements.resultModeLabel.textContent = "SigLIP2 cosine search";
}

async function loadSemanticResults() {
  const serial = state.requestSerial;
  state.loading = true;
  try {
    const parameters = new URLSearchParams({ q: state.query, limit: "100" });
    const payload = await requestJSON(
      `/api/search?${parameters.toString()}`,
      state.controller?.signal,
    );
    if (serial !== state.requestSerial) {
      return;
    }
    state.assets = payload.results;
    state.total = payload.total;
    state.hasMore = false;
    state.relationBasis = payload.basis;
    appendCards(0);
    if (state.assets.length > 0) {
      selectAsset(0, false);
    }
  } finally {
    finishResultLoad(serial, "NO SEMANTIC MATCHES", "Try a different visual idea or scene.");
  }
}

async function loadNextPage() {
  if (state.mode !== "date" || state.loading || !state.hasMore || !state.currentDate) {
    return;
  }
  const serial = state.requestSerial;
  const offset = state.assets.length;
  const parameters = new URLSearchParams({
    date: state.currentDate,
    limit: String(PAGE_SIZE),
    offset: String(offset),
  });
  state.loading = true;
  updatePaginationStatus();
  try {
    const payload = await requestJSON(
      `/api/assets?${parameters.toString()}`,
      state.controller?.signal,
    );
    if (serial !== state.requestSerial) {
      return;
    }
    state.assets.push(...payload.assets);
    state.total = payload.total;
    state.hasMore = payload.has_more;
    appendCards(offset);
    elements.emptyState.hidden = state.assets.length > 0;
    if (offset === 0 && state.assets.length > 0) {
      selectAsset(0, false);
    }
  } finally {
    finishResultLoad(
      serial,
      "NO CACHED FRAMES",
      "Run the preview worker to populate the local index.",
    );
  }
}

function finishResultLoad(serial, emptyCode, emptyMessage) {
  if (serial !== state.requestSerial) {
    return;
  }
  state.loading = false;
  elements.loadingState.hidden = true;
  updateResultCounts();
  updatePaginationStatus();
  if (state.assets.length === 0) {
    showEmpty(emptyCode, emptyMessage);
  }
}

function updateResultCounts() {
  const singular = state.mode === "date" ? "frame" : "match";
  const plural = state.mode === "date" ? "frames" : "matches";
  if (state.assets.length < state.total) {
    elements.frameCount.textContent = `${state.assets.length} of ${state.total} ${plural}`;
  } else {
    elements.frameCount.textContent = `${state.total} ${state.total === 1 ? singular : plural}`;
  }
}

function updatePaginationStatus() {
  elements.paginationStatus.hidden = !state.hasMore;
  elements.loadMoreButton.disabled = state.loading;
  elements.loadMoreButton.textContent = state.loading ? "Loading" : "Load more";
  elements.paginationProgress.textContent = `${state.assets.length} of ${state.total || "…"} loaded`;
}

function selectAsset(index, openOverlay) {
  if (index < 0 || index >= state.assets.length) {
    return;
  }
  state.selectedIndex = index;
  const cards = [...elements.contactGrid.querySelectorAll(".photo-card")];
  cards.forEach((card, cardIndex) => {
    card.setAttribute("aria-selected", cardIndex === index ? "true" : "false");
  });
  const asset = state.assets[index];
  elements.inspectorEmpty.hidden = true;
  elements.inspectorContent.hidden = false;
  elements.inspectorTitle.textContent = asset.name;
  elements.inspectorImage.src = asset.preview_url;
  elements.inspectorImage.alt = `Cached preview of ${asset.name}`;
  elements.previewDimensions.textContent = asset.preview_available
    ? `${asset.preview_width} × ${asset.preview_height} cached preview`
    : "Contact preview / medium preview evicted";
  elements.metaTaken.textContent = formatCaptureDate(asset.capture_at);
  elements.metaCamera.textContent = asset.camera_model || "Unknown";
  elements.metaLens.textContent = asset.lens_model || "Unknown";
  elements.metaSize.textContent = formatBytes(asset.size_bytes);
  elements.archivePath.textContent = asset.remote_path;
  elements.copyPathButton.textContent = "Copy archive path";
  elements.findSimilarButton.disabled = state.embeddingCount === 0;
  if (typeof asset.similarity === "number") {
    const interpretation =
      state.relationBasis?.interpretation ||
      "Similarity is model evidence, not proof of place, identity, or story.";
    elements.relationEvidence.textContent =
      `Cosine ${asset.similarity.toFixed(3)}. ${interpretation}`;
    elements.relationEvidence.hidden = false;
  } else {
    elements.relationEvidence.hidden = true;
    elements.relationEvidence.textContent = "";
  }
  if (openOverlay && window.matchMedia("(max-width: 1040px)").matches) {
    elements.appShell.classList.add("inspector-open");
  }
}

function clearInspector() {
  state.selectedIndex = -1;
  elements.inspectorContent.hidden = true;
  elements.inspectorEmpty.hidden = false;
  elements.inspectorTitle.textContent = "No selection";
  elements.inspectorImage.removeAttribute("src");
  elements.findSimilarButton.disabled = state.embeddingCount === 0;
  elements.relationEvidence.hidden = true;
  elements.relationEvidence.textContent = "";
}

function closeInspector(restoreFocus = true) {
  elements.appShell.classList.remove("inspector-open");
  if (restoreFocus && state.selectedIndex >= 0) {
    const selected = elements.contactGrid.querySelector(`[data-index="${state.selectedIndex}"]`);
    selected?.focus();
  }
}

function scheduleSearch() {
  window.clearTimeout(searchTimer);
  searchTimer = window.setTimeout(() => {
    const query = elements.librarySearch.value.trim();
    if (query !== state.query) {
      void resetResults(query);
    }
  }, SEARCH_DELAY_MS);
}

async function findSimilar() {
  const source = state.assets[state.selectedIndex];
  if (!source || state.embeddingCount === 0) {
    return;
  }
  prepareResults("similar", "");
  elements.librarySearch.value = "";
  elements.dateBreadcrumb.textContent = `SIMILAR / ${source.name.replace(/\.ARW$/i, "")}`;
  elements.dateTitle.textContent = "Visual Neighbors";
  elements.resultModeLabel.textContent = "SigLIP2 image cosine";
  const url = new URL(window.location.href);
  url.searchParams.delete("q");
  window.history.replaceState({}, "", url);
  const serial = state.requestSerial;
  state.loading = true;
  let failure = null;
  try {
    const payload = await requestJSON(
      `/api/assets/${source.id}/similar?limit=100`,
      state.controller?.signal,
    );
    if (serial !== state.requestSerial) {
      return;
    }
    state.assets = payload.results;
    state.total = payload.total;
    state.relationBasis = payload.basis;
    appendCards(0);
    if (state.assets.length > 0) {
      selectAsset(0, false);
    }
  } catch (error) {
    failure = error;
  } finally {
    finishResultLoad(
      serial,
      "NO VISUAL NEIGHBORS",
      "This frame does not have another indexed visual neighbor yet.",
    );
  }
  if (failure && failure.name !== "AbortError") {
    showEmpty("SIMILARITY UNAVAILABLE", failure.message);
  }
}

function moveSelection(delta) {
  if (state.assets.length === 0) {
    return;
  }
  const next = Math.min(
    state.assets.length - 1,
    Math.max(0, state.selectedIndex + delta),
  );
  selectAsset(next, false);
  const card = elements.contactGrid.querySelector(`[data-index="${next}"]`);
  card?.focus({ preventScroll: true });
  card?.scrollIntoView({ block: "nearest", inline: "nearest" });
  if (next === state.assets.length - 1 && state.hasMore) {
    void loadNextPage();
  }
}

function gridColumns() {
  const cards = elements.contactGrid.querySelectorAll(".photo-card");
  if (cards.length < 2) {
    return 1;
  }
  const firstTop = cards[0].offsetTop;
  let columns = 1;
  while (columns < cards.length && cards[columns].offsetTop === firstTop) {
    columns += 1;
  }
  return columns;
}

function showEmpty(code, message) {
  elements.loadingState.hidden = true;
  elements.paginationStatus.hidden = true;
  elements.emptyState.hidden = false;
  elements.emptyState.querySelector(".empty-code").textContent = code;
  elements.emptyState.querySelector("p").textContent = message;
}

function captureTime(value) {
  const match = /\s(\d{2}:\d{2})/.exec(value || "");
  return match ? match[1] : "--:--";
}

function captureDate(value) {
  const match = /^(\d{4}):(\d{2}):(\d{2})/.exec(value || "");
  return match ? `${match[1]}-${match[2]}-${match[3]}` : "UNKNOWN DATE";
}

function formatSimilarity(value) {
  return typeof value === "number" ? value.toFixed(3) : "--";
}

function formatCaptureDate(value) {
  if (!value) {
    return "Unknown";
  }
  const normalized = value.replace(
    /^(\d{4}):(\d{2}):(\d{2})/,
    "$1-$2-$3",
  );
  const parsed = new Date(normalized.replace(" ", "T"));
  if (Number.isNaN(parsed.valueOf())) {
    return value;
  }
  return new Intl.DateTimeFormat("en", {
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    month: "short",
    second: "2-digit",
    year: "numeric",
  }).format(parsed);
}

function parseArchiveDate(value) {
  if (!value) {
    return null;
  }
  const parsed = new Date(`${value}T12:00:00`);
  return Number.isNaN(parsed.valueOf()) ? null : parsed;
}

function formatBytes(value) {
  const units = ["B", "KiB", "MiB", "GiB"];
  let amount = Number(value) || 0;
  let unitIndex = 0;
  while (amount >= 1024 && unitIndex < units.length - 1) {
    amount /= 1024;
    unitIndex += 1;
  }
  return `${amount.toFixed(unitIndex === 0 ? 0 : 2)} ${units[unitIndex]}`;
}

async function copyArchivePath() {
  const asset = state.assets[state.selectedIndex];
  if (!asset) {
    return;
  }
  try {
    await navigator.clipboard.writeText(asset.remote_path);
    elements.copyPathButton.textContent = "Copied";
  } catch {
    elements.copyPathButton.textContent = "Copy unavailable";
  }
}

elements.librarySearch.addEventListener("input", scheduleSearch);
elements.loadMoreButton.addEventListener("click", () => void loadNextPage());
elements.mobileDateSelect.addEventListener("change", (event) => loadDate(event.target.value));
elements.inspectorClose.addEventListener("click", () => closeInspector());
elements.copyPathButton.addEventListener("click", copyArchivePath);
elements.findSimilarButton.addEventListener("click", () => void findSimilar());

document.addEventListener("keydown", (event) => {
  const typing = event.target instanceof HTMLInputElement || event.target instanceof HTMLSelectElement;
  if ((event.key === "/" || (event.metaKey && event.key.toLowerCase() === "k")) && !typing) {
    event.preventDefault();
    elements.librarySearch.focus();
    return;
  }
  if (event.key === "Escape") {
    if (typing) {
      event.target.blur();
    }
    closeInspector();
    return;
  }
  if (typing) {
    return;
  }
  const columns = gridColumns();
  const movements = {
    ArrowLeft: -1,
    ArrowRight: 1,
    ArrowUp: -columns,
    ArrowDown: columns,
  };
  if (event.key in movements) {
    event.preventDefault();
    moveSelection(movements[event.key]);
  }
});

boot();
