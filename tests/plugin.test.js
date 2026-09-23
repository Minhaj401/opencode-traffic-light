import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import test from "node:test";
import { createContext, SourceTextModule, SyntheticModule } from "node:vm";

const source = await readFile(new URL("../traffic-light.js", import.meta.url), "utf8");
const flush = () => new Promise((resolve) => setImmediate(resolve));

function harness() {
  const world = { now: 0, owner: null, processes: [], timers: [], actions: [], widgetRunning: false };
  const handle = () => ({ unrefs: 0, unref() { this.unrefs++; return this; } });

  function timer(process, fn, delay, interval = false) {
    const entry = { ...handle(), process, fn, delay, interval, at: world.now + delay, active: true };
    world.timers.push(entry);
    return entry;
  }

  world.advance = async (ms) => {
    const end = world.now + ms;
    while (true) {
      const next = world.timers.filter((t) => t.active && t.at <= end)
        .sort((a, b) => a.at - b.at)[0];
      if (!next) break;
      world.now = next.at;
      if (next.interval) next.at += next.delay;
      else next.active = false;
      next.fn();
      await flush();
    }
    world.now = end;
    await flush();
  };

  world.stop = (process) => {
    for (const t of world.timers) if (t.process === process) t.active = false;
    if (world.owner === process) world.owner = null;
  };

  world.request = async (path = "/status", body) => {
    assert.ok(world.owner, "a simulated process owns the port");
    const handler = world.owner.server.handler;
    return new Promise((resolve) => {
      const listeners = {};
      const req = {
        url: path,
        method: body === undefined ? "GET" : "POST",
        on(name, fn) { (listeners[name] ||= []).push(fn); return this; },
      };
      const chunks = [];
      const res = {
        statusCode: 200,
        headers: {},
        setHeader(k, v) { this.headers[k] = v; },
        end(data) {
          const text = String(data || "");
          resolve({
            status: this.statusCode,
            headers: { get: (k) => this.headers[k.toLowerCase()] },
            json: async () => JSON.parse(text),
          });
        },
      };
      handler(req, res);
      if (body !== undefined) {
        const text = typeof body === "string" ? body : JSON.stringify(body);
        for (const fn of listeners["data"] || []) fn(text);
      }
      for (const fn of listeners["end"] || []) fn();
    });
  };
  world.state = async () => (await (await world.request()).json()).state;

  world.emit = async (p, ev) => {
    if (p.emitWaiters.length) p.emitWaiters.shift()({ value: ev, done: false });
    else p.emitQueue.push(ev);
    await flush();
    await flush();
  };

  world.load = async (options = {}) => {
    const p = {
      id: `process-${world.processes.length + 1}`,
      execs: [], requests: [], writes: [], output: [], timeouts: [], children: [],
      hooks: {}, emitQueue: [], emitWaiters: [],
      fetchMode: null, serveError: false,
    };
    world.processes.push(p);
    const mocks = {
      "node:http": {
        default: {
          createServer(handler) {
            const srv = { ...handle(), handler };
            srv.on = () => srv;
            srv.listen = (port, host, cb) => {
              assert.equal(host, "127.0.0.1");
              assert.equal(port, 4390);
              world.actions.push(`serve:${p.id}`);
              if (world.owner || p.serveError || options.serveError) throw new Error("EADDRINUSE");
              p.server = srv;
              world.owner = p;
              if (typeof cb === "function") cb();
              return srv;
            };
            return srv;
          },
        },
      },
      "node:child_process": {
        execFile(file, args, execOptions, callback) {
          world.actions.push(`exec:${p.id}`);
          const call = { file, args: [...args], options: { ...execOptions }, completed: false };
          p.execs.push(call);
          if (options.cliThrows) throw new Error("spawn failed");
          const child = { ...handle(), stdin: handle(), stdout: handle(), stderr: handle() };
          p.children.push(child);
          const finish = (error) => {
            call.completed = true;
            call.error = error;
            callback(error, "ignored output", "ignored error output");
          };
          if (options.cliHangs) {
            timer(p, () => finish(Object.assign(new Error("timeout"), { killed: true })), execOptions.timeout);
          } else {
            if (!options.cliError && !options.disabled) world.widgetRunning = true;
            queueMicrotask(() => finish(options.cliError ? new Error("ENOENT") : null));
          }
          return child;
        },
      },
      "node:crypto": { randomUUID: () => p.id },
      "node:fs": {
        writeFileSync(path, content) {
          p.writes.push({ path, content });
          if (options.writeError) throw new Error("read-only filesystem");
        },
      },
      "node:os": { homedir: () => "/fake home/$(not-a-shell)" },
      "node:path": { join },
    };
    const context = createContext({
      URL, Request, Response, AbortController,
      process: { platform: options.platform || "darwin" },
      Date: class extends Date {
        constructor(...args) { super(...(args.length ? args : [world.now])); }
        static now() { return world.now; }
      },
      console: {
        log(...args) { p.output.push(args); },
        info(...args) { p.output.push(args); },
        warn(...args) { p.output.push(args); },
        error(...args) { p.output.push(args); },
      },
      setInterval: (fn, delay) => timer(p, fn, delay, true),
      AbortSignal: {
        timeout(ms) {
          const controller = new AbortController();
          p.timeouts.push(ms);
          timer(p, () => controller.abort(new Error("request timeout")), ms).unref();
          return controller.signal;
        },
      },
      fetch(url, requestOptions) {
        assert.equal(url, "http://127.0.0.1:4390/heartbeat");
        assert.equal(requestOptions.method, "POST");
        assert.equal(requestOptions.headers["content-type"], "application/json");
        const call = { body: JSON.parse(requestOptions.body), signal: requestOptions.signal };
        p.requests.push(call);
        if (p.fetchMode === "hang") {
          return new Promise((resolve, reject) => {
            call.resolve = resolve;
            requestOptions.signal.addEventListener("abort", () => reject(requestOptions.signal.reason), { once: true });
          });
        }
        if (p.fetchMode === "throw") throw new Error("network failure");
        if (p.fetchMode === "reject") return Promise.reject(new Error("network failure"));
        if (!world.owner) return Promise.reject(new Error("ECONNREFUSED"));
        return (async () => {
          const response = await world.request("/heartbeat", call.body);
          return { body: { cancel: async () => {} }, status: response.status };
        })();
      },
    });
    const module = new SourceTextModule(source, { context });
    await module.link((specifier) => {
      assert.ok(Object.hasOwn(mocks, specifier), `unexpected import: ${specifier}`);
      return new SyntheticModule(Object.keys(mocks[specifier]), function () {
        for (const [key, value] of Object.entries(mocks[specifier])) this.setExport(key, value);
      }, { context });
    });
    await module.evaluate();
    const def = module.namespace.default;
    assert.equal(def.id, "traffic-light");
    assert.equal(typeof def.setup, "function");
    const ctx = {
      tool: { hook: async (name, fn) => { p.hooks[`tool.${name}`] = fn; } },
      permission: { hook: async (name, fn) => { p.hooks[`permission.${name}`] = fn; } },
      event: {
        subscribe: () => ({
          [Symbol.asyncIterator]() { return this; },
          next: () => new Promise((resolve) => {
            if (p.emitQueue.length) resolve({ value: p.emitQueue.shift(), done: false });
            else p.emitWaiters.push(resolve);
          }),
        }),
      },
    };
    p.init = () => def.setup(ctx);
    p.cleanup = await p.init();
    p.before = (tool, extra = {}) => p.hooks["tool.execute.before"]({ tool, ...extra });
    p.after = (tool, extra = {}) => p.hooks["tool.execute.after"]({ tool, ...extra });
    p.permit = (effect, extra = {}) => p.hooks["permission.evaluate"]({ effect, ...extra });
    p.event = (type, data = {}) => world.emit(p, { type, data });
    await flush();
    return p;
  };
  return world;
}

