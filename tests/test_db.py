"""db.py 자체 점검. `python test_db.py` 로 실행."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import db

UTC = timezone.utc


def fake_msg(mid, content, created_at, author_id="u1", author="가람", channel_id="c1"):
    return SimpleNamespace(
        id=mid,
        guild=SimpleNamespace(id="g1"),
        channel=SimpleNamespace(
            id=channel_id, name="아이디어", type=SimpleNamespace(name="text")
        ),
        author=SimpleNamespace(id=author_id, display_name=author),
        content=content,
        created_at=created_at,
        edited_at=None,
        reference=None,
        attachments=[],
    )


def main(tmp="data/_test.db"):
    import os

    if os.path.exists(tmp):
        os.remove(tmp)
    conn = db.connect(tmp)

    # KST 경계: 8/27 KST 00:00 == 8/26 15:00 UTC
    start, end = db.kst_day_range("2026-08-27")
    assert start.startswith("2026-08-26T15:00:00"), start
    assert end.startswith("2026-08-27T15:00:00"), end

    db.add_channel(conn, "c1", "g1", "아이디어")
    assert db.is_collected(conn, "c1")
    assert not db.is_collected(conn, "c2")

    # 8/27 KST 오후 3:11 == 8/27 06:11 UTC — 하루 안에 들어와야 한다
    inside = datetime(2026, 8, 27, 6, 11, tzinfo=UTC)
    # 8/27 KST 00:00 직전(=8/26 14:59 UTC)은 8/26 에 속해야 한다
    before = datetime(2026, 8, 26, 14, 59, tzinfo=UTC)
    db.upsert_message(conn, fake_msg("1", "봇 넣으면 ㄱㄴ해요", inside))
    db.upsert_message(conn, fake_msg("2", "어제 메시지", before))

    day = db.get_conversation(conn, "c1", "2026-08-27")
    assert [r["id"] for r in day] == ["1"], [r["id"] for r in day]
    assert [r["id"] for r in db.get_conversation(conn, "아이디어", "2026-08-26")] == ["2"]

    # 검색
    assert len(db.search_messages(conn, "ㄱㄴ")) == 1
    assert len(db.search_messages(conn, "없는말")) == 0
    assert len(db.search_messages(conn, "메시지", since="2026-08-27")) == 0

    # 삭제 플래그는 조회에서 빠지고, 재수집해도 살아나지 않는다
    db.mark_deleted(conn, "1")
    assert db.get_conversation(conn, "c1", "2026-08-27") == []
    db.upsert_message(conn, fake_msg("1", "봇 넣으면 ㄱㄴ해요", inside))
    assert db.get_conversation(conn, "c1", "2026-08-27") == [], "재수집이 삭제를 되살림"

    # 옵트아웃은 기존 메시지까지 가린다
    db.upsert_message(conn, fake_msg("3", "사업내용 하나 더", inside, author_id="u2"))
    assert len(db.get_conversation(conn, "c1", "2026-08-27")) == 1
    assert db.opt_out(conn, "u2") == 1
    assert db.get_conversation(conn, "c1", "2026-08-27") == []
    assert db.is_opted_out(conn, "u2")
    db.opt_in(conn, "u2")
    assert not db.is_opted_out(conn, "u2")
    # 옵트인해도 이미 세운 삭제 플래그는 유지 (되살리지 않는다)
    assert db.get_conversation(conn, "c1", "2026-08-27") == []

    ch = db.list_channels(conn)
    assert len(ch) == 1 and ch[0]["channel_name"] == "아이디어"
    assert db.remove_channel(conn, "c1") and not db.remove_channel(conn, "c1")

    conn.close()
    os.remove(tmp)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(tmp + suffix):
            os.remove(tmp + suffix)
    print("test_db: 통과")


if __name__ == "__main__":
    main()
