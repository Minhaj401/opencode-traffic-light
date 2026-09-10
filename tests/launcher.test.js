import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import test from "node:test";
import { createContext, SourceTextModule, SyntheticModule } from "node:vm";

const source = await readFile(new URL("../traffic-light-launcher.js", import.meta.url), "utf8");

async function load(options = {}) {
  const calls = [];
  const handle = () => ({ unrefs: 0, unref() { this.unrefs++; } });
  const child = { ...handle(), stdin: handle(), stdout: handle(), stderr: handle() };
  if (options.noPipes) {
    delete child.stdin;
    delete child.stdout;
    delete child.stderr;
  }
  const mocks = {
    "node:child_process": {
      execFile(file, args, settings, callback) {
        calls.push({ file, args: [...args], settings: { ...settings } });
        if (options.throws) throw new Error("spawn failed");
        if (!options.hangs) queueMicrotask(() => callback(options.missing ? new Error("ENOENT") : null));
        return child;
      },
    },
    "node:os": { homedir: () => "/fake home/$(not-a-shell)" },
    "node:path": { join },
  };
  const context = createContext({ process: { platform: options.platform ?? "darwin" } });
  const module = new SourceTextModule(source, { context });
  await module.link((specifier) => {
    assert.ok(Object.hasOwn(mocks, specifier), `Unexpected dependency: ${specifier}`);
    return new SyntheticModule(Object.keys(mocks[specifier]), function () {
      for (const [name, value] of Object.entries(mocks[specifier])) this.setExport(name, value);
    }, { context });
  });
  await module.evaluate();
  assert.equal(module.namespace.default, module.namespace.TrafficLightLauncher);
  const init = module.namespace.default;
  const hooks = await init();
  return { calls, child, hooks, init };
}

test("supported platforms use one bounded absolute-path startup command", async () => {
  for (const platform of ["darwin", "linux"]) {
    const { calls, child, hooks } = await load({ platform });
    assert.deepEqual(calls, [{
      file: "/fake home/$(not-a-shell)/.local/bin/traffic-light",
      args: ["autostart"],
      settings: { timeout: 3000 },
    }]);
    assert.deepEqual(Object.keys(hooks), []);
    for (const value of [child, child.stdin, child.stdout, child.stderr]) assert.equal(value.unrefs, 1);
  }
});

test("repeated initialization does not relaunch a dismissed widget", async () => {
  const { init, calls } = await load();
  await Promise.all([init(), init(), init()]);
  assert.equal(calls.length, 1);
});

test("missing or failed commands do not break initialization or add retries", async () => {
  for (const options of [{ throws: true }, { missing: true }]) {
    const { init, calls, hooks } = await load(options);
    await init();
    assert.equal(calls.length, 1);
    assert.deepEqual(Object.keys(hooks), []);
  }
});

test("a hanging command does not block initialization", { timeout: 2000 }, async () => {
  const { calls, hooks } = await load({ hangs: true });
  assert.equal(calls[0].settings.timeout, 3000);
  assert.deepEqual(Object.keys(hooks), []);
});

test("startup tolerates runtimes without pipe unref handles", async () => {
  const { child, calls } = await load({ noPipes: true });
  assert.equal(calls.length, 1);
  assert.equal(child.unrefs, 1);
});

test("unsupported platforms do not launch a desktop widget", async () => {
  for (const platform of ["win32", "freebsd"]) {
    const { calls, hooks } = await load({ platform });
    assert.deepEqual(calls, []);
    assert.deepEqual(Object.keys(hooks), []);
  }
});
