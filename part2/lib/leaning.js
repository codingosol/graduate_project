// 채널 → 성향 버킷(진보/중도/보수) 룩업. data/leaning_table.json 을 로드해 인덱싱.
// ⚠️ 점수 부호로 나누지 않는다 — Part 1이 검증한 3버킷 라벨을 그대로 쓴다(Part2.md §3).
// 테이블에 없는 채널은 null(미상).

globalThis.Leaning = (function () {
  let byTitle = null; // 정규화된 채널명 → bucket
  let byId = null; // channel_id → bucket
  let ready = null;

  const norm = (s) => (s || "").normalize("NFC").trim();

  async function load() {
    if (ready) return ready;
    ready = (async () => {
      const url = chrome.runtime.getURL("data/leaning_table.json");
      const table = await (await fetch(url)).json();
      byTitle = new Map();
      byId = new Map();
      for (const ch of table.channels || []) {
        if (ch.bucket && ch.bucket !== "미상") {
          if (ch.title) byTitle.set(norm(ch.title), ch.bucket);
          if (ch.channel_id) byId.set(ch.channel_id, ch.bucket);
        }
      }
    })();
    return ready;
  }

  // {channel, channelId} → "진보"|"중도"|"보수"|null
  function lookup({ channel, channelId }) {
    if (channelId && byId.has(channelId)) return byId.get(channelId);
    if (channel && byTitle.has(norm(channel))) return byTitle.get(norm(channel));
    return null;
  }

  // 버킷 → 색 토큰(lib/ui.css). 미상은 흐린 회색(탈강조).
  const COLORS = { 진보: "var(--c-left)", 중도: "var(--c-center)", 보수: "var(--c-right)", 미상: "var(--c-unknown)" };

  // 판별된 영상 기준 기울기: (보수 − 진보) / 판별 수. 임의 임계값 대신 %p 차이를 그대로 말한다.
  function summary(counts) {
    const n = counts.진보 + counts.중도 + counts.보수;
    if (!n) return null;
    const diff = Math.round(((counts.보수 - counts.진보) / n) * 100);
    const side = diff > 0 ? "보수" : "진보";
    return {
      diff,
      short: Math.abs(diff) < 5 ? "균형" : `${side} +${Math.abs(diff)}%p`,
      text: Math.abs(diff) < 5
        ? "진보·보수 채널 영상이 고르게 섞여 있어요"
        : `${side} 채널 영상이 반대쪽보다 ${Math.abs(diff)}%p 더 많아요`,
    };
  }

  return { load, lookup, summary, COLORS };
})();