test("startup binds loopback before one absolute-path autostart and unrefs handles", async () => {
  const w = harness();
  const p = await w.load();
  assert.deepEqual(w.actions, [`serve:${p.id}`, `exec:${p.id}`]);
  assert.equal(p.execs.length, 1);
  assert.equal(p.execs[0].file, "/fake home/$(not-a-shell)/.local/bin/traffic-light");
  assert.deepEqual(p.execs[0].args, ["autostart"]);
  assert.deepEqual(p.execs[0].options, { timeout: 3000 });
  assert.equal(p.server.unrefs, 1);
  const [interval] = w.timers.filter((t) => t.interval);
  assert.equal(interval.delay, 250);
  assert.equal(interval.unrefs, 1);
  const [child] = p.children;
  for (const h of [child, child.stdin, child.stdout, child.stderr]) assert.equal(h.unrefs, 1);
  assert.equal(p.writes.length, 1);
  assert.equal(p.writes[0].path, "/tmp/traffic-light-plugin.loaded");
  assert.deepEqual(JSON.parse(p.writes[0].content), { at: "1970-01-01T00:00:00.000Z", serving: true, v2: true });
  const response = await w.request();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type"), /application\/json/);
  assert.deepEqual(await response.json(), { state: "green" });
});

test("debug endpoint exposes local state for troubleshooting", async () => {
  const w = harness();
  const p = await w.load();
  await p.before("subagent", { sessionID: "a", id: "call-1" });
  const debug = await (await w.request("/debug")).json();
  assert.equal(debug.serving, true);
  assert.deepEqual(debug.local, { a: "yellow" });
  assert.deepEqual(debug.tasks, ["a"]);
  assert.deepEqual(debug.legacy, {});
  assert.equal(debug.aggregate, "green".replace("green", "yellow"));
});

