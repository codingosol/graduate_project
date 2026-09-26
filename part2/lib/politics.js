// 국내 정당정치 제목 판별 — part1/Data/keywords.py + collect.py:is_political_title 포팅.
// KW_DATA 는 keywords.gen.js 가 먼저 로드되어 정의해 둔다.
//
// 판정 3단계(Part 1과 동일):
//   1. 국내 고유 키워드(정당·별칭·한자축약·정치인)가 있으면 정치. 해외 정상 이름 무시.
//   2. 없고 일반 용어만 있는데 해외 정상 이름이 있으면 비정치.
//   3. 그 외 일반 용어가 있으면 정치.
// 비교 전 NFC 정규화(호환용 한자 李 U+F9E1 문제 — Part 1 keywords.normalize 참고).

(function () {
  const DOMESTIC_CATEGORIES = ["parties", "hanja_abbreviations", "nicknames", "politician_names"];
  const GENERIC_CATEGORIES = ["institutions_terms"];
  const EXCLUDE_CATEGORY = "exclude_foreign_leaders";

  const norm = (s) => (s || "").normalize("NFC");
  const collect = (cats) => cats.flatMap((c) => (KW_DATA[c] || []).map(norm));

  const DOMESTIC = collect(DOMESTIC_CATEGORIES);
  const GENERIC = collect(GENERIC_CATEGORIES);
  const FOREIGN = (KW_DATA[EXCLUDE_CATEGORY] || []).map(norm);

  function isPoliticalTitle(title) {
    const t = norm(title);
    if (DOMESTIC.some((kw) => t.includes(kw))) return true;
    if (FOREIGN.some((fl) => t.includes(fl))) return false;
    return GENERIC.some((kw) => t.includes(kw));
  }

  globalThis.Politics = { isPoliticalTitle, normalize: norm };
})();
