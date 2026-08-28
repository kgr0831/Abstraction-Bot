# 배포 — DisHost + Cloudflare Tunnel

무료로 24시간 돌리고 고정 HTTPS 주소를 얻는 조합이다.
봇 호스팅은 상시 구동과 영구 디스크를 주고, 터널이 밖에서 들어오는 길을 만든다.

```
[DisHost 컨테이너]
   run.py (봇 + 콘솔 + SQLite)  ←루프백─  cloudflared  ──아웃바운드──▶  [Cloudflare]
                                                                          │
                                                            https://고정주소 ─▶ 팀원
```

## 왜 이 조합인가

| | |
|---|---|
| 무료 웹호스팅(Render 등) | 무활동 15분에 재운다 → 게이트웨이가 끊겨 대화를 놓친다 |
| Cloudflare Workers / Vercel | 상시 프로세스가 불가능 → discord.py 를 못 돌린다 |
| Oracle Always Free | 넉넉하지만 **유휴 회수 대상**(7일 CPU 20% 미만). 우리 봇이 딱 그 모양이다 |
| **봇 호스팅 + 터널** | 상시 구동이 본업이라 안 잔다. HTTPS 만 터널로 채운다 |

## 필요한 것

- 메모리 **128MB 이상** — 실측 95MB, 여유 33MB
- 디스크 **200MB 이상** — 패키지 57MB + SQLite (보존 90일이면 15MB 안쪽)
- 아웃바운드 인터넷, 시작 명령 수정 권한

## 1. Cloudflare 터널 만들기 (고정 주소)

Zero Trust > Networks > Tunnels > Create a tunnel > **Cloudflared** 선택.

1. 터널 이름을 정하면 **토큰**이 나온다. 복사해둔다 (`TUNNEL_TOKEN`)
2. Public Hostname 추가:
   - Subdomain/Domain: 쓰고 싶은 주소
   - Service: `HTTP` / `localhost:8787`

토큰 없이도 돌아가지만 **재시작마다 주소가 바뀐다.** 커넥터 주소가 바뀌면
팀원 전원이 다시 등록해야 하므로, 실사용은 반드시 토큰 방식으로 한다.

## 2. 호스팅에 올리기

파일을 올리고(Git 또는 SFTP), 패널 **Variables** 에 넣는다.

| 변수 | 값 |
|---|---|
| `DISCORD_TOKEN` | 봇 토큰 (필수) |
| `TUNNEL_TOKEN` | 1번에서 받은 토큰 |
| `SERVER_URL` | `https://정한주소` — 커넥터 주소가 여기서 나온다 |
| `LAUNCHER_URL` | `https://정한주소` — 봇이 `/시작` 에서 안내하는 주소 |
| `ANTHROPIC_API_KEY` 또는 `OPENAI_API_KEY` | 결산용. 없으면 통계 결산 |

**시작 명령(Startup Command):**

```bash
bash deploy/start.sh
```

스크립트가 알아서 한다 — 의존성 설치(처음 한 번), cloudflared 내려받기(처음 한 번),
터널과 앱을 같이 띄우고, **하나가 죽으면 둘 다 내린다**(반쪽만 살아 있으면 조용히
고장난 상태가 되므로).

## 3. 확인

1. 콘솔 로그에 `Abstraction Bot#... 접속` 이 뜨는가
2. `https://정한주소` 가 열리는가
3. 디스코드에서 `/시작` → 링크가 그 주소로 오는가

## 컨테이너(Docker)로 올릴 때

Fly.io·Oracle·아무 컨테이너 호스팅이든 `Dockerfile` 을 그대로 쓴다.

```bash
docker build -t abstraction .
docker run -d --name abstraction \
  -e DISCORD_TOKEN=... -e SERVER_URL=https://... -e LAUNCHER_URL=https://... \
  -v abstraction-data:/app/data -p 8787:8787 abstraction
```

`/app/data` 를 **반드시 볼륨으로 붙인다.** 안 붙이면 재시작 때 SQLite 와
연동 정보가 날아가고 팀원 전원이 다시 연동해야 한다.

## 주의

- **`PUBLIC_SERVER=1` 은 켜진 채로 둔다.** `start.sh` 와 `Dockerfile` 에 이미 박혀 있다.
  터널·프록시는 요청을 `127.0.0.1` 로 전달하므로, 루프백 무토큰 예외를 켜둔 채
  공개하면 주소만 아는 사람이 전부 무토큰으로 통과한다.
- **주소를 바꾸면 팀원이 다시 등록해야 한다.** 커넥터 주소에 서버 주소가 박히기 때문.
  처음부터 고정 도메인으로 시작한다.
- **DisHost 무료는 7일마다 대시보드에서 연장 버튼**을 눌러야 한다. 안 누르면 7일 뒤 삭제.
