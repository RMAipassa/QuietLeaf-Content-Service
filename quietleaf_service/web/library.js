((root) => {
  "use strict";
  const sorts = [
    ["last_read_desc", "Last read · newest first"], ["last_read_asc", "Last read · oldest first"],
    ["added_desc", "Date added · newest first"], ["added_asc", "Date added · oldest first"], ["title", "Title · A–Z"],
  ];
  function setupSort(select, key, changed) {
    select.replaceChildren(...sorts.map(([value, label]) => {
      const option = document.createElement("option"); option.value = value; option.textContent = label; return option;
    }));
    let saved;
    try { saved = root.localStorage?.getItem(`quietleaf.library.${key}`); } catch (_) { }
    select.value = sorts.some(([value]) => value === saved) ? saved : "last_read_desc";
    select.addEventListener("change", () => {
      try { root.localStorage?.setItem(`quietleaf.library.${key}`, select.value); } catch (_) { }
      return changed();
    });
  }
  const dates = (book) => `Added ${book.addedAt ? new Date(book.addedAt).toLocaleString() : "unknown"} · Last read ${book.lastReadAt ? new Date(book.lastReadAt).toLocaleString() : "never"}`;
  root.QuietLeafLibrary = { setupSort, dates };
})(window);
