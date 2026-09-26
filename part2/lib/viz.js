// 가벼운 SVG/CSS 차트 모음 — Plotly(1MB)를 대체한다. 의존성 없음, 익스텐션 CSP(script-src 'self') 안에서 동작.
// 모션 원칙(조사 근거는 DECISIONS.md 09-26): 0.45s 안팎 ease-out, 요소는 한꺼번에가 아니라 단계적으로
// (40ms 시차, 앞 8개까지만 — 동시에 추적 가능한 움직임은 3~4개), prefers-reduced-motion이면 즉시 최종 상태.
// 채널명 등 라벨은 신뢰할 수 없는 데이터(사용자가 올린 Takeout 파일)라 전부 textContent로 넣는다(innerHTML 금지).

(function () {
  const NS = "http://www.w3.org/2000/svg";
  const EASE = "cubic-bezier(.2,.8,.2,1)";
  const DUR = 450;
  const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
  const delay = (i) => Math.min(i, 8) * 40;
  const fmtInt = (v) => Math.round(v).toLocaleString("ko-KR");
  const pct = (x) => `${Math.round(x * 100)}%`;

  // ── DOM 헬퍼 ────────────────────────────────────────────
  // 스타일 객체 적용. "--i" 같은 CSS 변수는 Object.assign으로 안 들어가 setProperty로 넣는다.
  function setStyle(n, st) {
    for (const [k, v] of Object.entries(st)) k.startsWith("--") ? n.style.setProperty(k, v) : (n.style[k] = v);
  }
  function h(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "style") setStyle(n, v);
      else if (k === "text") n.textContent = v;
      else n.setAttribute(k, v);
    }
    for (const k of kids.flat()) if (k != null) n.append(k);
    return n;
  }
  function s(tag, attrs, parent) {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null) continue;
      if (k === "text") n.textContent = v;
      else if (k === "style") setStyle(n, v);
      else n.setAttribute(k, v);
    }
    if (parent) parent.append(n);
    return n;
  }

  // ── 모션 ────────────────────────────────────────────────
  function play(node, keyframes, opts = {}) {
    if (reduced() || !node || !node.animate) return;
    node.animate(keyframes, { duration: DUR, easing: EASE, fill: "backwards", ...opts });
  }
  function countUp(node, to, fmt = fmtInt, opts = {}) {
    if (reduced()) { node.textContent = fmt(to); return; }
    const dur = opts.duration ?? 500;
    const start = performance.now() + (opts.delay || 0);
    node.textContent = fmt(0);
    const step = (now) => {
      const p = Math.min(1, Math.max(0, (now - start) / dur));
      node.textContent = fmt(to * (1 - Math.pow(1 - p, 3)));
      if (p < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }
  // 화면에 들어올 때 한 번만 재생(스크롤 아래 차트).
  function onVisible(node, fn) {
    if (!("IntersectionObserver" in window)) return fn();
    const io = new IntersectionObserver((ents) => {
      if (ents.some((e) => e.isIntersecting)) { io.disconnect(); fn(); }
    }, { threshold: 0.2 });
    io.observe(node);
  }

  // ── 툴팁(하나를 공유). 값이 먼저, 라벨이 뒤. 키보드 포커스도 같은 내용 ──
  let tipEl = null;
  function tip() {
    if (!tipEl) { tipEl = h("div", { class: "viz-tip", role: "tooltip" }); document.body.append(tipEl); }
    return tipEl;
  }
  function showTip(at, title, rows) {
    const t = tip();
    t.replaceChildren();
    if (title) t.append(h("div", { class: "t", text: title }));
    for (const r of rows) {
      t.append(h("div", { class: "r" },
        r.color ? h("span", { class: "k", style: { background: r.color } }) : null,
        h("b", { text: r.value }), h("span", { text: r.label || "" })));
    }
    const x = at.clientX ?? at.left + at.width / 2;
    const y = at.clientY ?? at.top;
    t.classList.add("on");
    const w = t.offsetWidth, hh = t.offsetHeight;
    let left = x + 14, top = y - hh - 10;
    if (left + w > innerWidth - 8) left = x - w - 14;
    if (top < 8) top = y + 18;
    t.style.left = `${Math.max(8, left)}px`;
    t.style.top = `${top}px`;
  }
  function hideTip() { if (tipEl) tipEl.classList.remove("on"); }
  function bindTip(node, content, onHot) {
    const on = (at) => { const c = content(); showTip(at, c.title, c.rows); if (onHot) onHot(true); };
    const off = () => { hideTip(); if (onHot) onHot(false); };
    node.addEventListener("pointermove", on);
    node.addEventListener("pointerleave", off);
    node.addEventListener("focus", () => on(node.getBoundingClientRect()));
    node.addEventListener("blur", off);
  }
  // 한 마크에 호버하면 나머지를 흐리게(차트 전체 .dim, 해당 마크 .hot).
  function hotToggle(svgRoot, mark) {
    return (on) => { svgRoot.classList.toggle("dim", on); mark.classList.toggle("hot", on); };
  }

  // ── 표로 보기: 모든 차트의 접근성 대체물 ──
  function tableView(headers, rows, summary = "표로 보기") {
    return h("details", { class: "table-view" }, h("summary", { text: summary }),
      h("table", null,
        h("thead", null, h("tr", null, headers.map((x) => h("th", { text: x })))),
        h("tbody", null, rows.map((r) => h("tr", null, r.map((c) => h("td", { text: String(c) })))))));
  }

  // ── 눈금 ────────────────────────────────────────────────
  function niceStep(span, count) {
    const raw = span / Math.max(1, count);
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const n = raw / mag;
    return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * mag;
  }
  function ticks(min, max, count = 5) {
    const st = niceStep(max - min || 1, count);
    const lo = Math.floor(min / st) * st, hi = Math.ceil(max / st) * st;
    const out = [];
    for (let v = lo; v <= hi + st / 2; v += st) out.push(+v.toFixed(10));
    return out;
  }
  // 한글은 대략 1em, 라틴은 0.58em 폭으로 텍스트 폭을 어림한다(라벨 충돌 회피용).
  const textW = (str, size = 11.5) =>
    [...str].reduce((a, ch) => a + (ch.charCodeAt(0) > 0x2e80 ? size : size * 0.58), 0);

  // ── 누적 막대(부분-전체) ────────────────────────────────
  function stack(parts, opts = {}) {
    const total = parts.reduce((a, p) => a + p.value, 0);
    const bar = h("div", { class: "stack", role: "img",
      "aria-label": parts.map((p) => `${p.label} ${p.value}`).join(", ") });
    for (const p of parts) {
      if (!p.value) continue;
      const seg = h("i", { tabindex: "0", style: { flexGrow: p.value, background: p.color } });
      bindTip(seg, () => ({ title: p.label, rows: [{ value: pct(p.value / total), label: `${fmtInt(p.value)}${opts.unit || "개"}`, color: p.color }] }));
      bar.append(seg);
    }
    if (!bar.children.length) bar.append(h("i", { style: { flexGrow: 1, background: "var(--grid)" } }));
    const legend = h("div", { class: "legend" }, parts.map((p) =>
      h("span", { class: "it" }, h("span", { class: "sw", style: { background: p.color } }),
        h("span", { text: p.label }), h("b", { text: total ? pct(p.value / total) : "–" }),
        h("span", { text: `(${fmtInt(p.value)})` }))));
    const wrap = h("div", null, bar, legend);
    wrap.play = (d = 0) => {
      play(bar, [{ clipPath: "inset(0 100% 0 0 round 6px)" }, { clipPath: "inset(0 0% 0 0 round 6px)" }], { duration: 600, delay: d });
      play(legend, [{ opacity: 0, transform: "translateY(4px)" }, { opacity: 1, transform: "none" }], { delay: d + 250 });
    };
    return wrap;
  }

  // ── 미터(비율 하나) ─────────────────────────────────────
  function meter(ratio, label) {
    const r = Math.max(0, Math.min(1, ratio || 0));
    const fill = h("i", { style: { width: `${r * 100}%` } });
    const m = h("div", { class: "meter", role: "meter", "aria-label": label || "",
      "aria-valuenow": String(Math.round(r * 100)), "aria-valuemin": "0", "aria-valuemax": "100" }, fill);
    m.play = (d = 0) => play(fill, [{ transform: "scaleX(0)" }, { transform: "scaleX(1)" }], { delay: d, duration: 600 });
    return m;
  }

  // ── 가로 막대 목록(값 크기 비교) ─────────────────────────
  function hbars(rows, opts = {}) {
    const max = opts.max ?? Math.max(1e-9, ...rows.map((r) => r.value));
    const fmt = opts.fmt || fmtInt;
    const g = h("div", { class: "hbars" });
    const bars = [];
    rows.forEach((r) => {
      const bar = h("span", { class: "bar", style: { width: `${(r.value / max) * 100}%`, background: r.color } });
      const track = h("span", { class: "track", tabindex: "0" }, bar);
      bindTip(track, () => ({ title: r.label, rows: [{ value: fmt(r.value), label: r.tipLabel || "", color: r.color }] }));
      g.append(h("span", { class: "name", title: r.label, text: r.label }), track,
        h("span", { class: "val" }, r.chip || null, r.chip ? " " : null, h("span", { text: fmt(r.value) })));
      bars.push(bar);
    });
    g.play = (d = 0) => bars.forEach((b, i) =>
      play(b, [{ transform: "scaleX(0)" }, { transform: "scaleX(1)" }], { delay: d + delay(i) }));
    return g;
  }

  // 위 모서리만 둥근 막대(데이터 끝 4px 둥글게, 기준선 쪽은 직각).
  function topRounded(x, y, w, hgt, r) {
    r = Math.min(r, w / 2, hgt);
    return `M${x},${y + hgt}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + hgt}Z`;
  }

  // ── 월별 누적 기둥 ───────────────────────────────────────
  // cols = [{key:'2025-07', values:{진보:3,...}}], series = [{key, label, color}] (아래에서 위로 쌓는 순서)
  function columns(cols, series, opts = {}) {
    const W = 920, H = opts.height || 250, m = { l: 34, r: 6, t: 10, b: 26 };
    const pw = W - m.l - m.r, ph = H - m.t - m.b;
    const sums = cols.map((c) => series.reduce((a, se) => a + (c.values[se.key] || 0), 0));
    const yt = ticks(0, Math.max(1, ...sums), 4);
    const ymax = yt[yt.length - 1];
    const y = (v) => m.t + ph - (v / ymax) * ph;
    const svg = s("svg", { class: "viz", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": opts.label || "" });
    for (const t of yt) {
      s("line", { class: t === 0 ? "axis" : "grid", x1: m.l, x2: W - m.r, y1: y(t), y2: y(t) }, svg);
      s("text", { x: m.l - 6, y: y(t) + 4, "text-anchor": "end", text: fmtInt(t) }, svg);
    }
    const band = pw / Math.max(1, cols.length);
    const bw = Math.max(3, Math.min(24, band * 0.7));
    const every = Math.ceil(cols.length / 9);
    const groups = [];
    cols.forEach((c, i) => {
      const cx = m.l + band * i + band / 2;
      const g = s("g", { class: "mark", style: { transformBox: "fill-box", transformOrigin: "center bottom" } }, svg);
      let acc = 0;
      const visible = series.filter((se) => c.values[se.key] > 0);
      visible.forEach((se, k) => {
        const v = c.values[se.key];
        const y0 = y(acc), y1 = y(acc + v);
        const gap = k > 0 ? 2 : 0;                       // 조각 사이 2px 표면 간격
        const hgt = Math.max(0.5, y0 - y1 - gap);
        const isTop = k === visible.length - 1;
        if (isTop) s("path", { d: topRounded(cx - bw / 2, y1, bw, hgt, 4), style: { fill: se.color } }, g);
        else s("rect", { x: cx - bw / 2, y: y1, width: bw, height: hgt, style: { fill: se.color } }, g);
        acc += v;
      });
      // 히트 영역: 막대보다 넓게(밴드 전체 높이)
      const hit = s("rect", { x: cx - band / 2, y: m.t, width: band, height: ph, fill: "transparent", tabindex: "0" }, svg);
      bindTip(hit, () => ({ title: opts.xFmt ? opts.xFmt(c.key) : c.key,
        rows: series.map((se) => ({ value: fmtInt(c.values[se.key] || 0), label: se.label, color: se.color }))
          .concat([{ value: fmtInt(sums[i]), label: "합계" }]) }), hotToggle(svg, g));
      if (i % every === 0) s("text", { x: cx, y: H - 8, "text-anchor": "middle", text: opts.xFmt ? opts.xFmt(c.key) : c.key }, svg);
      groups.push(g);
    });
    svg.play = (d = 0) => groups.forEach((g, i) =>
      play(g, [{ transform: "scaleY(0)", opacity: 0.3 }, { transform: "scaleY(1)", opacity: 1 }],
        { delay: d + Math.min(i * 14, 420) }));
    return svg;
  }

  // ── 점 차트(채널 성향 점수 비교) ─────────────────────────
  // rows = [{label, value, color, note}]; domain = [min, max]
  function dotplot(rows, opts = {}) {
    const rowH = 30, W = 920, m = { l: 120, r: 70, t: 22, b: 8 };
    const H = m.t + rows.length * rowH + m.b;
    const [d0, d1] = opts.domain;
    const pw = W - m.l - m.r;
    const x = (v) => m.l + ((v - d0) / (d1 - d0)) * pw;
    const svg = s("svg", { class: "viz", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": opts.label || "" });
    for (const t of ticks(d0, d1, 7).filter((t) => t >= d0 - 1e-9 && t <= d1 + 1e-9)) {
      s("line", { class: t === 0 ? "axis" : "grid", x1: x(t), x2: x(t), y1: m.t - 6, y2: H - m.b }, svg);
      s("text", { x: x(t), y: m.t - 10, "text-anchor": "middle", text: (opts.tickFmt || String)(t) }, svg);
    }
    const dots = [];
    rows.forEach((r, i) => {
      const cy = m.t + i * rowH + rowH / 2;
      const g = s("g", { class: "mark" }, svg);
      s("text", { class: "lbl", x: m.l - 12, y: cy + 4, "text-anchor": "end", text: r.label }, g);
      s("line", { class: "grid", x1: m.l, x2: W - m.r, y1: cy, y2: cy }, g);
      const dot = s("circle", { cx: x(r.value), cy, r: 6.5, style: { fill: r.color, stroke: "var(--surface)", strokeWidth: 2 } }, g);
      s("text", { class: "lbl-strong", x: x(r.value) + 12, y: cy + 4, text: (opts.fmt || String)(r.value) }, g);
      const hit = s("rect", { x: m.l - 110, y: cy - rowH / 2, width: W - m.l + 110 - m.r + 60, height: rowH, fill: "transparent", tabindex: "0" }, svg);
      bindTip(hit, () => ({ title: r.label, rows: [{ value: (opts.fmt || String)(r.value), label: r.note || "", color: r.color }] }), hotToggle(svg, g));
      dots.push({ dot, dx: (opts.from != null ? x(opts.from) : m.l) - x(r.value) });
    });
    svg.play = (d = 0) => dots.forEach(({ dot, dx }, i) =>
      play(dot, [{ transform: `translateX(${dx}px)`, opacity: 0 }, { transform: "translateX(0)", opacity: 1 }],
        { delay: d + delay(i), duration: 600 }));
    return svg;
  }

  // ── 산점도(성향 × 과격도) — 라벨은 겹치지 않는 자리에만, 못 놓으면 툴팁·표가 맡는다 ──
  function scatter(points, opts) {
    const W = 460, H = 330, m = { l: 46, r: 14, t: 26, b: 40 };
    const pw = W - m.l - m.r, ph = H - m.t - m.b;
    const xt = ticks(opts.x.domain[0], opts.x.domain[1], 5), yt = ticks(opts.y.domain[0], opts.y.domain[1], 5);
    const [x0, x1] = [xt[0], xt[xt.length - 1]], [y0, y1] = [yt[0], yt[yt.length - 1]];
    const X = (v) => m.l + ((v - x0) / (x1 - x0)) * pw;
    const Y = (v) => m.t + ph - ((v - y0) / (y1 - y0)) * ph;
    const svg = s("svg", { class: "viz", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": opts.label || "" });
    for (const t of yt) {
      s("line", { class: "grid", x1: m.l, x2: W - m.r, y1: Y(t), y2: Y(t) }, svg);
      s("text", { x: m.l - 6, y: Y(t) + 4, "text-anchor": "end", text: opts.y.fmt(t) }, svg);
    }
    for (const t of xt) {
      s("line", { class: t === 0 ? "axis" : "grid", x1: X(t), x2: X(t), y1: m.t, y2: m.t + ph }, svg);
      s("text", { x: X(t), y: m.t + ph + 16, "text-anchor": "middle", text: opts.x.fmt(t) }, svg);
    }
    s("text", { x: m.l + pw, y: H - 4, "text-anchor": "end", text: opts.x.label }, svg);
    s("text", { x: m.l, y: 12, text: `↑ ${opts.y.label}` }, svg);

    const placed = [], marks = [];
    const hitsBox = (b) => placed.some((p) => !(b.x2 <= p.x1 || b.x1 >= p.x2 || b.y2 <= p.y1 || b.y1 >= p.y2)); // 맞닿는 건 겹침 아님
    const pts = points.map((p) => ({ ...p, cx: X(p.x), cy: Y(p.y) }));
    for (const p of pts) placed.push({ x1: p.cx - 8, x2: p.cx + 8, y1: p.cy - 8, y2: p.cy + 8 }); // 점 자리 선점
    for (const p of pts) {
      const g = s("g", { class: "mark" }, svg);
      const dot = s("circle", { cx: p.cx, cy: p.cy, r: 6.5, style: { fill: p.color, stroke: "var(--surface)", strokeWidth: 2,
        transformBox: "fill-box", transformOrigin: "center" } }, g);
      const w = textW(p.label, 11.5);
      const cands = [[10, 4, "start"], [-10, 4, "end"], [0, -11, "middle"], [0, 18, "middle"]];
      let label = null;
      for (const [dx, dy, anchor] of cands) {
        const lx = anchor === "start" ? p.cx + dx : anchor === "end" ? p.cx + dx - w : p.cx - w / 2;
        const b = { x1: lx - 2, x2: lx + w + 2, y1: p.cy + dy - 11, y2: p.cy + dy + 3 };
        if (b.x1 < m.l || b.x2 > W - 2 || b.y1 < 0 || b.y2 > m.t + ph || hitsBox(b)) continue;
        placed.push(b);
        label = s("text", { class: "lbl", x: p.cx + dx, y: p.cy + dy, "text-anchor": anchor, text: p.label }, g);
        break;
      }
      const hit = s("circle", { cx: p.cx, cy: p.cy, r: 14, fill: "transparent", tabindex: "0" }, svg); // 히트 영역 ≥24px
      bindTip(hit, () => ({ title: p.label, rows: p.tip }), hotToggle(svg, g));
      marks.push({ dot, label });
    }
    svg.play = (d = 0) => marks.forEach(({ dot, label }, i) => {
      play(dot, [{ transform: "scale(0)" }, { transform: "scale(1)" }], { delay: d + delay(i), duration: 500 });
      if (label) play(label, [{ opacity: 0 }, { opacity: 1 }], { delay: d + delay(i) + 200 });
    });
    return svg;
  }

  globalThis.Viz = { h, s, play, countUp, onVisible, bindTip, hideTip, tableView, ticks,
                     stack, meter, hbars, columns, dotplot, scatter, pct, fmtInt, delay, reduced };
})();
