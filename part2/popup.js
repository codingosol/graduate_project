// 모드 A: 현재 탭의 피드 카드를 content script에서 받아 분류·집계·렌더.
// 분류 로직은 Part 1 재사용(politics.js = 제목 필터, leaning.js = 채널 3버킷).

const $ = (id) => document.getElementById(id);
const { h } = Viz;

async function getActiveTabCards() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !/^https:\/\/(www\.)?youtube\.com/.test(tab.url || "")) {
    return { error: "유튜브 탭에서 열어 주세요", hint: "youtube.com 페이지를 연 상태에서 아이콘을 눌러야 피드를 읽을 수 있어요." };
  }
  try {
    return (await chrome.tabs.sendMessage(tab.id, { type: "getFeed" })) || { cards: [] };
  } catch {
    // content script가 아직 없음(익스텐션 로드 전부터 열려 있던 탭). 프로그램적으로 주입 후 재시도.
    try {
      await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
      await new Promise((r) => setTimeout(r, 600)); // 초기 스캔 대기
      return (await chrome.tabs.sendMessage(tab.id, { type: "getFeed" })) || { cards: [] };
    } catch {
      return { error: "피드를 읽지 못했어요", hint: "유튜브 페이지를 새로고침한 뒤 다시 열어 주세요." };
    }
  }
}

function classify(cards) {
  const counts = { 진보: 0, 중도: 0, 보수: 0, 미상: 0 };
  let political = 0;
  for (const c of cards) {
    if (!Politics.isPoliticalTitle(c.title)) continue;
    political++;
    counts[Leaning.lookup(c) || "미상"]++;
  }
  return { counts, political, total: cards.length };
}

function kpi(label, meta) {
  const v = h("div", { class: "v" });
  const box = h("div", { class: "kpi reveal" }, h("div", { class: "l", text: label }), v, meta);
  return { box, v };
}

function empty(icon, title, hint) {
  $("main").replaceChildren(h("div", { class: "empty fade" },
    h("div", { class: "ico", "aria-hidden": "true", text: icon }), h("b", { text: title }), h("p", { text: hint })));
}

function render({ counts, political, total }) {
  const classified = counts.진보 + counts.중도 + counts.보수;
  const coverage = political ? classified / political : 0;

  const k1 = kpi("정치·뉴스 영상", h("div", { class: "m", text: `전체 ${total}개 중 ${Viz.pct(total ? political / total : 0)}` }));
  const cov = Viz.meter(coverage, "판별 커버리지");
  const k2 = kpi("판별 커버리지", h("div", null, cov));
  k2.box.style.setProperty("--i", 1);

  const panel = h("div", { class: "card panel reveal", style: { "--i": 2 } });
  const s = Leaning.summary(counts);
  if (!political) {
    panel.append(h("div", { class: "card-title", text: "성향 분포" }),
      h("p", { class: "summary", text: "지금 화면에는 정치·뉴스 영상이 없어요. 피드가 정치 쪽으로 기울어 있지 않다는 뜻이에요." }));
  } else {
    const parts = ["진보", "중도", "보수"].map((b) => ({ label: b, value: counts[b], color: Leaning.COLORS[b] }));
    const st = Viz.stack(parts);
    panel.append(
      h("div", { class: "card-title" }, "성향 분포", h("span", { text: `판별 ${classified}개` })),
      h("div", { style: { marginTop: "12px" } }, st),
      s ? h("p", { class: "summary" }, h("b", { text: s.short }), ` · ${s.text}`) : null,
      counts.미상 ? h("p", { class: "note", text: `미상 ${counts.미상}개는 성향 표에 없는 채널이라 분포에서 빠졌어요.` }) : null);
    requestAnimationFrame(() => st.play(180));
  }

  $("main").replaceChildren(h("div", { class: "kpis" }, k1.box, k2.box), panel);
  Viz.countUp(k1.v, political);
  Viz.countUp(k2.v, coverage * 100, (v) => `${Math.round(v)}%`, { delay: 40 });
  cov.play(120);
}

async function main() {
  const [data] = await Promise.all([getActiveTabCards(), Leaning.load()]);
  if (data.error) return empty("↗", data.error, data.hint);
  if (!data.cards.length) return empty("⌛", "보이는 영상이 없어요", "피드를 조금 스크롤한 뒤 다시 열어 주세요.");
  $("ctx").textContent = `현재 탭에 보이는 영상 ${data.cards.length}개 기준`;
  render(classify(data.cards));
}

$("detail").addEventListener("click", () => {
  chrome.tabs.create({ url: chrome.runtime.getURL("analysis.html") });
});

main();