test("duplicate initialization and ordinary activity never reopen a manually closed widget", async () => {
  const w = harness();
  const p = await w.load();
  w.widgetRunning = false;
  await p.init();
  await p.init();
  await p.before("question", { sessionID: "a" });
  await p.after("question", { sessionID: "a" });
  await p.event("session.execution.succeeded", { sessionID: "a" });
  await p.event("session.deleted", { sessionID: "a" });
  await w.advance(6000);
  assert.equal(p.execs.length, 1);
  assert.equal(w.timers.filter((t) => t.interval).length, 1);
  assert.equal(p.writes.length, 1);
  assert.equal(w.widgetRunning, false);
  const newProcess = await w.load();
  assert.equal(newProcess.execs.length, 1);
  assert.equal(w.widgetRunning, true);
});

test("missing CLI, spawn errors and marker failures are quiet", async () => {
  for (const options of [
    { cliError: true }, { cliThrows: true }, { writeError: true }, { disabled: true },
  ]) {
    const w = harness();
    const p = await w.load(options);
    await p.permit("ask", { sessionID: "a" });
    await w.advance(1000);
    assert.equal(await w.state(), "red");
    assert.equal(p.execs.length, 1);
    assert.deepEqual(p.output, [["[traffic-light] serving state at http://127.0.0.1:4390/status"]]);
    if (options.disabled) assert.equal(w.widgetRunning, false);
  }
});

test("hung startup does not block initialization or hooks; CLI timeout is 3000ms", { timeout: 2000 }, async () => {
  const w = harness();
  const p = await w.load({ cliHangs: true });
  assert.equal(p.execs[0].completed, false);
  await p.before("bash");
  assert.equal(await w.state(), "yellow");
  await w.advance(2999);
  assert.equal(p.execs[0].completed, false);
  await w.advance(1);
  assert.equal(p.execs[0].error.killed, true);
  await p.init();
  assert.equal(p.execs.length, 1);
  assert.deepEqual(p.output, [["[traffic-light] serving state at http://127.0.0.1:4390/status"]]);
});

