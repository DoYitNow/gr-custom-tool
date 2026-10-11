// Copyright (C) 2026 DoYitNow.
// SPDX-License-Identifier: GPL-2.0-only

"use strict";

const $ = (id) => document.getElementById(id);
const state = { history: null, rollback: null, project: null, selectedFilter: null, selectedCrop: null, page: "filters", selectedShutdown: null, shutdownDialogMode: "add", shutdownDialogFile: null, dirty: false, busy: false, status: null, settings: null, allowOlder: true, dialogMode: "add" };
const selectedFilter = () => state.project?.filters?.find(row => row.id === state.selectedFilter);
const selectedCrop = () => state.project?.crops?.find((row) => row.id === state.selectedCrop);
const shutdownDraft = () => state.project?.shutdown;
const selectedShutdown = () => shutdownDraft()?.items?.find((row) => row.id === state.selectedShutdown);
const selectedShutdownAsset = () => shutdownDraft()?.assets?.find((row) => row.id === selectedShutdown()?.asset_id);
const factoryShutdown = () => state.project?.shutdown_inventory?.resources?.find((row) => row.id === "goodbye") || state.project?.shutdown_inventory?.resources?.[0];
const cropPreviews = new Map();
const cropInputs = new Map();
const copy = (value) => structuredClone(value);
const RECENT_DRAFT_KEY = "firmware-editor-demo-recent-draft";
let noticeTimer;
function recentDraftId() { try { return localStorage.getItem(RECENT_DRAFT_KEY); } catch { return null; } }
function rememberDraft(id) { try { localStorage.setItem(RECENT_DRAFT_KEY, id); } catch {} }
function unconfirmedCrops() { return !!state.project?.crops.some(row => cropEditable(row) && cropInput(row).needsConfirm); }
function displayVersion(value) { return value || "未知版本"; }
function shortSha(value) { return value ? value.slice(0, 10) : "来源未知"; }
function displayTime(value) { if (!value) return ""; const date = new Date(value); return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false }); }
function uiText(value) {
  const original = String(value ?? "");
  const text = original.replace(/理光/g, "").replace(/(^|[^a-z])(?:RICOH|GR(?:[\s_-]*(?:IV|III|4|3))?)(?=$|[^a-z])/gi, "$1");
  if (text === original) return original;
  const cleaned = text.replace(/[ \t]{2,}/g, " ").replace(/^[\s_-]+|[\s_-]+$/g, "");
  return /^\.[^.\s]+$/.test(cleaned) ? "固件" + cleaned : cleaned;
}
function rollbackSuggestedVersion(target) {
  const suffix = value => {
    const match = /^1\.11\.10\.(\d{1,3})$/.exec(String(value || ""));
    return match && Number(match[1]) <= 255 ? Number(match[1]) : null;
  };
  const versions = [state.project?.input.version, target.version, ...(state.history?.nodes || []).map(node => node.version)].map(suffix).filter(value => value !== null);
  const next = Math.max(suffix(state.project?.suggested_version) || 0, ...versions.map(value => value + 1));
  return next > 0 && next <= 255 ? `1.11.10.${next}` : "";
}
function switchView(view) {
  $("editor-workspace").hidden = false;
  const history = $("history-workspace");
  if (view === "history") { history.hidden = false; if (!history.open) history.showModal(); }
  else if (history.open) history.close();
}
function openSupport() { $("more-menu").open = false; $("support-dialog").showModal(); }
function showBuildResult(target, result, label = "已生成") {
  target.replaceChildren(element("strong", `${label} ${result.version}`));
  const updatePolicy = result.report?.package?.update_policy;
  if (updatePolicy?.status === "verified") {
    const enabled = updatePolicy.installed === true && updatePolicy.policy === "allow_older";
    target.append(element("p", "低版本支持：" + (enabled ? "已开启" : "已关闭"), "field-help"));
  } else if (updatePolicy?.status === "unsupported") {
    target.append(element("p", "低版本策略未识别，已保留输入固件原策略；本次开关未修改它。", "field-help"));
  }
  for (const [text, href] of [["下载固件", result.download_url], ["构建清单", result.manifest_url]]) if (href) { const link = element("a", text, "button small secondary"); link.href = href; target.append(link); }
  const support = element("div", undefined, "export-support");
  support.append(element("p", "本项目完全免费，如果你在其他地方收费获取了该项目，请退款并联系原作者。", "field-help"));
  support.append(element("p", "作者发布渠道免费；此说明不限制 GPL 允许的收费再分发。", "field-help"));
  const links = element("div", undefined, "inline-actions");
  const terms = element("a", "使用协议", "button small secondary"); terms.href = "/terms.html"; terms.target = "_blank"; terms.rel = "noopener";
  const donate = element("button", "捐赠", "button small support-entry"); donate.type = "button"; donate.onclick = openSupport;
  links.append(terms, donate); support.append(links); target.append(support);
  target.hidden = false;
}
async function refreshHistory() {
  state.history = await request("/api/history" + (state.project ? "?project_id=" + encodeURIComponent(state.project.id) : ""));
  renderHistory();
}
function renderHistoryBaseline() {
  const input = state.project?.input;
  $("history-baseline-version").textContent = input ? displayVersion(input.version) : "暂无基线";
  $("history-baseline-name").textContent = uiText(input?.filename); $("history-baseline-name").hidden = !input;
  $("history-baseline-sha").textContent = input ? shortSha(input.sha256) : "";
  $("history-baseline-sha").hidden = !input;
  $("history-baseline-sha").title = input ? `SHA-256 ${input.sha256}` : "";
}
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = tag === "pre" ? text : uiText(text);
  if (className) node.className = className;
  return node;
}
async function request(url, data, raw = false) {
  const options = data === undefined ? {} : {
    method: "POST", body: raw ? data : JSON.stringify(data),
    headers: { "Content-Type": raw ? "application/octet-stream" : "application/json" },
  };
  const response = await fetch(url, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `请求失败 (${response.status})`);
  return result;
}
function notice(message, error = false) {
  clearTimeout(noticeTimer);
  const dialog = [...document.querySelectorAll("dialog[open]")].at(-1);
  if (dialog) dialog.prepend($("notice"));
  else $("editor-workspace").before($("notice"));
  $("notice").textContent = uiText(message);
  $("notice").className = "notice" + (error ? " error" : "");
  $("notice").setAttribute("role", error ? "alert" : "status");
  $("notice").hidden = false;
  if (!error) noticeTimer = setTimeout(() => { $("notice").hidden = true; }, 4000);
}
async function operation(label, action) {
  if (state.busy) return;
  state.busy = true;
  $("busy-label").textContent = uiText(label);
  $("busy-overlay").hidden = false;
  try { await action(); } catch (error) { notice(error.message, true); }
  finally { state.busy = false; $("busy-overlay").hidden = true; }
}
async function requireRiskAcknowledgement(action) {
  const dialog = $("risk-dialog");
  if (state.busy || dialog.open) return false;
  const backup = action === "backup";
  $("risk-title").textContent = backup ? "导出编辑备份" : action === "rollback" ? "生成回退固件" : "生成固件";
  $("risk-summary").textContent = "本工具只生成文件，不会自动刷写设备。通过本项目生成得到的结果，不保证完全可以使用。擅自刷入可能使设备出现故障，请自行承担一切因修改而引起的后果，且可能影响保修、官方支持及设备稳定性，并可能存在尚未发现的隐患。";
  const ack = $("risk-ack"); const confirm = $("risk-confirm"); const cancel = $("risk-cancel");
  ack.checked = false; confirm.disabled = true; confirm.textContent = backup ? "继续导出" : "继续生成";
  dialog.returnValue = "";
  return new Promise(resolve => {
    const update = () => { confirm.disabled = !ack.checked; };
    const accept = () => { if (ack.checked) dialog.close("confirmed"); };
    const decline = () => dialog.close("cancelled");
    const finish = () => {
      ack.removeEventListener("change", update); confirm.removeEventListener("click", accept); cancel.removeEventListener("click", decline);
      resolve(dialog.returnValue === "confirmed" && ack.checked);
    };
    ack.addEventListener("change", update); confirm.addEventListener("click", accept); cancel.addEventListener("click", decline);
    dialog.addEventListener("close", finish, { once: true }); dialog.showModal();
  });
}
function dirty() { state.dirty = true; $("save-status").textContent = "未保存"; $("save-status").classList.add("unsaved"); }
function acceptProject(project) {
  if (state.project?.id !== project.id) {
    $("build-result").hidden = true; $("build-result").replaceChildren();
    cropPreviews.clear(); cropInputs.clear(); state.selectedCrop = null;
    state.selectedShutdown = project.shutdown?.selected_item_id || null; state.selectedFilter = null;
  }
  project.filters ||= [];
  project.capabilities ||= {};
  state.selectedFilter = project.filters.some(row => row.id === state.selectedFilter) ? state.selectedFilter : project.filters.find(row => row.editable)?.id || project.filters[0]?.id;
  project.crops ||= [];
  project.original_crops ||= state.project?.id === project.id ? state.project.original_crops : copy(project.crops);
  project.shutdown_inventory ||= { resources: [] };
  project.shutdown ||= { schema_version: 2, assets: [], items: [], selected_item_id: null };
  project.shutdown.assets ||= [];
  project.shutdown.items ||= [];
  state.allowOlder = project.build_options?.allow_older !== false;
  if (!project.shutdown.items.some(row => row.id === state.selectedShutdown)) state.selectedShutdown = null;
  state.project = project;
  rememberDraft(project.id);
  state.selectedCrop = project.crops.some(row => row.id === state.selectedCrop) ? state.selectedCrop : project.crops[0]?.id;
  state.dirty = false;
  render();
}
async function saveDraft() {
  if (!state.project) return;
  if (unconfirmedCrops()) throw new Error("请先确认裁切比例");
  if (!state.dirty) return;
  for (const row of state.project.crops) if (cropEditable(row) && !row.geometry) {
    const preview = cropPreviews.get(row.id);
    if (preview?.pending) await preview.promise;
    if (!row.geometry) throw new Error("请先确认裁切比例");
  }
  acceptProject(await request("/api/project", {
    project_id: state.project.id, title: $("project-title").value,
    crops: state.project.crops, shutdown: state.project.shutdown, filters: state.project.filters,
    build_options: { allow_older: $("allow-older").checked },
  }));
}
async function refreshStatus() {
  [state.status, state.settings] = await Promise.all([request("/api/status"), request("/api/settings")]);
  const records = state.status.projects || []; const projects = $("local-projects");
  projects.replaceChildren(new Option("选择记录", ""));
  for (const project of records) {
    const version = project.input?.version;
    projects.add(new Option(uiText(version && !project.title.includes(version) ? `${project.title} · ${version}` : project.title) || "固件编辑", project.id));
  }
  projects.value = state.project?.id || "";
  $("resume-edit").hidden = !!state.project || !records.some(project => project.id === recentDraftId());
  renderHistoryBaseline(); renderCapabilities(state.project?.capabilities || state.status.capabilities || {}); renderBuildAvailability();
  if (!state.project) { renderFilterList(); renderCropList(); renderShutdownList(); renderPage(); }
}
function revealSelected() {
  const list = $(state.page === "shutdown" ? "shutdown-list" : state.page === "filters" ? "filter-list" : "crop-list"); const active = list.querySelector(".active");
  if (!active) return;
  const item = active.getBoundingClientRect(); const bounds = list.getBoundingClientRect();
  if (item.top < bounds.top) list.scrollTop += item.top - bounds.top;
  else if (item.bottom > bounds.bottom) list.scrollTop += item.bottom - bounds.bottom;
}
function cropEditable(row) { return !!state.project?.can_generate && !!state.project.crop_capabilities?.can_edit && row?.kind === "custom" && row.editable !== false; }
function ratioValue(value) {
  if (typeof value === "number") return value > 0 && Number.isFinite(value) ? value : null;
  const parts = String(value || "").trim().match(/^(\d+(?:\.\d+)?)\s*(?:[:：/]\s*(\d+(?:\.\d+)?))?$/);
  if (!parts) return null;
  const ratio = Number(parts[1]) / Number(parts[2] || 1);
  return ratio > 0 && Number.isFinite(ratio) ? ratio : null;
}
function cropInput(row) {
  let input = cropInputs.get(row.id);
  if (!input || (!input.needsConfirm && input.sourceRatio !== row.requested_ratio)) {
    const parts = String(row.requested_ratio || "").trim().split(/[:：/]/).map(part => part.trim());
    input = { width: parts[0] || "", height: parts[1] || "1", sourceRatio: row.requested_ratio, ratio: `${parts[0] || ""}:${parts[1] || "1"}`, needsConfirm: !row.geometry, error: "" };
    cropInputs.set(row.id, input);
  }
  return input;
}
function renderCropConfirm(row) {
  const input = cropInput(row); const preview = cropPreviews.get(row.id);
  $("confirm-crop").disabled = !cropEditable(row) || !!preview?.pending || (!input.needsConfirm && !preview?.error && !!row.geometry);
}
function renderCropList() {
  const rows = state.project?.crops || []; const list = $("crop-list"); list.replaceChildren();
  $("crop-count").textContent = rows.length;
  const caps = state.project?.crop_capabilities || {};
  const canAdd = !!state.project?.can_generate && !!caps.can_add;
  $("add-crop").disabled = !canAdd;
  $("add-crop").title = canAdd ? "新增比例" : "当前固件无法新增比例";
  for (const row of rows) {
    const button = element("button", undefined, "filter-row crop-row" + (row.id === state.selectedCrop ? " active" : ""));
    button.type = "button"; button.setAttribute("aria-pressed", String(row.id === state.selectedCrop));
    const info = element("span", undefined, "filter-label");
    const name = row.name || row.requested_ratio || "未命名比例";
    const label = row.kind === "factory" ? "原厂" : "自定义";
    info.append(element("strong", name), element("small", `${label}${row.requested_ratio && row.requested_ratio !== name ? ` · ${row.requested_ratio}` : ""}`));
    button.append(info);
    button.onclick = () => { state.selectedCrop = row.id; renderCropList(); renderCropSelected(); };
    list.append(button);
  }
  if (!rows.length) list.append(element("p", "暂无比例", "list-empty"));
  revealSelected();
}
function renderCropGeometry(row) {
  const preview = cropPreviews.get(row.id); const geometry = row.geometry; const input = cropInput(row);
  const screen = geometry?.screen;
  const physical = screen && Number(screen.width ?? screen.w) / Number(screen.height ?? screen.h);
  const actual = preview?.actual ?? row.actual_ratio ?? geometry?.actual_ratio ?? physical;
  const actualNumber = ratioValue(actual);
  const error = input.error || (!input.needsConfirm ? preview?.error : "");
  const status = $("crop-preview-status"); status.className = "callout" + (error ? " warning" : "");
  status.textContent = uiText(error || (preview?.pending ? "更新预览…" : ""));
  status.hidden = !status.textContent;
  $("crop-frame").hidden = !actualNumber;
  if (actualNumber) {
    $("crop-frame-inner").style.aspectRatio = String(actualNumber);
    $("crop-frame-inner").style.width = actualNumber >= 1.5 ? "100%" : `${actualNumber / 1.5 * 100}%`;
    $("crop-frame").setAttribute("aria-label", `裁切预览 ${row.requested_ratio}`);
  }
  renderCropConfirm(row);
}
function renderCropSelected() {
  const row = selectedCrop(); if (!row) { renderPage(); return; }
  const editable = cropEditable(row);
  $("crop-editor-name").textContent = uiText(row.name) || "裁切比例";
  $("crop-editor-subtitle").textContent = row.kind === "factory" ? "原厂比例" : "只读";
  $("crop-editor-subtitle").hidden = editable;
  $("crop-name").value = row.name || ""; $("crop-name").disabled = !editable;
  const input = cropInput(row);
  $("crop-width").value = input.width; $("crop-width").disabled = !editable;
  $("crop-height").value = input.height; $("crop-height").disabled = !editable;
  $("remove-crop").disabled = !editable;
  const rows = state.project.crops; const index = rows.indexOf(row);
  $("crop-move-up").disabled = !editable || !cropEditable(rows[index - 1]);
  $("crop-move-down").disabled = !editable || !cropEditable(rows[index + 1]);
  renderCropGeometry(row);
}
function shutdownEditable() { return !!state.project?.can_generate && !!state.project.shutdown_inventory?.capabilities?.can_build_menu; }
function shutdownResourceKnown(row) { return !!(row?.width && row?.height && row?.size && row?.preview_data_url); }
function shutdownMenuAssetReady(asset) {
  if (!asset) return false;
  const ready = asset.menu_ready ?? asset.checks?.menu_ready;
  return ready === undefined ? !!asset.source_payload_base64 : ready === true;
}
function shutdownMenuStatus() {
  const draft = shutdownDraft(); const rows = draft?.items || [];
  if (rows.length > 16) return { enabled: true, ready: false, reason: "最多添加 16 个自定义图案" };
  for (const row of rows) {
    if (!row.name?.trim()) return { enabled: true, ready: false, reason: "请填写图案名称" };
    if (row.name.length > 24) return { enabled: true, ready: false, reason: "图案名称最多 24 个字" };
    const asset = draft.assets.find(asset => asset.id === row.asset_id);
    if (!asset) return { enabled: true, ready: false, reason: `请为「${row.name}」选择图片` };
    if (!shutdownMenuAssetReady(asset)) return { enabled: true, ready: false, reason: `「${row.name}」的图片不可用，请重新上传` };
  }
  const caps = state.project?.shutdown_inventory?.capabilities || {};
  if (rows.length && !caps.can_build_menu) return { enabled: true, ready: false, reason: "当前固件暂不支持自定义关机图案" };
  return { enabled: rows.length > 0, ready: true };
}
function shutdownThumbnail(url, alt) {
  const frame = element("span", undefined, "shutdown-thumbnail");
  if (url) { const image = element("img"); image.src = url; image.alt = alt; frame.append(image); }
  else frame.append(element("span", "—"));
  return frame;
}
function renderShutdownList() {
  const list = $("shutdown-list"); list.replaceChildren();
  const rows = shutdownDraft()?.items || [];
  $("shutdown-count").textContent = `${rows.length} / 16`;
  $("shutdown-count").title = "自定义图案数量";
  $("add-shutdown").disabled = !shutdownEditable() || rows.length >= 16;
  if (!state.project) { list.append(element("p", "暂无图案", "list-empty")); return; }
  const entries = [{ id: null, name: "原厂默认", preview_data_url: factoryShutdown()?.preview_data_url }, ...rows];
  for (const row of entries) {
    const active = row.id === state.selectedShutdown;
    const button = element("button", undefined, "filter-row shutdown-row" + (active ? " active" : ""));
    button.type = "button"; button.setAttribute("aria-pressed", String(active));
    const info = element("span", undefined, "filter-label");
    const asset = shutdownDraft().assets.find(asset => asset.id === row.asset_id);
    info.append(element("strong", row.name || "未命名图案"), element("small", row.id === null ? "原厂 · 只读" : asset ? "自定义" : "自定义 · 待选图片"));
    button.append(shutdownThumbnail(row.preview_data_url || asset?.menu_preview_data_url || asset?.preview_data_url, ""), info);
    button.onclick = () => { state.selectedShutdown = row.id; shutdownDraft().selected_item_id = row.id; renderShutdownList(); renderShutdownSelected(); };
    list.append(button);
  }
  revealSelected();
}
function renderShutdownSelected() {
  const row = selectedShutdown(); const asset = selectedShutdownAsset(); const original = !row; const editable = !!row && shutdownEditable();
  const name = original ? "原厂默认" : row.name || "未命名图案";
  $("shutdown-editor-name").textContent = uiText(name);
  $("shutdown-editor-subtitle").textContent = editable ? "" : "只读"; $("shutdown-editor-subtitle").hidden = editable;
  $("shutdown-name").value = name; $("shutdown-name").disabled = !editable;
  $("shutdown-name").title = editable ? "" : "只读";
  const preview = original ? factoryShutdown()?.preview_data_url : row.preview_data_url || asset?.menu_preview_data_url || asset?.preview_data_url;
  const image = $("shutdown-preview-image");
  image.hidden = !preview; $("shutdown-preview-empty").hidden = !!preview;
  $("shutdown-preview-empty").textContent = original ? "暂无预览" : "请选择图片";
  if (preview) { image.src = preview; image.alt = `${uiText(name)}预览`; } else image.removeAttribute("src");
  $("shutdown-preview").style.aspectRatio = "3 / 2";
  $("shutdown-preview-name").textContent = original ? "" : uiText(asset?.filename || asset?.name);
  $("replace-shutdown-image").disabled = !editable;
  $("shutdown-fit").disabled = !editable || !asset;
  $("shutdown-fit").value = row?.fit || "contain";
  $("remove-shutdown-item").disabled = !editable;
  const index = shutdownDraft()?.items.indexOf(row) ?? -1;
  $("shutdown-move-up").disabled = !editable || index <= 0;
  $("shutdown-move-down").disabled = !editable || index >= shutdownDraft().items.length - 1;
  const status = $("shutdown-check-status"); status.className = "callout warning";
  const error = row && (!asset ? "请选择图片" : !shutdownMenuAssetReady(asset) ? "图片不可用，请重新上传" : "");
  status.hidden = !error; status.textContent = uiText(error);
}
function openShutdownDialog(mode) {
  if (!shutdownEditable()) return;
  state.shutdownDialogMode = mode; state.shutdownDialogFile = null;
  $("shutdown-dialog-title").textContent = mode === "add" ? "添加关机图案" : "更换图片";
  $("confirm-shutdown").textContent = mode === "add" ? "添加" : "更换";
  $("new-shutdown-name").value = mode === "add" ? "新图案" : selectedShutdown().name;
  $("shutdown-dialog-name").hidden = mode !== "add";
  $("new-shutdown-name").required = mode === "add";
  const sources = $("shutdown-dialog-source"); sources.replaceChildren(new Option("上传图片", "upload"));
  const originals = element("optgroup"); originals.label = "原厂图案";
  const sourceHashes = new Set();
  for (const source of state.project.shutdown_inventory.resources.filter(row => shutdownResourceKnown(row) && row.payload_base64)) {
    originals.append(new Option(uiText(source.name) || "原厂图案", `source:${source.id}`));
    if (source.sha256) sourceHashes.add(source.sha256);
  }
  if (originals.children.length) sources.append(originals);
  const materials = element("optgroup"); materials.label = "已有图片";
  for (const asset of shutdownDraft().assets.filter(asset => shutdownMenuAssetReady(asset))) {
    if (asset.source_sha256 && sourceHashes.has(asset.source_sha256)) continue;
    materials.append(new Option(uiText(asset.filename || asset.name) || "已有图片", `asset:${asset.id}`));
    if (asset.source_sha256) sourceHashes.add(asset.source_sha256);
  }
  if (materials.children.length) sources.append(materials);
  sources.value = "upload"; $("shutdown-file").value = "";
  renderShutdownDialog(); $("shutdown-dialog").showModal();
}
function renderShutdownDialog() {
  const upload = $("shutdown-dialog-source").value === "upload";
  $("shutdown-dialog-upload").hidden = !upload;
  $("shutdown-dialog-file-name").textContent = uiText(state.shutdownDialogFile?.name) || "未选择";
  $("confirm-shutdown").disabled = state.shutdownDialogMode === "replace" && upload && !state.shutdownDialogFile;
  $("shutdown-dialog-note").textContent = ""; $("shutdown-dialog-note").hidden = true;
}

