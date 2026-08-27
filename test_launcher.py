"""launcher.py 자체 점검.

핵심은 4번 — 격리 홈이 진짜로 격리되는지. 이게 깨지면 "새 계정 추가"가
전부 기존 로그인을 그대로 보고하게 된다.
실제 로그인은 부작용이라 건드리지 않는다 (읽기 전용 status 만 호출).
"""

import json
import tempfile
from pathlib import Path

from starlette.testclient import TestClient

import launcher


def main():
    c = TestClient(launcher.app)

    # 1. 페이지가 뜨고 에이전트 목록이 주입된다
    html = c.get("/").text
    assert "__AGENTS__" not in html, "에이전트 목록이 주입되지 않음"
    assert "Claude" in html and "Codex" in html, html[:200]

    # 2. 등록부 읽기
    accts = c.get("/api/accounts").json()
    assert isinstance(accts, list)

    # 3. 없는 에이전트는 404
    assert c.post("/api/link/nope").status_code == 404

    # 4. 격리 홈에는 기존 로그인이 새지 않는다
    with tempfile.TemporaryDirectory() as tmp:
        who = launcher.identity("claude", Path(tmp))
        assert who is None, f"격리 실패 — 빈 홈에서 계정이 보임: {who}"

    # 5. 저장/불러오기 왕복
    orig = launcher.load_accounts()
    try:
        launcher.save_accounts([{"agent": "claude", "identity": "a@b.c", "home": "X"}])
        assert c.get("/api/accounts").json()[0]["identity"] == "a@b.c"
        # 6. 연결 해제는 AGENTS_DIR 밖 경로를 지우지 않는다
        with tempfile.TemporaryDirectory() as outside:
            probe = Path(outside) / "keep.txt"
            probe.write_text("x", encoding="utf-8")
            c.post("/api/unlink", json={"home": str(Path(outside))})
            assert probe.exists(), "AGENTS_DIR 밖 경로를 지움 — 경로 가드 실패"
        # 등록부에 없는 경로를 지워도 기존 항목은 남아 있어야 한다
        assert c.get("/api/accounts").json()[0]["identity"] == "a@b.c"
        c.post("/api/unlink", json={"home": "X"})
        assert c.get("/api/accounts").json() == []
    finally:
        launcher.save_accounts(orig)

    print("test_launcher: 통과")


if __name__ == "__main__":
    main()