test("Linux requests autostart; unsupported platforms skip CLI without losing status", async () => {
  for (const platform of ["linux", "win32", "freebsd"]) {
    const w = harness();
    const p = await w.load({ platform });
    assert.equal(p.execs.length, platform === "linux" ? 1 : 0);
    assert.equal(await w.state(), "green");
  }
});

test("hooks retain red > yellow > green priority and session transitions", async () => {
  const w = harness();
  const p = await w.load();
  await p.event("session.step.started", { sessionID: "a" });
  assert.equal(await w.state(), "yellow");
  await p.before("bash", { sessionID: "a" });
  assert.equal(await w.state(), "yellow");
  await p.permit("ask", { sessionID: "b" });
  await p.event("session.step.ended", { sessionID: "a" });
  assert.equal(await w.state(), "red");
  await p.permit("allow", { sessionID: "b" });
  await p.before("bash", { sessionID: "b" });
  assert.equal(await w.state(), "yellow");
  await p.event("session.execution.succeeded", { sessionID: "b" });
  assert.equal(await w.state(), "yellow");
  await p.event("session.execution.succeeded", { sessionID: "a" });
  assert.equal(await w.state(), "green");
  for (const tool of ["question", "ask", "input", "prompt", "CONFIRM"]) {
    await p.before(tool, { sessionID: "a" });
    assert.equal(await w.state(), "red");
    await p.after(tool, { sessionID: "a" });
    assert.equal(await w.state(), "yellow");
  }
  await p.event("session.execution.interrupted", { sessionID: "a" });
  assert.equal(await w.state(), "green");
  await p.before(undefined);
  assert.equal(await w.state(), "yellow");
  await p.event("session.deleted");
  assert.equal(await w.state(), "green");
});

test("running subagent tasks stay yellow after parent step ends until every call finishes", async () => {
  const w = harness();
  const p = await w.load();
  const first = { sessionID: "parent", tool: "subagent", id: "first" };
  const second = { sessionID: "parent", tool: "subagent", id: "second" };
  await p.hooks["tool.execute.before"](first);
  assert.equal(await w.state(), "yellow"); // "subagent" keeps working state
  await p.hooks["tool.execute.before"](first); // Duplicate delivery is not another task.
  await p.hooks["tool.execute.before"](second);
  await p.event("session.step.ended", { sessionID: "parent" });
  assert.equal(await w.state(), "yellow");
  await p.hooks["tool.execute.after"](second);
  assert.equal(await w.state(), "yellow");
  await p.hooks["tool.execute.after"](first);
  await p.event("session.step.ended", { sessionID: "parent" });
  assert.equal(await w.state(), "yellow");
  await p.event("session.execution.succeeded", { sessionID: "parent" });
  assert.equal(await w.state(), "green");
  await p.hooks["tool.execute.after"](first);
  assert.equal(await w.state(), "green");
});

test("legacy task tool name is tracked the same as subagent", async () => {
  const w = harness();
  const p = await w.load();
  await p.hooks["tool.execute.before"]({ sessionID: "parent", tool: "task", id: "call-id" });
  await p.event("session.step.ended", { sessionID: "parent" });
  assert.equal(await w.state(), "yellow");
  await p.hooks["tool.execute.after"]({ sessionID: "parent", tool: "task", id: "call-id" });
  await p.event("session.step.ended", { sessionID: "parent" });
  assert.equal(await w.state(), "yellow");
  await p.event("session.execution.succeeded", { sessionID: "parent" });
  assert.equal(await w.state(), "green");
});

