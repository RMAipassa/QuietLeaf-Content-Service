(() => {
  "use strict";
  const el = (id) => document.getElementById(id);
  const validId = (id) => typeof id === "string" && /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/.test(id);
  const encode = encodeURIComponent;
  const params = new URLSearchParams(location.search);
  const store = { getItem: (key) => localStorage.getItem(key), setItem: (key, value) => localStorage.setItem(key, value) };
  let clientId;
  try { clientId = store.getItem("quietleaf.web-client") || crypto.randomUUID(); store.setItem("quietleaf.web-client", clientId); }
  catch (_) { clientId = crypto.randomUUID(); }
  let authenticated = false, selection = 0, librarySequence = 0, current = null, chapter = null;
  let controller = null, libraryController = null, saveTimer = null, searchTimer = null, readyToSave = false;
  let pageIndex = 0, pageCount = 0, imageUrl = null;
  let pullingProgress = false;
  let scrollArmed = false, userScrolled = false;

  function node(tag, text, className = "") {
    const item = document.createElement(tag);
    item.textContent = text;
    item.className = className;
    return item;
  }
  function mode() { return el("reader-mode").value; }
  function key() { return current ? `${current.source}/${current.id}` : null; }
  function progressPath(bookKey) { return `/v1/reading-progress/${bookKey.split("/").map(encode).join("/")}`; }
  function clearImage() { if (imageUrl) URL.revokeObjectURL(imageUrl); imageUrl = null; el("reader-page-image").removeAttribute("src"); }
  function lock(message = "") {
    authenticated = false; selection++; librarySequence++;
    controller?.abort(); libraryController?.abort(); readyToSave = false;
    current = chapter = null; clearImage();
    ["reader-prose", "reader-book-title", "reader-book-author", "reader-chapter-title", "reader-book-source", "reader-status", "reader-sync-status"].forEach((id) => { el(id).textContent = ""; });
    ["reader-book-list", "reader-chapters", "reader-inspection-details"].forEach((id) => el(id).replaceChildren());
    document.title = "QuietLeaf Web Reader";
    el("reader-workspace").hidden = true; el("reader-login").hidden = false; el("reader-logout").hidden = true;
    el("reader-login-error").textContent = message;
  }
  async function request(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("Accept", "application/json");
    if (!["GET", "HEAD"].includes(options.method || "GET")) headers.set("X-QuietLeaf-Admin", "1");
    const response = await fetch(path, { ...options, headers, credentials: "same-origin", cache: "no-store" });
    if (response.status === 401) { lock("Sign in to continue reading."); throw new Error("Authentication required"); }
    return response;
  }
  async function json(path, options = {}) {
    const response = await request(path, options);
    const payload = await response.json();
    if (!response.ok) throw new Error(typeof payload.detail === "string" ? payload.detail : `Service returned HTTP ${response.status}`);
    return payload;
  }
  const progress = new window.QuietLeafProgress.ProgressClient(store, async (bookKey, pending, keepalive) => {
    const response = await request(progressPath(bookKey), { method: "PUT", keepalive,
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(pending) });
    const payload = await response.json();
    if (response.status === 409) return { conflict: true, progress: payload.progress };
    if (!response.ok) throw new Error(payload.detail || "Progress sync failed");
    return payload;
  }, clientId, () => crypto.randomUUID());

  function syncState() {
    if (mode() === "inspect") {
      el("reader-sync-status").textContent = "Inspection mode — reading progress is not changed.";
      el("reader-conflict").hidden = true; el("reader-sync").disabled = true;
      return;
    }
    el("reader-sync").disabled = !current;
    const state = key() && progress.load(key());
    el("reader-conflict").hidden = !state?.conflict;
    el("reader-sync-status").textContent = state?.conflict ? "Progress changed on another device — choose a position below."
      : state?.pending || !window.QuietLeafProgress.equalPosition(state?.draft, state?.baseline)
        ? "Position saved in this browser; waiting to sync." : "Reading progress is synced with your service.";
  }
  async function flush(bookKey = key(), keepalive = false) {
    if (!bookKey || mode() !== "read" || !authenticated) return;
    try { await progress.flush(bookKey, keepalive); if (bookKey === key()) syncState(); }
    catch (_) { if (bookKey === key() && authenticated) el("reader-sync-status").textContent = "Position kept in this browser. Sync will retry when connected."; }
  }
  function displayedFraction() {
    if (pageCount) return pageCount <= 1 ? 0 : pageIndex / (pageCount - 1);
    const prose = el("reader-prose");
    const top = prose.getBoundingClientRect().top + window.scrollY;
    const end = Math.min(document.documentElement.scrollHeight - window.innerHeight,
      top + prose.scrollHeight - window.innerHeight + 90);
    return Math.max(0, Math.min(1, (window.scrollY - top) / Math.max(1, end - top)));
  }
  function stagePosition() {
    if (!readyToSave || !chapter || !key() || mode() !== "read") return;
    if (!pageCount && !userScrolled) return;
    progress.stage(key(), { chapterId: chapter.id, fraction: Math.round(displayedFraction() * 1e9) / 1e9 });
    syncState();
    window.clearTimeout(saveTimer);
    saveTimer = window.setTimeout(() => flush(), 1200);
  }
  function updateUrl(chapterId) {
    const query = new URLSearchParams({ source: current.source, book: current.id, chapter: chapterId, mode: mode() });
    if (mode() === "read") query.set("resume", "1");
    history.replaceState(null, "", `/reader?${query}`);
  }
  function chapterOptions() {
    const query = el("reader-chapter-filter").value.trim().toLowerCase();
    const options = (current?.chapters || []).flatMap((item, index) => {
      if (!el("reader-show-missing").checked && !item.archived) return [];
      const title = `${index + 1}. ${item.title}${item.archived ? "" : " — not saved"}`;
      if (query && !title.toLowerCase().includes(query)) return [];
      const option = node("option", title);
      option.value = item.id; option.disabled = !item.archived; option.selected = item.id === chapter?.id;
      return [option];
    });
    el("reader-chapters").replaceChildren(...options);
  }
  function navigation() {
    const saved = (current?.chapters || []).filter((item) => item.archived);
    const index = saved.findIndex((item) => item.id === chapter?.id);
    ["reader-previous", "reader-previous-bottom"].forEach((id) => { el(id).disabled = index <= 0; });
    ["reader-next", "reader-next-bottom"].forEach((id) => { el(id).disabled = index < 0 || index >= saved.length - 1; });
  }
  function inspection(data) {
    const when = data.savedAt ? new Date(data.savedAt * 1000).toLocaleString() : "Unknown";
    const values = [["Stored copy", data.origin], ["Chapter ID", data.id], ["Saved", when],
      ["Stored bytes", String(data.storedBytes)], [data.mediaType === "text" ? "Displayed characters" : "Pages", String(data.characters ?? data.pageCount)],
      ...("cleanupApplied" in data ? [["Display cleanup applied", data.cleanupApplied ? "Yes" : "No"], ["Stored characters", String(data.storedCharacters)]] : [])];
    el("reader-inspection-details").replaceChildren(...values.map(([label, value]) => {
      const row = document.createElement("div"); row.append(node("dt", label), node("dd", value)); return row;
    }));
    el("reader-plain").hidden = data.mediaType !== "text";
    el("reader-plain").href = `/v1/reader/books/${encode(current.source)}/${encode(current.id)}/chapters/${encode(data.id)}?plain=true&raw=true`;
  }
  async function showPage(index, sequence) {
    pageIndex = Math.max(0, Math.min(pageCount - 1, index));
    el("reader-page-number").value = String(pageIndex + 1); el("reader-page-count").textContent = `of ${pageCount}`;
    el("reader-page-previous").disabled = pageIndex === 0; el("reader-page-next").disabled = pageIndex >= pageCount - 1;
    const wanted = pageIndex;
    const response = await request(`/v1/private/books/${encode(current.id)}/pages/${pageIndex}`, { signal: controller.signal });
    if (!response.ok) throw new Error("Saved page could not be loaded");
    if (Number(response.headers.get("content-length") || 0) > 10 * 1024 * 1024) throw new Error("Page image exceeds size limit");
    const blob = await response.blob();
    if (blob.size > 10 * 1024 * 1024) throw new Error("Page image exceeds size limit");
    if (sequence !== selection || wanted !== pageIndex) return;
    clearImage(); imageUrl = URL.createObjectURL(blob); el("reader-page-image").src = imageUrl;
    stagePosition();
  }
  async function loadChapter(id, fraction = 0) {
    if (!current || !validId(id)) return;
    stagePosition(); flush();
    const sequence = ++selection;
    controller?.abort(); controller = new AbortController(); readyToSave = false; pageCount = 0; clearImage();
    scrollArmed = userScrolled = false;
    chapter = current.chapters.find((item) => item.id === id) || { id, title: id };
    el("reader-prose").textContent = ""; el("reader-page-image").hidden = true; el("reader-page-controls").hidden = true;
    el("reader-chapter-title").textContent = chapter.title; el("reader-status").textContent = "Loading saved copy...";
    chapterOptions(); navigation(); updateUrl(id);
    try {
      const data = await json(`/v1/reader/books/${encode(current.source)}/${encode(current.id)}/chapters/${encode(id)}`, { signal: controller.signal });
      if (sequence !== selection) return;
      chapter = { ...chapter, ...data }; el("reader-chapter-title").textContent = data.title;
      inspection(data);
      if (data.mediaType !== "text") {
        pageCount = data.pageCount;
        el("reader-page-controls").hidden = false; el("reader-page-image").hidden = false;
        el("reader-page-number").max = String(pageCount);
        await showPage(Math.round(fraction * Math.max(0, pageCount - 1)), sequence);
      } else {
        el("reader-prose").textContent = data.text;
        await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        if (sequence !== selection) return;
        const prose = el("reader-prose"), top = prose.getBoundingClientRect().top + window.scrollY;
        const end = Math.min(document.documentElement.scrollHeight - window.innerHeight,
          top + prose.scrollHeight - window.innerHeight + 90);
        window.scrollTo(0, fraction > 0 ? top + fraction * Math.max(0, end - top) : 0);
      }
      if (sequence !== selection) return;
      readyToSave = true;
      el("reader-status").textContent = data.mediaType === "text" ? `${data.characters.toLocaleString()} characters · saved on your service`
        : `${pageCount} saved pages`;
      if (mode() === "read") {
        progress.stage(key(), { chapterId: id, fraction });
        flush();
      }
      syncState();
    } catch (error) {
      if (error.name !== "AbortError" && sequence === selection && authenticated) el("reader-status").textContent = error.message;
    }
  }
  async function loadBook(source, id, requestedChapter = "") {
    if (!validId(source) || !validId(id)) { el("reader-status").textContent = "Invalid book link"; return; }
    stagePosition(); flush();
    const sequence = ++selection;
    controller?.abort(); controller = new AbortController(); readyToSave = false;
    current = chapter = null; pageCount = 0; clearImage(); el("reader-prose").textContent = "";
    el("reader-chapters").disabled = true;
    el("reader-chapters").replaceChildren();
    el("reader-book-title").textContent = "Loading saved book...";
    el("reader-book-author").textContent = ""; el("reader-book-source").textContent = "";
    el("reader-chapter-title").textContent = "";
    try {
      const payload = await json(`/v1/reader/books/${encode(source)}/${encode(id)}`, { signal: controller.signal });
      if (sequence !== selection) return;
      current = { ...payload.book, source, id, chapters: payload.chapters };
      el("reader-chapters").disabled = false;
      document.title = `${current.title} · QuietLeaf`;
      el("reader-book-title").textContent = current.title; el("reader-book-author").textContent = current.author || "";
      el("reader-book-source").textContent = `${source} · ${payload.chapters.filter((item) => item.archived).length}/${payload.chapters.length} chapters saved`;
      const state = mode() === "read" ? progress.open(key(), payload.progress) : null;
      const desired = state?.draft || payload.progress;
      const candidate = requestedChapter || desired?.chapterId || payload.chapters.find((item) => item.archived)?.id;
      chapterOptions(); syncState();
      if (candidate) await loadChapter(candidate, desired?.chapterId === candidate && mode() === "read" ? desired.fraction : 0);
      else el("reader-status").textContent = "No chapter text is saved yet. Refresh the chapter list after archiving.";
    } catch (error) {
      if (error.name !== "AbortError" && sequence === selection && authenticated) el("reader-status").textContent = error.message;
    }
  }
  async function loadLibrary() {
    const sequence = ++librarySequence;
    libraryController?.abort(); libraryController = new AbortController();
    try {
      const payload = await json(`/v1/reader/books?q=${encode(el("reader-book-filter").value.trim())}`, { signal: libraryController.signal });
      if (sequence !== librarySequence) return;
      el("reader-library-note").textContent = `Showing ${payload.items.length} of ${payload.total} saved items`;
      el("reader-book-list").replaceChildren(...payload.items.map((book) => {
        const button = node("button", "", "reader-book");
        button.append(node("strong", book.title), node("small", `${book.author || "Unknown author"} · ${book.source}`));
        button.addEventListener("click", () => loadBook(book.source, book.id));
        return button;
      }));
    } catch (error) {
      if (error.name !== "AbortError" && authenticated) el("reader-library-note").textContent = error.message;
    }
  }
  async function open() {
    try {
      await json("/v1/health"); authenticated = true;
      el("reader-login").hidden = true; el("reader-workspace").hidden = false; el("reader-logout").hidden = false;
      await loadLibrary();
      const requested = new URLSearchParams(location.search);
      const requestedChapter = mode() === "read" && requested.get("resume") === "1" ? "" : requested.get("chapter") || "";
      if (requested.get("source") && requested.get("book")) await loadBook(requested.get("source"), requested.get("book"), requestedChapter);
    } catch (_) { /* The request helper opens the login panel on 401. */ }
  }
  async function resolveConflict(keepLocal) {
    if (!current) return;
    const selectedKey = key();
    const remote = (await json(progressPath(selectedKey))).progress;
    const state = progress.resolve(selectedKey, remote, keepLocal);
    syncState();
    if (keepLocal) await flush(selectedKey);
    else if (state.draft && key() === selectedKey) { readyToSave = false; await loadChapter(state.draft.chapterId, state.draft.fraction); }
  }
  el("reader-login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await json("/admin/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password: el("reader-password").value }) });
      el("reader-password").value = ""; await open();
    } catch (error) { el("reader-login-error").textContent = error.message; }
  });
  el("reader-logout").addEventListener("click", async () => { stagePosition(); await flush(); await json("/admin/logout", { method: "POST" }); lock(); });
  el("reader-book-filter").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadLibrary, 300); });
  el("reader-chapter-filter").addEventListener("input", chapterOptions);
  el("reader-show-missing").addEventListener("change", chapterOptions);
  el("reader-chapters").addEventListener("change", () => loadChapter(el("reader-chapters").value));
  el("reader-refresh").addEventListener("click", () => { if (current) loadBook(current.source, current.id, chapter?.id); });
  function move(delta) {
    const saved = (current?.chapters || []).filter((item) => item.archived);
    const index = saved.findIndex((item) => item.id === chapter?.id);
    if (saved[index + delta]) loadChapter(saved[index + delta].id);
  }
  ["reader-previous", "reader-previous-bottom"].forEach((id) => el(id).addEventListener("click", () => move(-1)));
  ["reader-next", "reader-next-bottom"].forEach((id) => el(id).addEventListener("click", () => move(1)));
  el("reader-mode").value = params.get("mode") === "inspect" ? "inspect" : "read";
  el("reader-mode").addEventListener("change", async () => {
    syncState();
    if (mode() === "inspect") { if (current && chapter) updateUrl(chapter.id); return; }
    readyToSave = false;
    if (current) await loadBook(current.source, current.id, chapter?.id);
  });
  for (const setting of ["theme", "size"]) {
    try { el(`reader-${setting}`).value = localStorage.getItem(`quietleaf.reader.${setting}`) || (setting === "theme" ? "dark" : "medium"); } catch (_) { }
    const apply = () => { document.documentElement.setAttribute(`data-reader-${setting}`, el(`reader-${setting}`).value);
      try { localStorage.setItem(`quietleaf.reader.${setting}`, el(`reader-${setting}`).value); } catch (_) { } };
    apply(); el(`reader-${setting}`).addEventListener("change", apply);
  }
  el("reader-use-service").addEventListener("click", () => resolveConflict(false).catch((error) => { el("reader-sync-status").textContent = error.message; }));
  el("reader-keep-local").addEventListener("click", () => resolveConflict(true).catch((error) => { el("reader-sync-status").textContent = error.message; }));
  async function syncFromService() {
    if (!authenticated || !current || !readyToSave || mode() !== "read" || pullingProgress) return;
    pullingProgress = true;
    try {
      await flush();
      const selectedKey = key(), remote = (await json(progressPath(selectedKey))).progress;
      if (key() !== selectedKey || mode() !== "read") return;
      const state = progress.open(selectedKey, remote); syncState();
      if (!state.conflict && state.draft) { readyToSave = false; await loadChapter(state.draft.chapterId, state.draft.fraction); }
    } catch (error) { if (authenticated) el("reader-sync-status").textContent = error.message; }
    finally { pullingProgress = false; }
  }
  el("reader-sync").addEventListener("click", syncFromService);
  el("reader-page-previous").addEventListener("click", () => showPage(pageIndex - 1, selection).catch((error) => { el("reader-status").textContent = error.message; }));
  el("reader-page-next").addEventListener("click", () => showPage(pageIndex + 1, selection).catch((error) => { el("reader-status").textContent = error.message; }));
  el("reader-page-number").addEventListener("change", () => showPage(Number(el("reader-page-number").value) - 1, selection).catch((error) => { el("reader-status").textContent = error.message; }));
  for (const event of ["wheel", "touchmove", "pointerdown"]) {
    window.addEventListener(event, () => { scrollArmed = true; }, { passive: true });
  }
  window.addEventListener("keydown", (event) => {
    if (["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "].includes(event.key)) scrollArmed = true;
  });
  window.addEventListener("scroll", () => {
    if (scrollArmed && readyToSave) { userScrolled = true; stagePosition(); }
  }, { passive: true });
  window.addEventListener("online", () => flush());
  window.addEventListener("focus", syncFromService);
  window.addEventListener("pagehide", () => { stagePosition(); flush(key(), true); });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { stagePosition(); flush(key(), true); } else syncFromService();
  });
  open();
})();
