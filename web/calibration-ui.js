// Copyright (C) 2026 DoYitNow. SPDX-License-Identifier: GPL-2.0-only
"use strict";

function renderXmpCalibration(target, row) {
  const node = (tag, text, className) => {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = text;
    if (className) item.className = className;
    return item;
  };
  target.replaceChildren();
  target.classList.remove("warning");
  if (!row?.xmp) {
    target.append(node("p", row?.kind === "native" ? "新增自定义滤镜后即可导入 XMP，转换与校色结果会显示在这里。" : "导入 XMP 后，转换与校色结果会显示在这里。"));
    return;
  }
  const job = row.calibration || {};
  const status = job.status || "awaiting_dng";
  const engine = job.render_engine || row.xmp.render_engine || "offline";
  const engineName = engine === "lightroom" ? "Lightroom Classic" : "本地模拟";
  const complete = status === "fitted_offline";
  const failed = ["calibration_failed", "fit_failed"].includes(status);
  const rendered = complete || ["rendered", "fitting", "fit_failed"].includes(status);
  const steps = node("div", undefined, "calibration-steps");
  steps.append(node("span", engineName, "calibration-engine-label"));
  for (const [name, value] of [["转换", rendered ? "已完成" : failed ? "失败" : ["rendering_offline", "rendering_lightroom"].includes(status) ? "进行中" : "等待中"], ["校色", complete ? "已完成" : failed ? "失败" : status === "fitting" ? "进行中" : "等待中"]]) {
    steps.append(node("span", `${name} · ${value}`, "calibration-step" + (value === "已完成" ? " complete" : "")));
  }
  target.append(node("strong", row.xmp.filename || "XMP"), steps);
  const labels = {awaiting_dng: "校色样片未就绪", awaiting_offline: "等待本地转换", rendering_offline: "正在本地套用 XMP", awaiting_lightroom: "等待打开 Lightroom Classic 并连接插件", rendering_lightroom: "Lightroom Classic 正在渲染样片", rendered: "目标渲染已完成，等待拟合", fitting: "正在拟合机内颜色资源"};
  if (!complete) target.append(node("p", failed ? job.message || "转换或校色失败，请在设置中重新转换。" : labels[status] || job.message || status));
  target.classList.toggle("warning", failed);
  if (complete && job.id) {
    const metrics = job.fit_report?.candidate_evaluation?.[job.heldout]?.full_image;
    const summary = node("div", undefined, "calibration-metrics");
    summary.append(node("span", `独立检查样片：${job.heldout || "—"}`));
    if (Number.isFinite(metrics?.rgb_mae_8bit)) summary.append(node("span", `RGB 平均误差 ${metrics.rgb_mae_8bit.toFixed(2)} / 255`));
    if (Number.isFinite(metrics?.delta_e76_median)) summary.append(node("span", `ΔE76 中位数 ${metrics.delta_e76_median.toFixed(2)}`));
    target.append(summary);
    const url = filename => `/api/calibration-output?job_id=${encodeURIComponent(job.id)}&filename=${encodeURIComponent(filename)}`;
    const previews = node("div", undefined, "calibration-previews");
    for (const [label, filename] of [[engine === "lightroom" ? "Lightroom 目标" : "XMP 本地目标", job.heldout + "_target_srgb_preview.jpg"], ["拟合滤镜预览", job.heldout + "_hypothesis_preview.jpg"]]) {
      const figure = node("figure"); const link = node("a");
      link.href = url(filename); link.target = "_blank"; link.rel = "noopener";
      const image = node("img"); image.src = link.href; image.alt = `${job.heldout} · ${label}`; image.loading = "lazy";
      link.append(image); figure.append(link, node("figcaption", label)); previews.append(figure);
    }
    const links = node("div", undefined, "calibration-links"); const report = node("a", "详细校色报告");
    report.href = url("fit_report.json"); report.target = "_blank"; report.rel = "noopener"; links.append(report);
    target.append(previews, node("p", "软件域拟合结果与模拟预览，机内效果待实拍确认。", "calibration-boundary"), links);
  }
  if (row.xmp.inventory?.unsupported_fields?.length) target.append(node("p", "暂不支持：" + row.xmp.inventory.unsupported_fields.join("、")));
}
