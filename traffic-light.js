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
// find the port taken and forward their events to it via POST /event,
// so multiple TUIs still drive one light.
const PORT = 4390;

const states = new Map(); // sid -> color

function aggregate() {
  let s = "green";
  for (const c of states.values()) {
    if (c === "red") return "red";
    if (c === "yellow") s = "yellow";
  }
  return s;
}

async function forward(sid, color) {
  try {
    await fetch(`http://127.0.0.1:${PORT}/event`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ sid, state: color }),
    });
  } catch {
    // server gone mid-flight; next event retries the serve
  }
}

let serving = false;
function ensureServer() {
  if (serving) return true;
  try {
    Bun.serve({
      port: PORT,
      hostname: "127.0.0.1",
      fetch(req) {
        const url = new URL(req.url);
        if (req.method === "POST" && url.pathname === "/event") {
          return req
            .json()
            .then((b) => {
              if (b && b.sid && b.state) states.set(b.sid, b.state);
              return Response.json({ ok: true });
            })
            .catch(() => new Response("bad", { status: 400 }));
        }
        return Response.json({ state: aggregate() });
      },
    });
    serving = true;
    return true;
  } catch {
    return false; // port taken: forward mode
  }
}

async function set(sid, color) {
  states.set(sid || "global", color);
  if (!ensureServer()) await forward(sid || "global", color);
}

function sidOf(event) {
  const p = event.properties || {};
  return p.sessionID || p.sessionId || (p.session && p.session.id) || "global";
}

function statusColor(s) {
  s = String(s || "").toLowerCase();
  if (/wait|permission|ask|approval|blocked/.test(s)) return "red";
  if (/busy|work|run|active|processing/.test(s)) return "yellow";
  if (/idle|done|complete|finish|compact/.test(s)) return "green";
  return null;
}

export const TrafficLightPlugin = async (ctx) => {
  try {
    const fs = await import("node:fs");
    const serving = ensureServer();
    fs.writeFileSync(
      "/tmp/traffic-light-plugin.loaded",
      JSON.stringify({ at: new Date().toISOString(), serving }) + "\n"
    );
    if (ctx && ctx.client) {
      await ctx.client.app.log({
        body: {
          service: "traffic-light",
          level: "info",
          message: serving
            ? "serving state at http://127.0.0.1:4390/status"
            : "port taken, forwarding events to state server",
        },
      });
    }
  } catch {}
  return {
    event: async ({ event }) => {
      if (!event || !event.type) return;
      const sid = sidOf(event);
      switch (event.type) {
        case "permission.asked":
          await set(sid, "red");
          break;
        case "permission.replied":
          await set(sid, "yellow");
          break;
        case "session.idle":
        case "session.created":
        case "session.error":
          await set(sid, "green");
          break;
        case "session.deleted":
          states.delete(sid);
          break;
        case "session.status": {
          const c = statusColor(event.properties && event.properties.status);
          if (c) await set(sid, c);
          break;
        }
        case "message.updated": {
          const p = event.properties || {};
          const role = p.role || (p.info && p.info.role);
          if (role === "assistant") await set(sid, "yellow");
          break;
        }
      }
    },
    "tool.execute.before": async (input) => {
      const sid =
        (input && (input.sessionID || input.sessionId)) || "global";
      // the question tool blocks for user input: any question is red
      const tool = (input && input.tool) || "";
      await set(sid, /question|ask|input|prompt|confirm/i.test(tool) ? "red" : "yellow");
    },
    "tool.execute.after": async (input) => {
      const sid =
        (input && (input.sessionID || input.sessionId)) || "global";
      const tool = (input && input.tool) || "";
      // answering a question resumes work
      if (/question|ask|input|prompt|confirm/i.test(tool)) await set(sid, "yellow");
    },
  };
};

export default TrafficLightPlugin;
