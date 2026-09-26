"""part1/Data/keywords.json → part2/lib/keywords.gen.js 생성.

익스텐션은 keywords.json을 손으로 복붙하지 않는다(Part 1과 드리프트 방지).
Part 1의 keywords.json이 단일 소스이고, 이 스크립트가 그걸 그대로 JS로 옮긴다.
keywords.json을 갱신하면 이 스크립트를 다시 돌려 keywords.gen.js를 재생성한다.

생성물은 globalThis.KW_DATA 에 카테고리별 배열을 그대로 넣는다.
판정 로직(정규화·3단계 판정)은 part2/lib/politics.js 가 담당한다.

실행: python part2/tools/build_keywords_js.py
"""
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # graduateProject/
SRC = os.path.join(ROOT, "part1", "Data", "keywords.json")
OUT = os.path.join(ROOT, "part2", "lib", "keywords.gen.js")


def main():
    with open(SRC, encoding="utf-8") as f:
        data = json.load(f)
    # _comment 등 메타데이터 키는 제외(카테고리만 넘긴다).
    data = {k: v for k, v in data.items() if not k.startswith("_")}

    body = json.dumps(data, ensure_ascii=False, indent=2)
    js = (
        "// 자동 생성 파일 — 직접 수정하지 말 것.\n"
        "// 출처: part1/Data/keywords.json\n"
        "// 재생성: python part2/tools/build_keywords_js.py\n"
        "globalThis.KW_DATA = " + body + ";\n"
    )
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(js)
    n = sum(len(v) for v in data.values() if isinstance(v, list))
    print(f"  {os.path.relpath(OUT, ROOT)} 생성 (카테고리 {len(data)}개 / 키워드 {n}개)")


if __name__ == "__main__":
    main()