function filterEditable(row) { return !!state.project?.can_generate && row?.editable !== false && (state.project.capabilities?.can_build ?? true); }
function paintIcon(target, row) {
  if (!target) return;
  target.replaceChildren();
  if (row?.icon_data_url) {
    const image = element("img"); image.src = row.icon_data_url; image.alt = uiText(row.name || "滤镜") + " 图标"; target.append(image);
  } else target.append(element("span", "—"));
}
function hiddenFilterMarker() {
  const marker = document.createElement("span"); marker.className = "filter-visibility"; marker.title = "已隐藏"; marker.setAttribute("role", "img"); marker.setAttribute("aria-label", "已隐藏");
  marker.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2.5 12s3.5-5 9.5-5 9.5 5 9.5 5-3.5 5-9.5 5-9.5-5-9.5-5Z"></path><circle cx="12" cy="12" r="2.5"></circle><path d="m4 4 16 16"></path></svg>';
  return marker;
}
function renderFilterList() {
  const list = $("filter-list"); if (!list) return;
  list.replaceChildren(); const rows = state.project?.filters || []; $("filter-count").textContent = rows.length;
  const query = ($("filter-search")?.value || "").trim().toLocaleLowerCase();
  const visible = query ? rows.filter(row => String(row.name || "").toLocaleLowerCase().includes(query)) : rows;
  const canAdd = !!state.project?.can_generate && state.project?.capabilities?.can_add_filter !== false; $("add-filter").disabled = !canAdd;
  for (const row of visible) {
    const button = element("button", undefined, "filter-row" + (row.id === state.selectedFilter ? " active" : "") + (row.enabled === false ? " is-hidden" : ""));
    button.type = "button"; button.setAttribute("aria-pressed", String(row.id === state.selectedFilter));
    const icon = element("span", undefined, "preset-icon"); paintIcon(icon, row);
    const info = element("span", undefined, "filter-label"); const meta = element("small", row.kind === "custom" ? "自定义" : "原厂");
    if (row.enabled === false) meta.append(hiddenFilterMarker()); info.append(element("strong", row.name || "未命名滤镜"), meta); button.append(icon, info);
    button.addEventListener("click", () => { state.selectedFilter = row.id; renderFilterList(); renderFilterSelected(); renderPage(); }); list.append(button);
  }
  if (!visible.length) list.append(element("p", rows.length ? "无匹配滤镜" : "暂无滤镜", "list-empty")); revealSelected();
}
function renderCapabilities(capabilities = {}) {
  const target = $("capability-summary"); if (!target) return; target.replaceChildren();
  const limit = capabilities.maximum_custom_slots ?? capabilities.max_custom_slots; const active = capabilities.active_custom_slots;
  if (limit !== undefined && active !== undefined) target.append(element("span", `自定义 ${active} / ${limit}`));
  const calibration = state.project?.calibration || state.status?.calibration || {};
  const xmp = element("span", calibration.dataset_ready || capabilities.xmp_ready ? "XMP 就绪" : "XMP 未就绪"); xmp.title = uiText(calibration.message || "校色资源未就绪"); target.append(xmp); target.hidden = false;
}
function filterVisibilityEditable(row) {
  if (!row || !state.project?.can_generate) return false;
  const backend = state.project.capabilities?.slot_backend || {};
  const explicit = row.visibility_editable ?? row.visibility_supported;
  if (explicit !== undefined) return explicit === true;
  if (row.kind === "native") return backend.native_visibility_supported === true || backend.native_filter_visibility_supported === true;
  return row.editable !== false && backend.visibility_supported !== false;
}
function parameterSupported(parameter) {
  if (!parameter || typeof parameter !== "object") return false;
  if (parameter.parameter_supported !== undefined) return parameter.parameter_supported === true;
  if (parameter.native_field_supported !== undefined) return parameter.native_field_supported === true;
  return parameter.default !== null && parameter.default !== undefined;
}
function parameterEnabled(parameter) { return typeof parameter?.enabled === "boolean" ? parameter.enabled : parameterSupported(parameter); }
function parameterToggleEditable(row, parameter) {
  if (!row || !state.project?.can_generate || parameterSupported(parameter) === false) return false;
  const explicit = parameter.parameter_enable_supported ?? parameter.enabled_editable ?? parameter.toggle_editable ?? parameter.activation_editable;
  if (explicit !== undefined) return explicit === true;
  return state.project.capabilities?.slot_backend?.parameter_enabled_supported === true && row.editable !== false && parameter.editable === true;
}
function renderFilterSelected() {
  const row = selectedFilter(); if (!row) { renderPage(); return; }
  const editable = filterEditable(row); const caps = state.project?.capabilities || {};
  $("editor-name").textContent = uiText(row.name || "滤镜"); $("editor-subtitle").textContent = editable ? "" : filterVisibilityEditable(row) ? "原厂资源只读 · 菜单开关可编辑" : "只读"; $("editor-subtitle").hidden = editable && !filterVisibilityEditable(row);
  paintIcon($("icon-preview"), row); $("filter-name").value = row.name || ""; $("filter-name").maxLength = 80; $("filter-name").disabled = !editable; $("filter-name").title = editable ? "" : "只读";
  $("change-icon").disabled = !editable || caps.icon_edit_supported === false; $("replace-filter").disabled = !editable || caps.replacement_supported === false;
  $("remove-filter").disabled = row.kind !== "custom" || !editable || caps.can_remove_filter === false; $("import-xmp").disabled = !editable;
  const parameters = $("parameters"); parameters.replaceChildren();
  for (const parameter of row.parameters || []) {
    const item = element("div", undefined, "parameter-item"); const heading = element("div", undefined, "parameter-heading"); heading.append(element("span", parameter.name || parameter.id));
    const toggle = element("label", undefined, "toggle parameter-toggle"); const enabled = parameterEnabled(parameter); const toggleInput = element("input"); toggleInput.type = "checkbox"; toggleInput.checked = enabled;
    const canToggle = parameterToggleEditable(row, parameter); toggleInput.disabled = !canToggle; toggleInput.setAttribute("aria-label", `${parameter.name || parameter.id}开关`); toggleInput.title = canToggle ? "控制该字段是否写入此滤镜" : parameter.enabled === undefined ? "当前固件只读，未读出字段开关" : "该字段的开关写入路径尚未验证";
    toggleInput.addEventListener("change", () => { parameter.enabled = toggleInput.checked; dirty(); renderFilterSelected(); }); toggle.append(toggleInput, element("span", undefined, "toggle-track"), element("span", "启用")); heading.append(toggle); item.append(heading);
    if (parameter.default === null || parameter.default === undefined) { const unavailable = element("span", "—", "parameter-default"); unavailable.title = "固件中未提供默认值"; unavailable.setAttribute("aria-label", "固件中未提供默认值"); item.append(unavailable); }
    else { const input = element("input"); input.type = "number"; input.min = parameter.min; input.max = parameter.max; input.value = parameter.default; input.disabled = !parameterEnabled(parameter) || !editable || !parameter.editable || caps.parameter_defaults_known === false; input.addEventListener("input", () => { parameter.default = Number(input.value); dirty(); }); item.append(input); }
    parameters.append(item);
  }
  const parameterRows = row.parameters || []; const supported = parameterRows.filter(parameterSupported); const enabled = supported.filter(parameterEnabled); const known = parameterRows.every(parameter => parameter.default !== null && parameter.default !== undefined);
  $("parameter-status").textContent = parameterRows.some(parameter => typeof parameter.enabled === "boolean") ? `${enabled.length} / ${supported.length} 启用` : known ? "默认值" : "只读";
  $("filter-visible").checked = row.enabled !== false; $("filter-visible").disabled = !filterVisibilityEditable(row);
  const orderSupported = !!caps.slot_backend?.menu_reorder_supported; const index = state.project.filters.indexOf(row);
  $("move-up").disabled = !editable || !orderSupported || index === 0 || !state.project.filters[index - 1]?.editable; $("move-down").disabled = !editable || !orderSupported || index === state.project.filters.length - 1 || !state.project.filters[index + 1]?.editable;
  if ($("run-calibration")) $("run-calibration").hidden = !row.xmp;
  renderCalibrationForFilter(row);
}
function renderCalibrationForFilter(row) {
  renderXmpCalibration($("calibration"), row);
}
function renderPage() {
  if (!["filters", "crops", "shutdown"].includes(state.page)) state.page = "filters";
  const filters = state.page === "filters"; const crops = state.page === "crops"; const shutdown = state.page === "shutdown"; const loaded = !!state.project;
  $("filter-section").hidden = !filters; $("crop-section").hidden = !crops; $("shutdown-section").hidden = !shutdown;
  $("capability-summary").hidden = true; $("editor-content").hidden = !filters || !selectedFilter(); $("crop-editor").hidden = !crops || !selectedCrop(); $("shutdown-editor").hidden = !shutdown || !loaded;
  $("empty-editor").hidden = !!(shutdown ? loaded : crops ? selectedCrop() : selectedFilter());
  const title = $("empty-editor").querySelector("h2"), note = $("empty-editor").querySelector("p");
  if (!loaded) { title.textContent = "等待载入"; note.textContent = $("resume-edit").hidden ? "导入后即可开始编辑" : "导入或继续上次编辑"; }
  else if (crops) { const has = !!state.project.crops.length; title.textContent = has ? "选择裁切比例" : "暂无裁切比例"; note.textContent = has ? "从左侧选择一个比例" : state.project.crop_capabilities?.can_add ? "在左侧新增比例" : "暂不支持编辑裁切比例"; }
  else if (filters) { const has = !!state.project.filters.length; title.textContent = has ? "选择滤镜" : "暂无滤镜"; note.textContent = has ? "从左侧选择一个滤镜" : state.project.capabilities?.can_add_filter ? "在左侧新增滤镜" : ""; }
  else { title.textContent = "关机图案"; note.textContent = ""; }
  note.hidden = !note.textContent;
  for (const page of ["filters", "crops", "shutdown"]) { $("nav-" + page).classList.toggle("active", state.page === page); $("nav-" + page).setAttribute("aria-pressed", String(state.page === page)); }
  revealSelected();
}
function renderBuildAvailability() {
  if (!state.project) return;
  const featureBackend = state.project.capabilities?.feature_backend_verified !== false;
  const toolchainMissing = featureBackend && state.settings?.toolchain?.ready !== true;
  const cropsUnconfirmed = unconfirmedCrops();
  const cropsPending = cropsUnconfirmed || state.project.crops.some(row => cropEditable(row) && (!row.geometry || cropPreviews.get(row.id)?.pending || cropPreviews.get(row.id)?.error));
  const cropFields = rows => rows.map(({ id, name, requested_ratio, identity }) => ({ id, name, requested_ratio, identity }));
  const cropsChanged = JSON.stringify(cropFields(state.project.crops)) !== JSON.stringify(cropFields(state.project.original_crops));
  const cropsBlocked = cropsChanged && !state.project.crop_capabilities?.can_write;
  const filterPending = state.project.filters.some(row => row.enabled !== false && (row.pending_color_conversion || (row.xmp && row.calibration?.status === "calibration_failed")));
  const menu = shutdownMenuStatus(); const menuBlocked = menu.enabled && !menu.ready;
  $("allow-older").checked = state.allowOlder;
  $("allow-older").disabled = !state.project.can_generate;
  $("build-firmware").disabled = !state.project.can_generate || toolchainMissing || cropsPending || cropsBlocked || menuBlocked || filterPending;
  $("build-note").textContent = uiText(!state.project.can_generate ? "当前固件无法重包" : toolchainMissing ? "请在更多 → 设置中配置工具路径" : cropsUnconfirmed ? "请先确认裁切比例" : cropsPending ? "请完成裁切比例" : cropsBlocked ? "当前固件不支持修改裁切比例" : menuBlocked ? menu.reason : filterPending ? "请等待 XMP 校色完成，或处理校色提示后重新转换" : "");
  $("build-note").hidden = !$("build-note").textContent;
}
async function previewCrop(row) {
  const projectId = state.project.id; const ratio = row.requested_ratio;
  let preview = cropPreviews.get(row.id);
  if (preview?.promise && preview.ratio === ratio) return preview.promise;
  preview = { ratio, pending: true }; cropPreviews.set(row.id, preview);
  const update = () => { if (state.project?.id !== projectId) return; if (state.selectedCrop === row.id) renderCropGeometry(row); renderBuildAvailability(); };
  update();
  preview.promise = (async () => {
    try {
      if (!ratioValue(ratio)) throw new Error("请输入有效比例，如 5:4");
      const result = await request("/api/crop-preview", { project_id: projectId, ratio });
      if (!result.geometry) throw new Error("无法生成预览，请重试");
      if (state.project?.id !== projectId || row.requested_ratio !== ratio || !state.project.crops.includes(row)) return;
      row.geometry = result.geometry; preview.actual = result.actual_ratio;
      if (result.requested_ratio) row.requested_ratio = result.requested_ratio;
    } catch (error) {
      preview.error = error.message;
      if (state.project?.id === projectId && row.requested_ratio === ratio && state.project.crops.includes(row)) throw error;
    } finally { preview.pending = false; update(); }
  })();
  return preview.promise;
}
function renderSettings() {
  const toolchain = state.settings?.toolchain || {};
  if ($("toolchain-status")) { $("toolchain-status").textContent = uiText(toolchain.message || "请配置生成工具链"); $("toolchain-status").className = "callout" + (toolchain.ready === false ? " warning" : ""); }
  if ($("toolchain-clang")) $("toolchain-clang").value = toolchain.clang || "";
  if ($("toolchain-lld")) $("toolchain-lld").value = toolchain.lld || "";
}
function renderCalibrationSettings() {
  const calibration = state.status?.calibration || state.project?.calibration || {};
  if ($("setup-title")) $("setup-title").textContent = calibration.dataset_ready ? "设置" : "首次设置";
  if ($("calibration-dialog-status")) $("calibration-dialog-status").textContent = uiText(calibration.message || "校色资源未就绪");
  if ($("dng-files") && !$("dng-files").files.length) {
    $("dng-selection").textContent = calibration.dataset_ready ? `${calibration.dataset_source === "bundled" ? "内置默认" : "已保存"} ${calibration.samples} 张样片：${(calibration.sample_names || []).join("、")}` : "至少 3 张支持机型的相机原始 DNG，含 1 张独立检查样片。";
    $("calibration-heldout").replaceChildren(...(calibration.sample_names || []).map(name => new Option(name, name.replace(/\.dng$/i, ""))));
    $("calibration-heldout").value = calibration.heldout || "";
  }
  if ($("run-calibration")) $("run-calibration").hidden = !selectedFilter()?.xmp;
  renderSettings();
  renderCalibrationEngine();
}
let calibrationEngine = "offline";
function renderCalibrationEngine() {
  const calibration = state.status?.calibration || state.project?.calibration || {};
  $("calibration-engine").value = calibrationEngine;
  $("calibration-engine-status").textContent = calibrationEngine === "lightroom"
    ? (calibration.lightroom_ready ? "Lightroom 插件已连接，可重新转换。" : calibration.classic_installed ? "已安装 Lightroom Classic；提交转换后请打开 Classic，在增效工具管理器中启用 GR4 Firmware Bridge。" : "需要在 Windows 安装 Lightroom Classic，并启用 GR4 Firmware Bridge 插件。")
    : "本地模拟无需 Lightroom；生成的机内滤镜仍需实拍确认。";
}
function openSetup() {
  calibrationEngine = selectedFilter()?.xmp?.render_engine || calibrationEngine;
  renderCalibrationSettings(); $("calibration-dialog").showModal();
}
function loadHistoryFirmware(node) {
  return operation("载入固件…", async () => {
    await saveDraft();
    acceptProject(await request("/api/history/open", { sha256: node.sha256 }), null);
    $("history-detail-dialog").close(); switchView("editor");
    await refreshStatus(); notice("固件已载入");
  });
}
function openHistoryRollback(node) {
  const suggested = rollbackSuggestedVersion(node);
  if (!suggested) { notice("当前版本第四段已满，不能未经适配自动进位", true); return; }
  state.rollback = node; $("rollback-source").textContent = `${displayVersion(node.version)} · ${shortSha(node.sha256)}`;
  $("rollback-current").textContent = `${displayVersion(state.project.input.version)} · ${shortSha(state.project.input.sha256)}`;
  $("rollback-version").value = suggested; $("rollback-dialog").showModal();
}
function historyActions(node, showDetails = false) {
  const actions = element("div", undefined, "history-actions");
  if (showDetails) { const open = element("button", "打开卡片", "button small secondary"); open.type = "button"; open.onclick = () => openHistoryDetails(node); actions.append(open); }
  if (node.available && node.download_url) { const link = element("a", "下载固件", "button small secondary"); link.href = node.download_url; actions.append(link); }
  const load = element("button", "载入编辑", "button small secondary"); load.type = "button"; load.disabled = !node.available; load.onclick = () => loadHistoryFirmware(node);
  const rollback = element("button", "生成回退固件", "button small quiet"); rollback.type = "button"; rollback.disabled = !node.available || !state.project; rollback.title = !state.project ? "请先载入当前固件" : "恢复此版本的内容"; rollback.onclick = () => openHistoryRollback(node);
  actions.append(load, rollback);
  return actions;
}
function openHistoryDetails(node) {
  const nodes = new Map((state.history?.nodes || []).map(row => [row.sha256, row]));
  const parent = nodes.get(node.parent_sha256);
  const content = $("history-detail-content"); content.replaceChildren();
  $("history-detail-title").textContent = `固件 ${displayVersion(node.version)}`;
  const fields = element("dl", undefined, "history-detail-fields");
  const kind = node.kind === "rollback" ? "回退构建" : node.kind === "build" ? "生成" : "导入";
  const rows = [
    ["版本", displayVersion(node.version)], ["记录类型", node.sha256 === state.project?.input.sha256 ? `${kind} · 当前基线` : kind],
    ["记录时间", displayTime(node.created_at) || "未知"], ["文件名", node.filename || "固件"],
    ["来源", node.parent_sha256 ? `${displayVersion(parent?.version)} · ${node.parent_sha256}` : "起点 · 上游来源未知"],
    ["SHA-256", node.sha256], ["固件文件", node.available ? "可用" : "不可用"],
  ];
  if (node.restored_from_sha256) rows.push(["恢复内容", `${displayVersion(nodes.get(node.restored_from_sha256)?.version)} · ${node.restored_from_sha256}`]);
  if (node.rollback_from_sha256) rows.push(["恢复前基线", `${displayVersion(nodes.get(node.rollback_from_sha256)?.version)} · ${node.rollback_from_sha256}`]);
  for (const [label, value] of rows) {
    const field = element("div", undefined, "history-detail-field"); field.append(element("dt", label), element("dd", value)); fields.append(field);
  }
  content.append(fields, historyActions(node));
  if (node.manifest_url) { const manifest = element("a", "构建清单", "button small quiet"); manifest.href = node.manifest_url; content.append(manifest); }
  $("history-detail-dialog").showModal();
}
function renderHistory() {
  const target = $("history-list"); target.replaceChildren();
  const nodes = state.history?.nodes || []; const bySha = new Map(nodes.map(node => [node.sha256, node]));
  const groups = new Map();
  for (const node of nodes) { const root = node.root_sha256 || node.sha256; if (!groups.has(root)) groups.set(root, []); groups.get(root).push(node); }
  const currentSha = state.project?.input.sha256 || state.history?.current_sha256;
  for (const [rootSha, rows] of groups) {
    const group = element("section", undefined, "history-group"); const root = bySha.get(rootSha);
    const heading = element("div", undefined, "history-group-heading");
    heading.append(element("h2", `来源 ${displayVersion(root?.version)}`), element("span", shortSha(rootSha), "mono muted")); group.append(heading);
    rows.sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
    for (const node of rows) {
      const item = element("article", undefined, "history-record" + (node.sha256 === currentSha ? " current" : ""));
      const info = element("div", undefined, "history-info"); const title = element("div", undefined, "history-record-title");
      const name = element("h3"); const open = element("button", displayVersion(node.version), "history-record-open"); open.type = "button"; open.title = "打开固件卡片"; open.onclick = () => openHistoryDetails(node); name.append(open);
      title.append(name, element("span", node.sha256 === currentSha ? "当前基线" : node.kind === "rollback" ? "回退构建" : node.kind === "build" ? "生成" : "导入", "badge neutral"));
      const parent = bySha.get(node.parent_sha256); const source = node.parent_sha256 ? `来源 ${displayVersion(parent?.version)} · ${shortSha(node.parent_sha256)}` : "起点 · 上游来源未知";
      info.append(title, element("p", `${source} · ${displayTime(node.created_at)}`, "muted"), element("p", `${node.filename || "固件"} · ${shortSha(node.sha256)}`, "mono muted"));
      item.append(info, historyActions(node, true));
      if (!node.available) info.append(element("p", "固件文件不可用", "field-help"));
      group.append(item);
    }
    target.append(group);
  }
  if (!nodes.length) target.append(element("p", "暂无记录", "list-empty"));
}

