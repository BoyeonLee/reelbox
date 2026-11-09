# Cloudflare 설정 (보기 전용 사이트, Phase 8)

PC가 꺼져 있을 때 볼 스냅샷을 Cloudflare Pages에 올리고, Cloudflare Access로 **내 이메일만** 들어오게 잠근다.
처음 한 번만 하면 되고, 30~40분쯤 걸린다.

- 영어 메뉴 이름은 화면에 보이는 그대로 괄호 안에 적었다. Cloudflare가 메뉴 이름을 자주 바꾸니, 조금 달라도 비슷한 이름을 찾으면 된다(2026-11 기준 공식 문서로 확인함).
- **순서가 중요하다.** Access로 잠그기(4단계) 전에는 아무것도 올리지 않는다. `reelbox publish`도 Access가 꺼져 있으면 배포를 거부한다.

| 단계 | 하는 일 | 결과 |
| --- | --- | --- |
| 1 | Cloudflare 가입 | 계정 |
| 2 | API 토큰 만들기 | `.env` 의 `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID` |
| 3 | Pages 프로젝트 만들기 (빈 프로젝트) | `https://reelbox.pages.dev` |
| 4 | Zero Trust 준비 + 일회용 코드 로그인 켜기 | 이메일 로그인 |
| 5 | Access로 사이트 두 주소 잠그기 | 나만 열 수 있음 |
| 6 | `.env` 채우고 첫 배포 | 휴대폰에서 열기 |

---

## 1. Cloudflare 가입

1. 브라우저에서 <https://dash.cloudflare.com/sign-up> 을 연다.
2. 이메일과 비밀번호를 넣고 **계정 만들기(Sign up)** 를 누른다.
3. 받은 메일함에서 확인 메일의 링크를 눌러 이메일을 인증한다.
4. 처음 화면에서 도메인을 연결하라고 하면 건너뛴다. 도메인은 필요 없다(`pages.dev` 주소를 쓴다).

> 무료 범위: 가입한 뒤 Pages와 Zero Trust 모두 **무료 요금제(Free)** 로 쓴다. 개인 혼자 쓰는 정도는 무료 범위 안이지만, 요금제 화면에서 현재 한도를 한 번 확인한다.

## 2. API 토큰 만들기 (wrangler가 배포할 때 씀)

1. 오른쪽 위 사람 아이콘 → **프로필(My Profile)** → 왼쪽 **API 토큰(API Tokens)**.
   - 계정 화면에 **계정 API 토큰(Account API Tokens)** 메뉴가 있으면 거기서 해도 된다.
2. **토큰 만들기(Create Token)** 를 누른다.
3. 맨 아래 **사용자 지정 토큰(Custom token)** 의 **시작하기(Get started)** 를 누른다.
4. **토큰 이름(Token name)**: `reelbox-publish`
5. **권한(Permissions)**: 딱 하나만 체크한다 → `Cloudflare Pages` · `편집(Edit)`
   - 권한이 그룹 목록으로 나오는 화면이면: 범위는 **Entire Account** 그대로 두고, 검색칸(Search for permission groups...)에 `Pages` 를 쳐서 **Developer Platform** 안의 **Pages** 줄에서 **Edit** 만 체크한다. 같이 검색되는 **Account Custom Pages**, **Access: Custom Pages** 는 다른 기능이라 체크하지 않는다.
   - 예전 화면이면 줄을 `계정(Account)` · `Cloudflare Pages` · `편집(Edit)` 으로 고른다.
   - **Cloudflare One / Zero Trust** 의 Access 권한은 체크하지 않는다. Access는 5단계에서 대시보드로 직접 설정하고, publish의 Access 확인은 토큰 없이 주소만 열어 본다.
6. **계정 리소스(Account Resources)** 는 `포함(Include)` · 내 계정.
7. **요약으로 계속(Continue to summary)** → **토큰 만들기(Create Token)**.
8. 화면에 나온 토큰을 **지금 복사**한다. 다시 보여주지 않는다.
9. 계정 ID: 왼쪽 메뉴 **Workers 및 Pages(Workers & Pages)** 를 열면 오른쪽에 **계정 ID(Account ID)** 가 있다. 이것도 복사한다.

`~/reelbox/.env` 에 넣는다 (이 파일은 커밋되지 않는다):

```bash
CLOUDFLARE_API_TOKEN=복사한_토큰
CLOUDFLARE_ACCOUNT_ID=복사한_계정ID
```

## 3. Pages 프로젝트 만들기

대시보드의 "드래그해서 올리기"는 파일을 바로 공개해 버린다. 그래서 **아무 파일도 올리지 않고 빈 프로젝트만 만드는** 명령으로 한다. WSL 터미널에서:

```bash
cd ~/reelbox
set -a; source .env; set +a      # .env 값을 이 터미널에만 잠깐 불러온다
npx --yes wrangler@4 pages project create reelbox --production-branch main
```

