"use strict";

const elements = {
  activeSequenceSelect: document.querySelector("#activeSequenceSelect"),
  addCuratorSeedButton: document.querySelector("#addCuratorSeedButton"),
  addToSequenceButton: document.querySelector("#addToSequenceButton"),
  appShell: document.querySelector("#appShell"),
  archivePath: document.querySelector("#archivePath"),
  cachedAssetCount: document.querySelector("#cachedAssetCount"),
  contactGrid: document.querySelector("#contactGrid"),
  contactStage: document.querySelector(".contact-stage"),
  copyPathButton: document.querySelector("#copyPathButton"),
  curatorButton: document.querySelector("#curatorButton"),
  curatorHeadline: document.querySelector("#curatorHeadline"),
  curatorLimitations: document.querySelector("#curatorLimitations"),
  curatorObservations: document.querySelector("#curatorObservations"),
  curatorReport: document.querySelector("#curatorReport"),
  curatorSequence: document.querySelector("#curatorSequence"),
  dateBreadcrumb: document.querySelector("#dateBreadcrumb"),
  dateList: document.querySelector("#dateList"),
  dateTitle: document.querySelector("#dateTitle"),
  discoverMotifsButton: document.querySelector("#discoverMotifsButton"),
  embeddingReadyCount: document.querySelector("#embeddingReadyCount"),
  editSequenceButton: document.querySelector("#editSequenceButton"),
  emptyState: document.querySelector("#emptyState"),
  findSimilarButton: document.querySelector("#findSimilarButton"),
  frameCount: document.querySelector("#frameCount"),
  inspector: document.querySelector("#inspector"),
  inspectorClose: document.querySelector("#inspectorClose"),
  inspectorContent: document.querySelector("#inspectorContent"),
  inspectorEmpty: document.querySelector("#inspectorEmpty"),
  inspectorImage: document.querySelector("#inspectorImage"),
  inspectorTitle: document.querySelector("#inspectorTitle"),
  inspectorNewSequenceButton: document.querySelector("#inspectorNewSequenceButton"),
  librarySearch: document.querySelector("#librarySearch"),
  loadingLabel: document.querySelector("#loadingLabel"),
  loadMoreButton: document.querySelector("#loadMoreButton"),
  loadingState: document.querySelector("#loadingState"),
  metaCamera: document.querySelector("#metaCamera"),
  metaLens: document.querySelector("#metaLens"),
  metaSize: document.querySelector("#metaSize"),
  metaTaken: document.querySelector("#metaTaken"),
  mobileDateSelect: document.querySelector("#mobileDateSelect"),
  moveSequenceEarlierButton: document.querySelector("#moveSequenceEarlierButton"),
  moveSequenceLaterButton: document.querySelector("#moveSequenceLaterButton"),
  motifEmpty: document.querySelector("#motifEmpty"),
  motifList: document.querySelector("#motifList"),
  mobileMotifLabel: document.querySelector("#mobileMotifLabel"),
  mobileMotifSelect: document.querySelector("#mobileMotifSelect"),
  newSequenceButton: document.querySelector("#newSequenceButton"),
  openSequenceButton: document.querySelector("#openSequenceButton"),
  paginationProgress: document.querySelector("#paginationProgress"),
  paginationStatus: document.querySelector("#paginationStatus"),
  previewDimensions: document.querySelector("#previewDimensions"),
  queuedJobCount: document.querySelector("#queuedJobCount"),
  readyJobCount: document.querySelector("#readyJobCount"),
  relationEvidence: document.querySelector("#relationEvidence"),
  resultModeLabel: document.querySelector("#resultModeLabel"),
  sequenceCancelButton: document.querySelector("#sequenceCancelButton"),
  sequenceDialog: document.querySelector("#sequenceDialog"),
  sequenceDialogTitle: document.querySelector("#sequenceDialogTitle"),
  sequenceEmpty: document.querySelector("#sequenceEmpty"),
  sequenceForm: document.querySelector("#sequenceForm"),
  sequenceFormError: document.querySelector("#sequenceFormError"),
  sequenceList: document.querySelector("#sequenceList"),
  sequenceNameInput: document.querySelector("#sequenceNameInput"),
  sequenceNoteInput: document.querySelector("#sequenceNoteInput"),
  sequenceOrderControls: document.querySelector("#sequenceOrderControls"),
  sequenceStatus: document.querySelector("#sequenceStatus"),
  sequenceSubmitButton: document.querySelector("#sequenceSubmitButton"),
  toolbarMotifsButton: document.querySelector("#toolbarMotifsButton"),
};