function render() {
  renderHistoryBaseline();
  if (!state.project) return;
  $("resume-edit").hidden = true;
  const project = state.project;
  $("source-summary").hidden = false; $("build-panel").hidden = false;
  $("source-name").textContent = uiText(project.input.filename);
  $("source-sha").textContent = shortSha(project.input.sha256);
  $("source-meta").textContent = project.input.version;
  $("source-meta").title = `SHA-256 ${project.input.sha256}`;
  $("filter-search").disabled = false;
  $("project-title").disabled = false; $("project-title").value = project.title;
  $("save-project").disabled = !project.can_generate; $("export-project").disabled = false;
  $("save-status").textContent = "已保存"; $("save-status").classList.remove("unsaved");
  $("build-version").disabled = !project.can_generate; $("build-version").value = project.suggested_version;
  $("suggested-version").textContent = "建议 " + project.suggested_version;
  $("build-version").title = "建议版本 " + project.suggested_version;
  renderBuildAvailability(); renderCapabilities(project.capabilities || {}); renderFilterList(); renderFilterSelected(); renderCropList(); renderCropSelected(); renderShutdownList(); renderShutdownSelected(); renderPage();
  if (state.history) renderHistory();
}
$("import-firmware").onclick = () => { $("firmware-file").value = ""; $("firmware-file").click(); };
$("firmware-file").onchange = () => operation("导入固件…", async () => {
  const file = $("firmware-file").files[0]; if (!file) return;
  await saveDraft();
  acceptProject(await request("/api/import?name=" + encodeURIComponent(file.name), await file.arrayBuffer(), true), null);
  switchView("editor"); await refreshStatus(); notice("固件已导入"); $("firmware-file").value = "";
});
$("open-drafts").onclick = () => { $("more-menu").open = false; $("draft-dialog").showModal(); };
$("load-draft").onclick = () => operation("载入已保存的编辑…", async () => {
  const id = $("local-projects").value; if (!id) throw new Error("请选择编辑记录");
  await saveDraft(); const project = await request("/api/project?id=" + encodeURIComponent(id));
  $("draft-dialog").close(); acceptProject(project); switchView("editor");
});
$("resume-edit").onclick = () => operation("继续上次编辑…", async () => {
  const id = recentDraftId(); if (!state.status?.projects.some(project => project.id === id)) throw new Error("编辑记录已不存在");
  await saveDraft(); acceptProject(await request("/api/project?id=" + encodeURIComponent(id)), null); switchView("editor");
});
$("open-history-card").onclick = () => { switchView("history"); operation("读取固件历史…", refreshHistory); };
$("refresh-history").onclick = () => operation("刷新固件历史…", refreshHistory);
$("rollback-form").onsubmit = async (event) => {
  event.preventDefault(); if (!await requireRiskAcknowledgement("rollback")) return;
  return operation("生成回退固件并检查…", async () => {
    if (!state.project || !state.rollback) throw new Error("请先载入当前固件");
    await saveDraft();
    const result = await request("/api/history/rollback", { sha256: state.rollback.sha256, project_id: state.project.id, version: $("rollback-version").value.trim(), allow_older: $("allow-older").checked });
    $("rollback-dialog").close(); $("history-detail-dialog").close();
    const current = await request("/api/project?id=" + encodeURIComponent(state.project.id));
    state.project.suggested_version = current.suggested_version;
    $("build-version").value = current.suggested_version; $("build-version").title = "建议版本 " + current.suggested_version;
    $("suggested-version").textContent = "建议 " + current.suggested_version;
    await refreshStatus(); await refreshHistory();
    showBuildResult($("history-result"), result, "已生成回退固件"); notice("回退固件已生成");
  });
};
$("refresh-status").onclick = () => operation("刷新状态…", async () => { await saveDraft(); await refreshStatus(); if (state.project) acceptProject(await request("/api/project?id=" + state.project.id)); });
for (const page of ["filters", "crops", "shutdown"]) $("nav-" + page).onclick = () => { state.page = page; renderPage(); renderBuildAvailability(); };
$("add-shutdown").onclick = () => openShutdownDialog("add");
$("replace-shutdown-image").onclick = () => openShutdownDialog("replace");
$("choose-shutdown-file").onclick = () => $("shutdown-file").click();
$("shutdown-file").onchange = () => { state.shutdownDialogFile = $("shutdown-file").files[0] || null; renderShutdownDialog(); };
$("shutdown-dialog-source").onchange = renderShutdownDialog;
$("shutdown-form").onsubmit = (event) => {
  event.preventDefault(); if (!shutdownEditable()) return;
  operation("更新关机图案…", async () => {
    const mode = state.shutdownDialogMode; const sourceValue = $("shutdown-dialog-source").value;
    const name = mode === "add" ? $("new-shutdown-name").value.trim() : selectedShutdown()?.name;
    if (!name) throw new Error("请填写图案名称");
    if (name.length > 24) throw new Error("图案名称最多 24 个字");
    const selectedId = state.selectedShutdown; const file = state.shutdownDialogFile;
    await saveDraft();
    if (mode === "add" && !(sourceValue === "upload" && file)) {
      const body = { project_id: state.project.id, name };
      if (sourceValue.startsWith("source:")) body.source_id = sourceValue.slice(7);
      if (sourceValue.startsWith("asset:")) body.asset_id = sourceValue.slice(6);
      const project = await request("/api/shutdown-item", body);
      $("shutdown-dialog").close();
      state.selectedShutdown = project.shutdown.selected_item_id || project.shutdown.items.at(-1).id; acceptProject(project);
    } else state.selectedShutdown = selectedId;
    let bytes, filename;
    if (sourceValue === "upload" && file) { bytes = await file.arrayBuffer(); filename = file.name; }
    else if (mode === "replace") {
      const source = sourceValue.startsWith("source:") ? state.project.shutdown_inventory.resources.find(row => row.id === sourceValue.slice(7)) : shutdownDraft().assets.find(row => row.id === sourceValue.slice(6));
      const payload = source?.source_payload_base64 || source?.menu_payload_base64 || source?.payload_base64;
      if (!payload) throw new Error("图片不可用，请重新上传");
      bytes = Uint8Array.from(atob(payload), char => char.charCodeAt(0)).buffer;
      filename = source.filename || `${source.name || "关机图案"}.jpg`;
    }
    if (bytes) {
      const row = selectedShutdown();
      const itemQuery = mode === "add" ? `&item_name=${encodeURIComponent(name)}` : `&item_id=${encodeURIComponent(row.id)}`;
      const project = await request(`/api/shutdown-image?project_id=${encodeURIComponent(state.project.id)}${itemQuery}&name=${encodeURIComponent(filename)}&fit=${mode === "add" ? "contain" : row.fit || "contain"}`, bytes, true);
      $("shutdown-dialog").close();
      state.selectedShutdown = project.shutdown.selected_item_id || state.selectedShutdown; acceptProject(project);
    }
    $("shutdown-dialog").close(); state.shutdownDialogFile = null;
    renderShutdownList(); renderShutdownSelected(); renderBuildAvailability();
  });
};
$("shutdown-name").oninput = () => {
  const row = selectedShutdown(); if (!row || !shutdownEditable()) return;
  row.name = $("shutdown-name").value; $("shutdown-editor-name").textContent = uiText(row.name) || "未命名图案";
  dirty(); renderShutdownList(); renderBuildAvailability();
};
$("shutdown-fit").onchange = () => {
  const row = selectedShutdown(); if (!row || !shutdownEditable() || row.fit === $("shutdown-fit").value) return;
  row.fit = $("shutdown-fit").value; dirty();
  operation("更新显示方式…", async () => { await saveDraft(); });
};
$("remove-shutdown-item").onclick = () => {
  const row = selectedShutdown(); if (!row || !shutdownEditable()) return;
  const draft = shutdownDraft(); const index = draft.items.indexOf(row); draft.items.splice(index, 1);
  if (draft.selected_item_id === row.id) draft.selected_item_id = null;
  state.selectedShutdown = draft.items[Math.min(index, draft.items.length - 1)]?.id || null;
  draft.selected_item_id = state.selectedShutdown;
  dirty(); renderShutdownList(); renderShutdownSelected(); renderBuildAvailability();
};
for (const [id, delta] of [["shutdown-move-up", -1], ["shutdown-move-down", 1]]) $(id).onclick = () => {
  const rows = shutdownDraft()?.items; const row = selectedShutdown(); const index = rows?.indexOf(row) ?? -1; const other = index + delta;
  if (!row || !shutdownEditable() || !rows[other]) return;
  [rows[index], rows[other]] = [rows[other], rows[index]];
  dirty(); renderShutdownList(); renderShutdownSelected(); renderBuildAvailability();
};
$("add-crop").onclick = () => {
  if (!state.project?.can_generate || !state.project.crop_capabilities?.can_add) return;
  const row = { id: "crop-draft-" + crypto.randomUUID(), name: "新裁切比例", kind: "custom", requested_ratio: "5:4", editable: true };
  state.project.crops.push(row); state.selectedCrop = row.id; dirty();
  renderCropList(); renderCropSelected(); renderPage();
  renderBuildAvailability(); $("crop-width").focus();
};
$("crop-name").oninput = () => {
  const row = selectedCrop(); if (!cropEditable(row)) return;
  row.name = $("crop-name").value; $("crop-editor-name").textContent = uiText(row.name);
  dirty(); renderCropList(); renderBuildAvailability();
};
for (const id of ["crop-width", "crop-height"]) $(id).oninput = () => {
  const row = selectedCrop(); if (!cropEditable(row)) return;
  const input = cropInput(row);
  input.width = $("crop-width").value.trim(); input.height = $("crop-height").value.trim();
  input.needsConfirm = `${input.width}:${input.height}` !== input.ratio || (!row.geometry && !cropPreviews.get(row.id)?.pending);
  input.error = ""; renderCropGeometry(row); renderBuildAvailability();
};
$("confirm-crop").onclick = async () => {
  const row = selectedCrop(); if (!cropEditable(row) || cropPreviews.get(row.id)?.pending) return;
  const input = cropInput(row); const ratio = `${input.width}:${input.height}`;
  if (!/^\d+(?:\.\d+)?$/.test(input.width) || !/^\d+(?:\.\d+)?$/.test(input.height) || !ratioValue(ratio)) {
    input.error = "宽和高必须为大于零的数字"; renderCropGeometry(row); return;
  }
  if ($("notice").textContent === "请先确认裁切比例") $("notice").hidden = true;
  input.needsConfirm = false; input.error = ""; input.ratio = ratio; input.sourceRatio = ratio;
  row.requested_ratio = ratio; delete row.geometry; delete row.actual_ratio;
  cropPreviews.delete(row.id); dirty(); renderCropList();
  await previewCrop(row).catch(() => {});
};
$("remove-crop").onclick = () => {
  const row = selectedCrop(); if (!cropEditable(row)) return;
  cropPreviews.delete(row.id); cropInputs.delete(row.id);
  const rows = state.project.crops; const index = rows.indexOf(row); rows.splice(index, 1);
  state.selectedCrop = rows[Math.min(index, rows.length - 1)]?.id;
  dirty(); renderCropList(); renderCropSelected(); renderPage(); renderBuildAvailability();
};
for (const [id, delta] of [["crop-move-up", -1], ["crop-move-down", 1]]) $(id).onclick = () => {
  const rows = state.project.crops; const row = selectedCrop(); const index = rows.indexOf(row); const other = index + delta;
  if (!cropEditable(row) || !cropEditable(rows[other])) return;
  [rows[index], rows[other]] = [rows[other], rows[index]];
  dirty(); renderCropList(); renderCropSelected(); renderBuildAvailability();
};
$("project-title").oninput = dirty;
$("save-project").onclick = () => operation("保存修改…", async () => { await saveDraft(); await refreshStatus(); notice("修改已保存"); });
$("allow-older").onchange = () => {
  state.allowOlder = $("allow-older").checked;
  dirty(); renderBuildAvailability();
  if (state.allowOlder) $("allow-older-warning-dialog").showModal();
};
$("export-project").onclick = async () => {
  if (!await requireRiskAcknowledgement("backup")) return;
  return operation("导出编辑备份…", async () => { await saveDraft(); window.location.href = "/api/export?id=" + state.project.id; });
};
$("import-project").onclick = () => { $("more-menu").open = false; $("project-file").value = ""; $("project-file").click(); };
$("project-file").onchange = () => operation("导入编辑备份…", async () => {
  const file = $("project-file").files[0]; if (!file) return;
  await saveDraft();
  acceptProject(await request("/api/project-import", JSON.parse(await file.text())), null); switchView("editor"); await refreshStatus(); $("project-file").value = "";
});
$("open-setup").onclick = () => { $("more-menu").open = false; return openSetup(); };
for (const button of document.querySelectorAll("[data-open-support]")) button.onclick = openSupport;
$("save-toolchain").onclick = () => operation("检查工具链…", async () => {
  await request("/api/toolchain", { clang: $("toolchain-clang").value.trim(), lld: $("toolchain-lld").value.trim() });
  $("calibration-dialog").close();
  await refreshStatus(); renderSettings(); notice("工具链设置已保存");
});
$("build-firmware").onclick = async () => {
  if (!await requireRiskAcknowledgement("build")) return;
  return operation("生成固件…", async () => {
    renderBuildAvailability(); if ($("build-firmware").disabled) throw new Error($("build-note").textContent);
    const version = $("build-version").value.trim(); await saveDraft();
    const result = await request("/api/build", { project_id: state.project.id, version, allow_older: $("allow-older").checked });
    acceptProject(await request("/api/project?id=" + state.project.id)); await refreshStatus();
    showBuildResult($("build-result"), result); notice("固件已生成");
    if (state.history) await refreshHistory();
  });
};
for (const button of document.querySelectorAll("[data-close-dialog]")) button.onclick = () => button.closest("dialog").close();
for (const dialog of document.querySelectorAll("dialog")) dialog.addEventListener("close", () => {
  if ($("notice").parentElement === dialog) {
    const remaining = [...document.querySelectorAll("dialog[open]")].at(-1);
    if (remaining) remaining.prepend($("notice"));
    else $("editor-workspace").before($("notice"));
  }
});
document.addEventListener("keydown", (event) => { if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") { event.preventDefault(); if (state.project) $("save-project").click(); } });
window.addEventListener("resize", revealSelected);
window.addEventListener("beforeunload", (event) => { if (state.dirty || unconfirmedCrops()) { event.preventDefault(); event.returnValue = ""; } });

function openFilterDialog(mode) {
  state.dialogMode = mode;
  $("filter-dialog-title").textContent = mode === "add" ? "添加滤镜" : "替换滤镜";
  $("confirm-filter").textContent = mode === "add" ? "添加" : "替换";
  $("new-filter-name").value = mode === "add" ? "新滤镜" : selectedFilter()?.name || "";
  $("template-select").replaceChildren();
  for (const row of (state.project?.filters || []).filter(row => row.resources?.banks?.length === 3)) $("template-select").add(new Option(row.name, row.id));
  $("template-select").value = selectedFilter()?.id || $("template-select").options[0]?.value || "";
  $("filter-dialog-note").textContent = ""; $("filter-dialog-note").hidden = true;
  $("filter-dialog").showModal();
}
function details(title, value) {
  $("details-title").textContent = uiText(title); $("details-content").replaceChildren(element("pre", JSON.stringify(value, null, 2))); $("details-dialog").showModal();
}
function assetSummary(row) {
  return { identity: row.identity, parameters: row.parameters, resources: {
    banks: row.resources?.banks?.map(bank => ({ hardware_index: bank.hardware_index, descriptors: bank.descriptor_addresses,
      matrix: { address: bank.matrix?.address, sha256: bank.matrix?.sha256 }, gamma: { bytes: bank.gamma?.bytes, sha256: bank.gamma?.sha256 },
      multi_main: { address: bank.multi_main?.address, sha256: bank.multi_main?.sha256 } })),
    icon: { ...row.resources?.icon, hex: undefined }, name: { ...row.resources?.name, hex: undefined, record_hex: undefined },
  }, inspection_notes: row.inspection_notes, calibration: row.calibration ? { status: row.calibration.status, message: row.calibration.message, render_engine: row.calibration.render_engine, approximate: row.calibration.approximate, render_boundary: row.calibration.render_boundary } : null };
}
$("filter-search")?.addEventListener("input", renderFilterList);
$("add-filter")?.addEventListener("click", () => openFilterDialog("add"));
$("replace-filter")?.addEventListener("click", () => openFilterDialog("replace"));
$("filter-form")?.addEventListener("submit", event => {
  event.preventDefault(); operation("更新预设…", async () => {
    const name = $("new-filter-name").value.trim(); const templateId = $("template-select").value;
    if (!name) throw new Error("请填写预设名称");
    if (state.dialogMode === "add") {
      const dialog = $("filter-dialog"); dialog.close();
      let project;
      try {
        await saveDraft(); project = await request("/api/add-filter", { project_id: state.project.id, template_id: templateId, name });
      } catch (error) {
        dialog.showModal(); throw error;
      }
      state.selectedFilter = project.filters.at(-1)?.id || state.selectedFilter; acceptProject(project);
    } else {
      const row = selectedFilter(); const template = state.project.filters.find(item => item.id === templateId);
      if (!row || !template) throw new Error("请选择有效的替换模板");
      row.name = name;
      for (const bank of row.resources?.banks || []) {
        const source = template.resources?.banks?.find(item => item.hardware_index === bank.hardware_index); if (!source) continue;
        for (const family of ["matrix", "gamma", "multi_main"]) if (source[family]) bank[family].hex = source[family].hex;
      }
      if (template.resources?.icon?.hex && row.resources?.icon) row.resources.icon.hex = template.resources.icon.hex;
      row.icon_data_url = template.icon_data_url; delete row.xmp; delete row.calibration; row.pending_color_conversion = false; dirty(); await saveDraft();
      $("filter-dialog").close();
    }
    renderFilterList(); renderFilterSelected(); notice("滤镜已保存");
  });
});
$("filter-name")?.addEventListener("input", () => { const row = selectedFilter(); if (!row || !filterEditable(row)) return; row.name = $("filter-name").value; $("editor-name").textContent = uiText(row.name); dirty(); renderFilterList(); });
$("change-icon")?.addEventListener("click", () => $("icon-file").click());
$("icon-file")?.addEventListener("change", () => operation("更新图标…", async () => { const file = $("icon-file").files[0]; const row = selectedFilter(); if (!file || !row || !filterEditable(row)) return; await saveDraft(); acceptProject(await request(`/api/icon?project_id=${encodeURIComponent(state.project.id)}&filter_id=${encodeURIComponent(row.id)}`, await file.arrayBuffer(), true)); $("icon-file").value = ""; }));
for (const [id, delta] of [["move-up", -1], ["move-down", 1]]) $(id)?.addEventListener("click", () => { const rows = state.project?.filters || []; const row = selectedFilter(); const index = rows.indexOf(row); const other = index + delta; if (!row || !filterEditable(row) || other < 0 || other >= rows.length || !filterEditable(rows[other])) return; [rows[index], rows[other]] = [rows[other], rows[index]]; dirty(); renderFilterList(); renderFilterSelected(); });
$("remove-filter")?.addEventListener("click", () => operation("删除滤镜…", async () => { const row = selectedFilter(); if (!row) return; await saveDraft(); state.selectedFilter = null; acceptProject(await request("/api/remove-filter", { project_id: state.project.id, filter_id: row.id })); }));
$("filter-details")?.addEventListener("click", () => { const row = selectedFilter(); if (row) details(row.name, assetSummary(row)); });
$("filter-visible")?.addEventListener("change", () => { const row = selectedFilter(); if (!row || !filterVisibilityEditable(row)) return; row.enabled = $("filter-visible").checked; dirty(); renderFilterList(); renderFilterSelected(); renderBuildAvailability(); });
$("import-xmp")?.addEventListener("click", () => $("xmp-file").click());
$("xmp-file")?.addEventListener("change", () => operation("检查 XMP…", async () => { const file = $("xmp-file").files[0]; const row = selectedFilter(); if (!file || !row || !filterEditable(row)) return; await saveDraft(); acceptProject(await request(`/api/xmp?project_id=${state.project.id}&filter_id=${row.id}&name=${encodeURIComponent(file.name)}&render_engine=${calibrationEngine}`, await file.arrayBuffer(), true)); $("xmp-file").value = ""; }));
$("calibration-engine").onchange = () => { calibrationEngine = $("calibration-engine").value; renderCalibrationEngine(); };
$("calibration-settings")?.addEventListener("click", openSetup);
$("choose-dng")?.addEventListener("click", () => $("dng-files").click());
$("dng-files")?.addEventListener("change", () => { const files = [...$("dng-files").files]; $("dng-selection").textContent = files.length ? `已选择 ${files.length} 张样片` : "至少 3 张支持机型的相机原始 DNG，含 1 张独立检查样片。"; $("calibration-heldout").replaceChildren(...files.map(file => new Option(file.name, file.name.replace(/\.dng$/i, "")))); $("register-calibration").disabled = files.length < 3; });
function fileBase64(file) { return new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(",")[1]); reader.onerror = reject; reader.readAsDataURL(file); }); }
$("register-calibration")?.addEventListener("click", () => operation("保存参考样片…", async () => { const files = [...$("dng-files").files]; const samples = await Promise.all(files.map(async file => ({ name: file.name, base64: await fileBase64(file) }))); await request("/api/calibration/register", { samples, heldout: $("calibration-heldout").value }); $("dng-files").value = ""; $("register-calibration").disabled = true; $("calibration-dialog").close(); await refreshStatus(); if (state.project) acceptProject(await request(`/api/project?id=${encodeURIComponent(state.project.id)}`)); renderCalibrationSettings(); notice("样片已保存"); }));
$("run-calibration")?.addEventListener("click", () => operation("启动校色…", async () => { const row = selectedFilter(); if (!row) return; await saveDraft(); const project = await request("/api/calibration/run", { project_id: state.project.id, filter_id: row.id, render_engine: calibrationEngine }); $("calibration-dialog").close(); acceptProject(project); renderCalibrationSettings(); notice("校色已启动"); }));

