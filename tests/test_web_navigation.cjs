// Run with: node --test tests/test_web_navigation.cjs
// The browser boundary is faked; navigation, rendering, requests, and event handlers
// execute the actual app.js. No packages, photos, or model API calls are required.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

class Element {
  constructor(tag = "div") {
    this.tagName = tag;
    this.children = [];
    this.dataset = {};
    this.attributes = new Map();
    this.listeners = new Map();
    this.value = "";
    this.textContent = "";
    this.classList = { add() {}, remove() {}, toggle() {} };
  }
  append(...children) {
    this.children.push(...children.flatMap((child) =>
      child.tagName === "fragment" ? child.children : [child]));
  }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  setAttribute(name, value) { this.attributes.set(name, value); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  removeAttribute(name) { this.attributes.delete(name); }
  addEventListener(name, callback) { this.listeners.set(name, callback); }
  fire(name) { return this.listeners.get(name)?.({ target: this }); }
  querySelectorAll(selector) {
    return this.children.filter((child) =>
      (child.className || "").split(" ").includes(selector.slice(1)));
  }
  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || new Element();
  }
}

const dates = ["2026-08-29", "2025-12-25"];
const source = fs.readFileSync(
  path.join(__dirname, "../src/latent/web_assets/app.js"), "utf8",
);

async function openApp(query = "", searchResponse = async () => ({ results: [], total: 0 })) {
  const nodes = new Map();
  const requests = [];
  const timers = new Map();
  let timerId = 0;
  const document = {
    hidden: true,
    querySelector(selector) {
      if (!nodes.has(selector)) nodes.set(selector, new Element());
      return nodes.get(selector);
    },
    createElement: (tag) => new Element(tag),
    createDocumentFragment: () => new Element("fragment"),
    addEventListener() {},
  };
  const window = {
    location: new URL(`http://localhost/${query}`),
    history: { replaceState(_state, _title, url) { window.location = new URL(url); } },
    addEventListener() {},
    clearTimeout(id) { timers.delete(id); },
    setTimeout(callback) { const id = ++timerId; timers.set(id, callback); return id; },
    matchMedia: () => ({ matches: false }),
  };
  const context = vm.createContext({
    document, window, URL, URLSearchParams, AbortController, Intl, console,
    fetch: async (url, { signal } = {}) => {
      requests.push(url);
      const request = new URL(url, window.location);
      let body;
      if (request.pathname === "/api/library") {
        body = {
          dates: dates.map((capture_date) => ({ capture_date, asset_count: 1 })),
          cached_assets: 2, preview_jobs: { succeeded: 2, pending: 0 },
          embedding_index: { total_assets: 2, indexed_assets: 2, phase: "complete" },
        };
      } else if (request.pathname === "/api/sequences") {
        body = { sequences: [] };
      } else if (request.pathname === "/api/assets") {
        body = { assets: [], total: 0, has_more: false };
      } else if (request.pathname === "/api/search") {
        body = await searchResponse(request.searchParams, signal);
      } else {
        throw new Error(`Unexpected request: ${url}`);
      }
      return { ok: true, json: async () => body };
    },
  });
  vm.runInContext(source.replace(/boot\(\);\s*$/, "globalThis.booted = boot();"), context);
  await context.booted;
  return {
    node: (selector) => document.querySelector(selector),
    selectedDates: () => document.querySelector("#dateList").children
      .filter((button) => button.getAttribute("aria-current") === "date")
      .map((button) => button.dataset.date),
    requests,
    get url() { return window.location; },
    async input(value) {
      const input = document.querySelector("#librarySearch");
      input.value = value;
      input.fire("input");
    },
    async settle() {
      const pending = [...timers.values()];
      timers.clear();
      for (const callback of pending) callback();
      await new Promise(setImmediate);
    },
    async chooseDate(date) {
      await document.querySelector("#dateList").children
        .find((button) => button.dataset.date === date).fire("click");
    },
  };
}

