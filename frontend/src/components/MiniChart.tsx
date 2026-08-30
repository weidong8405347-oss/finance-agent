// 迷你 SVG 图表（零依赖）：折线（时序）+ 条形（对比）。服务端 HTML 存档之外的 UI 内联版。
export function LineChart({
  points, // [{x: label, y: number}]
  height = 160,
}: {
  points: { x: string; y: number }[];
  height?: number;
}) {
  const W = 560, H = height, padL = 44, padB = 22, padT = 12;
  if (points.length < 2) {
    return <div className="py-3 text-center text-[11px] text-neutral-400">数据点不足（{points.length}）</div>;
  }
  const ys = points.map((p) => p.y);
  const lo = Math.min(...ys), hi = Math.max(...ys);
  const rng = hi - lo || 1;
  const xs = points.map((_, i) => padL + (i * (W - padL - 12)) / (points.length - 1));
  const yy = points.map((p) => padT + (1 - (p.y - lo) / rng) * (H - padT - padB));
  const poly = xs.map((x, i) => `${x.toFixed(0)},${yy[i].toFixed(0)}`).join(" ");
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full">
      <line x1={padL} y1={padT} x2={padL} y2={H - padB} stroke="#e5e5e5" />
      <line x1={padL} y1={H - padB} x2={W - 8} y2={H - padB} stroke="#e5e5e5" />
      <text x={4} y={padT + 8} fontSize="9" fill="#a3a3a3" fontFamily="monospace">{hi.toLocaleString()}</text>
      <text x={4} y={H - padB} fontSize="9" fill="#a3a3a3" fontFamily="monospace">{lo.toLocaleString()}</text>
      <polyline fill="none" stroke="#171717" strokeWidth="1.6" points={poly} />
      {xs.map((x, i) => (
        <g key={i}>
          <circle cx={x} cy={yy[i]} r="2.5" fill="#171717" />
          <text x={x} y={H - 6} fontSize="9" fill="#a3a3a3" fontFamily="monospace" textAnchor="middle">
            {points[i].x}
          </text>
        </g>
      ))}
    </svg>
  );
}

export function BarCompare({
  items, // [{label, value}]
  height = 160,
}: {
  items: { label: string; value: number; warn?: boolean }[];
  height?: number;
}) {
  const W = 560, H = height, padL = 44, padB = 22, padT = 12;
  if (items.length === 0) {
    return <div className="py-3 text-center text-[11px] text-neutral-400">暂无可对比实体</div>;
  }
  const hi = Math.max(...items.map((i) => Math.abs(i.value))) || 1;
  const bw = Math.min(70, (W - padL - 16) / items.length - 14);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full">
      <line x1={padL} y1={H - padB} x2={W - 8} y2={H - padB} stroke="#e5e5e5" />
      <text x={4} y={padT + 8} fontSize="9" fill="#a3a3a3" fontFamily="monospace">{hi.toLocaleString()}</text>
      {items.map((it, i) => {
        const x = padL + 8 + (i * (W - padL - 16)) / items.length;
        const h = (Math.abs(it.value) / hi) * (H - padT - padB);
        return (
          <g key={it.label}>
            <rect x={x} y={H - padB - h} width={bw} height={h} fill={it.warn ? "#b45309" : "#171717"} />
            <text x={x + bw / 2} y={H - padB - h - 4} fontSize="9" fill="#525252" fontFamily="monospace" textAnchor="middle">
              {it.value.toLocaleString()}
            </text>
            <text x={x + bw / 2} y={H - 6} fontSize="9" fill="#a3a3a3" fontFamily="monospace" textAnchor="middle">
              {it.label}
            </text>
          </g>
        );
      })}
    </svg>
  );
}