test("session tool events reconcile call IDs and clear failures", async () => {
  const w = harness();
  const p = await w.load();
  await p.event("session.tool.called", { sessionID: "parent", id: "call-id" });
  await p.event("session.step.ended", { sessionID: "parent" });
  assert.equal(await w.state(), "yellow");
  await p.event("session.tool.success", { sessionID: "parent", id: "call-id" });
  await p.event("session.step.ended", { sessionID: "parent" });
  assert.equal(await w.state(), "yellow");
  await p.event("session.execution.succeeded", { sessionID: "parent" });
  assert.equal(await w.state(), "green");
  await p.event("session.tool.called", { sessionID: "parent", id: "call-id" });
  assert.equal(await w.state(), "yellow");
  await p.event("session.tool.failed", { sessionID: "parent", id: "call-id" });
  await p.event("session.step.ended", { sessionID: "parent" });
  assert.equal(await w.state(), "yellow");
  await p.event("session.execution.succeeded", { sessionID: "parent" });
  assert.equal(await w.state(), "green");
});

test("input-tool tokens exclude subagent and unrelated substrings but retain namespaced prompts", async () => {
  const w = harness();
  const p = await w.load();
  for (const tool of ["subagent", "task", "mask", "askpass", "confirmation", "inputstream"]) {
    await p.before(tool, { sessionID: "parent", id: tool });
    assert.equal(await w.state(), "yellow", tool);
    await p.event("session.execution.succeeded", { sessionID: "parent" });
    await p.after(tool, { sessionID: "parent", id: tool });
    assert.equal(await w.state(), "green", tool);
  }
  for (const tool of ["question", "ask_user", "functions.question", "mcp__ui__confirm", "ui/prompt"]) {
    await p.before(tool, { sessionID: "parent" });
    assert.equal(await w.state(), "red", tool);
    await p.after(tool, { sessionID: "parent" });
    assert.equal(await w.state(), "yellow", tool);
  }
});

test("ended steps do not clear red waiting sessions", async () => {
  const w = harness();
  const p = await w.load();
  await p.permit("ask", { sessionID: "waiting" });
  await p.event("session.step.ended", { sessionID: "waiting" });
  assert.equal(await w.state(), "red");
  await p.before("bash", { sessionID: "waiting" });
  await p.event("session.execution.succeeded", { sessionID: "waiting" });
  assert.equal(await w.state(), "green");
});

test("task busy state is forwarded in snapshots and survives status-server takeover", async () => {
  const w = harness();
  const owner = await w.load();
  const peer = await w.load();
  const input = { sessionID: "parent", tool: "subagent", id: "task-call" };
  await peer.hooks["tool.execute.before"](input);
  await peer.event("session.step.ended", { sessionID: "parent" });
  await w.advance(1000);
  assert.deepEqual(peer.requests.at(-1).body.sessions, { parent: "yellow" });
  assert.equal(await w.state(), "yellow");
  await owner.permit("ask", { sessionID: "waiting" });
  assert.equal(await w.state(), "red");
  w.stop(owner);
  await w.advance(1000);
  assert.equal(w.owner, peer);
  assert.equal(await w.state(), "yellow");
  await peer.hooks["tool.execute.after"](input);
  await peer.event("session.step.ended", { sessionID: "parent" });
  assert.equal(await w.state(), "yellow");
  await peer.event("session.execution.succeeded", { sessionID: "parent" });
  assert.equal(await w.state(), "green");
  assert.equal(peer.execs.length, 1, "task activity must not relaunch a dismissed widget");
});

test("multiple processes send full periodic snapshots without sharing session ownership", async () => {
  const w = harness();
  const owner = await w.load();
  const peer = await w.load();
  const other = await w.load();
  assert.notEqual(peer.requests[0].body.processId, other.requests[0].body.processId);
  assert.deepEqual(peer.requests[0].body.sessions, {});
  await owner.permit("ask", { sessionID: "same" });
  await peer.event("session.step.started", { sessionID: "same" });
  await other.hooks["tool.execute.before"]({ sessionID: "work", tool: "bash" });
  await flush();
  assert.equal(await w.state(), "red");
  await owner.event("session.deleted", { sessionID: "same" });
  assert.equal(await w.state(), "yellow");
  await peer.permit("ask", { sessionID: "same" });
  await flush();
  await other.event("session.execution.succeeded", { sessionID: "same" });
  await flush();
  assert.equal(await w.state(), "red");
  await peer.event("session.deleted", { sessionID: "same" });
  await flush();
  assert.deepEqual(peer.requests.at(-1).body.sessions, {});
  assert.equal(await w.state(), "yellow");
  const count = other.requests.length;
  await w.advance(249);
  assert.equal(other.requests.length, count);
  await w.advance(1);
  assert.equal(other.requests.length, count + 1);
  assert.deepEqual(other.requests.at(-1).body.sessions, { work: "yellow", same: "green" });
  assert.ok(other.timeouts.every((ms) => ms === 800));
  await w.advance(9000);
  assert.equal(await w.state(), "yellow");
});

