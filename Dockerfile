FROM python:3.11-slim

# 에이전트 CLI 가 서버에서 돌아야 한다. API 키를 안 쓰기로 했으므로
# claude / codex 바이너리가 없으면 묻기와 결산이 통계 요약으로 주저앉는다.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates git ripgrep \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g --no-fund --no-audit @anthropic-ai/claude-code @openai/codex \
 && npm cache clean --force \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY run.py ./

# 컨테이너는 앞단 프록시 뒤에 있다 — 루프백 무토큰 예외를 끈다
ENV PUBLIC_SERVER=1 \
    LAUNCHER_HOST=0.0.0.0 \
    LAUNCHER_PORT=8787 \
    PYTHONUNBUFFERED=1

# SQLite 와 등록 정보, 그리고 계정마다 격리된 CLI 홈.
# 볼륨으로 붙이지 않으면 재시작 때 팀원 전원이 다시 로그인해야 한다.
VOLUME ["/app/data"]
EXPOSE 8787

CMD ["python", "run.py"]
