FROM python:3.11-slim

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

# SQLite 와 등록 정보. 볼륨으로 붙이지 않으면 재시작 때 날아간다.
VOLUME ["/app/data"]
EXPOSE 8787

CMD ["python", "run.py"]
