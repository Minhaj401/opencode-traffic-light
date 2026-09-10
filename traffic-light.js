// traffic-light — opencode plugin.
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
import { randomUUID } from "node:crypto";
import { writeFileSync } from "node:fs";

const PORT = 4390;
const COLORS = new Set(["green", "yellow", "red"]);
// Match whole tool-name tokens: "task" contains "ask" but is not an input prompt.
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
  // Subtask hooks can use the part ID instead of callID. Merge both identities
  // so duplicate hooks/part updates cannot count one task twice or strand it.
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

function ensureServer() {
  if (serving) return true;
  try {
    Bun.serve({
      port: PORT,
      hostname: "127.0.0.1",
      async fetch(req) {
        const url = new URL(req.url);
        if (req.method === "POST" && ["/event", "/heartbeat"].includes(url.pathname)) {
          try {
            const b = await req.json();
            if (url.pathname === "/event") {
              if (!b || typeof b.sid !== "string" || !b.sid || !COLORS.has(b.state)) {
                return new Response("bad", { status: 400 });
              }
              legacyStates.set(b.sid, b.state);
            } else {
              if (!b || typeof b.processId !== "string" || !b.processId ||
                  !b.sessions || typeof b.sessions !== "object" || Array.isArray(b.sessions)) {
                return new Response("bad", { status: 400 });
              }
              const entries = Object.entries(b.sessions);
              if (entries.some(([sid, color]) => !sid || !COLORS.has(color))) {
                return new Response("bad", { status: 400 });
              }
              if (b.processId !== processId) {
                peers.set(b.processId, { at: Date.now(), states: new Map(entries) });
              }
            }
            return Response.json({ ok: true });
          } catch {
            return new Response("bad", { status: 400 });
          }
        }
        return Response.json({ state: aggregate() });
      },
    }).unref();
    serving = true;
    return true;
  } catch {
    return false; // port taken: forward mode
  }
}

async function heartbeat() {
  if (inFlight) return;
  inFlight = true;
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
  }
}

function set(sid, color) {
  localStates.set(sid || "global", color);
  void heartbeat();
}

function sidOf(event) {
  const p = event.properties || {};
  return p.sessionID || p.sessionId || p.session?.id || p.info?.sessionID || p.part?.sessionID ||
    (event.type.startsWith("session.") && p.info?.id) || "global";
}

function statusColor(s) {
  if (s && typeof s === "object") s = s.type;
  s = String(s || "").toLowerCase();
  if (/wait|permission|ask|approval|blocked/.test(s)) return "red";
  if (/busy|work|run|active|processing|retry/.test(s)) return "yellow";
  if (/idle|done|complete|finish|compact/.test(s)) return "green";
  return null;
}

export const TrafficLightPlugin = async (ctx) => {
  if (!initialized) {
    initialized = true;
    void heartbeat();
    setInterval(heartbeat, 1000).unref();
    try {
      writeFileSync(
        "/tmp/traffic-light-plugin.loaded",
        JSON.stringify({ at: new Date().toISOString(), serving }) + "\n"
      );
    } catch {}
    try {
      if (ctx?.client) void Promise.resolve(ctx.client.app.log({
        body: {
          service: "traffic-light",
          level: "info",
          message: serving
            ? "serving state at http://127.0.0.1:4390/status"
            : "port taken, forwarding snapshots to state server",
        },
      })).catch(() => {});
    } catch {}
  }
  return {
    event: async ({ event }) => {
      if (!event || !event.type) return;
      const sid = sidOf(event);
      switch (event.type) {
        case "permission.asked":
          set(sid, "red");
          break;
        case "permission.replied":
          set(sid, "yellow");
          break;
        case "session.idle":
        case "session.created":
          set(sid, "green");
          break;
        case "session.error":
          activeTasks.delete(sid);
          set(sid, "green");
          break;
        case "session.deleted":
          activeTasks.delete(sid);
          localStates.delete(sid);
          void heartbeat();
          break;
        case "session.status": {
          const c = statusColor(event.properties && event.properties.status);
          if (c) set(sid, c);
          break;
        }
        case "message.updated": {
          const p = event.properties || {};
          const role = p.role || (p.info && p.info.role);
          if (role === "assistant") set(sid, "yellow");
          break;
        }
        case "message.part.updated": {
          const part = event.properties?.part;
          if (part?.type !== "tool" || part.tool !== "task") break;
          const status = part.state?.status;
          if (!["pending", "running", "completed", "error"].includes(status)) break;
          trackTask(sid, [part.callID, part.id], status === "pending" || status === "running");
          void heartbeat();
          break;
        }
        case "message.part.removed":
          trackTask(sid, [event.properties?.partID], false);
          void heartbeat();
          break;
      }
    },
    "tool.execute.before": async (input) => {
      const sid =
        (input && (input.sessionID || input.sessionId)) || "global";
      const tool = (input && input.tool) || "";
      if (tool === "task") trackTask(sid, [input?.callID], true);
      set(sid, INPUT_TOOL.test(tool) ? "red" : "yellow");
    },
    "tool.execute.after": async (input) => {
      const sid =
        (input && (input.sessionID || input.sessionId)) || "global";
      const tool = (input && input.tool) || "";
      if (tool === "task") {
        trackTask(sid, [input?.callID], false);
        void heartbeat();
      }
      // answering a question resumes work
      if (INPUT_TOOL.test(tool)) set(sid, "yellow");
    },
  };
};

export default TrafficLightPlugin;
