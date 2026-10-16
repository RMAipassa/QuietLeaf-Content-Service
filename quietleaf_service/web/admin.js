(() => {
  "use strict";

  const byId = (id) => document.getElementById(id);
  const loginPanel = byId("login-panel");
  const consolePanel = byId("console");
  const loginForm = byId("login-form");
  const tokenInput = byId("token-input");
  const loginError = byId("login-error");
  const lockButton = byId("lock-button");
  const refreshButton = byId("refresh-button");
  const bookFilter = byId("book-filter");
  const notice = byId("notice");

  let token = "";
  let timer = null;
  let refreshing = false;
  let state = { health: null, books: [], booksTotal: 0, jobs: [], follows: [] };

  function node(tag, className = "", text = "") {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text) item.textContent = text;
    return item;
  }

  function setConnection(online) {
    byId("connection-dot").classList.toggle("online", online);
    byId("connection-label").textContent = online ? "Connected" : "Locked";
  }

  function showNotice(message) {
    notice.textContent = message;
    notice.hidden = !message;
  }

  function lock(message = "") {
    token = "";
    state = { health: null, books: [], booksTotal: 0, jobs: [], follows: [] };
    window.clearInterval(timer);
    timer = null;
    consolePanel.hidden = true;
    loginPanel.hidden = false;
    lockButton.hidden = true;
    tokenInput.value = "";
    loginError.textContent = message;
    setConnection(false);
    tokenInput.focus();
  }

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("Authorization", `Bearer ${token}`);
    headers.set("Accept", "application/json");
    const response = await fetch(path, { ...options, headers, cache: "no-store" });
    if (response.status === 401) {
      lock("The token was rejected or has changed.");
      throw new Error("Authentication failed");
    }
    if (!response.ok) {
      let detail = `Request failed with HTTP ${response.status}`;
      try {
        const body = await response.json();
        if (body.detail) detail = body.detail;
      } catch (_) {
        // Keep the status-based message for non-JSON failures.
      }
      throw new Error(detail);
    }
    return response.status === 204 ? null : response.json();
  }

  function formatBytes(value) {
    const bytes = Number(value || 0);
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MiB`;
  }

  function formatTime(value) {
    const seconds = Number(value || 0);
    return seconds ? new Date(seconds * 1000).toLocaleString() : "Never";
  }

  function titleFor(source, bookId) {
    return state.books.find((book) => book.source === source && book.id === bookId)?.title || bookId;
  }

  function latestJob(source, bookId) {
    return state.jobs.find((job) => job.source === source && job.book_id === bookId);
  }

  function renderSummary() {
    const active = state.jobs.filter((job) => ["queued", "running", "paused"].includes(job.status));
    const enabledFollows = state.follows.filter((follow) => Number(follow.enabled) === 1);
    byId("metric-service").textContent = state.health.archiveWorkerRunning ? "Online" : "Idle";
    byId("metric-version").textContent = `Release ${state.health.releaseVersion || "unknown"}`;
    byId("metric-books").textContent = String(state.booksTotal);
    byId("metric-jobs").textContent = String(state.jobs.length);
    byId("metric-job-detail").textContent = active.length ? `${active.length} active or paused` : "No active work";
    byId("metric-follows").textContent = String(enabledFollows.length);
  }

  function renderService() {
    const details = byId("service-details");
    const values = [
      ["Service", state.health.service],
      ["API protocol", String(state.health.version)],
      ["Release", state.health.releaseVersion || "Unknown"],
      ["Archive worker", state.health.archiveWorkerRunning ? "Running" : "Not running"],
      ["Normal check interval", `${state.health.followIntervalSeconds || 0} seconds`],
      ["Active-book interval", `${state.health.activeIntervalSeconds || 0} seconds`],
      ["Sources", state.health.sources.map((source) => source.name).join(", ") || "None"],
      ["Archive storage", state.health.archiveEnabled ? "Enabled" : "Disabled"],
    ];
    details.replaceChildren(...values.map(([label, value]) => {
      const row = node("div");
      row.append(node("dt", "", label), node("dd", "", value));
      return row;
    }));
  }

  function renderBooks() {
    const list = byId("book-list");
    const needle = bookFilter.value.trim().toLowerCase();
    const books = state.books.filter((book) =>
      `${book.title} ${book.author} ${book.source}`.toLowerCase().includes(needle)
    );
    byId("book-result-note").textContent = state.booksTotal > state.books.length
      ? `Showing the newest ${state.books.length} of ${state.booksTotal} saved novels.`
      : `${state.books.length} saved ${state.books.length === 1 ? "novel" : "novels"}.`;
    if (!books.length) {
      list.replaceChildren(node("p", "empty-state", needle ? "No saved novels match this filter." : "No novels are archived yet."));
      return;
    }
    list.replaceChildren(...books.map((book) => {
      const card = node("article", "book-card");
      const copy = node("div");
      copy.append(node("h3", "", book.title), node("p", "", `${book.author || "Unknown author"} | ${book.source}`));
      const job = latestJob(book.source, book.id);
      const follow = state.follows.find((item) => item.source === book.source && item.book_id === book.id);
      const progress = job && job.total ? `${job.downloaded} / ${job.total} chapters archived` : "Archived metadata available";
      copy.append(node("p", "metadata", progress));

      const actions = node("div", "book-actions");
      const badge = node("span", `badge ${job?.status === "failed" ? "failed" : follow?.enabled ? "active" : ""}`,
        job?.status || (follow?.enabled ? "following" : "saved"));
      const remove = node("button", "danger-button", "Delete");
      const active = job && ["queued", "running", "paused"].includes(job.status);
      remove.disabled = Boolean(active);
      remove.title = active ? "Finish or cancel the archive job before deleting this novel." : "Delete this server archive";
      remove.addEventListener("click", () => deleteBook(book));
      actions.append(badge, remove);
      card.append(copy, actions);
      return card;
    }));
  }

  function renderJobs() {
    const list = byId("job-list");
    if (!state.jobs.length) {
      list.replaceChildren(node("p", "empty-state", "No archive jobs have run yet."));
      return;
    }
    list.replaceChildren(...state.jobs.slice(0, 12).map((job) => {
      const row = node("div", "compact-row");
      const copy = node("div");
      copy.append(node("strong", "", titleFor(job.source, job.book_id)));
      const progress = job.total ? `${job.downloaded} / ${job.total} chapters` : "Waiting for chapter list";
      copy.append(node("small", "", `${progress} | updated ${formatTime(job.updated_at)}`));
      if (job.error) copy.append(node("small", "", job.error));
      const actions = node("div", "row-actions");
      const badge = node("span", `badge ${job.status === "failed" ? "failed" : ["queued", "running"].includes(job.status) ? "active" : ""}`, job.status);
      actions.append(badge);
      const archived = state.books.some((book) => book.source === job.source && book.id === job.book_id);
      if (!archived && ["failed", "canceled", "completed"].includes(job.status)) {
        const clear = node("button", "secondary-button", "Clear");
        clear.addEventListener("click", () => clearOrphanedJob(job));
        actions.append(clear);
      }
      row.append(copy, actions);
      return row;
    }));
  }

  function renderFollows() {
    const list = byId("follow-list");
    if (!state.follows.length) {
      list.replaceChildren(node("p", "empty-state", "No novels have automatic checks configured."));
      return;
    }
    list.replaceChildren(...state.follows.map((follow) => {
      const row = node("div", "compact-row");
      const copy = node("div");
      copy.append(node("strong", "", titleFor(follow.source, follow.book_id)));
      const schedule = Number(follow.enabled) === 1 ? `Next check ${formatTime(follow.next_check_at)}` : "Automatic checks stopped";
      copy.append(node("small", "", schedule));
      if (follow.last_error) copy.append(node("small", "", follow.last_error));
      const actions = node("div", "row-actions");
      actions.append(node("span", `badge ${follow.enabled ? "active" : ""}`, follow.enabled ? "active" : "stopped"));
      if (Number(follow.enabled) === 1) {
        const stop = node("button", "secondary-button", "Stop");
        stop.addEventListener("click", () => stopFollowing(follow, stop));
        actions.append(stop);
      }
      row.append(copy, actions);
      return row;
    }));
  }

  function render() {
    renderSummary();
    renderService();
    renderBooks();
    renderJobs();
    renderFollows();
    byId("last-updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
  }

  async function refresh() {
    if (!token || refreshing) return;
    refreshing = true;
    refreshButton.disabled = true;
    showNotice("");
    try {
      const [health, books, jobs, follows] = await Promise.all([
        api("/v1/health"), api("/v1/archive/books"), api("/v1/archive/jobs"), api("/v1/archive/follows"),
      ]);
      state = {
        health,
        books: books.items,
        booksTotal: Number(books.total ?? books.items.length),
        jobs: jobs.items,
        follows: follows.items,
      };
      render();
    } catch (error) {
      if (token) showNotice(`Could not refresh the console: ${error.message}`);
    } finally {
      refreshing = false;
      refreshButton.disabled = false;
    }
  }

  async function deleteBook(book) {
    const job = latestJob(book.source, book.id);
    if (job && ["queued", "running", "paused"].includes(job.status)) {
      showNotice("Finish or cancel the active archive job before deleting this novel.");
      return;
    }
    const accepted = window.confirm(`Permanently delete "${book.title}" (${book.source}/${book.id})?\n\nThis removes its saved chapters, cover, job history, and follow state from the server.`);
    if (!accepted) return;
    try {
      await api(`/v1/archive/books/${encodeURIComponent(book.source)}/${encodeURIComponent(book.id)}`, { method: "DELETE" });
      await refresh();
    } catch (error) {
      showNotice(`Could not delete the novel: ${error.message}`);
    }
  }

  async function stopFollowing(follow, button) {
    button.disabled = true;
    try {
      await api(`/v1/archive/follows/${encodeURIComponent(follow.source)}/${encodeURIComponent(follow.book_id)}`, { method: "DELETE" });
      await refresh();
    } catch (error) {
      showNotice(`Could not stop automatic checks: ${error.message}`);
      button.disabled = false;
    }
  }

  async function clearOrphanedJob(job) {
    const accepted = window.confirm(`Clear the ${job.status} archive job for ${job.source}/${job.book_id}? This also removes its follow state.`);
    if (!accepted) return;
    try {
      await api(`/v1/archive/books/${encodeURIComponent(job.source)}/${encodeURIComponent(job.book_id)}`, { method: "DELETE" });
      await refresh();
    } catch (error) {
      showNotice(`Could not clear the archive job: ${error.message}`);
    }
  }

  loginForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    loginError.textContent = "";
    token = tokenInput.value;
    tokenInput.value = "";
    try {
      await api("/v1/health");
      loginPanel.hidden = true;
      consolePanel.hidden = false;
      lockButton.hidden = false;
      setConnection(true);
      await refresh();
      timer = window.setInterval(refresh, 15000);
    } catch (error) {
      if (token) loginError.textContent = error.message;
    }
  });

  lockButton.addEventListener("click", () => lock());
  refreshButton.addEventListener("click", refresh);
  bookFilter.addEventListener("input", renderBooks);
})();
