// The project page's search. archive.js holds the sample photos and, for every search and
// every photo, the orders Latent's ranking code produced when site/prepare.py ran.
// Nothing here runs a model or makes a request.
import archive from "./archive.js";

const VISIBLE = 12;
const EASE = "cubic-bezier(0.2, 0.7, 0.2, 1)";
const ORDER_LABELS = {
  closest: "most similar first",
  least_similar: "least similar first",
  variety: "with more variety",
};
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");

const gallery = document.querySelector("[data-gallery]");
const queries = document.querySelector("[data-queries]");
const barQuery = document.querySelector("[data-bar-query]");
const barNote = document.querySelector("[data-bar-note]");
const barReference = document.querySelector("[data-bar-reference]");
const barClear = document.querySelector("[data-bar-clear]");
const count = document.querySelector("[data-results-count]");
const more = document.querySelector("[data-more]");
const viewer = document.querySelector("[data-viewer]");
const viewerFrame = viewer.querySelector("[data-viewer-frame]");
const viewerImage = viewer.querySelector("[data-viewer-image]");

const state = { mode: "text", search: archive.first, reference: null, order: "closest", expanded: false };
let viewing = null;
let typing = 0;

const gridImage = (photo) => `photos/${photo.id}-grid.webp`;

const chips = archive.searches.map((search, index) => {
  const chip = document.createElement("button");
  chip.type = "button";
  chip.textContent = search.text;
  if (search.lang) chip.lang = search.lang;
  if (search.note) chip.title = search.note;
  chip.addEventListener("click", () => selectSearch(index));
  queries.append(chip);
  return chip;
});

const tiles = archive.photos.map((photo, index) => {
  const item = document.createElement("li");
  item.className = "tile";
  const button = document.createElement("button");
  button.type = "button";
  button.className = "tile-button";
  const image = document.createElement("img");
  image.src = gridImage(photo);
  image.width = 720;
  image.height = Math.round((720 * photo.height) / photo.width);
  image.alt = photo.alt;
  image.loading = "lazy";
  image.decoding = "async";
  image.style.backgroundColor = photo.color;
  const caption = document.createElement("span");
  caption.className = "tile-caption";
  const rank = document.createElement("span");
  const name = document.createElement("span");
  name.className = "tile-name";
  name.textContent = photo.id;
  const score = document.createElement("span");
  score.className = "tile-score";
  caption.append(rank, name, score);
  button.append(image, caption);
  button.addEventListener("click", () => openViewer(index));
  item.append(button);
  return { item, button, rank, score, x: 0, y: 0, shown: false, placed: false };
});

function results() {
  const source = state.mode === "text" ? archive.searches[state.search] : archive.similar[state.reference];
  return { scores: source.scores, order: source.orders[state.order] };
}

// Where a tile is on screen now, including partway through a move.
function onScreen(tile) {
  if (tile.item.getAnimations().length === 0) return { x: tile.x, y: tile.y, opacity: tile.shown ? 1 : 0 };
  const style = getComputedStyle(tile.item);
  const matrix = new DOMMatrixReadOnly(style.transform);
  return { x: matrix.m41, y: matrix.m42, opacity: Number(style.opacity) };
}

