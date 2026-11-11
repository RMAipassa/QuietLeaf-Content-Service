const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.listeners = new Map();
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this.classList = { toggle() {} };
  }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return (this.text || "") + this.children.map((child) => child.textContent).join(" "); }
  append(...items) { this.children.push(...items); }
  prepend(...items) { this.children.unshift(...items); }
  replaceChildren(...items) { this.children = items; this.text = ""; }
  setAttribute(name, value) { this[name] = value; }
  focus() {}
  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }
  async fire(type) { await Promise.all((this.listeners.get(type) || []).map((handler) => handler({ preventDefault() {}, target: this }))); }
}

function descendants(root) { return [root, ...root.children.flatMap(descendants)]; }
function button(root, label) { return descendants(root).find((item) => item.tagName === "BUTTON" && item.textContent === label); }
async function idle() { await new Promise((resolve) => setImmediate(resolve)); }

async function consoleFixture(initialJobs = []) {
  const html = fs.readFileSync(path.join(__dirname, "../quietleaf_service/web/index.html"), "utf8");
  const elements = new Map([...html.matchAll(/<([a-z][\w-]*)\b[^>]*\bid="([^"]+)"/gi)].map((match) => [match[2], new Element(match[1])]));
  const requests = [];
  const books = [
    { source: "novelping", id: "first", title: "First Book", author: "Writer", publicationStatus: "completed" },
    { source: "wtr-lab", id: "second", title: "Second Book", author: "Writer", publicationStatus: "ongoing" },
  ];
  const jobs = initialJobs.map((job) => ({ ...job }));
  const fetch = async (url, options = {}) => {
    const method = options.method || "GET";
    requests.push({ url, method, headers: new Headers(options.headers || {}), body: options.body });
    let payload;
    if (method === "POST" && (url === "/v1/archive/refresh" || url.endsWith("/refresh"))) {
      const selection = options.body ? JSON.parse(options.body) : {};
      const items = url === "/v1/archive/refresh" ? selection.items : [{ source: url.split("/")[3], bookId: url.split("/")[4] }];
      const queued = items.map((item, index) => ({ id: `job-${jobs.length + index + 1}`, source: item.source, book_id: item.bookId,
        status: "queued", refresh: true, total: selection.startChapter ? selection.endChapter - selection.startChapter + 1 : 3,
        downloaded: 0, updated_at: 1, range_start: selection.startChapter, range_end: selection.endChapter }));
      jobs.unshift(...queued);
      payload = url === "/v1/archive/refresh" ? { items: queued, queued: queued.length } : queued[0];
    } else if (method === "POST" && url.endsWith("/pause")) {
      const job = jobs.find((item) => item.id === url.split("/")[4]);
      job.status = "paused";
      payload = job;
    } else {
      payload = {
        "/v1/health": { service: "quietleaf-content-api", version: 1, releaseVersion: "0.11.0", archiveWorkerRunning: true,
          archiveConcurrency: 3, activeArchiveJobs: 0, archiveEnabled: true, privateLibraryEnabled: false,
          sources: [{ id: "novelping", name: "NovelPing" }, { id: "wtr-lab", name: "WTR-LAB" }],
          sourceCapabilities: { "wtr-lab": { chapterTextMode: "reader-ui" } } },
        "/v1/archive/config": { archiveConcurrency: 3, followIntervalSeconds: 3600, activeIntervalSeconds: 1800 },
        "/v1/archive/books": { items: books, total: books.length },
        "/v1/archive/jobs?history_only=true": { items: jobs.filter((job) => !["queued", "running", "paused"].includes(job.status)) },
        "/v1/archive/jobs/active": { items: jobs.filter((job) => ["queued", "running", "paused"].includes(job.status)) },
        "/v1/archive/follows": { items: [] },
      }[url];
    }
    assert.notEqual(payload, undefined, `Unexpected request ${method} ${url}`);
    return { ok: true, status: method === "POST" ? 202 : 200, json: async () => payload };
  };
  const document = { getElementById: (id) => elements.get(id), createElement: (tag) => new Element(tag) };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../quietleaf_service/web/admin.js"), "utf8"), {
    document, fetch, Headers, URLSearchParams,
    window: { setInterval: () => 1, clearInterval() {}, setTimeout: () => 1, clearTimeout() {}, confirm: () => true },
  });
  await idle();
  return { elements, requests, jobs };
}

test("a novel's re-archive button queues a refresh and exposes progress and pause controls", async () => {
  const { elements, requests } = await consoleFixture();
  await button(elements.get("book-list"), "Re-archive saved chapters").fire("click");
  assert.match(elements.get("book-list").textContent, /0 \/ 3 chapters re-archived/);
  const queued = requests.find((item) => item.method === "POST");
  assert.equal(queued.url, "/v1/archive/novelping/first/refresh");
  assert.equal(queued.headers.get("X-QuietLeaf-Admin"), "1");
  await button(elements.get("active-job-list"), "Pause").fire("click");
  assert.match(elements.get("active-job-list").textContent, /paused/);
  assert.ok(button(elements.get("active-job-list"), "Resume"));
});

