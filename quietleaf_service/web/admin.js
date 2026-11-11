(() => {
  "use strict";

  const byId = (id) => document.getElementById(id);
  const loginPanel = byId("login-panel");
  const consolePanel = byId("console");
  const loginForm = byId("login-form");
  const passwordInput = byId("password-input");
  const loginError = byId("login-error");
  const lockButton = byId("lock-button");
  const refreshButton = byId("refresh-button");
  const homeCacheButton = byId("home-cache-button");
  const bookFilter = byId("book-filter");
  const configurationForm = byId("configuration-form");
  const archiveConcurrency = byId("archive-concurrency");
  const followInterval = byId("follow-interval");
  const activeInterval = byId("active-interval");
  const configurationStatus = byId("configuration-status");
  const configurationSave = byId("configuration-save");
  const notice = byId("notice");
  const privateFilter = byId("private-filter");
  const privateUploadForm = byId("private-upload-form");
  const privateUploadStatus = byId("private-upload-status");
  const rearchiveSelected = byId("rearchive-selected");
  const selectedArchives = new Set();
  let queueingRefresh = false;

  let authenticated = false;
  let timer = null;
  let refreshing = false;
  let configurationDirty = false;
  let configurationRevision = 0;
  let state = { health: null, configuration: null, books: [], booksTotal: 0, jobs: [], activeJobs: [], follows: [] };

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
    authenticated = false;
    selectedArchives.clear();
    updateRefreshSelection();
    state = { health: null, configuration: null, books: [], booksTotal: 0, jobs: [], activeJobs: [], follows: [] };
    configurationDirty = false;
    window.clearInterval(timer);
    timer = null;
    consolePanel.hidden = true;
    loginPanel.hidden = false;
    lockButton.hidden = true;
    passwordInput.value = "";
    loginError.textContent = message;
    setConnection(false);
    passwordInput.focus();
  }

  function openConsole() {
    authenticated = true;
    loginPanel.hidden = true;
    consolePanel.hidden = false;
    lockButton.hidden = false;
    loginError.textContent = "";
    setConnection(true);
  }

  async function api(path, options = {}, lockOnUnauthorized = true) {
    const headers = new Headers(options.headers || {});
    headers.set("Accept", "application/json");
    const method = (options.method || "GET").toUpperCase();
    if (!["GET", "HEAD"].includes(method)) headers.set("X-QuietLeaf-Admin", "1");
    const response = await fetch(path, {
      ...options,
      headers,
      cache: "no-store",
      credentials: "same-origin",
    });
    if (response.status === 401) {
      if (lockOnUnauthorized) lock("Your admin session expired. Sign in again.");
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
    const jobs = state.jobs.filter((job) => job.source === source && job.book_id === bookId);
    return jobs.find((job) => ["queued", "running", "paused"].includes(job.status)) || jobs[0];
  }

  function updateRefreshSelection() {
    rearchiveSelected.textContent = `Re-archive selected (${selectedArchives.size})`;
    rearchiveSelected.disabled = queueingRefresh || !selectedArchives.size;
  }

  function renderSummary() {
    const active = state.activeJobs;
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
      ["Archive workers", state.health.archiveWorkerRunning
        ? `${state.health.archiveConcurrency} running, ${state.health.activeArchiveJobs} busy`
        : "Not running"],
      ["Normal check interval", `${state.health.followIntervalSeconds || 0} seconds`],
      ["Active-book interval", `${state.health.activeIntervalSeconds || 0} seconds`],
      ["Sources", state.health.sources.map((source) => source.name).join(", ") || "None"],
      ...state.health.sources.filter((source) => state.health.sourceCapabilities?.[source.id]?.chapterTextMode)
        .map((source) => [`${source.name} chapter mode`, state.health.sourceCapabilities[source.id].chapterTextMode]),
      ["Archive storage", state.health.archiveEnabled ? "Enabled" : "Disabled"],
    ];
    details.replaceChildren(...values.map(([label, value]) => {
      const row = node("div");
      row.append(node("dt", "", label), node("dd", "", value));
      return row;
    }));
  }

  function renderConfiguration() {
    if (!state.configuration || configurationDirty) return;
    archiveConcurrency.value = String(state.configuration.archiveConcurrency);
    followInterval.value = String(state.configuration.followIntervalSeconds / 60);
    activeInterval.value = String(state.configuration.activeIntervalSeconds / 60);
    configurationStatus.textContent = "Saved on this server";
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
      const publication = { ongoing: "Ongoing", completed: "Completed" }[book.publicationStatus];
      copy.append(node("h3", "", book.title), node("p", "", `${book.author || "Unknown author"} | ${book.source}${publication ? ` | ${publication}` : ""}`));
      const job = latestJob(book.source, book.id);
      const follow = state.follows.find((item) => item.source === book.source && item.book_id === book.id);
      const progress = job && job.total ? `${job.downloaded} / ${job.total} chapters ${job.refresh ? "re-archived" : "archived"}` : "Archived metadata available";
      copy.append(node("p", "metadata", progress));

      const actions = node("div", "book-actions");
      const badge = node("span", `badge ${job?.status === "failed" ? "failed" : follow?.enabled ? "active" : ""}`,
        job?.status || (follow?.enabled ? "following" : "saved"));
      const remove = node("button", "danger-button", "Delete");
      const active = job && ["queued", "running", "paused"].includes(job.status);
      const available = book.source !== "private" && state.health.sources.some((source) => source.id === book.source);
      const eligible = available && !active;
      const key = `${book.source}/${book.id}`;
      if (!eligible) selectedArchives.delete(key);
      const selection = node("label", "archive-selection");
      const checkbox = node("input");
      checkbox.type = "checkbox";
      checkbox.checked = selectedArchives.has(key);
      checkbox.disabled = !eligible || queueingRefresh;
      checkbox.setAttribute("aria-label", `Select ${book.title} for re-archiving`);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked && selectedArchives.size >= 50) {
          checkbox.checked = false;
          showNotice("Select up to 50 novels per re-archive batch.");
          return;
        }
        if (checkbox.checked) selectedArchives.add(key); else selectedArchives.delete(key);
        updateRefreshSelection();
      });
      selection.append(checkbox, node("span", "", "Select to re-archive"));
      copy.prepend(selection);
      const rearchive = node("button", "secondary-button", "Re-archive saved chapters");
      rearchive.type = "button";
      rearchive.disabled = !eligible || queueingRefresh;
      rearchive.title = !available ? "Enable this source adapter to fetch fresh chapters." : active ? "An archive job is already active for this novel." : "Fetch each saved chapter again using the current extractor.";
      rearchive.addEventListener("click", () => queueRearchive([{ source: book.source, bookId: book.id }]));
      remove.disabled = Boolean(active);
      remove.title = active ? "Finish or cancel the archive job before deleting this novel." : "Delete this server archive";
      remove.addEventListener("click", () => deleteBook(book));
      actions.append(badge, rearchive, remove);
      card.append(copy, actions);
      return card;
    }));
    updateRefreshSelection();
  }

  function jobRow(job) {
    const row = node("div", "compact-row");
    const copy = node("div");
    copy.append(node("strong", "", titleFor(job.source, job.book_id)));
    const progress = job.total ? `${job.downloaded} / ${job.total} chapters ${job.refresh ? "re-archived" : "archived"}` : "Waiting for chapter list";
    copy.append(node("small", "", `${progress} | updated ${formatTime(job.updated_at)}`));
    if (job.error) copy.append(node("small", "", job.error));
    const actions = node("div", "row-actions");
    const badge = node("span", `badge ${job.status === "failed" ? "failed" : ["queued", "running"].includes(job.status) ? "active" : ""}`, job.status);
    actions.append(badge);
    const controls = job.status === "paused" ? [["resume", "Resume"], ["cancel", "Cancel"]]
      : job.status === "failed" ? [["retry", "Retry"], ["cancel", "Cancel"]]
      : ["queued", "running"].includes(job.status) ? [["pause", "Pause"], ["cancel", "Cancel"]] : [];
    for (const [action, label] of controls) {
      const button = node("button", "secondary-button", label);
      button.type = "button";
      button.addEventListener("click", () => controlJob(job, action, button));
      actions.append(button);
    }
    const archived = state.books.some((book) => book.source === job.source && book.id === job.book_id);
    if (!archived && ["failed", "canceled", "completed"].includes(job.status)) {
      const clear = node("button", "secondary-button", "Clear");
      clear.addEventListener("click", () => clearOrphanedJob(job));
      actions.append(clear);
    }
    row.append(copy, actions);
    return row;
  }

  function renderJobs() {
    const active = state.activeJobs;
    byId("active-job-count").textContent = String(active.length);
    byId("active-job-list").replaceChildren(...(active.length ? active.map(jobRow)
      : [node("p", "empty-state", "No active archive jobs.")]));
    const history = state.jobs.filter((job) => !["queued", "running", "paused"].includes(job.status)).slice(0, 12);
    byId("job-list").replaceChildren(...(history.length ? history.map(jobRow)
      : [node("p", "empty-state", "No finished archive jobs yet.")]));
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
    renderConfiguration();
    renderBooks();
    renderJobs();
    renderFollows();
    renderPrivateBooks();
    byId("last-updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
  }

  async function refresh() {
    if (!authenticated || refreshing) return;
    refreshing = true;
    const startedConfigurationRevision = configurationRevision;
    refreshButton.disabled = true;
    showNotice("");
    try {
      const [health, configuration, books, jobs, activeJobs, follows] = await Promise.all([
        api("/v1/health"), api("/v1/archive/config"), api("/v1/archive/books"),
        api("/v1/archive/jobs?history_only=true"), api("/v1/archive/jobs/active"), api("/v1/archive/follows"),
      ]);
      const privateBooks = health.privateLibraryEnabled
        ? await api(`/v1/private/books?q=${encodeURIComponent(privateFilter.value.trim())}`)
        : { items: [], total: 0 };
      state = {
        health,
        privateBooks,
        configuration: startedConfigurationRevision === configurationRevision
          ? configuration
          : state.configuration,
        books: books.items,
        booksTotal: Number(books.total ?? books.items.length),
        jobs: [...activeJobs.items, ...jobs.items.filter((job) => !activeJobs.items.some((active) => active.id === job.id))],
        activeJobs: activeJobs.items,
        follows: follows.items,
      };
      render();
    } catch (error) {
      if (authenticated) showNotice(`Could not refresh the console: ${error.message}`);
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

  async function queueRearchive(items) {
    if (queueingRefresh) return;
    queueingRefresh = true;
    updateRefreshSelection();
    try {
      if (items.length === 1) {
        await api(`/v1/archive/${encodeURIComponent(items[0].source)}/${encodeURIComponent(items[0].bookId)}/refresh`, { method: "POST" });
      } else {
        await api("/v1/archive/refresh", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ items }) });
      }
      selectedArchives.clear();
      await refresh();
      showNotice(`Queued re-archiving for ${items.length} novel(s). Saved chapters remain readable while fresh text is fetched. Monitor progress below; source rate limits apply.`);
    } catch (error) { showNotice(`Could not start re-archiving: ${error.message}`); }
    finally { queueingRefresh = false; renderBooks(); updateRefreshSelection(); }
  }

  async function controlJob(job, action, button) {
    button.disabled = true;
    try {
      await api(`/v1/archive/jobs/${encodeURIComponent(job.id)}/${action}`, { method: "POST" });
      await refresh();
    } catch (error) { showNotice(error.message); button.disabled = false; }
  }

  rearchiveSelected.addEventListener("click", () => {
    const items = Array.from(selectedArchives).map((key) => {
      const [source, bookId] = key.split("/");
      return { source, bookId };
    });
    if (items.length) queueRearchive(items);
  });

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

  async function refreshHomeShelves() {
    homeCacheButton.disabled = true;
    showNotice("Refreshing home shelves from their sources...");
    try {
      const cleared = await api("/v1/home/cache", { method: "DELETE" });
      const feed = await api("/v1/home");
      const total = ["popular", "completed", "fresh", "new", "hot"]
        .reduce((count, category) => count + (feed[category]?.length || 0), 0);
      showNotice(`Home cache cleared for ${cleared.clearedSources} source(s); ${total} shelf entries loaded.`);
    } catch (error) {
      showNotice(`Could not refresh home shelves: ${error.message}`);
    } finally {
      homeCacheButton.disabled = false;
    }
  }

  function renderPrivateBooks() {
    byId("private-library-panel").hidden = !state.health?.privateLibraryEnabled;
    const catalog = state.privateBooks || { items: [], total: 0 };
    byId("private-result-note").textContent = `${catalog.total} private item(s); showing ${catalog.items.length}. Upload limit: ${formatBytes(state.health?.privateMaxUploadBytes)} per file.`;
    byId("private-book-list").replaceChildren(...catalog.items.map((book) => {
      const card = node("article", "book-card");
      const copy = node("div");
      copy.append(node("h3", "", book.title), node("p", "", book.author),
        node("p", "metadata", `${book.mediaType === "text" ? `${book.chapterCount} chapters` : `${book.pageCount} pages`} | ${book.originalName} | ${formatBytes(book.originalBytes)}`));
      const actions = node("div", "book-actions");
      const original = node("a", "secondary-button", "Original file");
      original.href = `/v1/private/books/${encodeURIComponent(book.id)}/file`;
      original.download = book.originalName;
      const remove = node("button", "danger-button", "Delete");
      remove.type = "button";
      remove.addEventListener("click", async () => {
        if (!window.confirm(`Delete "${book.title}" and its original file from your private library?`)) return;
        remove.disabled = true;
        try {
          await api(`/v1/private/books/${encodeURIComponent(book.id)}`, { method: "DELETE" });
          await refresh();
        } catch (error) { showNotice(error.message); remove.disabled = false; }
      });
      actions.append(original, remove);
      card.append(copy, actions);
      return card;
    }));
  }

  function uploadPrivateFile(file, title, author, position, total) {
    return new Promise((resolve, reject) => {
      const request = new XMLHttpRequest();
      const params = new URLSearchParams({ filename: file.name, title, author });
      request.open("POST", `/v1/private/books?${params}`);
      request.setRequestHeader("Content-Type", "application/octet-stream");
      request.setRequestHeader("X-QuietLeaf-Admin", "1");
      request.responseType = "json";
      request.upload.onprogress = (event) => {
        privateUploadStatus.textContent = event.lengthComputable && event.loaded < event.total
          ? `Uploading ${position}/${total}: ${file.name} — ${Math.round(event.loaded / event.total * 100)}%`
          : `Processing ${position}/${total}: ${file.name}...`;
      };
      request.onload = () => {
        if (request.status >= 200 && request.status < 300) resolve(request.response);
        else {
          if (request.status === 401) lock("Your admin session expired. Sign in again.");
          reject(new Error(request.response?.detail || `Upload failed with HTTP ${request.status}`));
        }
      };
      request.onerror = () => reject(new Error("Upload connection failed. Check the service connection."));
      request.onabort = () => reject(new Error("Upload canceled."));
      request.send(file);
    });
  }

  privateUploadForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const files = Array.from(byId("private-files").files || []);
    const limit = state.health?.privateMaxUploadBytes || 100000000;
    if (files.some((file) => !file.size || file.size > limit)) {
      privateUploadStatus.textContent = `Choose nonempty files up to ${formatBytes(limit)} each.`;
      return;
    }
    const button = byId("private-upload-button");
    button.disabled = true;
    try {
      for (let index = 0; index < files.length; index++) {
        await uploadPrivateFile(files[index], byId("private-title").value.trim(), byId("private-author").value.trim(), index + 1, files.length);
      }
      privateUploadForm.reset();
      privateUploadStatus.textContent = `${files.length} file(s) saved to your private library.`;
      await refresh();
    } catch (error) { privateUploadStatus.textContent = error.message; }
    finally { button.disabled = false; }
  });
  let privateSearchTimer;
  privateFilter.addEventListener("input", () => {
    window.clearTimeout(privateSearchTimer);
    privateSearchTimer = window.setTimeout(refresh, 300);
  });

  configurationForm.addEventListener("input", () => {
    configurationDirty = true;
    configurationStatus.textContent = "Unsaved changes";
  });

  configurationForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const concurrency = Number(archiveConcurrency.value);
    const normalMinutes = Number(followInterval.value);
    const activeMinutes = Number(activeInterval.value);
    if (![concurrency, normalMinutes, activeMinutes].every(Number.isInteger)) {
      showNotice("Operational settings must be whole numbers.");
      return;
    }
    configurationSave.disabled = true;
    configurationStatus.textContent = "Saving...";
    try {
      const configuration = await api("/v1/archive/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          archiveConcurrency: concurrency,
          followIntervalSeconds: normalMinutes * 60,
          activeIntervalSeconds: activeMinutes * 60,
        }),
      });
      state.configuration = configuration;
      state.health.archiveConcurrency = configuration.archiveConcurrency;
      state.health.followIntervalSeconds = configuration.followIntervalSeconds;
      state.health.activeIntervalSeconds = configuration.activeIntervalSeconds;
      configurationDirty = false;
      configurationRevision += 1;
      renderConfiguration();
      renderService();
      showNotice("Operational settings saved and applied.");
    } catch (error) {
      configurationStatus.textContent = "Save failed";
      showNotice(`Could not save operational settings: ${error.message}`);
    } finally {
      configurationSave.disabled = false;
    }
  });

  loginForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    loginError.textContent = "";
    const submitButton = loginForm.querySelector("button[type='submit']");
    submitButton.disabled = true;
    try {
      const response = await fetch("/admin/login", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept": "application/json" },
        body: JSON.stringify({ password: passwordInput.value }),
        cache: "no-store",
        credentials: "same-origin",
      });
      if (!response.ok) {
        let detail = `Sign-in failed with HTTP ${response.status}`;
        try {
          const body = await response.json();
          if (body.detail) detail = body.detail;
        } catch (_) {
          // Keep the status-based message for non-JSON failures.
        }
        throw new Error(detail);
      }
      passwordInput.value = "";
      openConsole();
      await refresh();
      timer = window.setInterval(refresh, 15000);
    } catch (error) {
      if (!authenticated) loginError.textContent = error.message;
    } finally {
      submitButton.disabled = false;
    }
  });

  lockButton.addEventListener("click", async () => {
    lockButton.disabled = true;
    try {
      await api("/admin/logout", { method: "POST" });
      lock();
    } catch (error) {
      if (authenticated) showNotice(`Could not log out: ${error.message}`);
    } finally {
      lockButton.disabled = false;
    }
  });
  refreshButton.addEventListener("click", refresh);
  homeCacheButton.addEventListener("click", refreshHomeShelves);
  bookFilter.addEventListener("input", renderBooks);

  (async () => {
    try {
      await api("/v1/health", {}, false);
      openConsole();
      await refresh();
      timer = window.setInterval(refresh, 15000);
    } catch (_) {
      lock();
    }
  })();
})();
