const test = require("node:test");
const assert = require("node:assert/strict");
const { ProgressClient } = require("../quietleaf_service/web/progress.js");

function fixture(transport) {
  const data = new Map(); let event = 0;
  const storage = { getItem: (key) => data.get(key) || null, setItem: (key, value) => data.set(key, value) };
  return { storage, client: new ProgressClient(storage, transport, "browser-client", () => `event-${++event}`) };
}

test("offline progress retains the exact operation across a restart/lost acknowledgement", async () => {
  const requests = []; let fail = true;
  const transport = async (_key, pending) => {
    requests.push({ ...pending });
    if (fail) throw new Error("network lost after server accepted");
    return { progress: { ...pending, revision: 1 } };
  };
  const { client, storage } = fixture(transport);
  client.open("fixture/book", null);
  client.stage("fixture/book", { chapterId: "two", fraction: 0.4 });
  await assert.rejects(client.flush("fixture/book"));
  fail = false;
  const restarted = new ProgressClient(storage, transport, "browser-client", () => "different-event");
  restarted.open("fixture/book", { ...requests[0], revision: 1 });
  await restarted.flush("fixture/book");
  assert.deepEqual(requests[0], requests[1]);
  assert.equal(restarted.load("fixture/book").revision, 1);
  assert.equal(restarted.load("fixture/book").pending, null);
});

test("stale offline edits produce a choice instead of overwriting a newer service position", async () => {
  const { client } = fixture(async () => ({ conflict: true, progress: { chapterId: "three", fraction: 0.7, revision: 3 } }));
  client.open("fixture/book", { chapterId: "one", fraction: 0.1, revision: 1 });
  client.stage("fixture/book", { chapterId: "two", fraction: 0.4 });
  await client.flush("fixture/book");
  const state = client.load("fixture/book");
  assert.equal(state.draft.chapterId, "two");
  assert.equal(state.conflict.progress.chapterId, "three");
  client.resolve("fixture/book", state.conflict.progress, false);
  assert.equal(client.load("fixture/book").draft.chapterId, "three");
});

test("fresh local edits while a request is in flight are rebased on its acknowledgement", async () => {
  let client; const seen = [];
  const setup = fixture(async (_key, pending) => {
    seen.push({ ...pending });
    if (seen.length === 1) client.stage("fixture/book", { chapterId: "two", fraction: 0.9 });
    return { progress: { ...pending, revision: seen.length } };
  });
  client = setup.client;
  client.open("fixture/book", null);
  client.stage("fixture/book", { chapterId: "one", fraction: 0.5 });
  await client.flush("fixture/book");
  assert.equal(seen.length, 2);
  assert.equal(seen[1].baseRevision, 1);
  assert.equal(seen[1].chapterId, "two");
});

test("late GET responses cannot roll back an acknowledged revision", async () => {
  const { client } = fixture(async (_key, pending) => ({ progress: { ...pending, revision: 4 } }));
  client.open("fixture/book", { chapterId: "one", fraction: 0, revision: 3 });
  client.stage("fixture/book", { chapterId: "two", fraction: 0.5 });
  await client.flush("fixture/book");
  assert.equal(client.open("fixture/book", { chapterId: "one", fraction: 0, revision: 3 }).revision, 4);
});

test("scroll-layout rounding noise does not become an edit or block a newer service resume", async () => {
  let writes = 0;
  const { client } = fixture(async () => { writes++; throw new Error("No write should be needed"); });
  client.open("fixture/book", { chapterId: "two", fraction: 0.6, revision: 2 });
  client.stage("fixture/book", { chapterId: "two", fraction: 0.599999985 });
  await client.flush("fixture/book");
  assert.equal(writes, 0);
  const resumed = client.open("fixture/book", { chapterId: "one", fraction: 0.8, revision: 3 });
  assert.equal(resumed.conflict, null);
  assert.equal(resumed.draft.chapterId, "one");
});