test("selected novels use the batch refresh endpoint and clear their selection", async () => {
  const { elements, requests } = await consoleFixture();
  const checkboxes = descendants(elements.get("book-list")).filter((item) => item.tagName === "INPUT" && item.type === "checkbox");
  assert.equal(checkboxes.length, 2);
  for (const checkbox of checkboxes) { checkbox.checked = true; await checkbox.fire("change"); }
  assert.equal(elements.get("rearchive-selected").textContent, "Re-archive selected (2)");
  await elements.get("rearchive-selected").fire("click");
  await idle();
  const queued = requests.find((item) => item.method === "POST");
  assert.equal(queued.url, "/v1/archive/refresh");
  assert.deepEqual(JSON.parse(queued.body), { items: [{ source: "novelping", bookId: "first" }, { source: "wtr-lab", bookId: "second" }] });
  assert.equal(elements.get("rearchive-selected").textContent, "Re-archive selected (0)");
  assert.equal(elements.get("rearchive-selected").disabled, true);
});

test("publication status is shown independently from archive job progress", async () => {
  const { elements } = await consoleFixture();
  assert.match(elements.get("book-list").textContent, /Writer \| novelping \| Completed/);
  assert.match(elements.get("book-list").textContent, /Writer \| wtr-lab \| Ongoing/);
});

test("the service panel identifies the active WTR-LAB reader mode", async () => {
  const { elements } = await consoleFixture();
  assert.match(elements.get("service-details").textContent, /WTR-LAB chapter mode reader-ui/);
});

test("an old running job stays visible separately from a long recent history", async () => {
  const history = Array.from({ length: 30 }, (_, index) => ({
    id: `history-${index}`, source: "novelping", book_id: "first", status: "completed",
    total: 10, downloaded: 10, updated_at: 100 + index,
  }));
  const old = { id: "old-running", source: "wtr-lab", book_id: "second", status: "running",
    total: 335, downloaded: 95, updated_at: 1 };
  const { elements } = await consoleFixture([...history, old]);
  assert.match(elements.get("active-job-list").textContent, /Second Book.*95 \/ 335/);
  assert.equal(elements.get("active-job-count").textContent, "1");
  assert.doesNotMatch(elements.get("job-list").textContent, /Second Book/);
  assert.match(elements.get("book-list").textContent, /95 \/ 335/);
  await button(elements.get("active-job-list"), "Pause").fire("click");
  assert.ok(button(elements.get("active-job-list"), "Resume"));
});

test("an individual re-archive sends an inclusive range and shows it with progress", async () => {
  const { elements, requests } = await consoleFixture();
  elements.get("rearchive-scope").value = "range";
  elements.get("rearchive-start").value = "39";
  elements.get("rearchive-end").value = "58";
  await elements.get("rearchive-scope").fire("change");
  assert.equal(elements.get("rearchive-start").disabled, false);
  await button(elements.get("book-list"), "Re-archive chapters 39–58").fire("click");
  const request = requests.find((item) => item.method === "POST");
  assert.deepEqual(JSON.parse(request.body), { startChapter: 39, endChapter: 58 });
  assert.match(elements.get("active-job-list").textContent, /0 \/ 20 chapters re-archived \| Chapters 39–58/);
  assert.match(elements.get("book-list").textContent, /Chapters 39–58/);
});

test("the chosen range applies to every selected novel", async () => {
  const { elements, requests } = await consoleFixture();
  elements.get("rearchive-scope").value = "range";
  elements.get("rearchive-start").value = "1";
  elements.get("rearchive-end").value = "20";
  await elements.get("rearchive-scope").fire("change");
  const checkboxes = descendants(elements.get("book-list")).filter((item) => item.tagName === "INPUT" && item.type === "checkbox");
  for (const checkbox of checkboxes) { checkbox.checked = true; await checkbox.fire("change"); }
  assert.equal(elements.get("rearchive-selected").textContent, "Re-archive chapters 1–20 (2)");
  await elements.get("rearchive-selected").fire("click");
  const request = requests.find((item) => item.method === "POST");
  assert.deepEqual(JSON.parse(request.body), { items: [
    { source: "novelping", bookId: "first" }, { source: "wtr-lab", bookId: "second" },
  ], startChapter: 1, endChapter: 20 });
});

test("invalid range input cannot queue a re-archive request", async () => {
  const { elements, requests } = await consoleFixture();
  elements.get("rearchive-scope").value = "range";
  elements.get("rearchive-start").value = "58";
  elements.get("rearchive-end").value = "39";
  await elements.get("rearchive-scope").fire("change");
  const action = button(elements.get("book-list"), "Re-archive chapter range");
  assert.equal(action.disabled, true);
  await action.fire("click");
  assert.equal(requests.some((item) => item.method === "POST"), false);
  assert.match(elements.get("rearchive-range-note").textContent, /whole chapter numbers/);
  elements.get("rearchive-scope").value = "all";
  await elements.get("rearchive-scope").fire("change");
  assert.equal(elements.get("rearchive-start").disabled, true);
  assert.ok(button(elements.get("book-list"), "Re-archive saved chapters"));
});