- 출력의 `It will be available at https://....pages.dev/` 주소를 **꼭 적어 둔다.** `pages.dev` 주소는 전 세계에서 하나라서, 이름이 이미 쓰이고 있으면 `reelbox-dvv.pages.dev` 처럼 뒤에 글자가 자동으로 붙는다. 프로젝트 이름은 그대로 `reelbox` 다.
  - 이 문서의 `reelbox.pages.dev` 는 전부 **이 주소로 바꿔서** 읽는다 (5단계 Access, 6단계 `.env`).
- 만들어졌는지 확인:
  - 터미널: `npx --yes wrangler@4 pages project list` → 표에 프로젝트와 주소가 보이면 된다.
  - 대시보드: 메뉴 이름이 자주 바뀌니, 로그인한 채로 주소창에 `https://dash.cloudflare.com/?to=/:account/workers-and-pages` 를 넣거나, 위쪽 검색(Ctrl+K)에 `Pages` 를 친다. 아직 배포 전이라 "배포 없음"으로 보이는 게 정상이다.

## 4. Zero Trust 준비 + 일회용 코드 로그인 켜기

1. 대시보드 왼쪽 메뉴 **Zero Trust** 를 누른다.
2. 처음이면 준비 화면이 나온다.
   - **팀 이름(Team name)**: 아무 영어 이름 (예: `boyeon-reelbox`). 로그인 주소 `<팀이름>.cloudflareaccess.com` 에 쓰인다.
   - 요금제(Plan): **무료(Free)** 를 고른다. 결제 수단을 물어볼 수 있는데, Free는 0원이다.
3. 왼쪽 **연동(Integrations)** → **ID 공급자(Identity providers)**.
   - 예전 화면에서는 **설정(Settings)** → **인증(Authentication)** 에 있다.
4. **새 ID 공급자 추가(Add new identity provider)** → **일회용 PIN(One-time PIN)** → 저장.
   - 새 조직에는 일회용 PIN이 자동으로 켜져 있지 않아서 직접 추가해야 한다.

## 5. Access로 사이트 잠그기 (주소 두 개)

Pages의 "Access 정책 켜기"는 **미리보기 주소(`*.reelbox.pages.dev`)만** 잠근다. 그래서 공식 문서 방법대로 두 번 켜서 **진짜 주소용 하나 + 미리보기용 하나**를 만든다. 배포할 때마다 생기는 `<해시>.reelbox.pages.dev` 주소까지 막으려면 둘 다 필요하다.

### 5-1. 첫 번째: `reelbox.pages.dev` (휴대폰에 설치할 주소)

1. **Workers 및 Pages(Workers & Pages)** → `reelbox` 프로젝트 → **설정(Settings)** → **일반(General)**.
   - 메뉴가 안 보이면 주소창에 `https://dash.cloudflare.com/?to=/:account/workers-and-pages` 를 넣고 `reelbox` 를 누른다.
   - 배포가 하나도 없으면 설정 화면 대신 "첫 배포 만들기"만 보일 수 있다. 그럴 땐 내용 없는 안내 페이지를 먼저 한 번 올린다(공개돼도 괜찮은 "준비 중" 한 줄):
     ```bash
     mkdir -p /tmp/reelbox-placeholder && echo '<h1>준비 중</h1>' > /tmp/reelbox-placeholder/index.html
     set -a; source .env; set +a
     npx --yes wrangler@4 pages deploy /tmp/reelbox-placeholder --project-name reelbox --branch main
     ```
2. **Access 정책 사용(Enable access policy)** 을 누른다.
   - 새 화면에서는 **Preview access** 줄 오른쪽의 **미리보기 제한(Restrict previews)** 버튼이다. 이미 "Preview deployments are restricted by a Cloudflare Access policy." 라고 나오면 켜진 상태이니 3번으로 간다.
3. 옆의 **관리(Manage)** 를 누르면 Zero Trust의 Access 앱 화면으로 넘어간다.
   - 직접 찾아가려면 **Zero Trust** → **Access 제어(Access controls)** → **애플리케이션(Applications)** → 앱 → **구성(Configure)**.
4. **공개 호스트 이름(Public hostname)** 의 **하위 도메인(Subdomain)** 칸에 있는 `*` 를 지운다. 칸이 `reelbox.pages.dev` 를 가리키게 된다.
5. **저장(Save)**. 이름이 겹친다는 오류가 나면 **애플리케이션 이름(Application name)** 을 `reelbox-main` 으로 바꾸고 다시 저장한다.

### 5-2. 두 번째: `*.reelbox.pages.dev` (미리보기·배포별 주소)

1. 다시 Pages 프로젝트 → **설정(Settings)** → **일반(General)** → **Access 정책 사용(Enable access policy)** 을 한 번 더 누른다.
   - 새 화면에서는 **Preview access** 줄이 "Preview deployments are public by default." 로 바뀌어 있다(꺼진 상태). 오른쪽의 **미리보기 제한(Restrict previews)** 을 누른다.