let calibrationPollRunning=false;
setInterval(async()=>{ if(!state.project||state.busy||calibrationPollRunning)return; const pending=(state.project.filters||[]).filter(r=>r.pending_color_conversion); if(!pending.length)return; calibrationPollRunning=true; try { for(const row of pending){ const data=await request(`/api/calibration-state?project_id=${state.project.id}&filter_id=${row.id}`); row.calibration=data.calibration; row.pending_color_conversion=data.pending; if(!data.pending&&data.resources&&row.resources?.banks) { const hashes=data.resource_sha256||{}; const calibration={source_xmp_sha256:data.calibration?.xmp_sha256,render_engine:data.calibration?.render_engine||"offline",approximate:data.calibration?.status==="fitted_offline"}; for(const bank of row.resources.banks){ if(data.resources.matrix) { bank.matrix.hex=data.resources.matrix; if(hashes.matrix) bank.matrix.sha256=hashes.matrix; } if(data.resources.gamma) { bank.gamma.hex=data.resources.gamma; if(hashes.gamma) bank.gamma.sha256=hashes.gamma; } if(data.resources.multi) { bank.multi_main.hex=data.resources.multi; if(hashes.multi) bank.multi_main.sha256=hashes.multi; } if(data.calibration?.status==="fitted_offline") bank.calibration={...(bank.calibration||{}),...calibration}; } } } renderFilterList(); renderFilterSelected(); renderBuildAvailability(); } catch(e){notice(e.message,true);} finally{calibrationPollRunning=false;} },4000);

refreshStatus().catch((error) => notice(error.message, true));
