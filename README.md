# 디스코드 대화 수집 → 요약 → 노션

봇이 대화를 모아 SQLite에 쌓고, MCP로 에이전트에 노출하고, 에이전트가 정리해서 노션에 쓴다.
기획은 [PLAN.md](PLAN.md), 발단이 된 대화 정리는 [SPEC.md](SPEC.md).

## 지금 상태

| | 상태 |
|---|---|
| 수집 봇 (`bot.py`) | 코드 완료 · 토큰 검증됨 · 슬래시 커맨드 6개 동기화 확인 |
| MCP 서버 (`mcp_server.py`) | 코드 완료 · stdio JSON-RPC 통신 확인 · 툴 4개 |
| 에이전트 스킬 2개 | 작성 완료 |
| 계정 등록기 (`launcher.py`) | 코드 완료 · UI 확인 · 격리 검증됨 |
| 실행 스크립트 (`start.bat`) | 봇 + 등록기를 한 창에서. 창 닫으면 같이 종료 |
| **`MESSAGE CONTENT INTENT`** | **꺼져 있음 — 이거 하나 때문에 봇이 못 뜬다** |

봇 계정: `Abstraction Bot#7657` · 이미 들어가 있는 서버: `AI Native 스터디`

## ▶ 지금 해야 할 것 — 체크박스 하나

https://discord.com/developers/applications/1528500011476713602/bot

**Privileged Gateway Intents** 에서 **MESSAGE CONTENT INTENT** 를 켜고 저장한다.
봇이 100서버 미만이면 심사 없이 토글만으로 끝난다.

켜기 전에는 봇이 뜨지 않고 안내 메시지를 내고 종료한다.

## 설치 · 실행

```bash
uv sync
```

`.env` 를 만든다 (`.env.example` 복사 후 `DISCORD_TOKEN` 채우기). `.env` 는 커밋되지 않는다.

**`start.bat` 더블클릭.** 창 하나에서 수집 봇과 계정 등록기가 같이 뜨고,
**그 창을 닫으면 둘 다 종료된다.**

```
계정 등록기  http://127.0.0.1:8787
Abstraction Bot#7657 접속. 서버 1개
```

위처럼 뜨면 성공.

## 다른 서버에 초대하기

최소 권한만 요청한다. `Administrator` 를 요구하면 승인이 거절된다.

```
https://discord.com/oauth2/authorize?client_id=1528500011476713602&permissions=68608&scope=bot+applications.commands
```

`permissions=68608` = 채널 보기(1024) + 메시지 보내기(2048) + 메시지 기록 읽기(65536)

## 사용법

### 1. 디스코드에서 수집 시작

| 커맨드 | 권한 | 하는 일 |
|---|---|---|
| `/수집시작 [채널]` | 서버 관리 | 화이트리스트 등록 + 채널에 고지 게시 |
| `/수집중지 [채널]` | 서버 관리 | 화이트리스트 해제 |
| `/backfill [채널] [일수=90]` | 서버 관리 | 과거 대화 일회성 적재 |
| `/수집중단` | 본인 | 내 메시지 제외 (이미 쌓인 것도) |
| `/수집재개` | 본인 | 다시 허용 |
| `/상태` | 누구나 | 채널·건수·최근 수집 시각 |

**지정한 채널만** 수집한다. 서버 전체를 읽지 않는다.

### 2. Claude Code에서 정리

`.mcp.json` 이 이미 등록돼 있어 이 폴더에서 Claude Code를 열면 툴이 붙는다.

```
8월 27일 아이디어 채널 정리해줘        → daily-report 스킬
지난주 대화에서 사업 아이디어 뽑아줘     → idea-extract 스킬
```

MCP 툴: `list_channels` · `recent_days` · `get_conversation(channel, date)` · `search_messages(query, ...)`

**노션 쓰기는 자동으로 하지 않는다.** 초안을 보여주고 확인받은 뒤에 기록한다.

### 3. 팀원 계정 등록 (선택)

`start.bat` 이 떠 있으면 http://127.0.0.1:8787 에서 **연결** 버튼 → 로그인 창에서 로그인.
누르는 것 말고 할 일이 없다.

계정마다 격리된 홈(`data/agents/<agent>/<id>/`)에서 벤더 CLI 자체의 로그인을 돌리므로
**자격증명은 이 PC를 떠나지 않는다.** `test_launcher.py` 가 이 격리를 검증한다.

## 파일

```
bot.py           수집 봇 — LLM 없음, DB에 넣기만 한다
db.py            스키마 + 공용 쿼리. UTC 저장, KST 조회
mcp_server.py    읽기 전용 MCP 서버
launcher.py      계정 등록기 (Starlette, 127.0.0.1:8787)
run.py           봇 + 등록기를 한 프로세스로
start.bat        실행 진입점. 창 닫으면 종료
.mcp.json        Claude Code MCP 등록 (uv run 이라 경로 무관)
.claude/skills/  daily-report · idea-extract
data/messages.db SQLite (gitignore됨)
```

## 테스트

```bash
uv run python test_db.py && uv run python test_mcp.py && uv run python test_launcher.py
```

## 설계 메모

- **봇은 멍청하게, 두뇌는 에이전트에.** 봇에 LLM 호출도 노션 클라이언트도 없다.
  요약 프롬프트를 고쳐도 봇을 재배포하지 않는다.
- **UTC 저장 / KST 조회.** 팀이 KST로 말하므로 `2026-08-27` 은 KST 하루를 뜻한다.
- **스레드 메시지는 부모 채널 소속으로 저장한다.** 하루치 요약에 스레드 논의가 같이 딸려와야 한다.
- **삭제·수집중단은 raw 이벤트로 잡는다.** 캐시에 없는 옛 메시지도 확실히 가려진다.
- **MCP 커넥션은 호출마다 연다.** MCP가 툴을 워커 스레드에서 돌리는데 SQLite 커넥션은
  스레드에 묶이기 때문.