2. **Zero Trust** → **Access 제어(Access controls)** → **애플리케이션(Applications)** 에 앱이 **두 개** 있는지 확인한다.
   - `reelbox.pages.dev`
   - `*.reelbox.pages.dev`

### 5-3. 두 앱 모두에 똑같이 설정

앱마다 **구성(Configure)** 을 누르고:

1. **정책(Policies)** 탭: 정책을 열어 **규칙(Rules)** 의 **포함(Include)** 을 `이메일(Emails)` = `<내 이메일>` 으로 둔다. 다른 사람이 들어 있으면 지운다. 동작(Action)은 **허용(Allow)**.
2. **로그인 방법(Login methods)**: **일회용 PIN(One-time PIN)** 만 켠다.
   - 하나만 켜면 보이는 **즉시 인증 적용(Apply instant authentication)** 은 켜도 된다(로그인 선택 화면을 건너뜀).
3. **세션 기간(Session Duration)**: **1개월(1 month)**. 짧으면 앱을 열 때마다 메일 코드를 다시 받아야 한다.
4. **저장(Save)**.

### 5-4. 잠겼는지 확인

PC 브라우저 시크릿 창으로 `https://reelbox.pages.dev` 를 연다. 사이트가 아직 비어 있어도, **이메일을 넣으라는 Cloudflare Access 화면**이 나오면 성공이다.

## 6. `.env` 채우고 첫 배포

`~/reelbox/.env` 에 추가한다:

```bash
REELBOX_PAGES_PROJECT=reelbox
REELBOX_PAGES_URL=https://reelbox.pages.dev
# tailscale serve 주소 (휴대폰에서 지금 접속하는 주소, 끝에 / 없이)
REELBOX_PC_URL=https://<컴퓨터이름>.<tailnet>.ts.net
```

그리고:

```bash
cd ~/reelbox
uv run reelbox publish --dry-run   # 파일 수·용량만 확인
uv run reelbox publish             # Access 확인 → 배포
```

- `Cloudflare Access 가 켜져 있지 않아 배포하지 않았어요` 가 나오면 5단계의 앱 두 개를 다시 확인한다.
- 처음엔 wrangler를 받느라 1~2분 걸린다.
- `reelbox serve` 가 켜져 있으면 서버가 켜질 때와 하루 한 번, `reelbox run` 이 끝날 때 자동으로 publish한다. 바뀐 게 없으면 올리지 않는다.

## 7. 휴대폰에 설치 (한 번만)

1. 아이폰 Safari로 `https://reelbox.pages.dev` 를 연다.
2. 이메일(`<내 이메일>`)을 넣고 → 메일로 온 6자리 코드를 넣는다.
3. 공유 버튼 → **홈 화면에 추가**. 예전에 Tailscale 주소로 설치한 Reelbox 아이콘은 지워도 된다.
4. 홈 화면 아이콘으로 연다.
   - 홈 화면 앱은 Safari와 로그인 정보를 따로 쓴다. 그래서 Access 코드를 한 번 더 받는다.
   - PC가 켜져 있으면 **토큰 입력 화면**이 나오니 `REELBOX_TOKEN` 을 붙여넣는다(처음 한 번만).
5. 단축어 수정: 설정 탭 → 아이폰 단축어 설치 안내의 2번처럼, 기존 "Reelbox에 추가" 단축어에 **클립보드에 복사(Copy to Clipboard)** 를 넣는다. 맨 위 "공유 시트에서 … 받기"는 입구 설정이라 움직일 수 없으니, 그 **바로 아래(URL 콘텐츠 가져오기보다 위)** 에 두면 된다. 단축어의 주소는 PC 주소(`…ts.net/api/items`) 그대로 둔다.

## 막히면

| 증상 | 확인할 것 |
| --- | --- |
| `wrangler 배포 실패: Authentication error` | `.env` 의 토큰 권한이 `Cloudflare Pages · Edit` 인지, 계정 ID가 맞는지 |
| `Project not found` | `REELBOX_PAGES_PROJECT` 가 3단계에서 만든 이름과 같은지 |
| 배포는 됐는데 옛 내용이 보임 | `REELBOX_PAGES_BRANCH`(기본 `main`)가 프로젝트의 운영 브랜치와 같은지. 다르면 미리보기 주소로만 올라간다 |
| PC가 켜져 있는데 "스냅샷"으로 뜸 | 아이폰 Tailscale이 켜져 있는지, `REELBOX_PC_URL` 이 맞는지, `REELBOX_PAGES_URL` 을 바꿨으면 `serve` 를 다시 켰는지(CORS) |
| 앱을 열 때마다 이메일 코드를 물어봄 | 5-3의 세션 기간(Session Duration)이 1개월인지 |
