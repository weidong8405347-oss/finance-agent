// 离线导出 viewer 入口（§11.4）：读取后端内嵌的冻结数据（window.__DOSSIER_EXPORT__），
// 安装离线数据层，然后渲染与在线 App 相同的档案页组件——同一代码 + 冻结数据 =
// 展示与交互和在线页面一致（服务器依赖操作在 UI 中禁用并注明）。

import React from "react";
import ReactDOM from "react-dom/client";

import "../index.css";
import { installOfflineData, type DossierExportData } from "../features/dossier/api";
import ExportApp from "./ExportApp";

declare global {
  interface Window {
    __DOSSIER_EXPORT__?: DossierExportData;
  }
}

const data = window.__DOSSIER_EXPORT__;
if (!data || data.kind !== "dossier-html-export") {
  document.getElementById("root")!.innerHTML =
    '<div style="max-width:480px;margin:80px auto;font:14px/1.7 sans-serif;color:#666">' +
    "内嵌导出数据缺失或格式不符（window.__DOSSIER_EXPORT__）——文件可能已损坏。" +
    "</div>";
} else {
  installOfflineData(data);
  // 初始深链：无 hash → 跳到冻结快照的档案页（携带 snapshot 参数，
  // StockDossierPage 的 URL 规范化因此不再触发额外 navigate）
  if (!window.location.hash) {
    const s = data.snapshot;
    window.location.hash =
      `#/knowledge/${s.entity.kind}/${encodeURIComponent(s.entity.id)}` +
      `?snapshot=${encodeURIComponent(s.context.snapshot_id)}`;
  }
  ReactDOM.createRoot(document.getElementById("root")!).render(
    <React.StrictMode>
      <ExportApp data={data} />
    </React.StrictMode>,
  );
}
