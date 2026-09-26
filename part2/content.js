// 유튜브 피드 DOM 관찰 → 영상 카드에서 {videoId, channel, channelId, title} 추출.
// 분류(정치 판별·성향 룩업)는 하지 않는다 — 팝업이 politics.js/leaning.js로 처리.
// content script는 "DOM에서 뽑기"만 담당해 얇게 유지한다.
//
// ⚠️ 유튜브 DOM은 자주 바뀐다. 셀렉터는 여러 후보를 두고, 못 찾으면 그 카드만 조용히 건너뛴다.

(function () {
  const feed = new Map(); // videoId → {videoId, channel, channelId, title}

  // 카드 컨테이너 후보(홈 그리드 / 구버전 / 워치 사이드바 / 신규 lockup).
  const CARD_SEL = [
    "ytd-rich-item-renderer",
    "ytd-rich-grid-media",
    "ytd-video-renderer",
    "ytd-grid-video-renderer",
    "ytd-compact-video-renderer",
    "yt-lockup-view-model",
  ].join(",");

  const TITLE_SEL = [
    "#video-title-link",
    "a#video-title",
    "#video-title",
    "a.yt-lockup-metadata-view-model__title",
    ".yt-lockup-metadata-view-model__title",
    "h3 a",
  ];
  const CHANNEL_SEL = [
    "ytd-channel-name a",
    "#channel-name a",
    'a[href^="/@"]',
    'a[href*="/channel/"]',
    ".yt-content-metadata-view-model-wiz__metadata-text",
  ];

  const pick = (root, sels) => {
    for (const s of sels) {
      const el = root.querySelector(s);
      if (el) return el;
    }
    return null;
  };

  const videoIdFrom = (href) => {
    if (!href) return null;
    try {
      const u = new URL(href, location.origin);
      if (u.pathname === "/watch") return u.searchParams.get("v");
      const m = u.pathname.match(/^\/shorts\/([\w-]+)/);
      return m ? m[1] : null;
    } catch {
      return null;
    }
  };

  const textOf = (el) => (el ? (el.getAttribute("title") || el.textContent || "").trim() : "");

  function extractCard(card) {
    // 제목: 후보 셀렉터 → 없으면 카드 내 watch 링크 중 텍스트 있는 것.
    let titleEl = pick(card, TITLE_SEL);
    let title = textOf(titleEl);
    let videoId = videoIdFrom(titleEl && titleEl.getAttribute("href"));

    if (!videoId || !title) {
      for (const a of card.querySelectorAll('a[href*="/watch?v="], a[href*="/shorts/"]')) {
        const vid = videoIdFrom(a.getAttribute("href"));
        const txt = textOf(a);
        if (vid && txt) {
          videoId = videoId || vid;
          title = title || txt;
          break;
        }
        if (vid && !videoId) videoId = vid;
      }
    }
    if (!videoId || !title) return null;

    // 채널명 + (가능하면) channelId.
    let channel = "";
    let channelId = null;
    for (const s of CHANNEL_SEL) {
      const el = card.querySelector(s);
      if (!el) continue;
      const t = textOf(el);
      if (!t) continue;
      channel = t;
      const href = el.getAttribute("href") || "";
      const m = href.match(/\/channel\/(UC[\w-]+)/);
      if (m) channelId = m[1];
      break;
    }
    return { videoId, channel, channelId, title };
  }

  function scan() {
    const cards = document.querySelectorAll(CARD_SEL);
    let added = 0;
    for (const card of cards) {
      const c = extractCard(card);
      if (c && !feed.has(c.videoId)) {
        feed.set(c.videoId, c);
        added++;
      }
    }
    return added;
  }

  // 초기 스캔 + 스크롤로 새 카드가 붙을 때마다(디바운스).
  let timer = null;
  const debouncedScan = () => {
    clearTimeout(timer);
    timer = setTimeout(scan, 400);
  };
  const mo = new MutationObserver(debouncedScan);
  mo.observe(document.documentElement, { childList: true, subtree: true });
  scan();

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg && msg.type === "getFeed") {
      scan(); // 응답 직전 최신화
      sendResponse({ cards: [...feed.values()], url: location.href });
    }
    return true;
  });
})();
