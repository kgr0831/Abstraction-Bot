"""mcp_server.py 자체 점검 — KST 변환·대화록 서식·툴 출력."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace

os.environ["COLLECTOR_DB"] = "data/_test_mcp.db"
os.environ["COLLECTOR_ACCOUNTS"] = "data/_test_accounts.json"
for f in ("data/_test_mcp.db", "data/_test_mcp.db-wal", "data/_test_mcp.db-shm",
          "data/_test_accounts.json"):
    if os.path.exists(f):
        os.remove(f)

import db  # noqa: E402
import mcp_server as m  # noqa: E402

UTC = timezone.utc


def msg(mid, content, dt, author="가람", thread=None, reply=None):
    ch = SimpleNamespace(id="c1", name="아이디어", type=SimpleNamespace(name="text"), parent=None)
    if thread:
        ch = SimpleNamespace(
            id=thread,
            name="스레드",
            type=SimpleNamespace(name="public_thread"),
            parent=SimpleNamespace(id="c1", name="아이디어"),
        )
    return SimpleNamespace(
        id=mid,
        guild=SimpleNamespace(id="g1"),
        channel=ch,
        author=SimpleNamespace(id="u1", display_name=author),
        content=content,
        created_at=dt,
        edited_at=None,
        reference=SimpleNamespace(message_id=reply) if reply else None,
        attachments=[],
    )


def main():
    conn = db.connect()

    # 게이트: 등록된 계정이 없으면 조회 자체가 막힌다
    assert "등록된 CLI 계정이 없습니다" in m.list_channels()

    # 계정만 있고 디스코드 연동이 없으면 여전히 막힌다
    db.ACCOUNTS_PATH.write_text(
        json.dumps([{"agent": "claude", "identity": "me@x.com", "home": "H"}]), "utf-8")
    assert "연동되지 않았습니다" in m.list_channels()

    # 페어링 코드를 소진하면 통과
    code, _ = db.new_pair_code(conn, "d1", "가람")
    got = db.redeem_pair_code(conn, code, "claude", "me@x.com")
    assert got[:2] == ("d1", "가람") and len(got[2]) > 20, got
    assert db.redeem_pair_code(conn, code, "claude", "me@x.com") is None, "코드가 재사용됨"
    assert "수집 중인 채널이 없습니다" in m.list_channels()
    db.add_channel(conn, "c1", "g1", "아이디어")
    # 8/27 06:11 UTC == 8/27 15:11 KST
    t = datetime(2026, 8, 27, 6, 11, tzinfo=UTC)
    db.upsert_message(conn, msg("1", "봇 넣으면 ㄱㄴ해요", t))
    db.upsert_message(conn, msg("2", "ㄹㅇ?", t, author="재림", reply="1"))
    db.upsert_message(conn, msg("3", "스레드에서 이어감", t, thread="t9"))

    out = m.get_conversation("아이디어", "2026-08-27")
    assert "15:11" in out, f"KST 변환 실패:\n{out}"
    assert "⟨1⟩" in out and "[→1]" in out, f"인용/답장 표시 실패:\n{out}"
    assert "[스레드1]" in out, f"스레드 표시 실패:\n{out}"
    assert "channels/g1/c1/" in out, f"링크 틀 실패:\n{out}"
    # 스레드 메시지가 부모 채널 하루치에 같이 잡혀야 한다
    assert out.count("⟨") == 3, out

    # 날짜 범위
    t2 = datetime(2026, 8, 29, 6, 0, tzinfo=UTC)          # 8/29 KST
    db.upsert_message(conn, msg("4", "이틀 뒤 메시지", t2))
    assert m.get_conversation("아이디어", "2026-08-27").count("⟨") == 3
    wide = m.get_conversation("아이디어", "2026-08-27", "2026-08-29")
    assert wide.count("⟨") == 4, wide
    assert "2026-08-27~2026-08-29" in wide
    assert "없습니다" in m.get_conversation("아이디어", "2026-08-30", "2026-08-31")
    assert "형식이 잘못" in m.get_conversation("아이디어", "8/27")
    assert "앞섭니다" in m.get_conversation("아이디어", "2026-08-29", "2026-08-27")

    assert "아이디어" in m.list_channels()
    assert "⟨1⟩" in m.search_messages("ㄱㄴ")
    assert "없음" in m.search_messages("존재하지않는말")
    assert len(m.recent_days("c1", days=3).splitlines()) == 3

    conn.close()
    for f in ("data/_test_mcp.db", "data/_test_mcp.db-wal", "data/_test_mcp.db-shm",
              "data/_test_accounts.json"):
        if os.path.exists(f):
            os.remove(f)
    print("test_mcp: 통과")


if __name__ == "__main__":
    main()