const state = {
  activeMotifId: null,
  assets: [],
  activeSequenceId: null,
  controller: null,
  currentSequence: null,
  curatorSeedAssetIds: [],
  curatorRequestSerial: 0,
  currentDate: null,
  dates: [],
  hasMore: false,
  embeddingCount: 0,
  editingSequenceId: null,
  loading: false,
  mode: "date",
  motifBasis: null,
  motifs: [],
  openSequenceAfterSave: false,
  query: "",
  relationBasis: null,
  requestSerial: 0,
  sequences: [],
  selectedIndex: -1,
  total: 0,
  workspaceWritable: false,
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

async function sendJSON(url, method, payload = undefined) {
  const response = await fetch(url, {
    method,
    headers: {
      Accept: "application/json",
      ...(payload === undefined ? {} : { "Content-Type": "application/json" }),
    },
    body: payload === undefined ? undefined : JSON.stringify(payload),
  });
  const result = await response.json();
  if (!response.ok) {
    throw new Error(result.message || `Workspace request failed with ${response.status}`);
  }
  return result;
}

async function boot() {
  try {
    const library = await requestJSON("/api/library");
    state.dates = library.dates;
    elements.cachedAssetCount.textContent = String(library.cached_assets);
    elements.readyJobCount.textContent = String(library.preview_jobs.succeeded);
    elements.queuedJobCount.textContent = String(library.preview_jobs.pending);
    state.embeddingCount = library.embedding_index?.indexed_assets || 0;
    state.workspaceWritable = library.workspace?.writable === true;
    elements.embeddingReadyCount.textContent = String(state.embeddingCount);
    elements.discoverMotifsButton.disabled = state.embeddingCount === 0;
    elements.toolbarMotifsButton.disabled = state.embeddingCount === 0;
    elements.motifEmpty.textContent =
      state.embeddingCount === 0 ? "Embedding index required" : "No motifs loaded";
    renderDates();
    renderMotifs();
    try {
      await refreshSequences();
    } catch {
      state.sequences = [];
      state.activeSequenceId = null;
      renderSequences();
      updateSequenceControls();
    }
    setupPaginationObserver();
    const parameters = new URLSearchParams(window.location.search);
    const requested = parameters.get("date");
    const requestedMotif = parameters.get("motif");
    const requestedSequence = parameters.get("sequence");
    const requestedQuery = (parameters.get("q") || "").trim();
    const initialDate = state.dates.some((item) => item.capture_date === requested)
      ? requested
      : state.dates[0]?.capture_date;
    if (initialDate) {
      state.currentDate = initialDate;
      elements.librarySearch.value = requestedQuery;
      updateDateChrome();
      if (requestedMotif && state.embeddingCount > 0) {
        await loadMotifs(requestedMotif);
      } else if (
        requestedSequence &&
        state.sequences.some((item) => item.id === requestedSequence)
      ) {
        await openSequence(requestedSequence);
      } else {
        await resetResults(requestedQuery);
      }
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

async function refreshSequences(preferredId = state.activeSequenceId) {
  const payload = await requestJSON("/api/sequences");
  state.sequences = payload.sequences;
  state.activeSequenceId = state.sequences.some((item) => item.id === preferredId)
    ? preferredId
    : state.sequences[0]?.id || null;
  renderSequences();
  updateSequenceControls();
}

function renderSequences() {
  elements.sequenceList.replaceChildren();
  elements.activeSequenceSelect.replaceChildren();
  elements.sequenceEmpty.hidden = state.sequences.length > 0;

  if (state.sequences.length === 0) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "No Sequence yet";
    elements.activeSequenceSelect.append(option);
  }

  for (const sequence of state.sequences) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "sequence-button";
    button.dataset.sequenceId = sequence.id;
    button.setAttribute(
      "aria-current",
      state.mode === "sequence" && state.currentSequence?.id === sequence.id ? "true" : "false",
    );
    button.addEventListener("click", () => void openSequence(sequence.id));

    const name = document.createElement("span");
    name.className = "sequence-button-name";
    name.textContent = sequence.name;
    const count = document.createElement("span");
    count.className = "sequence-button-count";
    count.textContent = String(sequence.item_count);
    button.append(name, count);
    elements.sequenceList.append(button);

    const option = document.createElement("option");
    option.value = sequence.id;
    option.textContent = `${sequence.name} (${sequence.item_count})`;
    elements.activeSequenceSelect.append(option);
  }
  elements.activeSequenceSelect.value = state.activeSequenceId || "";
}

function renderMotifs() {
  elements.motifList.replaceChildren();
  elements.mobileMotifSelect.replaceChildren();
  elements.motifEmpty.hidden = state.motifs.length > 0;
  elements.mobileMotifLabel.hidden = state.motifs.length === 0;
  elements.mobileMotifSelect.hidden = state.motifs.length === 0;
  for (const motif of state.motifs) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "motif-button";
    button.dataset.motifId = motif.id;
    button.setAttribute(
      "aria-current",
      state.mode === "motif" && state.activeMotifId === motif.id ? "true" : "false",
    );
    button.addEventListener("click", () => openMotif(motif.id));

    const name = document.createElement("span");
    name.className = "motif-button-name";
    name.textContent = `Motif ${String(motif.rank).padStart(2, "0")}`;
    const count = document.createElement("span");
    count.className = "motif-button-count";
    count.textContent = String(motif.report.member_count);
    const metadata = document.createElement("span");
    metadata.className = "motif-button-meta";
    metadata.textContent = motif.report.cross_year
      ? `${motif.report.years.length} recorded years`
      : motif.report.years[0] || "No recorded year";
    button.append(name, count, metadata);
    elements.motifList.append(button);

    const option = document.createElement("option");
    option.value = motif.id;
    option.textContent =
      `Motif ${String(motif.rank).padStart(2, "0")} / ` +
      `${motif.report.member_count} frames / ${metadata.textContent}`;
    elements.mobileMotifSelect.append(option);
  }
  elements.mobileMotifSelect.value = state.activeMotifId || state.motifs[0]?.id || "";
}

function setActiveSequence(sequenceId) {
  state.activeSequenceId = state.sequences.some((item) => item.id === sequenceId)
    ? sequenceId
    : null;
  renderSequences();
  updateSequenceControls();
}

function updateSequenceControls() {
  const active = state.sequences.find((item) => item.id === state.activeSequenceId);
  const asset = state.assets[state.selectedIndex];
  const canWrite = state.workspaceWritable;
  elements.activeSequenceSelect.disabled = !canWrite || state.sequences.length === 0;
  elements.newSequenceButton.disabled = !canWrite;
  elements.inspectorNewSequenceButton.disabled = !canWrite;
  elements.editSequenceButton.disabled = !canWrite || !active;
  elements.openSequenceButton.disabled = !active;
  elements.addToSequenceButton.disabled = !canWrite || !active || !asset?.id;
  elements.addCuratorSeedButton.disabled =
    !canWrite || !active || state.curatorSeedAssetIds.length === 0;
  const ordering = state.mode === "sequence" && state.currentSequence !== null;
  elements.sequenceOrderControls.hidden = !ordering;
  elements.moveSequenceEarlierButton.disabled = !ordering || state.selectedIndex <= 0;
  elements.moveSequenceLaterButton.disabled =
    !ordering || state.selectedIndex < 0 || state.selectedIndex >= state.assets.length - 1;
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
  url.searchParams.delete("motif");
  url.searchParams.delete("q");
  url.searchParams.delete("sequence");
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
    if (state.mode === "semantic" || state.mode === "similar" || state.mode === "motif") {
      card.classList.add("ranked-card");
    } else if (state.mode === "sequence") {
      card.classList.add("sequence-card");
    }
    card.dataset.index = String(index);
    card.setAttribute("aria-label", `Inspect ${asset.name}`);
    card.setAttribute("aria-selected", "false");
    card.addEventListener("click", () => selectAsset(index, true));

    const imageWell = document.createElement("span");
    imageWell.className = "image-well";
    if (asset.contact_url) {
      const image = document.createElement("img");
      image.src = asset.contact_url;
      image.alt = asset.name;
      image.loading = index < 12 ? "eager" : "lazy";
      image.decoding = "async";
      image.addEventListener("load", () => image.classList.add("loaded"));
      imageWell.append(image);
    } else {
      const missing = document.createElement("span");
      missing.className = "missing-frame";
      missing.textContent = "LOCAL PREVIEW MISSING";
      imageWell.append(missing);
    }
    const frameIndex = document.createElement("span");
    frameIndex.className = "frame-index";
    frameIndex.textContent = String(asset.rank || index + 1).padStart(2, "0");
    imageWell.append(frameIndex);

    const caption = document.createElement("span");
    caption.className = "card-caption";
    const name = document.createElement("span");
    name.className = "card-name";
    name.textContent = asset.name.replace(/\.ARW$/i, "");
    const time = document.createElement("span");
    time.className = "card-time";
    if (state.mode === "date") {
      time.textContent = captureTime(asset.capture_at);
    } else if (state.mode === "sequence") {
      time.textContent =
        `${String(index + 1).padStart(2, "0")} · ${asset.library_status.toUpperCase()}`;
    } else {
      time.textContent = `${captureDate(asset.capture_at)} · ${formatSimilarity(asset.similarity)}`;
    }
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
    url.searchParams.delete("motif");
    url.searchParams.delete("sequence");
    updateSemanticChrome(query);
  } else {
    url.searchParams.delete("motif");
    url.searchParams.delete("q");
    url.searchParams.delete("sequence");
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
  state.currentSequence = null;
  state.hasMore = mode === "date";
  state.loading = false;
  state.mode = mode;
  if (mode !== "motif") {
    state.activeMotifId = null;
  }
  state.query = query;
  state.relationBasis = null;
  state.selectedIndex = -1;
  state.total = 0;
  elements.contactGrid.replaceChildren();
  elements.contactStage.scrollTop = 0;
  elements.emptyState.hidden = true;
  elements.loadingState.hidden = false;
  const loadingLabels = {
    date: "Reading local index",
    semantic: "Loading local model / searching embeddings",
    sequence: "Opening writable workspace",
    similar: "Finding visual neighbors",
    motif: "Clustering local embeddings",
  };
  elements.loadingLabel.textContent = loadingLabels[mode] || "Reading local index";
  elements.paginationStatus.hidden = true;
  elements.sequenceStatus.textContent = "";
  closeInspector(false);
  clearInspector();
  updateResultCounts();
  renderSequences();
  renderMotifs();
}

function updateSemanticChrome(query) {
  elements.dateBreadcrumb.textContent = "SEMANTIC / ALL DATES";
  elements.dateTitle.textContent = `“${query}”`;
  elements.resultModeLabel.textContent = "SigLIP2 cosine search";
}

async function openSequence(sequenceId) {
  if (!sequenceId) {
    return;
  }
  state.activeSequenceId = sequenceId;
  prepareResults("sequence", "");
  elements.librarySearch.value = "";
  elements.dateBreadcrumb.textContent = "SEQUENCE / WRITABLE WORKSPACE";
  elements.dateTitle.textContent = "Opening Sequence";
  elements.resultModeLabel.textContent = "Local user state";
  const url = new URL(window.location.href);
  url.searchParams.set("sequence", sequenceId);
  url.searchParams.delete("motif");
  url.searchParams.delete("q");
  window.history.replaceState({}, "", url);
  const serial = state.requestSerial;
  state.loading = true;
  let failure = null;
  try {
    const payload = await requestJSON(
      `/api/sequences/${encodeURIComponent(sequenceId)}`,
      state.controller?.signal,
    );
    if (serial !== state.requestSerial) {
      return;
    }
    displaySequence(payload.sequence);
  } catch (error) {
    failure = error;
  } finally {
    finishResultLoad(
      serial,
      "EMPTY SEQUENCE",
      "Add a frame or a grounded Curator seed to begin this Sequence.",
    );
  }
  if (failure && failure.name !== "AbortError") {
    showEmpty("SEQUENCE UNAVAILABLE", failure.message);
  }
  updateSequenceControls();
}

function updateSequenceChrome(sequence) {
  elements.dateBreadcrumb.textContent = `SEQUENCE / ${sequence.origin.toUpperCase()}`;
  elements.dateTitle.textContent = sequence.name;
  elements.resultModeLabel.textContent = sequence.note ? "Sequence note saved" : "Local user state";
}

function sequenceItemAsset(item) {
  return {
    ...(item.asset || {}),
    id: item.asset?.id || null,
    name: item.asset?.name || item.name,
    remote_path: item.remote_path,
    size_bytes: item.asset?.size_bytes || 0,
    capture_at: item.asset?.capture_at || item.capture_at,
    camera_model: item.asset?.camera_model || item.camera_model,
    lens_model: item.asset?.lens_model || item.lens_model,
    preview_width: item.asset?.preview_width || null,
    preview_height: item.asset?.preview_height || null,
    contact_url: item.asset?.contact_url || null,
    preview_url: item.asset?.preview_url || null,
    preview_available: item.asset?.preview_available === true,
    library_status: item.library_status,
    sequence_item_id: item.id,
  };
}

function displaySequence(sequence, selectedItemId = null) {
  state.currentSequence = sequence;
  state.activeSequenceId = sequence.id;
  state.assets = sequence.items.map(sequenceItemAsset);
  state.total = sequence.item_count;
  state.hasMore = false;
  state.selectedIndex = -1;
  elements.contactGrid.replaceChildren();
  updateSequenceChrome(sequence);
  appendCards(0);
  if (state.assets.length > 0) {
    const selectedIndex = selectedItemId
      ? state.assets.findIndex((asset) => asset.sequence_item_id === selectedItemId)
      : 0;
    selectAsset(Math.max(selectedIndex, 0), false);
  } else {
    clearInspector();
  }
  renderSequences();
  updateResultCounts();
  updateSequenceControls();
}

async function loadMotifs(preferredId = null) {
  if (state.embeddingCount === 0) {
    return;
  }
  state.controller?.abort();
  state.controller = new AbortController();
  state.requestSerial += 1;
  const serial = state.requestSerial;
  elements.discoverMotifsButton.disabled = true;
  elements.discoverMotifsButton.textContent = "Reading";
  elements.toolbarMotifsButton.disabled = true;
  elements.toolbarMotifsButton.textContent = "Reading";
  try {
    const payload = await requestJSON(
      "/api/curator/motifs?clusters=12&limit=8",
      state.controller.signal,
    );
    if (serial !== state.requestSerial) {
      return;
    }
    state.motifs = payload.motifs;
    state.motifBasis = payload.basis;
    elements.motifEmpty.textContent = "No motif clusters available";
    renderMotifs();
    const selected = state.motifs.some((motif) => motif.id === preferredId)
      ? preferredId
      : state.motifs[0]?.id;
    if (selected) {
      openMotif(selected);
    } else {
      showEmpty("NO CURATOR MOTIFS", "The current local vector index produced no clusters.");
    }
  } catch (error) {
    if (error.name === "AbortError") {
      return;
    }
    elements.motifEmpty.textContent = error.message;
    showEmpty("CURATOR MOTIFS UNAVAILABLE", error.message);
  } finally {
    elements.discoverMotifsButton.disabled = state.embeddingCount === 0;
    elements.discoverMotifsButton.textContent = "Discover";
    elements.toolbarMotifsButton.disabled = state.embeddingCount === 0;
    elements.toolbarMotifsButton.textContent = "Motifs";
  }
}

function openMotif(motifId) {
  const motif = state.motifs.find((candidate) => candidate.id === motifId);
  if (!motif) {
    return;
  }
  prepareResults("motif", "");
  state.activeMotifId = motif.id;
  state.relationBasis = state.motifBasis;
  state.assets = motif.assets;
  state.total = motif.assets.length;
  state.hasMore = false;
  elements.librarySearch.value = "";
  elements.dateBreadcrumb.textContent = "CURATOR / ALL INDEXED YEARS";
  elements.dateTitle.textContent = `Motif ${String(motif.rank).padStart(2, "0")}`;
  const yearLabel = motif.report.cross_year
    ? `${motif.report.years.length} recorded years`
    : motif.report.years[0] || "no recorded year";
  elements.resultModeLabel.textContent =
    `${motif.report.member_count} clustered frames / ${yearLabel}`;
  const url = new URL(window.location.href);
  url.searchParams.set("motif", motif.id);
  url.searchParams.delete("q");
  url.searchParams.delete("sequence");
  window.history.replaceState({}, "", url);
  elements.loadingState.hidden = true;
  appendCards(0);
  if (state.assets.length > 0) {
    selectAsset(0, false);
  }
  updateResultCounts();
  renderMotifs();
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
  const frameMode =
    state.mode === "date" || state.mode === "sequence" || state.mode === "motif";
  const singular = frameMode ? "frame" : "match";
  const plural = frameMode ? "frames" : "matches";
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
  state.curatorRequestSerial += 1;
  const cards = [...elements.contactGrid.querySelectorAll(".photo-card")];
  cards.forEach((card, cardIndex) => {
    card.setAttribute("aria-selected", cardIndex === index ? "true" : "false");
  });
  const asset = state.assets[index];
  elements.inspector.scrollTop = 0;
  elements.inspectorEmpty.hidden = true;
  elements.inspectorContent.hidden = false;
  elements.inspectorTitle.textContent = asset.name;
  if (asset.preview_url) {
    elements.inspectorImage.src = asset.preview_url;
    elements.inspectorImage.alt = `Cached preview of ${asset.name}`;
    elements.previewDimensions.textContent = asset.preview_available
      ? `${asset.preview_width} × ${asset.preview_height} cached preview`
      : "Contact preview / medium preview evicted";
  } else {
    elements.inspectorImage.removeAttribute("src");
    elements.inspectorImage.alt = "";
    elements.previewDimensions.textContent = "Archive reference missing from local contact index";
  }
  elements.metaTaken.textContent = formatCaptureDate(asset.capture_at);
  elements.metaCamera.textContent = asset.camera_model || "Unknown";
  elements.metaLens.textContent = asset.lens_model || "Unknown";
  elements.metaSize.textContent = asset.size_bytes ? formatBytes(asset.size_bytes) : "Unknown";
  elements.archivePath.textContent = asset.remote_path;
  elements.copyPathButton.textContent = "Copy archive path";
  elements.findSimilarButton.disabled = state.embeddingCount === 0 || !asset.id;
  elements.curatorButton.disabled = state.embeddingCount === 0 || !asset.id;
  clearCuratorReport();
  if (typeof asset.similarity === "number") {
    const interpretation =
      state.relationBasis?.interpretation ||
      "Similarity is model evidence, not proof of place, identity, or story.";
    const relationLabel = state.mode === "motif" ? "Cosine to motif centroid" : "Cosine";
    elements.relationEvidence.textContent =
      `${relationLabel} ${asset.similarity.toFixed(3)}. ${interpretation}`;
    elements.relationEvidence.hidden = false;
  } else {
    elements.relationEvidence.hidden = true;
    elements.relationEvidence.textContent = "";
  }
  if (state.mode === "motif") {
    const motif = state.motifs.find((candidate) => candidate.id === state.activeMotifId);
    if (motif) {
      renderCuratorEvidence(motif.report);
    }
  }
  updateSequenceControls();
  if (openOverlay && window.matchMedia("(max-width: 1040px)").matches) {
    elements.appShell.classList.add("inspector-open");
  }
}

function clearInspector() {
  state.selectedIndex = -1;
  state.curatorRequestSerial += 1;
  elements.inspectorContent.hidden = true;
  elements.inspectorEmpty.hidden = false;
  elements.inspectorTitle.textContent = "No selection";
  elements.inspectorImage.removeAttribute("src");
  elements.findSimilarButton.disabled = state.embeddingCount === 0;
  elements.curatorButton.disabled = state.embeddingCount === 0;
  elements.relationEvidence.hidden = true;
  elements.relationEvidence.textContent = "";
  clearCuratorReport();
  updateSequenceControls();
}

function clearCuratorReport() {
  state.curatorSeedAssetIds = [];
  elements.curatorReport.hidden = true;
  elements.curatorHeadline.textContent = "";
  elements.curatorObservations.replaceChildren();
  elements.curatorSequence.textContent = "";
  elements.curatorLimitations.textContent = "";
  elements.curatorButton.textContent = "Build curator evidence";
  updateSequenceControls();
}

function renderCuratorEvidence(report) {
  elements.curatorReport.hidden = false;
  elements.curatorHeadline.textContent = report.headline;
  elements.curatorObservations.replaceChildren();
  for (const observation of report.observations) {
    const item = document.createElement("li");
    item.textContent = observation.statement;
    elements.curatorObservations.append(item);
  }
  const sequenceCount = report.sequence_seed.items.length;
  state.curatorSeedAssetIds = report.sequence_seed.items.map((item) => item.asset_id);
  elements.curatorSequence.textContent =
    `Sequence seed: ${sequenceCount} frames. ${report.sequence_seed.description}`;
  elements.curatorLimitations.textContent = report.limitations.join(" ");
  updateSequenceControls();
}

async function loadCurator() {
  const source = state.assets[state.selectedIndex];
  if (!source?.id || state.embeddingCount === 0) {
    return;
  }
  const serial = state.curatorRequestSerial + 1;
  state.curatorRequestSerial = serial;
  elements.curatorButton.disabled = true;
  elements.curatorButton.textContent = "Reading evidence";
  elements.curatorReport.hidden = false;
  elements.curatorHeadline.textContent = "Reading local model and EXIF evidence";
  elements.curatorObservations.replaceChildren();
  elements.curatorSequence.textContent = "";
  elements.curatorLimitations.textContent = "";
  try {
    const payload = await requestJSON(`/api/assets/${source.id}/curator?limit=12`);
    if (serial !== state.curatorRequestSerial) {
      return;
    }
    renderCuratorEvidence(payload.report);
  } catch (error) {
    if (serial !== state.curatorRequestSerial) {
      return;
    }
    elements.curatorHeadline.textContent = "Curator evidence unavailable";
    elements.curatorLimitations.textContent = error.message;
    state.curatorSeedAssetIds = [];
    updateSequenceControls();
  } finally {
    if (serial === state.curatorRequestSerial) {
      elements.curatorButton.disabled = state.embeddingCount === 0;
      elements.curatorButton.textContent = "Build curator evidence";
    }
  }
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
  if (!source?.id || state.embeddingCount === 0) {
    return;
  }
  prepareResults("similar", "");
  elements.librarySearch.value = "";
  elements.dateBreadcrumb.textContent = `SIMILAR / ${source.name.replace(/\.ARW$/i, "")}`;
  elements.dateTitle.textContent = "Visual Neighbors";
  elements.resultModeLabel.textContent = "SigLIP2 image cosine";
  const url = new URL(window.location.href);
  url.searchParams.delete("motif");
  url.searchParams.delete("q");
  url.searchParams.delete("sequence");
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

function openSequenceDialog(sequence = null, { openAfterSave = false } = {}) {
  if (!state.workspaceWritable) {
    return;
  }
  state.editingSequenceId = sequence?.id || null;
  state.openSequenceAfterSave = openAfterSave;
  elements.sequenceDialogTitle.textContent = sequence ? "Edit Sequence" : "New Sequence";
  elements.sequenceSubmitButton.textContent = sequence ? "Save changes" : "Create Sequence";
  elements.sequenceNameInput.value = sequence?.name || "";
  elements.sequenceNoteInput.value = sequence?.note || "";
  elements.sequenceFormError.textContent = "";
  elements.sequenceDialog.showModal();
  elements.sequenceNameInput.focus();
}

function closeSequenceDialog() {
  elements.sequenceDialog.close();
  elements.sequenceFormError.textContent = "";
}

async function submitSequenceForm(event) {
  event.preventDefault();
  const editingId = state.editingSequenceId;
  const payload = {
    name: elements.sequenceNameInput.value.trim(),
    note: elements.sequenceNoteInput.value.trim(),
  };
  if (!payload.name) {
    elements.sequenceFormError.textContent = "Sequence name is required.";
    return;
  }
  elements.sequenceSubmitButton.disabled = true;
  elements.sequenceFormError.textContent = "";
  try {
    const response = editingId
      ? await sendJSON(`/api/sequences/${encodeURIComponent(editingId)}`, "PATCH", payload)
      : await sendJSON("/api/sequences", "POST", payload);
    const sequence = response.sequence;
    const shouldOpen =
      state.openSequenceAfterSave ||
      (state.mode === "sequence" && state.currentSequence?.id === sequence.id);
    closeSequenceDialog();
    await refreshSequences(sequence.id);
    elements.sequenceStatus.textContent = editingId ? "Sequence details saved." : "Sequence created.";
    if (shouldOpen) {
      await openSequence(sequence.id);
    }
  } catch (error) {
    elements.sequenceFormError.textContent = error.message;
  } finally {
    elements.sequenceSubmitButton.disabled = false;
  }
}

async function addAssetsToActiveSequence(assetIds, label) {
  const sequenceId = state.activeSequenceId;
  if (!sequenceId || assetIds.length === 0) {
    return;
  }
  elements.sequenceStatus.textContent = `${label}…`;
  elements.addToSequenceButton.disabled = true;
  elements.addCuratorSeedButton.disabled = true;
  try {
    const response = await sendJSON(
      `/api/sequences/${encodeURIComponent(sequenceId)}/items`,
      "POST",
      { asset_ids: assetIds },
    );
    await refreshSequences(sequenceId);
    const result = [];
    if (response.added > 0) {
      result.push(`${response.added} added`);
    }
    if (response.skipped > 0) {
      result.push(`${response.skipped} already present`);
    }
    elements.sequenceStatus.textContent = result.join("; ") || "Sequence unchanged.";
    if (state.mode === "sequence" && state.currentSequence?.id === sequenceId) {
      displaySequence(response.sequence);
    }
  } catch (error) {
    elements.sequenceStatus.textContent = error.message;
  } finally {
    updateSequenceControls();
  }
}

async function moveCurrentSequenceItem(delta) {
  if (state.mode !== "sequence" || !state.currentSequence || state.selectedIndex < 0) {
    return;
  }
  const targetIndex = state.selectedIndex + delta;
  if (targetIndex < 0 || targetIndex >= state.assets.length) {
    return;
  }
  const selectedItemId = state.assets[state.selectedIndex].sequence_item_id;
  const itemIds = state.assets.map((asset) => asset.sequence_item_id);
  [itemIds[state.selectedIndex], itemIds[targetIndex]] = [
    itemIds[targetIndex],
    itemIds[state.selectedIndex],
  ];
  elements.sequenceStatus.textContent = "Saving Sequence order…";
  elements.moveSequenceEarlierButton.disabled = true;
  elements.moveSequenceLaterButton.disabled = true;
  try {
    const response = await sendJSON(
      `/api/sequences/${encodeURIComponent(state.currentSequence.id)}/items/order`,
      "PUT",
      { item_ids: itemIds },
    );
    displaySequence(response.sequence, selectedItemId);
    elements.sequenceStatus.textContent = `Moved to position ${targetIndex + 1}.`;
  } catch (error) {
    elements.sequenceStatus.textContent = error.message;
    updateSequenceControls();
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
elements.curatorButton.addEventListener("click", () => void loadCurator());
elements.discoverMotifsButton.addEventListener("click", () => void loadMotifs());
elements.toolbarMotifsButton.addEventListener("click", () => void loadMotifs());
elements.mobileMotifSelect.addEventListener("change", (event) => {
  openMotif(event.target.value);
});
elements.activeSequenceSelect.addEventListener("change", (event) => {
  setActiveSequence(event.target.value);
  elements.sequenceStatus.textContent = "Active Sequence selected.";
});
elements.newSequenceButton.addEventListener("click", () => {
  openSequenceDialog(null, { openAfterSave: true });
});
elements.inspectorNewSequenceButton.addEventListener("click", () => {
  openSequenceDialog();
});
elements.editSequenceButton.addEventListener("click", () => {
  const active = state.sequences.find((item) => item.id === state.activeSequenceId);
  if (active) {
    openSequenceDialog(active);
  }
});
elements.openSequenceButton.addEventListener("click", () => {
  if (state.activeSequenceId) {
    void openSequence(state.activeSequenceId);
  }
});
elements.addToSequenceButton.addEventListener("click", () => {
  const asset = state.assets[state.selectedIndex];
  if (asset?.id) {
    void addAssetsToActiveSequence([asset.id], "Adding frame");
  }
});
elements.addCuratorSeedButton.addEventListener("click", () => {
  void addAssetsToActiveSequence(state.curatorSeedAssetIds, "Adding Curator seed");
});
elements.moveSequenceEarlierButton.addEventListener("click", () => {
  void moveCurrentSequenceItem(-1);
});
elements.moveSequenceLaterButton.addEventListener("click", () => {
  void moveCurrentSequenceItem(1);
});
elements.sequenceForm.addEventListener("submit", (event) => void submitSequenceForm(event));
elements.sequenceCancelButton.addEventListener("click", closeSequenceDialog);

document.addEventListener("keydown", (event) => {
  const typing =
    event.target instanceof HTMLInputElement ||
    event.target instanceof HTMLSelectElement ||
    event.target instanceof HTMLTextAreaElement;
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