test("departed snapshots expire at 4000ms, refresh their lifetime, and never expire local state", async () => {
  const w = harness();
  const owner = await w.load();
  await owner.hooks["tool.execute.before"]({ sessionID: "same", tool: "bash" });
  const snapshot = { processId: "peer", sessions: { same: "red" } };
  await w.request("/heartbeat", snapshot);
  await w.advance(3000);
  await w.request("/heartbeat", snapshot);
  await w.advance(3999);
  assert.equal(await w.state(), "red");
  await w.advance(1);
  assert.equal(await w.state(), "yellow");
  await w.advance(8000);
  assert.equal(await w.state(), "yellow");
  await owner.event("session.deleted", { sessionID: "same" });
  assert.equal(await w.state(), "green");
});

test("idle survivors take over the port after owner exit and publish only their own state", async () => {
  const w = harness();
  const owner = await w.load();
  const survivor = await w.load();
  const peer = await w.load();
  await owner.permit("ask", { sessionID: "departing" });
  await survivor.hooks["tool.execute.before"]({ sessionID: "same", tool: "bash" });
  await peer.permit("ask", { sessionID: "same" });
  await flush();
  w.widgetRunning = false;
  w.stop(owner);
  await w.advance(1000);
  assert.equal(w.owner, survivor);
  assert.equal(survivor.server.unrefs, 1);
  assert.equal(await w.state(), "red");
  w.stop(peer);
  await w.advance(3999);
  assert.equal(await w.state(), "red");
  await w.advance(1);
  assert.equal(await w.state(), "yellow");
  await survivor.event("session.deleted", { sessionID: "same" });
  assert.equal(await w.state(), "green");
  assert.ok(w.processes.every((p) => p.execs.length === 1));
  assert.equal(w.widgetRunning, false);
});

test("hung forwarding is single-flight, bounded to 800ms, and never blocks hooks", { timeout: 2000 }, async () => {
  const w = harness();
  await w.load();
  const peer = await w.load();
  peer.fetchMode = "hang";
  await peer.permit("ask", { sessionID: "a" });
  const count = peer.requests.length;
  const request = peer.requests.at(-1);
  for (let i = 0; i < 10; i++) await peer.event("session.tool.progress", { sessionID: "a" });
  assert.equal(peer.requests.length, count);
  await w.advance(799);
  assert.equal(request.signal.aborted, false);
  assert.equal(peer.requests.length, count);
  await w.advance(1);
  assert.equal(request.signal.aborted, true);
  peer.fetchMode = null;
  await peer.event("session.execution.succeeded", { sessionID: "a" });
  await w.advance(150);
  assert.equal(peer.requests.length, count + 1);
  assert.deepEqual(peer.requests.at(-1).body.sessions, { a: "green" });
  assert.equal(await w.state(), "green");
  assert.equal(peer.execs.length, 1);
});

test("changes and deletions during a successful in-flight snapshot arrive on the next tick", async () => {
  const w = harness();
  await w.load();
  const peer = await w.load();
  peer.fetchMode = "hang";
  await peer.permit("ask", { sessionID: "a" });
  const request = peer.requests.at(-1);
  await peer.event("session.deleted", { sessionID: "a" });
  await peer.event("session.step.started", { sessionID: "b" });
  request.resolve(await w.request("/heartbeat", request.body));
  await flush();
  assert.equal(await w.state(), "red");
  peer.fetchMode = null;
  await w.advance(1000);
  assert.deepEqual(peer.requests.at(-1).body.sessions, { b: "yellow" });
  assert.equal(await w.state(), "yellow");
  await peer.event("session.step.ended", { sessionID: "b" });
  await w.advance(1100);
  assert.equal(await w.state(), "yellow");
  await peer.event("session.execution.succeeded", { sessionID: "b" });
  await w.advance(1100);
  assert.equal(await w.state(), "green");
});

