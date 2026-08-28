#!/bin/bash
# DisHost(Pterodactyl) 시작 스크립트.
#
# 컨테이너 안에서 cloudflared 와 앱을 같이 띄운다. 봇 호스팅은 HTTPS 도메인을
# 안 주지만 아웃바운드는 되므로, 터널이 밖에서 들어오는 길을 만든다.
#
# Pterodactyl 시작 명령에 이걸 넣는다:  bash deploy/start.sh
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

PORT="${LAUNCHER_PORT:-8787}"
export LAUNCHER_PORT="$PORT"
# cloudflared 가 같은 컨테이너에서 루프백으로 붙으므로 밖에는 안 연다.
export LAUNCHER_HOST=127.0.0.1
# 터널 뒤이므로 루프백 무토큰 예외를 반드시 끈다. 안 그러면 주소만 알면 다 통과한다.
export PUBLIC_SERVER=1

if [ -z "${DISCORD_TOKEN:-}" ]; then
  echo "[중단] DISCORD_TOKEN 환경변수가 없습니다. 패널의 Variables 에 넣으세요."
  exit 1
fi

# 1) 파이썬 의존성 — 처음 한 번만
if [ ! -f .deps_ok ]; then
  echo "[준비] 의존성 설치..."
  pip install --no-cache-dir -r requirements.txt && touch .deps_ok || exit 1
fi

# 2) cloudflared — 처음 한 번만
if [ ! -x ./cloudflared ]; then
  echo "[준비] cloudflared 내려받는 중..."
  ARCH="$(uname -m)"; CF_ARCH=amd64
  [ "$ARCH" = "aarch64" ] && CF_ARCH=arm64
  curl -fsSL -o cloudflared \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$CF_ARCH" \
    && chmod +x cloudflared || { echo "[중단] cloudflared 내려받기 실패"; exit 1; }
fi

# 3) 터널
if [ -n "${TUNNEL_TOKEN:-}" ]; then
  ./cloudflared tunnel --no-autoupdate run --token "$TUNNEL_TOKEN" &
else
  echo "[경고] TUNNEL_TOKEN 이 없어 임시 주소로 띄웁니다."
  echo "       재시작마다 주소가 바뀝니다 — 커넥터에는 쓰지 마세요."
  ./cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" &
fi
CF=$!

# 4) 앱
python3 run.py &
APP=$!

# 하나가 죽으면 둘 다 내린다. 반쪽만 살아 있으면 조용히 고장난 상태가 된다.
trap 'kill "$CF" "$APP" 2>/dev/null' EXIT INT TERM
wait -n
echo "[종료] 한쪽이 내려가 둘 다 종료합니다."
