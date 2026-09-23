// traffic-light — opencode plugin (V2 API).
//
// Aggregates session states and serves them at
// http://127.0.0.1:4390/status as {"state": "green"|"yellow"|"red"}
// for the desktop traffic-light widget:
//   green  = everything idle / finished
//   yellow = something working
//   red    = an approval is waiting
//
// Port sharing: the first opencode process owns the server; later ones
// publish process snapshots via POST /heartbeat and retry ownership each second.
//
// V2 notes (opencode 2.x):
// - Default export is a plain { id, setup } definition; no @opencode/plugin
//   import needed. V1 function-returning-hooks plugins do not load in V2.
// - Tool lifecycle arrives via ctx.tool.hook("execute.before"/"execute.after")
//   with { tool, sessionID, id, input, status }.
// - Permission prompts arrive via ctx.permission.hook("evaluate") with
//   { sessionID, action, effect }; effect === "ask" means waiting on the user.
// - Session lifecycle arrives via ctx.event.subscribe() with
//   { type, data }: session.step.started/ended, session.tool.called/success/
//   failed, session.execution.succeeded/interrupted. V1 names such as
//   session.idle, session.created, permission.asked and message.part.updated
//   are not emitted in V2.
import http from "node:http";
import { execFile } from "node:child_process";
import { randomUUID } from "node:crypto";
import { writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const PORT = 4390;
const COLORS = new Set(["green", "yellow", "red"]);
// Snapshot tick: peers publish full state this often; the widget polls every
// 400 ms, so end-to-end lag stays well under a second.
const HEARTBEAT_MS = 250;
// Match whole tool-name tokens: "task" contains "ask" but is not an input prompt.
// V2 renamed the task tool to "subagent"; track both names.
const TASK_TOOLS = new Set(["task", "subagent"]);
const INPUT_TOOL = /(?:^|[._:/-])(?:question|ask|input|prompt|confirm)(?:$|[._:/-])/i;
const processId = randomUUID();
const localStates = new Map();
const activeTasks = new Map();
const peers = new Map();
// Published /event clients have no process identity or heartbeat lifetime.
const legacyStates = new Map();

let initialized = false;
let serving = false;
let inFlight = false;
let queued = false;
let server = null;

function expirePeers() {
  const now = Date.now();
  for (const [id, peer] of peers) {
    if (now - peer.at >= 4000) peers.delete(id);
  }
}

function localSnapshot() {
  const states = new Map(localStates);
  for (const sid of activeTasks.keys()) {
    if (states.get(sid) !== "red") states.set(sid, "yellow");
  }
  return states;
}

function trackTask(sid, ids, running) {
  ids = ids.filter((id) => typeof id === "string" && id);
  if (!ids.length) return;
  let calls = activeTasks.get(sid);
  if (!calls) {
    if (!running) return;
    calls = new Map();
    activeTasks.set(sid, calls);
  }
  // Subagent hooks can report either the call ID or a part ID. Merge both
  // identities so duplicate deliveries cannot count one task twice or strand it.
  const aliases = new Set(ids);
  for (const [key, known] of calls) {
    if (ids.some((id) => known.has(id))) {
      for (const id of known) aliases.add(id);
      calls.delete(key);
    }
  }
  if (running) calls.set(ids[0], aliases);
  if (!calls.size) activeTasks.delete(sid);
}

function aggregate() {
  expirePeers();
  const snapshots = [localSnapshot(), legacyStates];
  for (const peer of peers.values()) snapshots.push(peer.states);
  let s = "green";
  for (const states of snapshots) {
    for (const c of states.values()) {
      if (c === "red") return "red";
      if (c === "yellow") s = "yellow";
    }
  }
  return s;
}

function handleBody(pathname, b) {
  if (pathname === "/event") {
    if (!b || typeof b.sid !== "string" || !b.sid || !COLORS.has(b.state)) {
      return { status: 400 };
    }
    legacyStates.set(b.sid, b.state);
  } else {
    if (!b || typeof b.processId !== "string" || !b.processId ||
        !b.sessions || typeof b.sessions !== "object" || Array.isArray(b.sessions)) {
      return { status: 400 };
    }
    const entries = Object.entries(b.sessions);
    if (entries.some(([sid, color]) => !sid || !COLORS.has(color))) {
      return { status: 400 };
    }
    if (b.processId !== processId) {
      peers.set(b.processId, { at: Date.now(), states: new Map(entries) });
    }
  }
  return { status: 200, json: { ok: true } };
}

function ensureServer() {
  if (serving) return true;
  try {
    server = http.createServer((req, res) => {
      const pathname = new URL(req.url || "/", "http://127.0.0.1").pathname;
      if (pathname === "/debug" && req.method === "GET") {
        expirePeers();
        const dump = (m) => Object.fromEntries(m);
        const peersDump = Object.fromEntries(
          [...peers.entries()].map(([id, p]) => [id, { ageMs: Date.now() - p.at, states: dump(p.states) }])
        );
        res.statusCode = 200;
        res.setHeader("content-type", "application/json");
        res.end(JSON.stringify({
          pid: process.pid, serving, processId,
          local: dump(localStates),
          tasks: [...activeTasks.keys()],
          legacy: dump(legacyStates),
          peers: peersDump,
          aggregate: aggregate(),
        }));
        return;
      }
      if (req.method === "POST" && (pathname === "/event" || pathname === "/heartbeat")) {        let raw = "";
        req.on("data", (chunk) => { raw += chunk; });
        req.on("end", () => {
          let parsed;
          try {
            parsed = JSON.parse(raw);
          } catch {
            parsed = undefined;
          }
          const out = handleBody(pathname, parsed);
          res.statusCode = out.status;
          res.setHeader("content-type", "application/json");
          res.end(JSON.stringify(out.json || { error: "bad" }));
        });
        return;
      }
      res.statusCode = 200;
      res.setHeader("content-type", "application/json");
      res.end(JSON.stringify({ state: aggregate() }));
    });
    server.on("error", () => {});
    server.listen(PORT, "127.0.0.1");
    if (typeof server.unref === "function") server.unref();
    serving = true;
    return true;
  } catch {
    return false; // port taken: forward mode
  }
}

async function heartbeat() {
  if (inFlight) return;
  inFlight = true;
  queued = false;
  try {
    expirePeers();
    if (ensureServer()) return;
    const response = await fetch(`http://127.0.0.1:${PORT}/heartbeat`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ processId, sessions: Object.fromEntries(localSnapshot()) }),
      signal: AbortSignal.timeout(800),
    });
    await response.body?.cancel();
  } catch {
    // The next tick retries ownership and publishes the latest local snapshot.
  } finally {
    inFlight = false;
    if (queued) {
      queued = false;
      void heartbeat();
    }
  }
}

