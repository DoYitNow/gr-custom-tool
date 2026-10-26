/* Execute the actual editor handlers with a small DOM and HTTP boundary. */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const web = path.resolve(process.argv[2]);
const html = fs.readFileSync(path.join(web, "index.html"), "utf8");
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
assert.equal(new Set(ids).size, ids.length, "HTML ids must be unique");
function node(id) {
  return { id, value: "", checked: false, disabled: false, hidden: false, files: [], options: [], children: [],
    classList: { add() {}, remove() {}, toggle() {} },
    setAttribute() {}, addEventListener() {}, removeEventListener() {}, before() {}, prepend() {},
    replaceChildren(...items) { this.children = items; }, append(...items) { this.children.push(...items); },
    querySelector() { return null; }, click() {}, showModal() {}, close() {},
  };
}
const nodes = new Map(ids.map(id => [id, node(id)]));
const get = id => { assert.ok(nodes.has(id), `Missing HTML control ${id}`); return nodes.get(id); };
get("firmware-family").options = ["auto", "1.11", "1.20"].map(value => ({ value, disabled: false }));
get("firmware-family").value = "auto";
const requests = []; const responses = [];
const context = vm.createContext({
  document: { getElementById: get, querySelectorAll() { return []; }, createElement: node, addEventListener() {} },
  window: { addEventListener() {}, location: {} }, localStorage: { getItem() {}, setItem() {} },
  structuredClone, setTimeout() {}, clearTimeout() {}, setInterval() {},
  fetch: async (url, options) => {
    requests.push({ url, ...options });
    assert.ok(responses.length, `Unexpected request ${url}`);
    const response = responses.shift();
    return { ok: response.ok !== false, status: response.ok === false ? 400 : 200,
      async json() { return structuredClone(response.value); } };
  },
});
let app = fs.readFileSync(path.join(web, "app.js"), "utf8");
const bootstrap = 'refreshStatus().catch((error) => notice(error.message, true));';
assert.equal(app.split(bootstrap).length, 2, "Expected one initial status fetch");
app = app.replace(bootstrap, "");
vm.runInContext(app + `
render = renderFirmwareControls;
refreshStatus = async () => {};
switchView = () => {};
globalThis.ui = { state, firmwareFamily, rollbackSuggestedVersion, renderFirmwareControls, acceptProject, saveDraft };
`, context, { filename: path.join(web, "app.js") });
const ui = context.ui;
function project(id, family, pictorialist = false) {
  return { id, title: id, input: { version: family + ".10.18", sha256: "0".repeat(64) },
    capabilities: { can_build: true, pictorialist_switch: family === "1.20" }, can_generate: true,
    suggested_version: family + ".10.19", filters: [], crops: [], original_crops: [],
    shutdown: { assets: [], items: [] }, shutdown_draft: { assets: [], items: [] },
    native_effects_draft: { schema_version: 1, pictorialist },
    native_mono_draft: { enabled: false }, build_options: { allow_older: true } };
}
function file(name) { return { name, async arrayBuffer() { return Uint8Array.from([1, 20, 0, 7]).buffer; } }; }
function reply(value, ok = true) { responses.push({ value, ok }); }
function lastJson() { return JSON.parse(requests.at(-1).body); }
async function main() {
  ui.state.status = { supported_families: ["1.11", "1.20"] };
  assert.equal(ui.firmwareFamily("1.20.0.7"), "1.20");
  assert.equal(ui.firmwareFamily({ family: "1.11" }), "1.11");
  assert.equal(ui.firmwareFamily("1.30.0.1"), null);
  ui.acceptProject(project("twenty", "1.20"));
  ui.state.history = { nodes: [{ version: "1.11.10.250" }, { version: "1.20.10.22" }] };
  assert.equal(ui.rollbackSuggestedVersion({ version: "1.11.10.254" }), "1.20.10.23");
  assert.equal(get("upgrade-firmware").hidden, true);
  assert.equal(get("native-effects-controls").hidden, false);
  assert.equal(get("pictorialist-toggle").checked, false);
  ui.acceptProject(project("eleven", "1.11"));
  assert.equal(ui.rollbackSuggestedVersion({ version: "1.20.10.254" }), "1.11.10.251");
  assert.equal(get("upgrade-firmware").hidden, false);
  assert.equal(get("native-effects-controls").hidden, true);
  ui.state.status.supported_families = ["1.11"];
  ui.renderFirmwareControls();
  assert.equal(get("upgrade-firmware").hidden, true);
  assert.equal(get("firmware-family").options[2].disabled, true);
  ui.state.status.supported_families.push("1.20");

  // The selected family reaches the binary import API, including encoded names.
  get("firmware-family").value = "1.20";
  get("firmware-file").files = [file("official 1.20.bin")];
  reply(project("imported-twenty", "1.20"));
  await get("firmware-file").onchange();
  assert.equal(requests.at(-1).url, "/api/import?name=official%201.20.bin&family=1.20");
  assert.equal(requests.at(-1).headers["Content-Type"], "application/octet-stream");
  assert.equal(ui.state.project.id, "imported-twenty");
  assert.equal(get("firmware-family-current").textContent, "当前基于 1.20");

  // Failed validation does not replace the currently loaded project.
  get("firmware-file").files = [file("wrong.bin")];
  reply({ error: "选择的版本与固件不符" }, false);
  await get("firmware-file").onchange();
  assert.equal(ui.state.project.id, "imported-twenty");
  assert.equal(get("notice").textContent, "选择的版本与固件不符");

  // Migration first persists unsaved changes, then accepts a separate target.
  const source = project("source eleven", "1.11");
  ui.acceptProject(source); ui.state.dirty = true;
  get("project-title").value = "Saved source";
  get("upgrade-firmware-file").files = [file("official120.bin")];
  const target = project("migrated-twenty", "1.20");
  target.migration = { source_version: source.input.version };
  reply(source); reply(target);
  const start = requests.length;
  await get("upgrade-firmware-file").onchange();
  assert.deepEqual(requests.slice(start).map(row => row.url), ["/api/project", "/api/upgrade?project_id=source%20eleven&name=official120.bin"]);
  assert.equal(JSON.parse(requests[start].body).title, "Saved source");
  assert.equal(ui.state.project.id, "migrated-twenty");
  assert.equal(source.input.version, "1.11.10.18");
  assert.match(get("firmware-migration-info").textContent, /1\.11\.10\.18 → 1\.20\.10\.18/);

  // Failed upgrades keep the source open and expose the server explanation.
  ui.acceptProject(project("still-eleven", "1.11"));
  get("upgrade-firmware-file").files = [file("not-official.bin")];
  reply({ error: "请选择官方 1.20 固件" }, false);
  await get("upgrade-firmware-file").onchange();
  assert.equal(ui.state.project.id, "still-eleven");
  assert.equal(get("notice").textContent, "请选择官方 1.20 固件");

  // The experimental option is separate from Mono unlock and survives saving.
  ui.acceptProject(project("effects-twenty", "1.20"));
  get("pictorialist-toggle").checked = true;
  get("pictorialist-toggle").onchange();
  assert.equal(ui.state.project.native_effects_draft.pictorialist, true);
  assert.equal(ui.state.project.native_mono_draft.enabled, false);
  assert.equal(ui.state.dirty, true);
  reply(ui.state.project);
  await ui.saveDraft();
  assert.deepEqual(lastJson().native_effects_draft, { schema_version: 1, pictorialist: true });
  assert.equal(get("pictorialist-toggle").checked, true);
  ui.acceptProject(project("effects-eleven", "1.11"));
  get("pictorialist-toggle").checked = true;
  get("pictorialist-toggle").onchange();
  assert.equal(ui.state.project.native_effects_draft.pictorialist, false);
  assert.equal(responses.length, 0);
  console.log(JSON.stringify({ status: "passed", workflows: 7, app: path.join(web, "app.js") }));
}
main().catch(error => { console.error(error); process.exitCode = 1; });
