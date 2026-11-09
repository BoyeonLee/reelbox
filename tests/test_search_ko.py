"""스냅샷 모드 한국어 단어 나누기(static/search_ko.js). node 로 실행해 확인한다 (없으면 건너뜀)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "src/reelbox/web/static/search_ko.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node 가 없음")


def run_js(expr: str) -> object:
    code = f"const k = require({json.dumps(str(SCRIPT))}); console.log(JSON.stringify({expr}));"
    out = subprocess.run([NODE or "node", "-e", code], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_query_splits_korean_into_two_letter_pieces() -> None:
    assert run_js('k.queryTokenize("파스타")') == ["파스", "스타"]
    assert run_js('k.queryTokenize("브라운 버터")') == ["브라", "라운", "버터"]


def test_index_pieces_cover_words_with_particles() -> None:
    tokens = run_js('k.indexTokenize("파스타를 만들어요")')
    assert isinstance(tokens, list)
    # "파스타"로 검색했을 때의 조각이 "파스타를"의 색인 조각에 모두 들어 있다
    assert {"파스", "스타"} <= set(tokens)
    # 한 글자 검색어도 찾을 수 있게 한 글자씩도 넣는다
    assert "밥" in run_js('k.indexTokenize("김밥")')  # type: ignore[operator]


def test_latin_words_stay_whole_and_lowercase() -> None:
    assert run_js('k.queryTokenize("BTS Pasta, 2024!")') == ["bts", "pasta", "2024"]
    assert run_js('k.queryTokenize("밥")') == ["밥"]
