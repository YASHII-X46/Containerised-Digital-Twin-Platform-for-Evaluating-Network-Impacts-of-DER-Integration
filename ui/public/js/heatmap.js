/* Bus×time voltage heatmap (thesis Fig 5.3) — canvas grid with a diverging
   blue–white–red scale centred at 1.0 p.u. Sizes to its flex container. */

function divergingColor(v, vmin, vmax) {
  const mid = 1.0;
  if (!Number.isFinite(v)) return "#444";
  if (v <= mid) {
    const t = Math.max(0, Math.min(1, (v - vmin) / (mid - vmin || 1)));
    // low (blue #3a6bff) → white
    return `rgb(${Math.round(58 + t * 197)},${Math.round(107 + t * 148)},255)`;
  }
  const t = Math.max(0, Math.min(1, (v - mid) / (vmax - mid || 1)));
  // white → high (red #ff4d4d)
  return `rgb(255,${Math.round(255 - t * 178)},${Math.round(255 - t * 178)})`;
}

export function drawHeatmap(canvas, matrix, { vmin = 0.9, vmax = 1.1, busLabels = null } = {}) {
  if (!canvas || !matrix || !matrix.length) return;
  const wrap = canvas.parentElement;
  const W = Math.max(20, wrap.clientWidth);
  const H = Math.max(20, wrap.clientHeight);
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(W * dpr);
  canvas.height = Math.round(H * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);

  const rows = matrix.length, cols = matrix[0].length;
  const padL = 30, padB = 16, padT = 4, padR = 6;
  const gw = (W - padL - padR) / cols;
  const gh = (H - padT - padB) / rows;

  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      ctx.fillStyle = divergingColor(matrix[r][c], vmin, vmax);
      ctx.fillRect(padL + c * gw, padT + r * gh, Math.ceil(gw) + 0.5, Math.ceil(gh) + 0.5);
    }
  }

  const muted = getComputedStyle(document.documentElement).getPropertyValue("--muted").trim() || "#8696a7";
  ctx.fillStyle = muted;
  ctx.font = "9px sans-serif";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  const firstBus = busLabels?.[0] ?? 1;
  const lastBus = busLabels?.[busLabels.length - 1] ?? rows;
  ctx.fillText("bus " + firstBus, padL - 3, padT + gh / 2);
  ctx.fillText("bus " + lastBus, padL - 3, padT + (rows - 0.5) * gh);
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  for (let h = 0; h <= 24; h += 6) {
    const c = Math.min(cols - 1, Math.round((h / 24) * cols));
    ctx.fillText(h + "h", padL + c * gw, H - padB + 2);
  }
}