function render({ animate = true } = {}) {
  const { scores, order } = results();
  const limit = state.expanded ? order.length : Math.min(VISIBLE, order.length);
  const ranked = new Set(order);
  const sequence = [...order, ...archive.photos.map((_, index) => index).filter((index) => !ranked.has(index))];

  const width = gallery.clientWidth;
  const columns = width >= 940 ? 4 : width >= 600 ? 3 : 2;
  const gap = width >= 940 ? 18 : width >= 600 ? 14 : 10;
  const columnWidth = (width - gap * (columns - 1)) / columns;
  const caption = parseFloat(getComputedStyle(gallery).getPropertyValue("--caption")) || 34;
  const heights = new Array(columns).fill(0);
  let bottom = 0;
  const plans = sequence.map((photoIndex, position) => {
    const photo = archive.photos[photoIndex];
    const column = heights.indexOf(Math.min(...heights));
    const x = column * (columnWidth + gap);
    const y = heights[column];
    const height = (columnWidth * photo.height) / photo.width + caption;
    heights[column] += height + gap;
    const shown = position < limit;
    if (shown) bottom = Math.max(bottom, y + height);
    return { tile: tiles[photoIndex], photoIndex, position, x, y, shown };
  });

  const moving = animate && !reducedMotion.matches;
  const previousHeight = gallery.offsetHeight;
  const before = plans.map((plan) => onScreen(plan.tile));
  for (const plan of plans) plan.tile.item.getAnimations().forEach((animation) => animation.cancel());
  // Keep the document in rank order, so Tab and screen readers follow the results.
  gallery.append(...plans.map((plan) => plan.tile.item));

  plans.forEach((plan, at) => {
    const { tile, x, y, shown } = plan;
    const from = before[at];
    const wasShown = tile.placed && from.opacity > 0;
    tile.item.style.width = `${columnWidth}px`;
    tile.item.style.transform = `translate(${x}px, ${y}px)`;
    tile.item.inert = !shown;
    tile.rank.textContent = String(plan.position + 1).padStart(2, "0");
    const score = scores[plan.photoIndex];
    tile.score.textContent = score == null ? "" : score.toFixed(3);
    if (moving && tile.placed) {
      const start = `translate(${from.x}px, ${from.y}px)`;
      const end = `translate(${x}px, ${y}px)`;
      if (wasShown && shown && start !== end) {
        tile.item.animate([{ transform: start, opacity: from.opacity }, { transform: end, opacity: 1 }],
          { duration: 560, easing: EASE });
      } else if (!wasShown && shown) {
        tile.item.animate([{ transform: `translate(${x}px, ${y + 12}px)`, opacity: 0 }, { transform: end, opacity: 1 }],
          { duration: 420, delay: 160, easing: EASE, fill: "backwards" });
      } else if (wasShown && !shown) {
        tile.item.animate([
          { transform: start, opacity: from.opacity, visibility: "visible" },
          { transform: start, opacity: 0, visibility: "visible" },
        ], { duration: 240, easing: "ease-out" });
      }
    }
    Object.assign(tile, { x, y, shown, placed: true });
  });

  gallery.style.height = `${bottom}px`;
  if (moving && previousHeight && previousHeight !== Math.round(bottom)) {
    gallery.animate([{ height: `${previousHeight}px` }, { height: `${bottom}px` }], { duration: 560, easing: EASE });
  }

  count.textContent = `${limit} of ${order.length} photos, ${ORDER_LABELS[state.order]}`;
  more.hidden = order.length <= VISIBLE;
  more.textContent = state.expanded ? `Show the first ${VISIBLE}` : `Show all ${order.length}`;
}

// Type the query into the bar, as if someone had just entered it.
function type(text, note) {
  const token = ++typing;
  barNote.textContent = "";
  if (reducedMotion.matches) {
    barQuery.textContent = text;
    barNote.textContent = note ?? "";
    return Promise.resolve(true);
  }
  const characters = [...text];
  const delay = characters.length > 12 ? 24 : 48;
  barQuery.dataset.typing = "";
  return new Promise((resolve) => {
    let typed = 0;
    const next = () => {
      if (token !== typing) return resolve(false);
      typed += 1;
      barQuery.textContent = characters.slice(0, typed).join("");
      if (typed < characters.length) return setTimeout(next, delay);
      delete barQuery.dataset.typing;
      barNote.textContent = note ?? "";
      resolve(true);
    };
    next();
  });
}

function selectSearch(index, { animate = true } = {}) {
  Object.assign(state, { mode: "text", search: index, reference: null });
  barReference.hidden = true;
  barClear.hidden = true;
  chips.forEach((chip, at) => chip.setAttribute("aria-pressed", String(at === index)));
  const search = archive.searches[index];
  barQuery.lang = search.lang ?? "";
  if (!animate) {
    typing += 1;
    delete barQuery.dataset.typing;
    barQuery.textContent = search.text;
    barNote.textContent = search.note ?? "";
    render({ animate: false });
    return;
  }
  type(search.text, search.note).then((finished) => finished && render());
}