function set(sid, color) {
  localStates.set(sid || "global", color);
  // A change landed: retry once any in-flight snapshot finishes instead of
  // waiting for the next tick.
  queued = true;
  void heartbeat();
}

// Step/tool completion must not downgrade a working or waiting session.
// Green is set only when the full execution finishes; otherwise a busy agent
// loop flickers yellow/green on every step boundary.
function holdWork(sid) {
  sid = sid || "global";
  if (localStates.get(sid) === "red") return;
  if (activeTasks.has(sid) || localStates.get(sid) === "yellow") {
    set(sid, "yellow");
    return;
  }
}

function onEvent(ev) {
  if (!ev || typeof ev.type !== "string") return;
  const data = ev.data || {};
  const sid = data.sessionID || data.sessionId || "global";
  switch (ev.type) {
    case "session.step.started":
      set(sid, "yellow");
      break;
    case "session.step.ended":
      holdWork(sid);
      break;
    case "session.tool.called":
      trackTask(sid, [data.id], true);
      set(sid, "yellow");
      break;
    case "session.tool.success":
    case "session.tool.failed":
      trackTask(sid, [data.id], false);
      void heartbeat();
      break;
    case "session.execution.succeeded":
    case "session.execution.interrupted":
    case "session.error":
      activeTasks.delete(sid);
      set(sid, "green");
      break;
    case "session.deleted":
      activeTasks.delete(sid);
      localStates.delete(sid);
      void heartbeat();
      break;
    default:
      break;
  }
}

async function setup(ctx) {
  if (!initialized) {
    initialized = true;
    void heartbeat();
    try {
      const timer = setInterval(heartbeat, HEARTBEAT_MS);
      if (timer && typeof timer.unref === "function") timer.unref();
    } catch {}
    if (process.platform === "darwin" || process.platform === "linux") {
      try {
        const child = execFile(
          join(homedir(), ".local", "bin", "traffic-light"),
          ["autostart"],
          { timeout: 3000 },
          () => {}
        );
        if (child && typeof child.unref === "function") child.unref();
        for (const stream of [child.stdin, child.stdout, child.stderr]) {
          try { stream?.unref?.(); } catch {}
        }
      } catch {}
    }
    try {
      writeFileSync(
        "/tmp/traffic-light-plugin.loaded",
        JSON.stringify({ at: new Date().toISOString(), serving, v2: true }) + "\n"
      );
    } catch {}
    try {
      console.info("[traffic-light] serving state at http://127.0.0.1:4390/status");
    } catch {}
  }

  await ctx.tool.hook("execute.before", (e) => {
    const sid = (e && (e.sessionID || e.sessionId)) || "global";
    const tool = (e && e.tool) || "";
    if (TASK_TOOLS.has(tool)) trackTask(sid, [e?.id], true);
    set(sid, INPUT_TOOL.test(tool) ? "red" : "yellow");
  });

  await ctx.tool.hook("execute.after", (e) => {
    const sid = (e && (e.sessionID || e.sessionId)) || "global";
    const tool = (e && e.tool) || "";
    if (TASK_TOOLS.has(tool)) {
      trackTask(sid, [e?.id], false);
      void heartbeat();
    }
    // answering a question resumes work
    if (INPUT_TOOL.test(tool)) set(sid, "yellow");
  });

  await ctx.permission.hook("evaluate", (e) => {
    if (!e) return;
    if (e.effect === "ask") {
      set((e.sessionID || e.sessionId) || "global", "red");
    }
  });

  const controller = new AbortController();
  void (async () => {
    try {
      for await (const ev of ctx.event.subscribe({ signal: controller.signal })) {
        try {
          onEvent(ev);
        } catch {}
      }
    } catch {}
  })();

  return () => controller.abort();
}

export default { id: "traffic-light", setup };
export { setup, onEvent, aggregate, localStates, activeTasks };