function assertGlobal(app) {
  assert.deepEqual(app.selectedDates(), []);
  const select = app.node("#mobileDateSelect");
  assert.equal(select.value, "");
  const selected = select.children.find((option) => option.value === select.value);
  assert.equal(selected?.textContent, "All dates");
  assert.equal(selected.disabled, true);
  assert.equal(app.node("#dateBreadcrumb").textContent, "SEMANTIC / ALL DATES");
}

function assertDate(app, date) {
  assert.deepEqual(app.selectedDates(), [date]);
  assert.equal(app.node("#mobileDateSelect").value, date);
  assert.equal(app.node("#dateBreadcrumb").textContent, date);
  assert.equal(app.url.searchParams.has("q"), false);
}

test("global search clears date selection and clearing restores the last browsed date", async () => {
  const app = await openApp(`?date=${dates[1]}`);
  assertDate(app, dates[1]);
  await app.input("snow");
  await app.settle();
  assertGlobal(app);
  const search = new URL(app.requests.find((url) => url.startsWith("/api/search")), app.url);
  assert.equal(search.searchParams.has("date"), false);
  assert.equal(search.searchParams.get("q"), "snow");
  await app.input("");
  await app.settle();
  assertDate(app, dates[1]);
  assert.equal(new URL(app.requests.at(-1), app.url).searchParams.get("date"), dates[1]);
});

test("search deep links, variety, reload, and whitespace clear retain the return date", async () => {
  let app = await openApp(`?date=${dates[1]}&q=snow&variety=1`);
  assertGlobal(app);
  assert.equal(app.node("#searchVarietyButton").getAttribute("aria-pressed"), "true");
  app = await openApp(app.url.search);
  assertGlobal(app);
  await app.input("   ");
  await app.settle();
  assertDate(app, dates[1]);
  assert.equal(app.url.searchParams.has("variety"), false);
});

test("choosing the same or a different sidebar date exits global search", async () => {
  for (const date of dates) {
    const app = await openApp(`?date=${dates[0]}&q=snow`);
    await app.chooseDate(date);
    assertDate(app, date);
    assert.equal(app.node("#librarySearch").value, "");
  }
});

test("mobile date selection exits search and becomes the next return date", async () => {
  const app = await openApp(`?date=${dates[0]}&q=snow`);
  const select = app.node("#mobileDateSelect");
  select.value = dates[1];
  await select.fire("change");
  assertDate(app, dates[1]);
  await app.input("city");
  await app.settle();
  assertGlobal(app);
  await app.input("");
  await app.settle();
  assertDate(app, dates[1]);
});

test("empty and failed search results keep the global navigation state", async () => {
  for (const response of [async () => ({ results: [], total: 0 }), async () => {
    throw new Error("Synthetic service failure");
  }]) {
    const app = await openApp("?q=missing", response);
    assertGlobal(app);
    assert.equal(app.node("#emptyState").hidden, false);
    await app.input("");
    await app.settle();
    assertDate(app, dates[0]);
  }
});

test("repeated and rapid input preserves scope without duplicate searches", async () => {
  const app = await openApp();
  await app.input("snow");
  await app.settle();
  await app.input(" snow ");
  await app.settle();
  assert.equal(app.requests.filter((url) => url.startsWith("/api/search")).length, 1);
  await app.input("ci");
  await app.input("city");
  await app.settle();
  assertGlobal(app);
  assert.equal(app.url.searchParams.get("q"), "city");
  assert.equal(app.requests.filter((url) => url.startsWith("/api/search")).length, 2);
});

test("leaving an in-flight search restores a date and ignores its late response", async () => {
  let finish;
  const app = await openApp("", () => new Promise((resolve) => { finish = resolve; }));
  await app.input("slow");
  await app.settle();
  assertGlobal(app);
  await app.chooseDate(dates[1]);
  assertDate(app, dates[1]);
  finish({ results: [], total: 0 });
  await app.settle();
  assertDate(app, dates[1]);
});
