// 离线导出 viewer 构建（§11.4）：单文件 IIFE 包，产出固定文件名
// dist-export/export-viewer.js + export-viewer.css——后端 HTML 导出
// （dossier/export_html.py）读取并内联这两个文件 + 冻结数据，生成
// 自包含交互 HTML。inlineDynamicImports 保证 echarts 等懒加载 chunk
// 一并打进单文件（导出文件不允许二次网络请求）。

import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist-export",
    emptyOutDir: true,
    cssCodeSplit: false,
    rollupOptions: {
      input: "export.html",
      output: {
        format: "iife",
        inlineDynamicImports: true,
        entryFileNames: "export-viewer.js",
        assetFileNames: "export-viewer.[ext]",
      },
    },
  },
});
