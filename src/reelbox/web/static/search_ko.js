// 스냅샷 모드 검색의 한국어 단어 나누기 (DESIGN.md 6장 "한국어 검색").
// MiniSearch 기본 단어 나누기는 띄어쓰기 단위라 "파스타를"로 저장된 글을 "파스타"로 못 찾는다.
// 그래서 한글이 든 단어는 두 글자씩 겹쳐 자른다: "파스타를" → 파스, 스타, 타를.
//   색인할 때: 두 글자 조각 + 한 글자씩 (한 글자 검색어 "밥"도 찾게)
//   검색할 때: 두 글자 조각만 (한 글자 단어면 그 글자). 모든 조각이 있는 글만 찾는다(AND).
// 영어·숫자 단어는 통째로 소문자로 쓴다. 브라우저(window)와 node(테스트) 양쪽에서 돈다.

(function (root) {
  "use strict";

  const HANGUL = /[가-힣ㄱ-ㆎ]/;
  const SPLIT = /[^\p{L}\p{N}]+/u;

  function words(text) {
    return String(text || "").normalize("NFC").toLowerCase().split(SPLIT).filter(Boolean);
  }

  function bigrams(word) {
    const chars = Array.from(word);
    if (chars.length < 2) return chars;
    const out = [];
    for (let i = 0; i < chars.length - 1; i++) out.push(chars[i] + chars[i + 1]);
    return out;
  }

  function indexTokenize(text) {
    const out = [];
    for (const w of words(text)) {
      if (HANGUL.test(w)) {
        out.push(...bigrams(w));
        const chars = Array.from(w);
        if (chars.length > 1) out.push(...chars);
      } else {
        out.push(w);
      }
    }
    return out;
  }

  function queryTokenize(text) {
    const out = [];
    for (const w of words(text)) out.push(...(HANGUL.test(w) ? bigrams(w) : [w]));
    return Array.from(new Set(out));
  }

  // MiniSearch 설정. 가중치는 PC 의 FTS5 와 비슷하게 제목·태그·메모를 높게.
  const MINISEARCH_OPTIONS = {
    fields: ["title", "tags", "note", "summary", "key_points", "topic", "author"],
    storeFields: ["id"],
    tokenize: indexTokenize,
    processTerm: (t) => t, // 이미 소문자로 나눴다
    searchOptions: {
      tokenize: queryTokenize,
      processTerm: (t) => t,
      combineWith: "AND",
      boost: { title: 3, tags: 3, note: 3, topic: 2 },
      // 영어 단어는 앞부분만 쳐도 찾게 (예: "pas" → pasta). 한글 조각은 정확히.
      prefix: (term) => !HANGUL.test(term) && term.length >= 3,
    },
  };

  const api = { indexTokenize, queryTokenize, MINISEARCH_OPTIONS };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ReelboxSearchKo = api;
})(typeof window !== "undefined" ? window : globalThis);
