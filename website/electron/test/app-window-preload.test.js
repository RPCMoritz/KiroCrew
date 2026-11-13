/**
 * app-window-preload — the whole surface a hosted app window's page gets.
 *
 * The bridge is enumerated, not a pass-through: a channel it does not name is a
 * channel the page cannot reach. Its identity comes from the host's own
 * `--kc-app-window=` argument and is informational only (the host resolves
 * every message by sender; see app-window-host.test.js).
 */

const test = require("node:test");
const assert = require("node:assert");
const path = require("path");
const Module = require("module");

function loadPreload(argv, invokeAnswer = () => undefined) {
  const exposed = {};
  const sent = [];
  const listeners = {};
  const electron = {
    contextBridge: { exposeInMainWorld: (key, api) => { exposed[key] = api; } },
    ipcRenderer: {
      send: (channel, ...args) => sent.push([channel, ...args]),
      invoke: (channel, ...args) => {
        sent.push([channel, ...args]);
        return Promise.resolve(invokeAnswer(channel));
      },
      on: (channel, fn) => { (listeners[channel] = listeners[channel] || []).push(fn); },
      removeListener: (channel, fn) => {
        listeners[channel] = (listeners[channel] || []).filter((f) => f !== fn);
      },
    },
  };
  const modPath = path.join(__dirname, "..", "app-window-preload.js");
  delete require.cache[require.resolve(modPath)];
  const origLoad = Module._load;
  const origArgv = process.argv;
  Module._load = function (request, parent, isMain) {
    if (request === "electron") return electron;
    return origLoad(request, parent, isMain);
  };
  process.argv = argv;
  try {
    require(modPath);
  } finally {
    Module._load = origLoad;
    process.argv = origArgv;
  }
  return { exposed, sent, listeners };
}

test("the bridge exposes exactly the host calls, under one global", () => {
  const { exposed } = loadPreload(["electron", "--kc-app-window=pets/pet/overlay/1"]);
  assert.deepStrictEqual(Object.keys(exposed), ["kirocrewDesktopWindow"]);
  const bridge = exposed.kirocrewDesktopWindow;
  assert.ok(Object.isFrozen(bridge));
  assert.deepStrictEqual(Object.keys(bridge).sort(), [
    "app", "capabilities", "chooseGateway", "close", "kind", "level", "listGateways", "onShortcut", "open",
    "openImage", "ready", "revealFile", "setFocusable", "setHitRegions", "window",
  ]);
  assert.deepStrictEqual(
    { app: bridge.app, window: bridge.window, kind: bridge.kind, level: bridge.level },
    { app: "pets", window: "pet", kind: "overlay", level: 1 },
  );
  assert.deepStrictEqual(bridge.capabilities, []);
});

test("the capabilities a window was opened with are reported, known names only", () => {
  const { exposed } = loadPreload(["--kc-app-window=pets/pet/overlay/2/gatewayChoice,anything,localFiles"]);
  const bridge = exposed.kirocrewDesktopWindow;
  assert.strictEqual(bridge.level, 2);
  assert.deepStrictEqual(bridge.capabilities, ["localFiles", "gatewayChoice"]);
  assert.ok(Object.isFrozen(bridge.capabilities));
});

test("each capability call reaches only its own channel, with coerced arguments", async () => {
  const answers = {
    "app-window:open-image": "yes",
    "app-window:list-gateways": { state: "ready", gateways: [] },
    "app-window:choose-gateway": { ok: true, chosen: "crew", showing: "crew" },
  };
  const { exposed, sent } = loadPreload(["--kc-app-window=pets/panel/panel/2/localFiles"], (c) => answers[c]);
  const bridge = exposed.kirocrewDesktopWindow;
  bridge.revealFile({ toString: () => "/tmp/a.txt" });
  assert.strictEqual(await bridge.openImage("/tmp/a.png"), false, "only a literal true reads as opened");
  assert.deepStrictEqual(await bridge.listGateways(), { state: "ready", gateways: [] });
  assert.deepStrictEqual(await bridge.chooseGateway(7), { ok: true, chosen: "crew", showing: "crew" });
  assert.deepStrictEqual(sent, [
    ["app-window:reveal-file", "/tmp/a.txt"],
    ["app-window:open-image", "/tmp/a.png"],
    ["app-window:list-gateways"],
    ["app-window:choose-gateway", "7"],
  ]);
});

test("a refused capability call resolves to its empty answer instead of throwing", async () => {
  const { exposed } = loadPreload(["--kc-app-window=pets/panel/panel/2/"], () => Promise.reject(new Error("no handler")));
  const bridge = exposed.kirocrewDesktopWindow;
  assert.strictEqual(await bridge.openImage("/tmp/a.png"), false);
  assert.strictEqual(await bridge.listGateways(), null);
  assert.deepStrictEqual(await bridge.chooseGateway("crew"), { ok: false });
});

test("each call sends its own channel with coerced arguments", () => {
  const { exposed, sent } = loadPreload(["--kc-app-window=pets/pet/overlay/1"]);
  const bridge = exposed.kirocrewDesktopWindow;
  bridge.ready();
  bridge.close();
  bridge.open({ toString: () => "panel" });
  bridge.setFocusable("yes");
  bridge.setHitRegions([{ x: "1", y: 2, width: 3, height: 4, secret: "x" }]);
  bridge.setHitRegions("everything");
  assert.deepStrictEqual(sent, [
    ["app-window:ready"],
    ["app-window:close"],
    ["app-window:open", "panel"],
    ["app-window:set-focusable", false],
    ["app-window:set-hit-regions", [{ x: 1, y: 2, width: 3, height: 4 }]],
    ["app-window:set-hit-regions", []],
  ]);
});

test("onShortcut delivers ids and unsubscribes", () => {
  const { exposed, listeners } = loadPreload(["--kc-app-window=pets/panel/panel/1"]);
  const got = [];
  const off = exposed.kirocrewDesktopWindow.onShortcut((id) => got.push(id));
  const [listener] = listeners["app-window:shortcut"];
  listener({}, "toggle");
  listener({}, { not: "an id" });
  off();
  assert.deepStrictEqual(got, ["toggle"]);
  assert.deepStrictEqual(listeners["app-window:shortcut"], []);
  assert.strictEqual(typeof exposed.kirocrewDesktopWindow.onShortcut(null), "function");
});

test("a window the host did not tag reports no identity", () => {
  const { exposed } = loadPreload(["electron"]);
  const bridge = exposed.kirocrewDesktopWindow;
  assert.deepStrictEqual(
    { app: bridge.app, window: bridge.window, level: bridge.level },
    { app: "", window: "", level: 0 },
  );
});
