# Reelbox — 저장한 쇼츠·릴스 주제별 정리 앱 설계 문서

Oct 29, 2026 · @이보연

## 1. 개요

Reelbox는 내가 이미 저장한 인스타그램 릴스와 유튜브 쇼츠를 한 번에 가져와서, AI가 내용을 요약하고 주제별로 묶어 주고, 검색까지 되게 하는 **내 컴퓨터용 웹앱**이다.

**비유:** 서랍에 아무렇게나 던져 둔 메모 수백 장(저장 목록)을 꺼내서, 비서가 한 장씩 읽고(자막·음성 받아쓰기), 한 줄 요약을 붙이고, "요리 / 운동 / 개발 / 여행" 같은 주제 바인더에 나눠 꽂아 주는 것. 나는 바인더를 넘겨 보거나 "그 브라운 버터 파스타"처럼 기억나는 대로 물어보면 된다.

### 목표

- 이미 저장한 콘텐츠를 **한꺼번에** 가져온다 (하나씩 공유하지 않음).
- 영상마다 **한국어 요약, 주제, 태그**를 자동으로 붙인다.
- **키워드 검색 + 의미 검색**(정확한 단어가 아니라 뜻이 비슷한 것도 찾기)을 둘 다 지원한다.
- 주제별로 묶어서 훑어보고, 영상은 탭하면 인스타그램·유튜브 앱으로 넘어가서 본다(댓글까지).
- 필요 없는 영상은 Reelbox 안에서 **숨기거나 삭제**한다. 인스타·유튜브의 원본 저장 목록은 건드리지 않는다.

### MVP 범위

| 포함 | 제외 (나중에) |
| --- | --- |
| 인스타 "내 정보 다운로드" 파일 가져오기 | 틱톡 등 다른 플랫폼 |
| 유튜브 "저장된 Shorts 동영상" 목록 가져오기·동기화 | 영상 화면 속 글자 인식(OCR) |
| 메타데이터·자막·음성 받아쓰기 | 여러 사용자, 회원가입 |
| AI 요약·주제·태그 + 주제 정리 | 앱스토어용 네이티브 앱 |
| **휴대폰 우선 웹 화면(PWA)** — 검색, 주제별 보기, 원본 앱으로 열기 | 보강·분석까지 클라우드로 옮기기 |
| 휴대폰 공유 버튼으로 새 릴스·쇼츠 추가 |  |
| Reelbox 안에서 숨기기·삭제(휴지통 30일) | 인스타·유튜브 원본 저장 목록에서 지우기 |
| PC가 꺼져 있을 때 보는 보기 전용 사이트(Cloudflare Pages) | PC가 꺼져 있을 때의 의미 검색 |

**휴대폰에서 쓰는 방식:** 무거운 처리(자막·음성·AI)는 집 PC가 서버로 맡고, 휴대폰은 브라우저로 접속해 "홈 화면에 추가"한 앱처럼 쓴다. 집 밖에서는 Tailscale(내 기기끼리만 연결되는 무료 사설망)로 접속한다. 비유하면 PC는 주방, 휴대폰은 주문·서빙 창구다. PC가 꺼져 있을 때는 문 앞에 붙여둔 메뉴판, 즉 클라우드에 올려둔 마지막 스냅샷(보기 전용 사이트)을 본다.

영상 파일은 기본적으로 저장하지 않고, 분석에 필요한 오디오만 잠깐 받아서 쓰고 지운다. 영상은 앱 안에서 재생하지 않고 인스타그램·유튜브 앱으로 넘겨서 본다(댓글도 함께).

## 2. 전체 구조

데이터는 다섯 단계를 한 방향으로 흐른다: **가져오기 → 보강 → AI 분석 → 주제 정리 → 검색·보기**. 각 단계는 따로 실행할 수 있는 CLI 명령이고, 결과는 전부 SQLite 파일 하나에 쌓인다. 공장 컨베이어처럼, 중간에 멈춰도 다시 돌리면 끝난 건 건너뛰고 이어서 처리한다. PC가 이 파이프라인과 웹 서버를 돌리고, 휴대폰은 그 웹 서버에 접속한다.

&#91;embedded content: Reelbox 구조 · PC 5단계 파이프라인 + 휴대폰 PWA\]

기존 저장분은 인스타 내보내기 파일과 유튜브 "저장된 Shorts" 목록으로 한 번에, 새로 저장하는 것은 휴대폰 공유로 들어와 같은 다섯 단계를 거친다.

### 기술 스택

| 역할 | 선택 | 이유 |
| --- | --- | --- |
| 언어 | Python 3.12 + uv | 자막·음성·임베딩 도구가 모두 Python. uv는 가상환경·패키지를 한 번에 관리 |
| CLI | Typer | 단계별 명령을 쉽게 만듦 |
| 저장소 | SQLite + FTS5(trigram) + sqlite-vec | 서버 없이 파일 하나. trigram은 한국어 부분 검색에 유리, sqlite-vec은 벡터 검색 |
| 메타·자막·오디오 | yt-dlp + ffmpeg | 유튜브·인스타 모두 지원, 영상 다운로드 없이 정보만 가능 |
| 음성 받아쓰기 | faster-whisper (로컬) | 자막 없는 쇼츠·릴스 대비. 무료, CPU로도 동작 |
| 요약·분류 | Claude API (Haiku 등급 모델) | 한국어 품질 좋고 저렴. 인터페이스로 감싸서 다른 모델로 교체 가능 |
| 임베딩 | BAAI/bge-m3 (로컬, sentence-transformers + CPU 전용 torch) | 다국어·한국어 의미 검색에 강함, 무료. torch 는 pytorch CPU 인덱스에서 받는다(CUDA 빌드는 수 GB 더 큼) |
| 웹 | FastAPI(JSON API) + 정적 화면(HTML + Alpine.js + Tailwind CDN) | 빌드 없는 화면 하나를 PC와 보기 전용 사이트가 같이 씀. 데이터만 PC API 또는 스냅샷에서 가져옴 |
| 휴대폰 앱화 | PWA (manifest + service worker) | 홈 화면 아이콘, 전체 화면. 앱스토어 등록 불필요 |
| 외부 접속 | Tailscale | 포트 개방 없이 내 휴대폰→PC 연결, HTTPS 주소도 발급 |
| 테스트 | pytest + 샘플 내보내기 파일 | 실제 네트워크 없이 파서·파이프라인 검증 |
| 보기 전용 호스팅 | Cloudflare Pages + Cloudflare Access + wrangler | PC가 꺼져 있어도 스냅샷을 보여줌. 이메일 로그인으로 나만 접근 |
| 휴대폰 안 검색 | MiniSearch (JS) | 스냅샷 모드에서 서버 없이 키워드 검색 |

Go가 더 익숙하더라도, 이 프로젝트는 AI·영상 도구 때문에 Python이 훨씬 짧게 끝난다.

## 3. 데이터 소스

기존 저장분은 각 플랫폼의 **공식 내 데이터 내보내기 파일**(인스타그램)과 "저장된 Shorts 동영상" 목록(유튜브)에서 가져온다. 이후 새로 저장하는 것은 인스타는 휴대폰 공유 버튼으로, 유튜브는 목록 동기화로 들어온다. 내보내기 파일에는 링크와 저장 시각만 있고 내용은 없으므로, 내용은 보강 단계에서 채운다.

### 인스타그램