function searchWith(index) {
  const photo = archive.photos[index];
  Object.assign(state, { mode: "similar", reference: index });
  typing += 1;
  chips.forEach((chip) => chip.setAttribute("aria-pressed", "false"));
  barReference.src = gridImage(photo);
  barReference.hidden = false;
  barClear.hidden = false;
  barQuery.lang = "";
  delete barQuery.dataset.typing;
  barQuery.textContent = `Similar to ${photo.id}`;
  barNote.textContent = "";
  render();
}

function openViewer(index) {
  viewing = index;
  fillViewer();
  if (!viewer.open) viewer.showModal();
  viewerFrame.focus({ preventScroll: true });
}

function fillViewer() {
  const index = viewing;
  const photo = archive.photos[index];
  viewerImage.width = photo.width;
  viewerImage.height = photo.height;
  viewerImage.style.backgroundColor = photo.color;
  viewerImage.alt = photo.alt;
  viewerImage.src = gridImage(photo);
  const large = new Image();
  large.src = `photos/${photo.id}-large.webp`;
  large.decode().then(() => { if (viewing === index) viewerImage.src = large.src; }, () => {});

  viewer.querySelector("[data-viewer-title]").textContent = photo.id;
  viewer.querySelector("[data-viewer-meta]").textContent = [photo.day, photo.camera, photo.lens].filter(Boolean).join(" · ");
  viewer.querySelector("[data-viewer-exposure]").textContent = [photo.focal, photo.exposure].filter(Boolean).join(" · ");
  const { scores, order } = results();
  const rank = order.indexOf(index);
  const scoreLine = viewer.querySelector("[data-viewer-score]");
  if (rank < 0) {
    scoreLine.textContent = "The photo these results are similar to";
  } else {
    const subject = state.mode === "text"
      ? `for “${archive.searches[state.search].text}”`
      : `like ${archive.photos[state.reference].id}`;
    scoreLine.textContent = `Rank ${rank + 1} of ${order.length} ${subject} · ${scores[index].toFixed(3)}`;
  }
  viewer.querySelector("[data-viewer-similar]").disabled = state.mode === "similar" && state.reference === index;
}

function step(delta) {
  const { order } = results();
  const sequence = state.mode === "similar" ? [state.reference, ...order] : order;
  const at = sequence.indexOf(viewing);
  viewing = sequence[(at + delta + sequence.length) % sequence.length];
  fillViewer();
}

viewer.addEventListener("keydown", (event) => {
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  const key = event.key.toLowerCase();
  if (key === "arrowleft" || key === "w") step(-1);
  else if (key === "arrowright" || key === "e") step(1);
  // Space closes the preview, as in the app, unless a button has focus.
  else if (key === " " && event.target === viewerFrame) viewer.close();
  else return;
  event.preventDefault();
});
viewer.addEventListener("click", (event) => {
  if (event.target === viewer || event.target === viewerFrame) viewer.close();
});
viewer.querySelectorAll("[data-viewer-step]").forEach((button) => {
  button.addEventListener("click", () => step(Number(button.dataset.viewerStep)));
});
viewer.querySelector("[data-viewer-close]").addEventListener("click", () => viewer.close());
viewer.querySelector("[data-viewer-similar]").addEventListener("click", () => {
  const index = viewing;
  viewer.close();
  searchWith(index);
  document.querySelector(".finder").scrollIntoView({ block: "start", behavior: reducedMotion.matches ? "auto" : "smooth" });
  barClear.focus({ preventScroll: true });
});

barClear.addEventListener("click", () => selectSearch(state.search));
more.addEventListener("click", () => {
  state.expanded = !state.expanded;
  render();
});
document.querySelectorAll('input[name="order"]').forEach((input) => {
  input.addEventListener("change", () => {
    state.order = input.value;
    render();
  });
});

let galleryWidth = 0;
new ResizeObserver(() => {
  if (gallery.clientWidth === galleryWidth) return;
  galleryWidth = gallery.clientWidth;
  render({ animate: false });
}).observe(gallery);

const copy = document.querySelector("[data-copy]");
if (navigator.clipboard) {
  copy.hidden = false;
  copy.addEventListener("click", async () => {
    await navigator.clipboard.writeText(document.getElementById(copy.dataset.copy).textContent);
    copy.textContent = "Copied";
    setTimeout(() => { copy.textContent = "Copy"; }, 2000);
  });
}

selectSearch(archive.first, { animate: false });
