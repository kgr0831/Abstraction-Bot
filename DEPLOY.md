# 배포 — Oracle Always Free + DuckDNS

무료로 24시간 돌리고, 팀원이 들어올 고정 HTTPS 주소를 얻는다.

```
[인터넷] ──443──▶ caddy (인증서 자동 발급·갱신)
                    │
                    ▼
                  app  ── run.py: 디스코드 봇 + 콘솔 + SQLite
                       ── claude / codex CLI (계정마다 격리된 홈)
```

## 먼저 알아야 할 것

**에이전트 CLI 가 서버에서 돈다.** API 키를 안 쓰기로 했으므로 `claude` 와
`codex` 바이너리가 서버에 있어야 하고, 팀원이 각자 콘솔에서 로그인하면
자기 계정 전용 홈이 서버에 생긴다. 자격증명은 그 홈 안에만 있고 서로 못 본다.

그래서 **메모리가 필요하다.** CLI 한 번 호출에 300~500MB 를 쓴다.
예전에 적어둔 128MB 봇 호스팅으로는 안 된다.

| | 필요 |
|---|---|
| 메모리 | 최소 2GB, 권장 6GB 이상 (동시에 묻는 사람 수만큼) |
| 디스크 | 10GB — 이미지 1.2GB + CLI 홈 + SQLite |
| 포트 | 80, 443 인바운드 |

## 1. 서버 만들기 (Oracle Cloud Always Free)

[cloud.oracle.com](https://cloud.oracle.com) 가입. 카드를 등록하지만 청구는 없다.
가입할 때 고른 **홈 리전은 나중에 못 바꾼다** — 한국이면 서울(ap-seoul-1)로.

Compute > Instances > **Create instance**

| 항목 | 값 |
|---|---|
| Image | **Ubuntu 22.04** |
| Shape | **Ampere VM.Standard.A1.Flex** — OCPU **2**, 메모리 **12GB** |
| SSH key | **Save private key** 눌러 받아둔다. 이거 없으면 못 들어간다 |

> **"Out of host capacity"** 가 뜨면 무료 ARM 재고가 없는 것이다. 리전을
> 바꾸거나(가용 도메인 AD-1/2/3 을 번갈아) 시간을 두고 다시 누르면 된다.
> 급하면 Shape 를 **VM.Standard.E2.1.Micro** 로 — 1GB 라 빠듯하지만 돌긴 한다.

만들어지면 **Public IP** 를 적어둔다.

### 포트 열기 — 두 군데 다 해야 한다

Oracle 은 여기서 대부분 막힌다. 방화벽이 **두 겹**이다.

**(1) VCN 보안 목록** — 인스턴스 > Virtual cloud network > Security Lists >
Default Security List > **Add Ingress Rules**

| Source CIDR | Protocol | Dest. Port |
|---|---|---|
| `0.0.0.0/0` | TCP | `80` |
| `0.0.0.0/0` | TCP | `443` |

**(2) 인스턴스 안 iptables** — SSH 로 들어가서:

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

## 2. 주소 만들기 (DuckDNS)

[duckdns.org](https://www.duckdns.org) — 구글/깃허브로 로그인.

1. 원하는 이름을 넣고 **add domain** → `내이름.duckdns.org`
2. `current ip` 에 1번의 Public IP 를 넣고 **update ip**
3. 페이지 위쪽 **token** 을 복사해둔다

이 주소는 **한 번 정하면 바꾸지 않는다.** 팀원의 커넥터 주소에 박히기 때문에
바꾸면 전원이 다시 등록해야 한다.

## 3. 올리기

SSH 로 들어가서:

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git
sudo usermod -aG docker $USER && newgrp docker
git clone https://github.com/kgr0831/Abstraction-Bot.git && cd Abstraction-Bot/deploy
cp .env.example .env
```

`.env` 를 채운다 (`nano .env`):

```ini
DISCORD_TOKEN=봇_토큰
DUCKDNS_SUB=내이름
DUCKDNS_TOKEN=덕디엔에스_토큰
SITE_DOMAIN=내이름.duckdns.org
```

띄운다. 첫 빌드는 Node 와 CLI 를 받느라 5~10분 걸린다.

```bash
docker compose up -d --build
```

## 4. 확인

```bash
docker compose logs -f app
```

`Abstraction Bot#... 접속` 이 뜨면 봇은 산 것이다. 그다음:

1. `https://내이름.duckdns.org` 가 인증서 경고 없이 열리는가
   (Caddy 가 인증서를 받는 데 처음 30초쯤 걸린다)
2. 디스코드에서 `/시작` → 온 링크를 누르면 콘솔이 열리는가
3. 콘솔 **설정 · 계정 연결** 에서 Claude 를 붙이면, 인증 링크가 뜨고
   그걸 눌러 로그인하면 계정이 잡히는가
4. **묻기** 탭에서 질문이 되는가

## 고칠 때

```bash
git pull && docker compose up -d --build
```

데이터는 `data` 볼륨에 있어서 다시 빌드해도 안 날아간다.

## 알아둘 것

- **유휴 회수.** Oracle 은 Always Free 인스턴스가 7일간 CPU·네트워크·메모리를
  모두 20% 미만으로 쓰면 회수 대상으로 본다. 봇이 딱 그 모양이라 위험하다.
  메모리를 12GB 가 아니라 **6GB 로 잡으면** 우리가 쓰는 비율이 올라가 안전해진다.
  (Always Free 를 하나만 쓰면 회수 안 한다는 보장은 없다 — 메일이 오면 무시 말 것)
- **SSH 키를 잃으면 서버에 못 들어간다.** 받은 `.key` 파일을 잘 둔다.
- **`.env` 는 깃에 안 올라간다.** 서버에서 직접 만든다.
- **인증서 볼륨(`caddy_data`)을 지우지 않는다.** 지우고 재시작을 반복하면
  Let's Encrypt 발급 한도(주당 5회)에 걸려 한동안 HTTPS 가 막힌다.
- **주소를 바꾸면 팀원 전원이 다시 등록한다.** 커넥터 주소에 박히기 때문.

## 다른 데 올릴 때

`Dockerfile` 만 있으면 어디든 된다. 조건은 세 개다.

- 상시 구동 (재우는 곳은 안 된다 — 게이트웨이가 끊겨 대화를 놓친다)
- `/app/data` 에 붙는 영구 볼륨 (없으면 재시작마다 전원 재로그인)
- 메모리 2GB 이상

| | |
|---|---|
| Render·Railway 무료 | 무활동에 재운다 → 수집이 끊긴다 |
| Workers·Vercel | 상시 프로세스가 없다 → discord.py 를 못 돌린다 |
| 봇 호스팅 128MB | CLI 를 못 깐다 → 묻기·결산이 통계 요약으로 주저앉는다 |
| Koyeb 무료 | 영구 디스크가 없다 → 재배포마다 전원 재로그인 |
