// 모드 B: Google Takeout 시청기록 업로드 → 파싱 → 정치·뉴스 판별 → 성향 분포/추세 시각화.
// + 참고용 채널 대시보드(Part 1 channel_dashboard.json). 차트는 lib/viz.js(SVG/CSS, 외부 라이브러리 없음).
// 분류는 Part 1 재사용(politics.js = 제목 필터, leaning.js = 채널 3버킷).
// 완전 클라이언트 사이드 — 업로드 파일은 브라우저 밖으로 나가지 않는다.

// ── 파싱 ─────────────────────────────────────────────
const videoIdFromUrl = (u) => { const m = (u || "").match(/[?&]v=([\w-]+)/); return m ? m[1] : null; };
const channelIdFromUrl = (u) => { const m = (u || "").match(/\/channel\/(UC[\w-]+)/); return m ? m[1] : null; };

function parseTime(s) {
  // 셀 텍스트는 "제목 · 채널명 · 시청 시각" 순이라 시청 시각이 마지막에 온다.
  // 제목 속 날짜("2001. 11. 3. 방송")에 속지 않도록 마지막 일치를 쓴다.
  // 한국어 로케일: "2026. 8. 23. 오후 10:00:00 GMT+09:00" / 영어 로케일: "Aug 23, 2026, 10:00:00 PM"
  const ko = [...s.matchAll(/(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\./g)].pop();
  if (ko) return new Date(+ko[1], +ko[2] - 1, +ko[3]);
  const en = [...s.matchAll(/([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})/g)].pop();
  if (en) { const d = new Date(en[1]); if (!isNaN(d)) return d; }
  return null;
}

function parseJson(text) {
  const arr = JSON.parse(text);
  const out = [];
  for (const e of arr || []) {
    const vid = videoIdFromUrl(e.titleUrl || "");
    if (!vid) continue; // 링크 없음(삭제된 영상) → 스킵
    if ((e.details || []).some((d) => /google ads/i.test(d.name || ""))) continue; // 광고
    const sub = (e.subtitles || [])[0] || {};
    out.push({
      vid,
      title: e.title || "", // "Watched <제목>" 접두어는 substring 매칭에 무해 → 그대로 사용
      channel: sub.name || "",
      channelId: channelIdFromUrl(sub.url || ""),
      time: e.time ? new Date(e.time) : null,
    });
  }
  return out;
}

function parseHtml(text) {
  const doc = new DOMParser().parseFromString(text, "text/html");
  const out = [];
  for (const cell of doc.querySelectorAll(".content-cell.mdl-typography--body-1")) {
    const raw = cell.textContent || "";
    if (/google ads/i.test(raw)) continue; // 광고
    const vlink = cell.querySelector('a[href*="/watch?v="]');
    if (!vlink) continue;
    const vid = videoIdFromUrl(vlink.getAttribute("href"));
    if (!vid) continue;
    const clink = cell.querySelector('a[href*="/channel/"]');
    out.push({
      vid,
      title: (vlink.textContent || "").trim(),
      channel: clink ? (clink.textContent || "").trim() : "",
      channelId: channelIdFromUrl(clink && clink.getAttribute("href")),
      time: parseTime(raw),
    });
  }
  return out;
}

function detectAndParse(text) {
  const head = text.slice(0, 256).trimStart();
  return head[0] === "[" || head[0] === "{" ? parseJson(text) : parseHtml(text);
}

// ── 분석 ─────────────────────────────────────────────
function analyze(entries) {
  const counts = { 진보: 0, 중도: 0, 보수: 0, 미상: 0 };
  const monthly = new Map(); // "YYYY-MM" → {진보,중도,보수}
  const chanCount = new Map(); // 채널명 → {bucket, n}
  let political = 0, first = null, last = null;
  for (const e of entries) {
    // 유튜브(2005~) 이전 날짜는 파싱 오류로 보고 기간 계산에서 뺀다.
    const t = e.time && !isNaN(e.time) && e.time.getFullYear() >= 2005 ? e.time : null;
    if (t) { if (!first || t < first) first = t; if (!last || t > last) last = t; }
    if (!Politics.isPoliticalTitle(e.title)) continue;
    political++;
    const bucket = Leaning.lookup({ channel: e.channel, channelId: e.channelId }) || "미상";
    counts[bucket]++;

    const key = e.channel || "(알 수 없음)";
    const c = chanCount.get(key) || { bucket, n: 0 };
    c.n++;
    chanCount.set(key, c);

    if (bucket !== "미상" && t) {
      const ym = t.getFullYear() + "-" + String(t.getMonth() + 1).padStart(2, "0");
      const mo = monthly.get(ym) || { 진보: 0, 중도: 0, 보수: 0 };
      mo[bucket]++;
      monthly.set(ym, mo);
    }
  }
  return { total: entries.length, political, counts, monthly, chanCount, first, last };
}

// 첫 달부터 마지막 달까지 빈 달도 채운다(빈 달을 빼면 시간축이 거짓말을 한다).
function monthRange(monthly) {
  const keys = [...monthly.keys()].sort();
  if (!keys.length) return [];
  let [y, m] = keys[0].split("-").map(Number);
  const [y1, m1] = keys[keys.length - 1].split("-").map(Number);
  const out = [];
  while (y < y1 || (y === y1 && m <= m1)) {
    const k = `${y}-${String(m).padStart(2, "0")}`;
    out.push({ key: k, values: monthly.get(k) || { 진보: 0, 중도: 0, 보수: 0 } });
    if (++m > 12) { m = 1; y++; }
  }
  return out;
}

// ── 결과 렌더 ─────────────────────────────────────────
const $ = (id) => document.getElementById(id);
const ymLabel = (d) => `${d.getFullYear()}.${String(d.getMonth() + 1).padStart(2, "0")}`;
const shortYm = (k) => `${k.slice(2, 4)}.${k.slice(5)}`;

function kpi(V, label, meta, i) {
  const v = V.h("div", { class: "v" });
  const box = V.h("div", { class: "kpi reveal", style: { "--i": i } }, V.h("div", { class: "l", text: label }), v, meta);
  return { box, v };
}

function cardHead(V, card, title, sub) {
  card.replaceChildren(V.h("h3", { class: "card-title", text: title }), V.h("p", { class: "card-sub", text: sub }));
}

function renderResult(r) {
  const V = Viz, { h } = V;
  const classified = r.counts.진보 + r.counts.중도 + r.counts.보수;
  const coverage = r.political ? classified / r.political : 0;
  const sum = Leaning.summary(r.counts);

  $("resultSub").textContent = r.first
    ? `${ymLabel(r.first)} ~ ${ymLabel(r.last)} 시청기록 ${V.fmtInt(r.total)}건 분석`
    : `시청기록 ${V.fmtInt(r.total)}건 분석`;

  // KPI
  const k1 = kpi(V, "총 시청 영상", h("div", { class: "m", text: "삭제된 영상·광고 제외" }), 0);
  const k2 = kpi(V, "정치·뉴스 영상", h("div", { class: "m", text: `전체의 ${((r.political / (r.total || 1)) * 100).toFixed(1)}%` }), 1);
  const cov = V.meter(coverage, "판별 커버리지");
  const k3 = kpi(V, "판별 커버리지", h("div", null, h("div", { class: "m", text: `성향을 아는 채널 ${V.fmtInt(classified)}개` }), cov), 2);
  const k4 = kpi(V, "기울기", h("div", { class: "m", text: sum ? "판별된 영상 기준 보수−진보" : "판별된 영상 없음" }), 3);
  k4.v.textContent = sum ? sum.short : "–";
  $("kpis").replaceChildren(k1.box, k2.box, k3.box, k4.box);

  // 성향 분포
  const dist = $("distCard");
  cardHead(V, dist, "성향 분포", `판별된 ${V.fmtInt(classified)}개 기준 · 미상 ${V.fmtInt(r.counts.미상)}개는 커버리지에 반영`);
  const st = V.stack(["진보", "중도", "보수"].map((b) => ({ label: b, value: r.counts[b], color: Leaning.COLORS[b] })));
  dist.append(st);
  if (sum) dist.append(h("p", { class: "summary" }, h("b", { text: sum.short }), ` · ${sum.text}`));
  dist.append(V.tableView(["성향", "영상 수", "비율"], ["진보", "중도", "보수", "미상"].map((b) =>
    [b, V.fmtInt(r.counts[b]), b === "미상" ? "–" : classified ? V.pct(r.counts[b] / classified) : "–"])));

  // 많이 본 정치·뉴스 채널
  const top = [...r.chanCount.entries()].sort((a, b) => b[1].n - a[1].n).slice(0, 8);
  const topCard = $("topCard");
  cardHead(V, topCard, "가장 많이 본 정치·뉴스 채널", "정치 키워드가 걸린 영상 수 · 상위 8개");
  let hb = null;
  if (top.length) {
    hb = V.hbars(top.map(([name, c]) => ({
      label: name, value: c.n, color: Leaning.COLORS[c.bucket], tipLabel: `${c.bucket} · 영상`,
      chip: h("span", { class: "chip" }, h("span", { class: "sw", style: { background: Leaning.COLORS[c.bucket] } }), c.bucket),
    })));
    topCard.append(hb, V.tableView(["채널", "성향", "영상 수"], top.map(([n, c]) => [n, c.bucket, c.n])));
  } else {
    topCard.append(h("p", { class: "note", text: "정치·뉴스로 판별된 영상이 없어요." }));
  }

  // 월별 추세
  const cols = monthRange(r.monthly);
  const trend = $("trendCard");
  cardHead(V, trend, "월별 정치·뉴스 시청", "성향을 아는 채널의 영상만, 월 단위 · 막대에 올리면 달별 값이 보여요");
  const series = ["진보", "중도", "보수"].map((b) => ({ key: b, label: b, color: Leaning.COLORS[b] }));
  let col = null;
  if (cols.length) {
    col = V.columns(cols, series, { xFmt: shortYm, label: "월별 성향별 시청 영상 수" });
    trend.append(h("div", { class: "legend", style: { margin: "0 0 10px" } }, series.map((se) =>
      h("span", { class: "it" }, h("span", { class: "sw", style: { background: se.color } }), h("span", { text: se.label })))), col,
    V.tableView(["월", "진보", "중도", "보수", "합계"], cols.filter((c) => c.values.진보 + c.values.중도 + c.values.보수)
      .map((c) => [c.key, c.values.진보, c.values.중도, c.values.보수, c.values.진보 + c.values.중도 + c.values.보수])));
  } else {
    trend.append(h("p", { class: "note", text: "시기를 알 수 있는 판별 영상이 없어요." }));
  }

  // 등장: 섹션 → KPI(단계적) → 막대들. 동시에 움직이는 무리는 3~4개 이내로.
  $("result").hidden = false;
  $("result").scrollIntoView({ behavior: V.reduced() ? "auto" : "smooth", block: "start" });
  V.countUp(k1.v, r.total);
  V.countUp(k2.v, r.political, undefined, { delay: 40 });
  V.countUp(k3.v, coverage * 100, (v) => `${Math.round(v)}%`, { delay: 80 });
  cov.play(200);
  st.play(260);
  if (hb) hb.play(320);
  if (col) V.onVisible(col, () => col.play(0));
}

// ── 참고 채널 대시보드 (Part 1) ───────────────────────
async function renderDashboard() {
  const V = Viz, { h } = V;
  let db;
  try {
    db = await (await fetch(chrome.runtime.getURL("data/channel_dashboard.json"))).json();
  } catch {
    $("ref").hidden = true; // 데이터 없으면 섹션째 생략
    return;
  }
  const chans = db.channels || [];
  const color = (b) => Leaning.COLORS[b] || Leaning.COLORS.미상;
  const f3 = (v) => (v > 0 ? "+" : "") + v.toFixed(3);

  // 성향 점수 — 점 차트(0을 기준선으로 쓰는 막대는 "0 = 중립"으로 오독되므로 점으로 위치만 보인다)
  const byLean = [...chans].sort((a, b) => a.leaning_score - b.leaning_score);
  const lt = V.ticks(Math.min(...byLean.map((c) => c.leaning_score)), Math.max(...byLean.map((c) => c.leaning_score)), 7);
  const lean = V.dotplot(byLean.map((c) => ({ label: c.outlet, value: c.leaning_score, color: color(c.bucket), note: c.bucket })),
    { domain: [lt[0], lt[lt.length - 1]], from: 0, fmt: f3, tickFmt: (t) => (t > 0 ? "+" : "") + t.toFixed(1), label: "채널별 성향 점수" });
  const leanCard = $("leanCard");
  cardHead(V, leanCard, "채널별 성향 점수", "← 진보 · 보수 → (댓글 성향의 채널 평균, −1~+1) · 색은 검증된 구분");
  leanCard.append(h("div", { class: "legend", style: { margin: "0 0 6px" } }, ["진보", "중도", "보수"].map((b) =>
    h("span", { class: "it" }, h("span", { class: "sw", style: { background: color(b), borderRadius: "50%" } }), h("span", { text: b })))),
  lean, V.tableView(["채널", "구분", "성향 점수", "과격도"], byLean.map((c) => [c.outlet, c.bucket, f3(c.leaning_score), c.intensity.toFixed(3)])));

  // 과격도 — 크기 비교라 한 가지 색(청록) 막대
  const byInt = [...chans].sort((a, b) => b.intensity - a.intensity);
  const avg = chans.reduce((a, c) => a + c.intensity, 0) / (chans.length || 1);
  const intBars = V.hbars(byInt.map((c) => ({ label: c.outlet, value: c.intensity, color: "var(--c-int)", tipLabel: `과격도 · ${c.bucket}` })),
    { max: V.ticks(0, Math.max(...byInt.map((c) => c.intensity)), 4).pop(), fmt: (v) => v.toFixed(3) });
  const intCard = $("intCard");
  cardHead(V, intCard, "채널별 과격도", `0 = 온건 ~ 1 = 과격 · 채널 평균 ${avg.toFixed(3)}`);
  intCard.append(intBars);

  // 성향 × 과격도 — 두 축이 독립이라는 게 핵심(가장 진보적인 채널이 가장 온건)
  const xs = chans.map((c) => c.leaning_score), ys = chans.map((c) => c.intensity);
  const sc = V.scatter(chans.map((c) => ({ label: c.outlet, x: c.leaning_score, y: c.intensity, color: color(c.bucket),
    tip: [{ value: f3(c.leaning_score), label: "성향", color: color(c.bucket) }, { value: c.intensity.toFixed(3), label: "과격도", color: "var(--c-int)" }] })),
  { x: { domain: [Math.min(...xs), Math.max(...xs)], label: "성향 →", fmt: (t) => t.toFixed(1) },
    y: { domain: [Math.min(...ys) - 0.01, Math.max(...ys) + 0.01], label: "과격도", fmt: (t) => t.toFixed(2) },
    label: "채널별 성향과 과격도" });
  const scCard = $("scatterCard");
  cardHead(V, scCard, "성향 × 과격도", "성향과 과격도는 따로 움직여요 — 가장 진보적인 채널이 가장 온건");
  scCard.append(sc);

  const when = db.generated_at ? new Date(db.generated_at) : null;
  $("snapshot").textContent = `기준 데이터: Part 1 분석 스냅샷${when ? ` (${when.toISOString().slice(0, 10)} 추출)` : ""} · ` +
    "채널 성향은 채널별 정치 영상 댓글 수만 건을 KcELECTRA로 분류해 평균한 값이에요.";

  V.onVisible(lean, () => lean.play());
  V.onVisible(intBars, () => intBars.play());
  V.onVisible(sc, () => sc.play());
}

// ── 업로드 배선 ───────────────────────────────────────
const status = (s) => ($("status").textContent = s);

async function handleFile(file) {
  if (!file) return;
  status(`읽는 중… (${(file.size / 1048576).toFixed(1)} MB)`);
  $("progress").hidden = false;
  await new Promise((r) => requestAnimationFrame(() => setTimeout(r, 30))); // 진행 표시를 먼저 그린다
  let entries;
  try {
    entries = detectAndParse(await file.text());
  } catch (e) {
    $("progress").hidden = true;
    status("파일을 읽지 못했어요: " + e.message);
    return;
  }
  $("progress").hidden = true;
  if (!entries.length) {
    status("시청기록 항목을 찾지 못했어요. '시청 기록' 파일이 맞는지 확인해 주세요.");
    return;
  }
  await Leaning.load();
  const r = analyze(entries);
  status(`분석 완료 — 시청기록 ${r.total.toLocaleString("ko-KR")}건 중 정치·뉴스 ${r.political.toLocaleString("ko-KR")}건`);
  renderResult(r);
}

if (typeof document !== "undefined") {
  const drop = $("drop"), input = $("file");
  drop.addEventListener("click", () => input.click());
  drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
  input.addEventListener("change", (e) => handleFile(e.target.files[0]));
  ["dragenter", "dragover"].forEach((ev) =>
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) =>
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => handleFile(e.dataTransfer.files[0]));
  Leaning.load().then(renderDashboard);
}

// Node 단위테스트용(익스텐션에선 module 미정의라 무시됨).
if (typeof module !== "undefined") module.exports = { parseJson, parseHtml, parseTime, detectAndParse, analyze, monthRange };
