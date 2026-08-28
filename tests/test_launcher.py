"""launcher.py 자체 점검.

핵심은 격리(4)와 권한 게이트(9~11) — 둘 다 조용히 깨지는 종류라 테스트가 필요하다.
실제 로그인은 부작용이라 건드리지 않는다 (읽기 전용 status 만 호출).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import json
import os
import tempfile
from pathlib import Path

os.environ.setdefault("COLLECTOR_DB", "data/_test_launcher.db")
os.environ.setdefault("COLLECTOR_ACCOUNTS", "data/_test_launcher_accounts.json")
for _f in ("data/_test_launcher.db", "data/_test_launcher.db-wal",
           "data/_test_launcher.db-shm", "data/_test_launcher_accounts.json"):
    if os.path.exists(_f):
        os.remove(_f)

from starlette.testclient import TestClient  # noqa: E402

import db  # noqa: E402
import launcher  # noqa: E402


def main():
    # 루프백에서 온 요청으로 흉내낸다 (기본값 'testclient' 는 인증에 걸린다)
    c = TestClient(launcher.app, client=("127.0.0.1", 50000))
    far = TestClient(launcher.app, client=("203.0.113.9", 50000))

    # 1. 콘솔 페이지가 뜨고 자리표시자가 전부 치환된다
    html = c.get("/").text
    for ph in ("__AGENTS__", "__MCP__", "__SELFMCP__", "__CODE__"):
        assert ph not in html, f"{ph} 미치환"
    assert "Claude" in html and "리더보드" in html and "온디맨드" in html

    # 2. 코드가 URL 로 오면 페이지에 실린다
    assert '"AB12CD"' in c.get("/?code=AB12CD").text

    # 3. 없는 에이전트는 404
    assert c.post("/api/link/nope").status_code == 404

    # 4. 격리 홈에는 기존 로그인이 새지 않는다
    with tempfile.TemporaryDirectory() as tmp:
        who = launcher.identity("claude", Path(tmp))
        assert who is None, f"격리 실패 — 빈 홈에서 계정이 보임: {who}"

    # 5. 대화 API 입력 검증
    assert c.get("/api/conversation").status_code == 400
    assert c.get("/api/conversation?channel=c1&date=8/27").status_code == 400
    assert c.get("/api/conversation?channel=c1&date=2026-08-29&until=2026-08-27").status_code == 400
    assert c.get("/api/conversation?channel=c1&date=2026-08-27").json()["messages"] == []
    assert c.get("/api/search?q=").json()["messages"] == []
    assert len(c.get("/api/days?days=5").json()) == 5
    assert c.get("/api/channels").json() == []

    # 6. 리더보드
    lb = c.get("/api/leaderboard?days=7").json()
    assert "rows" in lb and isinstance(lb["rows"], list)

    # 7. 페어링 — 틀린 코드는 거부
    r = c.post("/api/pair", json={"code": "ZZZZZZ", "agent": "claude", "identity": "a@b.c"})
    assert r.status_code == 400 and not r.json()["ok"], r.text

    # 8. 페어링 — 발급한 코드는 1회만 통한다
    conn = db.connect()
    try:
        code, _ = db.new_pair_code(conn, "d9", "테스터", is_manager=False)
    finally:
        conn.close()
    r = c.post("/api/pair", json={"code": code, "agent": "claude", "identity": "a@b.c"})
    assert r.json().get("ok") and r.json()["user_name"] == "테스터", r.text
    tok = r.json()["token"]
    assert len(tok) > 20
    assert c.post("/api/pair", json={"code": code, "agent": "claude", "identity": "a@b.c"}
                  ).status_code == 400, "코드가 재사용됨"

    # 7b. 루프백 밖에서는 토큰이 있어야 통한다
    assert far.get("/api/channels").status_code == 401
    assert far.get("/api/channels", headers={"X-Abstraction-Token": tok}).status_code == 200
    assert far.get("/api/channels", headers={"X-Abstraction-Token": "wrong"}).status_code == 401

    # 9. 계정이 있어도 관리 권한이 없으면 수집 관리는 막힌다
    fake_home = str((Path("data") / "_test_home").resolve())
    launcher.save_accounts([{"agent": "claude", "identity": "a@b.c", "home": fake_home}])
    me = c.get("/api/me").json()
    assert me["linked"] and not me["is_manager"], me
    assert c.post("/api/collect", json={"channel_id": "1", "on": True}).status_code == 403
    assert c.post("/api/backfill", json={"channel_id": "1", "days": 7}).status_code == 403

    # 10. 관리 권한이 붙으면 통과해서 다음 관문(봇 미실행)까지 간다
    conn = db.connect()
    try:
        code, _ = db.new_pair_code(conn, "d9", "테스터", is_manager=True)
    finally:
        conn.close()
    assert c.post("/api/pair",
                  json={"code": code, "agent": "claude", "identity": "a@b.c"}).json()["ok"]
    assert c.get("/api/me").json()["is_manager"]
    assert c.post("/api/collect", json={"channel_id": "1", "on": True}).status_code == 503
    assert c.get("/api/guild-channels").status_code == 503

    # 11. 온디맨드 MCP 는 CLI 를 부르기 전에 입력을 거른다
    assert c.post("/api/mcp/add", json={"name": "bad name", "url": "https://a"}).status_code == 400
    assert c.post("/api/mcp/add", json={"name": "ok", "url": "ftp://a"}).status_code == 400
    assert c.post("/api/mcp/remove", json={"name": "../evil"}).status_code == 400

    # 11b. 없는 모델은 저장 전에 막는다 (결산이 400 으로 죽는 걸 방지)
    import bot  # noqa: F401 — bot_bridge 가 sys.modules 로 찾는다
    r = c.post("/api/digest", json={"hour": 9, "model": "gpt-없는거"})
    assert r.status_code == 400 and "모르는 모델" in r.json()["error"], r.text
    assert c.post("/api/digest", json={"hour": 99}).status_code == 400

    # 12. 콘솔 브리지는 스레드마다 새 커넥션을 연다
    #     (SQLite 커넥션은 스레드에 묶여서, 봇 루프의 conn 을 스레드풀에서 쓰면 터진다)
    import threading

    assert bot.bridge_conn() is not bot.bridge_conn()
    boom = []

    def worker():
        try:
            c = bot.bridge_conn()
            try:
                db.is_collected(c, "1")
                db.remove_channel(c, "1")
            finally:
                c.close()
        except Exception as e:  # noqa: BLE001
            boom.append(e)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert not boom, boom

    # 13. 연결 해제는 AGENTS_DIR 밖 경로를 지우지 않는다
    with tempfile.TemporaryDirectory() as outside:
        probe = Path(outside) / "keep.txt"
        probe.write_text("x", encoding="utf-8")
        c.post("/api/unlink", json={"home": str(Path(outside))})
        assert probe.exists(), "AGENTS_DIR 밖 경로를 지움 — 경로 가드 실패"

    # 14. 수집 거부 왕복
    assert c.get("/api/optout").json()["opted_out"] is False
    assert c.post("/api/optout", json={"out": True}).json()["opted_out"] is True
    assert c.get("/api/optout").json()["opted_out"] is True
    assert c.post("/api/optout", json={"out": False}).json()["opted_out"] is False

    import shutil
    if "bot" in sys.modules:            # bot 임포트가 연 커넥션을 닫아야 파일이 지워진다
        sys.modules["bot"].conn.close()
    shutil.rmtree("data/_test_home", ignore_errors=True)
    for f in ("data/_test_launcher.db", "data/_test_launcher.db-wal",
              "data/_test_launcher.db-shm", "data/_test_launcher_accounts.json"):
        if os.path.exists(f):
            os.remove(f)
    print("test_launcher: 통과")


if __name__ == "__main__":
    main()