test("an idle peer takes over after its in-flight request to the departed owner times out", async () => {
  const w = harness();
  const owner = await w.load();
  const peer = await w.load();
  await w.advance(600);
  peer.fetchMode = "hang";
  await peer.permit("ask", { sessionID: "a" });
  const request = peer.requests.at(-1);
  w.stop(owner);
  await w.advance(400);
  assert.equal(w.owner, null);
  assert.equal(request.signal.aborted, false);
  await w.advance(400);
  assert.equal(request.signal.aborted, true);
  peer.fetchMode = null;
  await w.advance(600);
  assert.equal(w.owner, peer);
  assert.equal(await w.state(), "red");
  assert.equal(peer.execs.length, 1);
  assert.deepEqual(peer.output, [["[traffic-light] serving state at http://127.0.0.1:4390/status"]]);
});

test("background network and bind errors stay quiet and retry without relaunch", async () => {
  const w = harness();
  const owner = await w.load();
  const peer = await w.load();
  for (const mode of ["throw", "reject"]) {
    peer.fetchMode = mode;
    await peer.permit("ask", { sessionID: "a" });
    await w.advance(1000);
  }
  peer.fetchMode = null;
  await w.advance(1000);
  assert.equal(await w.state(), "red");
  w.stop(owner);
  peer.serveError = true;
  await w.advance(1000);
  assert.equal(w.owner, null);
  peer.serveError = false;
  await w.advance(1000);
  assert.equal(w.owner, peer);
  assert.equal(await w.state(), "red");
  assert.equal(peer.execs.length, 1);
  assert.deepEqual(peer.output, [["[traffic-light] serving state at http://127.0.0.1:4390/status"]]);
});

test("malformed snapshots are rejected atomically and cannot refresh or replace peer state", async () => {
  const w = harness();
  const owner = await w.load();
  await w.request("/heartbeat", { processId: "peer", sessions: { a: "red" } });
  await w.advance(3000);
  for (const body of [
    "{", null, {}, { processId: "", sessions: {} }, { processId: 1, sessions: {} },
    { processId: "peer", sessions: null }, { processId: "peer", sessions: [] },
    { processId: "peer", sessions: "red" }, { processId: "peer", sessions: { "": "red" } },
    { processId: "peer", sessions: { a: "green", b: "blue" } },
  ]) {
    const response = await w.request("/heartbeat", body);
    assert.equal(response.status, 400);
    assert.equal(await w.state(), "red");
  }
  await w.request("/heartbeat", { processId: owner.id, sessions: { forged: "red" } });
  await w.advance(1000);
  assert.equal(await w.state(), "green");
});

test("legacy POST /event remains usable but isolated from local and process snapshots", async () => {
  const w = harness();
  const owner = await w.load();
  await owner.hooks["tool.execute.before"]({ sessionID: "same", tool: "bash" });
  const response = await w.request("/event", { sid: "same", state: "red" });
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { ok: true });
  await w.request("/heartbeat", { processId: "peer", sessions: { same: "green" } });
  await w.request("/heartbeat", { processId: "peer", sessions: {} });
  await owner.event("session.deleted", { sessionID: "same" });
  await w.advance(5000);
  assert.equal(await w.state(), "red");
  await w.request("/event", { sid: "same", state: "green" });
  assert.equal(await w.state(), "green");
  for (const body of ["{", null, {}, { sid: "same", state: "blue" }]) {
    assert.equal((await w.request("/event", body)).status, 400);
  }
});