- 받는 법: 설정 → 계정 센터 → 내 정보 및 권한 → 내 정보 다운로드. **형식은 반드시 JSON**, 항목은 "저장됨"만 고르면 빨리 온다. 처리에 며칠 걸릴 수 있다.
- 파일: `saved_posts.json`(저장 목록), `saved_collections.json`(컨렉션). 폴더 경로는 버전마다 달라서 **압축을 푼 폴더 전체에서 파일 이름으로 찾는다.**
- 옛 구조 ([contextbolt 가이드](https://contextbolt.com/blog/instagram-saved-posts/)): `saved_saved_media[]` 배열의 각 항목에 `title`(작성자 아이디)과 `string_map_data["Saved on"]` 안의 `href`(게시물 링크), `timestamp`(저장 시각, 유닉스 초)가 있다.
- 2026년 구조 (Phase 1에서 실제 파일로 확인): `saved_posts.json`은 최상위가 리스트이고, 각 항목은 `{timestamp, media, label_values[], fbid}`이다. 최상위 `timestamp`가 저장 시각, `label_values` 중 `label: "URL"` 항목의 `href`가 게시물 링크다. 캡션·제목·해시태그·소유자(사용자 이름)도 `label_values` 안에 있다. `saved_collections.json`은 컬렉션 하나면 dict 하나이고, `label: "이름"`이 컬렉션 이름, `title: "미디어"` 아래에 게시물들이 중첩된다.
- 글자 깨짐: 라벨과 값의 한글이 UTF-8 바이트를 latin-1 글자로 저장한 형태로 깨져 있다(예: "캡션" → `\u00ec\u00ba\u00a1…`). 읽을 때 `latin-1`로 인코딩한 뒤 `utf-8`로 디코딩해 되돌린다.
- 주의: 키 이름이 언어 설정에 따라 바뀔 수 있고(예: "Saved on", "캡션"/"Caption"), 구조도 바뀐다. 파서는 키 이름에 기대지 말고 **`href`와 `timestamp`를 재귀적으로 찾는 방식**으로 만든다. 릴스와 사진 게시물이 섞여 있다(`/p/`, `/reel/`). 한 게시물이 여러 컬렉션에 있으면 `collection`에는 첫 번째만 기록한다.

### 유튜브

- **가져올 대상:** 쇼츠의 저장 버튼을 누르면 들어가는 기본 목록 "저장된 Shorts 동영상"(Saved Shorts). 좋아요 목록처럼 계정에 붙어 있는 시스템 재생목록이다 ([위키백과](https://en.wikipedia.org/wiki/YouTube_Shorts)).
- **주 경로 (yt-dlp + 로그인 쿠키):** PC 브라우저에서 유튜브 → 내 페이지 → 재생목록 → "저장된 Shorts 동영상"을 열어 주소(`youtube.com/playlist?list=…`)를 복사해 설정에 넣는다. `yt-dlp --flat-playlist`로 영상 다운로드 없이 ID 목록만 읽는다. 내 계정의 비공개 목록이라 로그인 쿠키가 필요하다.
- **자동 동기화:** 같은 목록을 다시 읽으면 새로 저장한 쇼츠만 추가된다. 그래서 유튜브는 휴대폰 공유 없이도 `reelbox sync youtube`(서버가 켜져 있으면 하루 몇 번 자동)로 새 저장분을 따라간다.
- **쿠키 주의:** 윈도우의 Chrome은 쿠키 암호화 때문에 yt-dlp가 쿠키를 못 읽는 경우가 많다. Firefox에서 유튜브에 로그인해 `--cookies-from-browser firefox`를 쓰거나, 쿠키를 `cookies.txt` 파일로 내보내 쓴다. 쿠키 파일은 비밀번호처럼 다루고 커밋하지 않는다.
- **한계:** 목록에는 저장 시각이 없을 수 있다. 그때는 목록 안 순서를 기록해 정렬에 쓰고, 처음 발견한 시각을 대신 넣는다.
- **보조 경로 (테이크아웃):** Google 테이크아웃의 재생목록 내보내기에 "저장된 Shorts 동영상"이 들어 있는지 Phase 1에서 확인한다. 들어 있으면 저장 시각을 채우는 데만 쓴다. (Phase 1: 테이크아웃이 없어 확인하지 못했다. `import youtube`는 테이크아웃이 생기면 그때 만든다.)

### 그 외 입구

- **URL 목록 가져오기:** 줄마다 링크 하나인 텍스트 파일. 테이크아웃에서 빠진 것이나 브라우저 확장으로 뽑은 목록용.
- **휴대폰 공유 (아이폰):** 인스타·유튜브 앱에서 공유 → "Reelbox에 추가" 단축어. 아이폰은 홈 화면에 추가한 웹앱을 공유 목록에 넣어 주지 않아서(PWA 공유 대상 미지원), 단축어(Shortcuts) 앱이 링크를 토큰 헤더와 함께 서버로 보낸다. 안드로이드용 공유 대상(`share_target`)은 아이폰만 쓰므로 만들지 않는다(Phase 6 결정).

모든 입구는 같은 함수(`add_item(url, saved_at, source, collection)`)로 모인다. 같은 영상은 플랫폼 + 고유 ID(예: 인스타 shortcode, 유튜브 동영상 ID)로 중복을 막는다.

## 4. 데이터 모델

모든 데이터는 `data/reelbox.db` 하나에 둔다. 핵심은 `items` 테이블의 `status` 열로, 각 단계는 자기 차례인 상태의 항목만 집어 처리하고 다음 상태로 넘긴다.

| 테이블 | 주요 열 | 설명 |
| --- | --- | --- |
| `items` | id, platform(`instagram`/`youtube`), source\_id, url, saved\_at, imported\_from, collection, title, author, description, duration\_sec, thumbnail\_url, thumbnail\_path, media\_type(`video`/`image`), status, error, attempts, created\_at, updated\_at | 저장한 영상 하나 = 한 줄. (platform, source\_id) 유일 |
| `transcripts` | item\_id, source(`captions`/`whisper`/`none`), language, text | 자막 또는 음성 받아쓰기 결과 |
| `analyses` | item\_id, summary\_ko, key\_points(JSON), tags(JSON), raw\_topic, content\_language, model, prompt\_version, created\_at | AI 분석 결과. prompt\_version으로 재분석 대상 판별 |
| `topics` | id, name, description, parent\_id, is\_user\_edited | 주제(바인더). 내가 수정한 주제는 자동 정리가 덮어쓰지 않음 |
| `item_topics` | item\_id, topic\_id, is\_primary | 영상↔주제 연결. 하나는 대표 주제 |
| `embeddings` (sqlite-vec) | item\_id, vector(1024) | 의미 검색용 |
| `items_fts` (FTS5, trigram) | title, description, transcript, summary, tags | 키워드 검색용 |
| `user_meta` | item\_id, favorite, note, hidden\_at, deleted\_at | 내가 휴대폰에서 남기는 표시·메모, 숨김·휴지통 시각 |
| `fetch_log` | id, platform, item\_id, fetched\_at | 보강 단계가 플랫폼에 요청한 기록. 인스타 일일 한도(최근 24시간 개수)를 센다 (Phase 2, `002_fetch_log.sql`) |
| `item_similar` | item\_id, similar\_id, rank, score | 영상마다 미리 계산한 "비슷한 영상" 5개. index 때마다 전부 다시 계산 (Phase 4, `003_similar.sql`) |

### status 흐름

`imported` → `enriched` → `analyzed` → `indexed`. 실패하면 `failed`(error에 이유, attempts 증가, 3회까지 재시도). 영상이 삭제·비공개면 `unavailable`, 휴지통에서 영구 삭제되면 `deleted`. `skipped`는 예약만 해 두고 지금은 쓰지 않는다(Phase 2: 긴 영상도 받아쓰기만 건너뛰고 분석한다). 스키마 변경은 간단한 번호별 SQL 마이그레이션 파일(`migrations/001_init.sql` …)로 관리한다.

### 숨기기와 삭제

숨기기는 서랍 맨 뒤로 밀어 넣기, 삭제는 휴지통에 버리기다. 휴지통에 버린 것은 30일 안에는 다시 꺼낼 수 있다.

| 동작 | 기록 | 목록·검색·주제에서 | 되돌리기 |
| --- | --- | --- | --- |
| 숨기기 | `user_meta.hidden_at` | 기본으로 빠짐. "숨긴 항목 보기" 필터로만 보임 | 언제든 |
| 삭제 | `user_meta.deleted_at` | 모두 빠짐. 설정 > 휴지통에만 보임 | 30일 안 복원 |
| 30일 경과(purge) | 자막·분석·임베딩·검색 색인·주제 연결 삭제, `items` 행만 `status=deleted`로 남김 | 없음 | 불가 |

- **`items` 행을 남기는 이유:** 행까지 지우면 나중에 내보내기 파일을 다시 가져올 때 같은 영상이 또 들어온다. 가져오기·공유는 삭제된 항목을 건너뛰고 "이미 삭제한 영상"이라고 알려준다. 휴지통에 있는 것을 다시 공유하면 복원한다.
- 주제 개수, 대표 썸네일, `organize` 입력에서도 숨김·삭제 항목은 뺀다. 주제에 남은 영상이 0개면 홈에서 주제 카드를 감춘다.
- purge는 `reelbox purge` 명령으로, 서버가 켜져 있으면 하루 한 번 자동으로 돈다.

## 5. 처리 단계 상세

모든 단계는 **멱등**(여러 번 돌려도 같은 결과)이고 `--limit N`으로 일부만 돌려볼 수 있어야 한다. 처음에는 10개로 확인하고 전체를 돌린다.

### 5-1. 가져오기 (import)

3장의 파서가 링크를 읽어 `items`에 `imported`로 넣는다. URL을 정규화(쿼리 제거, `youtu.be`·`/shorts/` → 동영상 ID)하고 중복은 건너뛴다. 끝나면 "새로 추가 N, 중복 M, 인식 실패 K" 를 출력한다.

### 5-2. 보강 (enrich)

1. yt-dlp로 **다운로드 없이** 메타데이터를 받는다: 제목, 작성자, 설명(인스타는 캡션), 길이, 썸네일. 썸네일은 320px webp로 data/thumbs/에 저장한다(인스타 썸네일 주소는 며칠 뒤 만료되기 때문).
2. 자막이 있으면 받는다. 우선순위: 영상 원래 언어의 자막(수동 → 자동) → 수동 자막(ko → en → 아무거나) → 자동 자막. 한국어 영상의 영어 번역 자막보다 한국어 원문 자막을 먼저 쓰기 위해서다 (Phase 2에서 실제 데이터로 확인). 유튜브는 자동 더빙 음성의 자막에도 `-orig`를 붙이므로(예: 한국어 영상에 `en-US-orig`), 원래 언어는 원래 음성 트랙(`language_preference`가 가장 높은 것)으로 판단한다.
3. 자막이 없고 영상이면 **오디오만** 임시 폴더(data/tmp)에 받아 faster-whisper로 받아쓴 뒤 바로 지운다(언어 자동 감지, 기본 모델 `small`, 설정으로 변경). 180초(설정 `max_transcribe_sec`)를 넘는 영상은 받아쓰기만 건너뛰고 제목·설명으로 분석한다. 자동 자막은 기계 번역본을 피하려고 원래 언어를 먼저 고른다.
4. 인스타는 로그인이 필요하다. yt-dlp의 `--cookies-from-browser` 또는 쿠키 파일을 설정으로 받는다. 요청 사이 **5–10초 무작위 대기**, 최근 24시간 처리 개수가 한도(기본 200)를 넘거나 로그인 차단·429 응답·쿠키 만료 경고가 오면 **인스타만** 즉시 멈추고(유튜브는 계속) 다음에 이어서 한다. 차단은 보통 몇 시간~하루면 풀린다.
5. 사진 게시물은 캡션만 쓴다. 삭제된 영상은 `unavailable`로 표시하고 목록에는 남긴다. 그 외 실패는 `failed`(error, attempts)로 남기고 3회까지 다시 시도한다.
6. 인스타 내보내기 파일의 캡션·작성자는 가져오기 단계에서 `description`·`author`에 미리 넣는다. 인스타가 계속 막히면 `reelbox enrich --captions-only`로 접속 없이 이 캡션만으로 `enriched`로 넘겨 분석할 수 있다.

### 5-3. AI 분석 (analyze)

제목·작성자·설명·자막(앞부분 약 6,000자)을 Claude에 보내 아래 JSON을 받는다. 프롬프트는 `prompts/analyze_v1.md` 파일로 두고, 버전을 올리면 해당 항목만 재분석할 수 있다.

```json
{
  "summary_ko": "2~3문장 한국어 요약",
  "key_points": ["핵심 1", "핵심 2"],
  "raw_topic": "짧은 주제 이름 (예: 파스타 레시피)",
  "tags": ["파스타", "브라운버터", "자취요리"],
  "content_language": "ko"
}
```

구조화 출력(`output_config.format`, JSON 스키마)으로 형식을 강제하고, 응답의 `stop_reason`(거절·길이 초과)을 먼저 확인한 뒤 Pydantic으로 검증한다. 형식 오류면 1회 다시 시도하고, 거절은 바로 `failed`로 둔다. API 키·모델 이름·잔액 문제(400/401/403/404)나 SDK 재시도 뒤에도 계속되는 429·5xx는 전체를 멈추고 항목은 `enriched`로 둔다. 실행 전에 `--dry-run`으로 항목 수와 예상 토큰 수(공식 토큰 세기 API로 최대 30개를 세고 나머지는 평균으로 추정)·예상 비용을 보여준다. 모델은 설정값(기본 `claude-haiku-5-5`, effort `low`). API 키는 `.env`의 `ANTHROPIC_API_KEY`.

`analyses.prompt_version`에 프롬프트 버전을 기록한다. 버전이 바뀐 항목을 다시 분석하는 명령은 아직 만들지 않았다(Phase 3 결정: 필요할 때 추가). 결과는 `reelbox review --limit N`으로 표로 확인한다.

### 5-4. 주제 정리 (organize)

비유: 직원들이 메모에 제멋대로 붙인 라벨(raw\_topic)을 팀장이 모아보고 바인더 10\~20개로 정리하는 것.

1. 모든 raw\_topic과 개수를 모아 Claude에 보내 **대주제 10\~20개** 를 받고, raw\_topic을 150개씩 다시 보내 "raw\_topic → 주제" 대응표를 받는다. 한 번에 다 받으면 출력이 너무 길어 잘릴 수 있어서 두 단계로 나눈다(Phase 4 결정). LLM 호출이 모두 끝난 뒤 한 트랜잭션으로 쓰므로 중간에 실패하면 아무것도 바뀌지 않는다.
2. 내가 수정한 주제(`is_user_edited`)는 고정값으로 함께 보내 유지한다. 자동 주제는 지우고 새로 만든다.
3. 새 항목은 전체 재정리 없이 기존 주제에 배정한다. 같은 raw\_topic의 항목이 이미 들어간 주제가 있으면 그대로 쓰고(LLM 안 부름), 아니면 임베딩이 가장 가까운 주제 후보 3개 → Claude가 선택(30개씩 묶어서). "어울리는 후보 없음"이면 가장 가까운 후보에 넣고 개수를 알려준다. 많으면 `--full`을 권한다. `reelbox organize --full`은 처음부터 다시 묶는다. 주제가 하나도 없으면 `--full`이 아니어도 전체 정리를 한다.
4. organize는 index보다 먼저 돌므로, 새 항목의 임베딩이 없으면 그 자리에서 계산해 `embeddings`에 저장하고 index가 다시 쓴다.
5. 소주제(parent\_id)는 아직 만들지 않는다(Phase 4 결정: 필요할 때 추가). 모델은 설정 `organize_model`(기본 Haiku, effort `medium`). 프롬프트는 `prompts/organize_v1.md`(대주제·대응표), `prompts/organize_assign_v1.md`(새 항목 배정).

### 5-5. 색인과 검색 (index / search)

- 색인: `analyzed` 항목을 bge-m3로 "제목 + 요약 + 태그 + 자막 앞부분(1,000자)"을 벡터로 만들고, FTS5에 텍스트를 넣은 뒤 `indexed`로 넘긴다. CPU에서 모델을 불러오는 데 약 30초, 항목당 약 1초.
- 검색: **키워드(FTS5 BM25) 결과와 의미(벡터) 결과를 RRF로 합친다.** 비유하면 도서관 사서 두 명(단어를 정확히 찾는 사람, 뜻으로 찾는 사람)의 추천 순위를 합산하는 것. 점수 대신 순위만 써서 각 목록의 r등에 1/(60+r)점을 주고 더한다.
  - trigram 색인은 3글자보다 짧은 검색어를 찾지 못한다. 한국어는 2음절 단어("김밥", "간식")가 많아서, 2글자 이하 단어는 LIKE로 따로 찾아 RRF의 세 번째 목록으로 합친다(Phase 4 결정).
  - 벡터 검색은 필터를 먼저 걸고 전체 거리를 계산한다(항목이 수천 개 이하라 충분히 빠름). KNN 후 필터는 결과가 빠질 수 있다.
- 필터: 플랫폼, 주제, 저장 기간, 즐겨찾기. 숨김·삭제 항목은 항상 뺀다. 색인 때 영상마다 "비슷한 영상" 5개를 미리 계산해 `item_similar`에 둔다(상세 화면과 보기 전용 사이트에서 사용, 숨김·삭제 제외).
- `reelbox search "..." [--platform] [--topic] [--since] [--until] [--favorite] [--keyword-only]`. `--keyword-only`는 모델을 불러오지 않아 빠르다.

## 6. 웹 UI (휴대폰 우선)

화면은 폭 390px 휴대폰을 기준으로 설계하고, PC에서는 같은 화면이 여러 열로 넓어지게 한다. 하단 탭바 4개: **홈 · 검색 · 추가 · 설정**.

| 화면 | 내용 |
| --- | --- |
| 홈 | 상단 검색창, 주제 카드 격자(이름 · 개수 · 대표 썸네일 3장), 아래에 최근 저장 |
| 검색 결과 | 카드 목록(썸네일, 제목, 요약 한 줄, 주제 칩, 플랫폼 아이콘). 카드를 누르면 상세, ▶ 버튼은 바로 원본 앱으로 연다. 필터는 아래에서 올라오는 시트. 스크롤하면 더 불러옴 |
| 주제 상세 | 그 주제의 영상 목록, 정렬(최근 저장순/오래된순), 주제 이름 수정·합치기 |
| 영상 상세 | 큰 썸네일과 "인스타그램/유튜브에서 보기" 버튼, 요약, 핵심 포인트, 태그, 자막 전문(접기), 즐겨찾기·메모, 주제 바꾸기, ⋯ 메뉴의 숨기기·삭제 |
| 추가 | 링크 붙여넣기 + 공유로 들어온 링크 확인. 처리 상태(대기 → 분석 중 → 완료) 표시 |
| 설정 | 파이프라인 현황(상태별 개수), 실패 목록과 다시 시도, 숨긴 항목, 휴지통(복원·비우기), 단축어 설치 안내 |

### 휴대폰에서 꼭 지킬 것

- **HTTPS 필수:** PWA의 서비스 워커와 홈 화면 설치는 HTTPS에서만 동작한다. `tailscale serve`로 `https://<컴퓨터이름>.<tailnet>.ts.net` 주소를 얻는다.
- **원본 앱으로 열기:** 영상은 앱 안에서 재생하지 않는다. 일반 링크(`instagram.com/reel/<코드>`, `youtube.com/shorts/<ID>`)를 열면 휴대폰에 설치된 인스타그램·유튜브 앱이 열려서 댓글까지 볼 수 있다. 단, 홈 화면에 추가한 웹앱에서는 링크가 앱 대신 브라우저로 열릴 수 있으니 실제 휴대폰에서 꼭 확인한다(9장).
- **터치:** 버튼 최소 44px, 썸네일은 지연 로딩(`loading=lazy`), 글자 16px 이상(iOS 입력창 확대 방지).
- **공유 받기:** `POST /api/items`(링크 하나, 아이폰 단축어와 추가 화면이 씀). 받은 항목은 백그라운드 워커가 보강→분석→주제 배정→색인까지 자동으로 처리한다.
- **숨기기·삭제:** 영상 상세의 ⋯ 메뉴, 또는 목록에서 카드를 길게 눌러 여러 개를 골라 한꺼번에. 실행 직후 5초간 "되돌리기" 알림을 띄우고, 휴지통 비우기만 확인 창을 한 번 띄운다. API: `PATCH /api/items/{id}`(숨김 켜기·끄기), `DELETE /api/items/{id}`(휴지통으로), `POST /api/items/{id}/restore`, `POST /api/items/bulk`(여러 개 한꺼번에).
- **접근 제한:** Tailscale 밖에서는 접근 불가. 추가로 긴 비밀 토큰(`REELBOX_TOKEN`)을 쿠키나 헤더로 확인한다(단축어는 헤더로 보냄).

### Phase 5 결정

- **화면 디자인:** Stitch로 만든 화면(`docs/design/stitch/`, Mist Lavender 테마)을 따른다. Stitch 화면 중 다른 Phase 기능(오프라인 홈 → 8, 즐겨찾기·메모·주제 바꾸기·실패 재시도 → 7)과 범위 밖 요소(조회수, TikTok, 태그 추가, 브라우저 확장, 클라우드 백업)는 넣지 않았다.
- **토큰 입력 화면:** 아이폰에서 홈 화면에 추가한 웹앱은 Safari와 쿠키를 따로 쓴다. 그래서 처음 열 때 토큰을 한 번 붙여넣으면 `POST /api/login`이 HttpOnly 쿠키(1년)를 심는다. 헤더는 `Authorization: Bearer <토큰>`. `REELBOX_TOKEN`이 비어 있으면 서버가 시작하지 않는다.
- **공개 범위:** 화면 껍데기(HTML·JS·manifest·서비스 워커·아이콘)는 토큰 없이 받는다(데이터가 없고, 로그인 전에도 떠야 함). `/api/*`와 `/thumbs/*`는 토큰이 필요하다.
- **추가 화면:** `POST /api/items`는 Phase 5에서 만들었다(링크를 `imported`로 넣기, 휴지통 항목이면 복원, 영구 삭제 항목은 건너뜀). 자동 처리 워커는 Phase 6.
- **가져오기와 휴지통:** 휴지통에 있는(아직 purge 전) 영상도 가져오기에서는 "이미 삭제한 영상"으로 건너뛴다. 공유·추가 화면으로 다시 넣을 때만 복원한다.
- **목록 범위:** 홈 최근 저장·주제 상세는 분석이 끝난(`analyzed`/`indexed`) 항목만 보여준다. 검색은 지금처럼 `indexed`만.
- **의미 검색 모델:** 서버가 켜질 때 bge-m3를 백그라운드에서 불러온다. 다 불러오기 전(약 30초)에는 키워드 검색만 하고, 화면에 "의미 검색 준비 중"을 표시한다.
- **API 목록:** `GET /api/home`, `/api/topics/{id}`, `/api/items?topic_id&sort&limit&offset`, `/api/items/{id}`, `/api/search?q&platform&topic&since&until&limit&offset`, `/api/status`, `/api/hidden`, `/api/trash`, `/api/adds`, `PATCH`·`DELETE /api/items/{id}`, `POST /api/items/{id}/restore`, `/api/items/bulk`(`hide`/`unhide`/`delete`/`restore`), `/api/trash/empty`, `/api/items`, `/api/login`, `/api/logout`.

### Phase 6 결정

- **아이폰만:** 휴대폰이 아이폰이라 안드로이드용 `manifest share_target`과 그것이 쓰는 `GET /share`는 만들지 않았다. 단축어는 Safari로 주소를 여는 대신 `POST /api/items`에 `Authorization: Bearer <토큰>` 헤더를 붙여 보낸다(홈 화면 앱과 Safari는 쿠키를 따로 써서 다시 로그인해야 하므로).
- **워커 (`worker.py`):** `reelbox serve` 안의 스레드 하나. 새 항목이 추가·복원되면 바로 깨어나고, 아니면 `worker_poll_sec`(기본 60초)마다 할 일을 확인한다.
- **공유 항목만 보강·분석:** `imported_from='share'` 항목만 보강·분석한다(`enrich_items`·`analyze_items`의 `source` 인자). 아직 처리하지 않은 내보내기 항목까지 집으면 인스타 한도와 API 요금을 쓰기 때문이다. 주제 배정과 색인은 CLI와 같은 함수 그대로라 분석이 끝난 항목이면 출처와 상관없이 처리한다.
- **주제 배정:** 새 항목 배정만 한다. 주제가 하나도 없으면 organize가 전체 정리를 해 버리므로 워커는 건너뛰고 색인만 한다(전체 정리는 CLI로).
- **잠시 쉬기:** 인스타·유튜브 보강이 멈추거나(차단·쿠키 오류·일일 한도) Claude API가 멈추면 그 단계만 `worker_backoff_sec`(기본 30분) 쉰다. 바로 다시 시도하면 같은 항목의 시도 횟수 3회를 몇 분 만에 써 버리기 때문이다.
- **임베딩 모델:** 서버가 불러온 bge-m3를 함께 쓴다. 다 불러오기 전에는 분석까지만 하고 다음 확인 때 색인한다. API 키가 없으면 보강까지만 한다.
- **링크 고르기:** 공유 시트가 링크 앞뒤에 글을 붙여 보내도 인식되는 첫 링크를 고른다. 단축어는 공유 항목이 여러 개(링크 + 제목 글)면 `url`을 목록으로 보내므로 목록도 받는다.
- **오류에도 message:** 단축어는 응답의 `message`를 알림으로 띄우므로, 401(토큰)·422(형식) 응답에도 `message`를 넣고 서버 터미널에 경고를 남긴다(공유한 내용 자체는 남기지 않음).
- **추가 화면:** `GET /api/worker`(작업 중인 단계, 남은 개수, 쉬는 단계와 이유·재개 시각)를 맨 위에 보여주고, 처리 중인 항목이 있으면 5초마다 새로고침한다. 설정 화면에 단축어 만드는 법과 주소 복사 버튼을 둔다.
- **화면 파일 캐시:** 화면 파일(HTML·JS·manifest·서비스 워커)은 `Cache-Control: no-cache`로 보내고, 서비스 워커도 `fetch(..., {cache: "no-cache"})`로 가져온다. 이렇게 하지 않으면 아이폰 Safari가 새 index.html과 옛 app.js를 섞어 써서 새 버튼이 눌리지 않았다.
- **PC가 꺼져 있을 때 공유:** 단축어는 PC로 바로 보내므로 PC가 꺼져 있으면 실패 알림만 뜬다. 오프라인 대기열(6장 "보기 전용 사이트")과 어떻게 이을지는 Phase 8에서 정한다.

### 보기 전용 사이트 (PC가 꺼져 있을 때)

PC가 켜질 때마다 결과를 **스냅샷**(JSON 파일 + 썸네일)으로 묶어 Cloudflare Pages에 올린다. 휴대폰 앱은 하나다. 먼저 PC에 연결을 시도하고, 2초 안에 답이 없으면 스냅샷을 연다. 비유하면 주방(PC)이 닫혀 있을 때 문 앞에 붙여둔 메뉴판을 보는 것이다.

&#91;embedded content: 보기 전용 사이트 · PC 켜짐/꺼짐에 따른 데이터 출처\]

화면 상단에 지금 어느 쪽을 보는지("PC 연결됨" / "스냅샷 · 마지막 업데이트 3일 전")와 대기열에 쌓인 개수를 표시한다.

| 기능 | PC 켜짐 (Tailscale) | PC 꺼짐 (스냅샷) |
| --- | --- | --- |
| 주제별 보기·원본 앱 열기 | 됨 | 됨 |
| 키워드 검색 | 서버 FTS5 | 휴대폰 안에서 MiniSearch |
| 의미 검색 | 됨 | 안 됨. 대신 미리 계산한 "비슷한 영상" |
| 데이터 | 실시간 | 마지막 publish 시점 |
| 공유로 추가 | 바로 처리 | 휴대폰에 쌓아두었다가 PC가 켜지면 전송 (단축어는 PC로 바로 보내므로 방법은 Phase 8에서 다시 정함) |
| 숨기기·삭제·즐겨찾기 | 바로 반영 | 휴대폰 화면에만 먼저 반영, PC가 켜지면 전송 |

- **화면은 하나:** 화면은 빌드 없는 정적 파일(HTML·JS)이고, 데이터는 `static/data.js` 한 곳에서만 읽는다. data.js가 PC API와 스냅샷 중 하나를 고른다. 같은 TV에서 채널만 바꾸는 것과 같다.
- **설치 주소:** 휴대폰 홈 화면에는 항상 열리는 **Pages 주소**로 설치한다. (공유는 단축어가 하므로 공유 대상 설정은 없다.)
- **스냅샷 내용:** 숨김·삭제가 아닌 항목의 제목·작성자·요약·핵심 포인트·태그·주제·저장 시각·원본 링크·즐겨찾기·메모, 썸네일(320px webp), 비슷한 영상 목록, 마지막 업데이트 시각. 자막 원문은 용량과 개인정보 때문에 넣지 않는다.
- **한국어 검색:** MiniSearch의 기본 단어 나누기는 한국어에 약하다. 두 글자씩 잘라 색인해(예: "파스타" → "파스", "스타") 조사가 붙어도 찾을 수 있게 한다.
- **보안:** Pages 주소는 인터넷에 공개되므로 **Cloudflare Access**(이메일로 받는 일회용 코드 로그인)를 걸어 나만 들어가게 한다. 로그인 유지 기간을 길게(예: 1개월) 잡아야 앱을 열 때마다 로그인하지 않는다.
- **배포:** `reelbox publish`가 `data/dist/`를 만들고 wrangler(Cloudflare 명령줄 도구, Node.js 필요)로 올린다. PC가 켜져 있으면 `run`이 끝날 때와 하루 한 번 자동으로 돈다. 바뀐 것이 없으면 올리지 않는다.
- **오프라인 대기열:** 휴대폰 브라우저 저장소(IndexedDB)에 쌓고, 앱을 열 때마다 PC 연결을 확인해 보낸다. PC API는 Pages 주소에서 오는 요청만 허용(CORS)하고 토큰을 확인한다.
- **비용:** 개인 용도는 Cloudflare Pages·Access 무료 플랜으로 충분할 것으로 보인다. 가입할 때 현재 무료 범위를 확인한다.

## 7. 폴더 구조와 CLI 명령

```text
reelbox/
├─ CLAUDE.md               # Claude Code 작업 규칙 (10장)
├─ docs/DESIGN.md          # 이 문서
├─ pyproject.toml          # uv 프로젝트
├─ .env.example            # ANTHROPIC_API_KEY, REELBOX_TOKEN, 쿠키 설정, 저장된 Shorts 목록 URL 등
├─ migrations/             # 001_init.sql …
├─ prompts/                # analyze_v1.md, organize_v1.md
├─ src/reelbox/
│  ├─ cli.py               # Typer 명령 모음
│  ├─ config.py            # 설정 (pydantic-settings)
│  ├─ db.py                # 연결, 마이그레이션, 저장소 함수
│  ├─ importers/           # instagram.py, youtube.py, urllist.py
│  ├─ enrich/              # metadata.py, captions.py, transcribe.py
│  ├─ ai/                  # llm.py(인터페이스), analyze.py, organize.py
│  ├─ search/              # embed.py, index.py, query.py
│  ├─ publish.py           # 스냅샷 만들기·Cloudflare Pages 배포
│  ├─ purge.py             # 휴지통 30일 지난 항목 영구 정리
│  ├─ worker.py            # 공유로 들어온 항목 자동 처리
│  └─ web/                 # app.py(앱·썸네일), auth.py(토큰), routes/api.py(JSON API), static/(index.html, app.js, data.js, sw.js, manifest.json, icons/)
├─ tests/                  # fixtures/(가짜 내보내기 파일), test_*.py
└─ data/                   # reelbox.db, tmp/, thumbs/, dist/ (git 제외)
```

| 명령 | 하는 일 |
| --- | --- |
| `reelbox init` | DB 만들기·마이그레이션 |
| `reelbox import instagram <폴더\|zip>` | 인스타 내보내기 가져오기 |
| `reelbox import youtube <폴더\|zip>` | 테이크아웃(보조): 저장 시각 채우기 |
| `reelbox import urls <파일>` | 링크 목록 가져오기 |
| `reelbox enrich [--limit N] [--platform] [--captions-only]` | 메타·자막·받아쓰기 (`--captions-only`: 접속 없이 내보내기 캡션만으로) |
| `reelbox analyze [--limit N] [--dry-run]` | AI 요약·태그 |
| `reelbox review [--limit N]` | 최근 분석 결과를 표로 보기 (제목·요약·주제·태그) |
| `reelbox organize [--full] [--limit N]` | 주제 정리 (`--limit`은 새 항목 배정에만) |
| `reelbox index [--limit N]` | 임베딩·검색 색인 |
| `reelbox run` | 위 4단계를 차례로 |
| `reelbox search "검색어"` | 터미널에서 검색 (확인용) |
| `reelbox status` | 상태별 개수, 최근 실패 |
| `reelbox serve [--host] [--port]` | 웹 서버 + 백그라운드 워커 |
| `reelbox purge` | 휴지통에서 30일 지난 항목 영구 정리 |
| `reelbox sync youtube` | 유튜브 "저장된 Shorts 동영상" 목록을 읽어 새 항목 추가 (처음 가져오기도 이 명령) |
| `reelbox publish [--dry-run]` | 스냅샷을 만들어 보기 전용 사이트에 배포 |

## 8. 단계별 개발 계획

한 번에 한 단계만 진행하고, **완료 기준을 내 눈으로 확인한 뒤** 다음으로 넘어간다. Phase 4가 끝나면 PC 터미널에서는 이미 쓸 수 있고, Phase 5부터 휴대폰으로 옮겨간다.

**데이터 범위 (2026-11-07 결정):** 포트폴리오로 빨리 완성하는 것이 우선이라, 저장한 영상 전부가 아니라 **약 100개(유튜브·인스타 반반)만 보강·분석**하고 기능 구현을 진행한다. 완료 기준의 "주제 수"·"검색어" 확인도 이 100개로 한다. 나머지는 모든 단계가 멱등이라 나중에 같은 명령을 다시 돌리면 이어서 처리된다.

| 단계 | 만드는 것 | 완료 기준 |
| --- | --- | --- |
| Phase 0. 뼈대 | uv 프로젝트, config, db + 마이그레이션, CLI 껍데기, pytest | `reelbox init`으로 DB 생성, `reelbox status` 동작, 테스트 통과 |
| Phase 1. 가져오기 | 인스타·유튜브·URL 목록 파서, 가짜 내보내기 파일 | 인스타 내보내기와 유튜브 저장된 Shorts의 개수가 앱 화면과 맞고, 다시 넣으면 새로 추가 0 |
| Phase 2. 보강 | yt-dlp 메타·자막, faster-whisper, 재시도·속도 제한 | 플랫폼별 10개에 설명·자막이 채워짐. 실패는 이유와 함께 기록 |
| Phase 3. AI 분석 | LLM 인터페이스, analyze, 프롬프트 파일 | 10개 요약·태그를 읽어보고 내가 납득. `--dry-run` 동작 |
| Phase 4. 주제 + 검색 | organize, 임베딩, FTS5, 하이브리드 검색, `reelbox search` | 내가 쓴 검색어 5개에서 원하는 영상이 상위 5개 안에. 주제 수 10\~20개 |
| Phase 5. 모바일 웹 | FastAPI JSON API + 정적 화면(data.js로 데이터 출처 분리), PWA, 원본 앱 열기, 숨기기·삭제·휴지통, 토큰 인증 | 휴대폰에서 Tailscale HTTPS 주소로 접속 → 홈 화면 추가 → 검색·원본 앱 열기·숨기기/삭제/복원 |
| Phase 6. 공유로 추가 | `/api/items`, 워커, 아이폰 단축어 안내, 추가 화면 처리 상태 | 휴대폰에서 공유한 릴스가 몇 분 안에 분석돼서 목록에 뜨기 |
| Phase 7. 다듬기 | 주제 수정·합치기, 즐겨찾기·메모, 실패 재시도 화면 | 일주일 실제로 쓰면서 불편한 점 메모 → 수정 |
| Phase 8. 보기 전용 사이트 | reelbox publish, 스냅샷 JSON·썸네일, data.js의 PC/스냅샷 전환, MiniSearch, 오프라인 대기열, Cloudflare Pages + Access | 노트북을 끄고 휴대폰 데이터(LTE)로 열어도 주제·검색·원본 열기가 되고, 그때 공유한 링크가 노트북을 켜면 처리됨 |

## 9. 설계 리스크와 대응

가장 큰 리스크는 **인스타그램 보강 단계**다. 공식 API가 없어서 내 로그인으로 가져와야 하고, 너무 빠르게 요청하면 계정이 제한될 수 있다.

| 리스크 | 영향 | 대응 |
| --- | --- | --- |
| 인스타 자동 수집이 약관에 어긋나고 계정 제한 가능 | 계정 일시 잠금, 보강 중단 | 개인 용도로만 사용, 요청 5\~10초 간격, 하루 최대 개수 설정(기본 200), 차단 신호 시 즉시 중지. 그래도 막히면 내보내기 파일의 캡션만으로 분석(`enrich --captions-only`) |
| 내보내기 파일 형식 변경 | 가져오기 실패 | 키 이름 대신 URL·시각을 재귀적으로 찾는 파서, 인식 못 한 항목은 로그로 보고 |
| 윈도우 Chrome 쿠키를 yt-dlp가 못 읽음 | 저장된 Shorts 목록을 못 가져옴 | Firefox 쿠키 또는 cookies.txt 사용. 그래도 안 되면 테이크아웃을 확인하거나, 브라우저에서 목록을 뽑아 URL 목록으로 가져오기 |
| 자막·말이 없는 영상(음악만, 화면 글자만) | 요약 품질 낮음 | 제목·캡션으로 분석하고 "정보 부족" 표시. 화면 글자 인식(OCR)은 나중에 |
| PC가 오래 꺼져 있음 | 스냅샷이 오래됨, 새 공유·의미 검색 멈춤 | 보기 전용 사이트(6장)로 마지막 스냅샷을 본다. 화면에 "마지막 업데이트 N일 전"과 대기열 개수를 표시하고, PC가 켜지면 자동으로 대기열 처리와 publish |
| 홈 화면 웹앱에서 링크가 원본 앱 대신 브라우저로 열림 | 로그인 안 된 웹이라 댓글·좋아요를 못 씀 | 링크를 새 창으로 열고 실제 기기에서 확인. 안 되면 앱 전용 주소(youtube://, instagram://)로 연다 |
| AI 비용 | API 요금 | Haiku 등급 모델, 자막 길이 제한, `--dry-run`으로 미리 확인, 한 번 분석한 건 다시 안 함 |
| 웹 서버가 외부에 노출 | 내 저장 목록 유출 | 공유기 포트 개방 금지, Tailscale + 토큰만 허용 |
| 저장한 영상의 저작권 | 재배포 시 문제 | 영상 파일을 보관·공유하지 않고 원본 앱으로 연결만 한다 |
| 보기 전용 사이트 주소가 인터넷에 공개됨 | 저장 목록·메모 유출 | Cloudflare Access로 내 이메일만 허용. 스냅샷에 자막 원문·숨김·삭제 항목·비밀값을 넣지 않음. 배포 전 Access가 켜져 있는지 publish가 확인 |
| 휴대폰 브라우저 저장소가 지워짐(특히 아이폰에서 오래 안 쓸 때) | 보내지 못한 공유·숨기기 대기열 손실 | 홈 화면에 설치해서 쓰기, 대기열 개수를 항상 표시, PC가 켜지면 바로 비움 |
| Cloudflare 설정 화면이 영어 | Pages·Access 설정에서 막힘 | Phase 8 프롬프트에서 Claude Code에게 화면별 한국어 단계 안내(영어 메뉴 이름 병기)를 요청 |

## 10. CLAUDE.md (프로젝트 루트에 저장)

Claude Code는 매 대화 시작 때 프로젝트 루트의 `CLAUDE.md`를 자동으로 읽는다. 아래 내용을 그대로 복사해 넣고, 이 문서는 Markdown으로 내보내 `docs/DESIGN.md`에 둔다.

```markdown
# Reelbox — Claude Code 작업 규칙

## 프로젝트
인스타그램·유튜브에 이미 저장한 릴스·쇼츠를 가져와 AI로 요약·주제 분류하고,
휴대폰(PWA)에서 검색·주제별로 보는 개인용 앱. PC가 서버, 휴대폰은 Tailscale로 접속.
PC가 꺼져 있으면 Cloudflare Pages에 올린 보기 전용 스냅샷을 본다.
설계는 docs/DESIGN.md 가 기준이다. 작업 전에 해당 절을 먼저 읽는다.

## 나에 대해
- Go 백엔드 경력은 있지만 Python은 초보다. 새 Python 문법·도구가 나오면
  Go에서는 무엇과 비슷한지 한두 줄로 설명해달라.
- 답변과 코드 주석은 한국어. 식별자·커밋 메시지는 영어.

## 작업 방식
- 한 번에 DESIGN.md 8장의 Phase 하나만 진행한다.
- 구현 전에 계획(만들 파일, 함수, 테스트)을 보여주고 승인받는다.
- 설계와 다르게 해야 할 이유가 생기면 먼저 묻고, 합의되면 DESIGN.md도 고친다.
- Phase가 끝나면: 테스트 통과 확인 → 완료 기준 체크리스트 → 내가 직접 실행해볼
  명령 → 커밋 메시지 제안. 커밋은 내가 요청할 때만 한다.

## 명령
- 설치: uv sync
- 테스트: uv run pytest
- 린트/포맷: uv run ruff check . && uv run ruff format .
- 실행: uv run reelbox <명령>
- 외부 도구: ffmpeg 필요 (없으면 설치 방법을 안내할 것)

## 코드 규칙
- Python 3.12, 타입 힌트 필수, 데이터 구조는 Pydantic 모델.
- 설정은 config.py(pydantic-settings) + .env. 비밀값은 코드·커밋에 넣지 않는다.
- 로그는 logging, CLI 출력은 rich. print 금지.
- 모든 처리 단계는 멱등하고 --limit N 을 지원한다. 상태는 items.status 로 관리.
- 외부 호출(yt-dlp, Claude API, whisper)은 작은 래퍼 함수 뒤에 두고
  테스트에서는 가짜로 바꾼다. 테스트는 네트워크 없이 통과해야 한다.
- DB 스키마 변경은 migrations/ 에 새 번호 파일로만 한다.
- LLM 프롬프트는 prompts/ 의 파일로 두고 버전을 기록한다.
- 화면 코드는 static/data.js 를 통해서만 데이터를 읽고 쓴다 (PC API ↔ 스냅샷 전환).

## 하지 말 것
- 영상 파일을 영구 저장하지 않는다 (오디오는 data/tmp 에 받고 사용 후 삭제).
- 인스타그램 요청 간격·일일 한도를 줄이거나 없애지 않는다.
- 웹 서버 기본 바인딩은 127.0.0.1. 외부 노출은 tailscale serve 로만.
- data/, .env, 쿠키 파일을 커밋하지 않는다.
- 스냅샷(보기 전용 사이트)에 자막 원문, 숨김·삭제 항목, 비밀값을 넣지 않는다.
- 내가 요청하지 않은 기능을 미리 만들지 않는다.
```

## 11. Claude Code에 붙여넣을 프롬프트

### 시작 전 준비

1. 인스타 "내 정보 다운로드"(JSON)와 Google 테이크아웃(재생목록, 보조용)을 **지금 요청**해 둔다. 파일이 오는 데 시간이 걸려서 Phase 1 전에 받아두는 게 좋다.
2. PC의 Firefox로 유튜브에 로그인하고, 내 페이지 → 재생목록 → "저장된 Shorts 동영상"을 열어 주소를 복사해 둔다.
3. `reelbox` 폴더를 만들고 `git init`, 이 문서를 Markdown으로 내보내 `docs/DESIGN.md`에, 10장 내용을 `CLAUDE.md`에 저장한다.
4. uv, ffmpeg, Node.js(Phase 8의 wrangler용)를 설치하고, VS Code에서 폴더를 열어 Claude Code를 실행한다.
5. 각 프롬프트는 **새 대화에서**(`/clear` 후) 보낸다. 필요하면 Shift+Tab으로 plan 모드를 켜서 계획부터 받는다.

### Phase 0 — 뼈대

```text
docs/DESIGN.md 전체와 CLAUDE.md를 읽어줘. 이번에는 Phase 0(뼈대)만 진행해.

할 일:
- uv로 Python 3.12 프로젝트 생성 (src 레이아웃, 패키지명 reelbox, 콘솔 스크립트 reelbox)
- DESIGN.md 7장의 폴더 구조 만들기 (빈 모듈은 독스트링만)
- config.py, db.py, migrations/001_init.sql (4장의 테이블 전부, FTS5·sqlite-vec 포함)
- CLI: init, status 구현. 나머지 명령은 "아직 구현 안 됨"만 출력
- ruff, pytest 설정과 첫 테스트, .gitignore, .env.example

먼저 계획을 보여주고 내 승인 후 구현해. sqlite-vec나 FTS5 trigram이
내 환경에서 안 되면 우회하지 말고 에러와 원인을 알려줘.
끝나면 완료 기준 체크리스트와 내가 실행해볼 명령을 정리해줘.
```

### Phase 1 — 가져오기

```text
DESIGN.md 3장, 5-1절을 읽고 Phase 1을 진행해.

1) 인스타그램: 내 실제 내보내기 폴더는 <압축 푼 폴더 경로>야.
   saved_posts.json, saved_collections.json의 실제 형식을 앞부분만 살펴보고
   (개인정보가 있으니 출력은 최소한으로), 그 형식을 흉내 낸 가짜 파일을
   tests/fixtures/에 만들고 importers/instagram.py를 구현해.

2) 유튜브: "저장된 Shorts 동영상" 목록 주소는 <재생목록 URL>이야.
   yt-dlp --flat-playlist + 로그인 쿠키(.env의 브라우저 이름 또는 cookies.txt 경로)로
   ID 목록을 읽는 importers/youtube.py와 reelbox sync youtube 를 만들어줘.
   먼저 10개만 읽어서 보여주고, 쿠키를 못 읽으면 우회하지 말고 원인과 해결 방법을 알려줘.
   다시 돌리면 새로 저장한 것만 추가돼야 해.
   테이크아웃 폴더(<경로>)가 있으면 저장된 Shorts가 들어 있는지 확인하고,
   있으면 저장 시각만 채우는 보조 가져오기(import youtube)도 만들어줘.

3) importers/urllist.py 와 import urls.

URL 정규화와 중복 방지 테스트는 필수. 테스트에서 yt-dlp는 가짜로 바꿔줘.
```

### Phase 2 — 보강

```text
DESIGN.md 5-2절과 9장을 읽고 Phase 2를 진행해.
enrich/metadata.py(yt-dlp, 다운로드 없이), captions.py, transcribe.py(faster-whisper)와
enrich 명령을 구현해.
- 인스타는 .env의 쿠키 설정 사용, 요청 간 5~10초 무작위 대기, 일일 한도,
  로그인 차단·429면 즉시 중단
- 실패는 status/error/attempts에 기록, 삭제된 영상은 unavailable
- 오디오는 data/tmp에 받고 반드시 삭제 (예외가 나도)
- whisper 모델 크기는 설정값
yt-dlp 옵션 중 내가 알아야 할 것은 주석으로 설명해줘.
끝나면 플랫폼별 --limit 5로 돌려볼 명령을 알려줘.
```

### Phase 3 — AI 분석

```text
DESIGN.md 5-3절을 읽고 Phase 3을 진행해.
- ai/llm.py: 모델을 교체할 수 있는 작은 인터페이스 + Anthropic 구현
  (모델명은 설정값. 최신 모델명과 구조화 출력 방법은 공식 문서에서 확인)
- prompts/analyze_v1.md 작성: 한국어 요약, 핵심 포인트, raw_topic, 태그
- 출력은 Pydantic으로 검증, 실패 시 1회 재시도
- analyze 명령 + --dry-run(항목 수, 예상 토큰 수)
테스트는 가짜 LLM으로. 끝나면 내가 10개 결과를 검토할 수 있게
제목/요약/태그를 표로 보여주는 명령도 알려줘.
```

### Phase 4 — 주제 정리 + 검색

```text
DESIGN.md 5-4, 5-5절을 읽고 Phase 4를 진행해.
- organize: raw_topic → 대주제 10~20개 통합, is_user_edited 주제 유지,
  새 항목은 기존 주제에 배정, --full은 전체 재정리
- index: bge-m3 임베딩 + FTS5 색인
- search: FTS5 BM25 + 벡터 검색을 RRF로 합치기, 필터 지원
- reelbox search "..." 로 터미널에서 확인
RRF가 무엇인지 코드 주석에 쉽게 설명해줘.
```

### Phase 5 — 모바일 웹

```text
DESIGN.md 6장을 읽고 Phase 5를 진행해.
FastAPI JSON API + 정적 화면(HTML + Alpine.js + Tailwind CDN, 빌드 없음)으로
휴대폰 우선 화면을 만들어. 화면은 static/data.js 한 곳에서만 데이터를 가져오게 해줘
(Phase 8에서 스냅샷으로 바꿀 수 있게):
홈, 검색 결과, 주제 상세, 영상 상세, 추가, 설정 + 하단 탭바.
- PWA: manifest.json, 간단한 service worker, 아이콘
- 영상은 재생하지 않고 "원본 앱에서 보기" 버튼으로 인스타그램·유튜브 앱을 연다 (6장 참고)
- 숨기기·삭제·휴지통 (DESIGN.md 4장 "숨기기와 삭제", 6장): 목록·검색·주제에서 제외,
  되돌리기 알림, 여러 개 선택, reelbox purge, 가져오기·공유는 삭제된 항목 건너뛰기
- REELBOX_TOKEN 인증(쿠키 또는 헤더), 기본 바인딩 127.0.0.1
- JSON API도 함께 (/api/search, /api/items/{id} 등)
끝나면 tailscale serve로 휴대폰에서 HTTPS 접속하고
홈 화면에 추가하는 방법을 단계별로 알려줘.
```

### Phase 6 — 휴대폰 공유로 추가

```text
DESIGN.md 3장 "그 외 입구"와 6장을 읽고 Phase 6을 진행해.
- POST /api/items, 백그라운드 워커(보강→분석→색인)
- 아이폰용 단축어 만드는 법을 설정 화면에 안내 (공유 시트 → URL을
  토큰 헤더와 함께 POST)
- 추가 화면에 처리 상태 표시
```

### Phase 8 — 보기 전용 사이트

```text
DESIGN.md 6장 "보기 전용 사이트"와 9장을 읽고 Phase 8을 진행해. 먼저 계획을 보여줘.
- reelbox publish: 숨김·삭제를 뺀 항목과 주제를 data/dist/ 에 JSON으로 만들기
  (자막 원문 제외), 썸네일 복사, 비슷한 영상 목록, 마지막 업데이트 시각 포함.
  --dry-run이면 파일 수와 용량만 출력. 바뀐 게 없으면 배포하지 않기.
- static/data.js: PC API(2초 제한)를 먼저 시도하고 실패하면 스냅샷 JSON을 읽기.
  화면 상단에 "PC 연결됨" / "스냅샷 · 마지막 업데이트 N일 전"과 대기열 개수 표시.
- 스냅샷 모드 검색: MiniSearch + 한국어 두 글자 단위 토큰화.
- 오프라인 대기열: 공유·숨기기·삭제·즐겨찾기를 IndexedDB에 쌓았다가
  PC에 연결되면 전송. PC API에 Pages 주소만 허용하는 CORS 추가.
- wrangler로 Cloudflare Pages에 배포. serve 실행 중이면 run이 끝날 때와 하루 한 번 자동 publish.
- Cloudflare 가입, Pages 프로젝트 만들기, Access(이메일 일회용 코드) 설정을
  한국어로 화면 단계별로 알려줘. 영어 메뉴 이름은 원문 그대로 괄호에 같이 적어줘.
끝나면 노트북을 끄고 휴대폰 LTE로 확인하는 순서를 알려줘.
```

### 막혔을 때

```text
지금 <증상>이 생겨. 바로 고치지 말고 원인 후보 2~3개와 확인 방법을 먼저 말해줘.
설계를 바꿔야 하는 문제면 DESIGN.md 어느 절을 어떻게 바꿀지 제안해줘.
```
