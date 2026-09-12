/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./export.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      // 档案页设计令牌（dossier 优化方案 §39）：90% 中性色 + 语义色仅表达状态，
      // 不表达「模块身份」。专业感来自 typography/alignment/whitespace/grid，
      // 不来自 shadow/gradient/glow。
      colors: {
        ink: {
          DEFAULT: "#0F172A", // text-primary
          soft: "#334155",    // text-secondary
          mute: "#64748B",    // text-muted
          faint: "#94A3B8",   // 辅助说明
        },
        line: "#E2E8F0",      // 统一 1px 边框
        paper: "#F8FAFC",     // 页面底
        accent: {
          DEFAULT: "#1D4ED8",
          soft: "#EFF6FF",
        },
        pos: {
          DEFAULT: "#15803D",
          soft: "#F0FDF4",
        },
        warn: {
          DEFAULT: "#B45309",
          soft: "#FFFBEB",
        },
        risk: {
          DEFAULT: "#B91C1C",
          soft: "#FEF2F2",
        },
      },
      fontSize: {
        // 中文正文 ≥14px；10px 不用于核心信息（§40）
        meta: ["12px", { lineHeight: "1.5" }],
      },
      borderRadius: {
        card: "8px",
      },
    },
  },
  plugins: [],
};
