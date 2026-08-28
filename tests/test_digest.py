"""digest.py 자체 점검. 실제 API 는 부르지 않는다 (키 없으면 통계로 강등되는지 확인)."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

os.environ.setdefault("COLLECTOR_DB", "data/_test_digest.db")
for _f in ("data/_test_digest.db", "data/_test_digest.db-wal", "data/_test_digest.db-shm"):
    if os.path.exists(_f):
        os.remove(_f)

import digest  # noqa: E402


def rows(n=6):
    who = ["김가람", "유재림", "김민준", "김가람", "유재림", "김가람"]
    return [{
        "id": str(100 + i), "author": who[i % len(who)],
        "content": "메시지 %d" % i,
        "at": "2026-08-27T1%d:0%d:00+09:00" % (i % 10, i % 10),
        "channel": "아이디어", "reply_to": None, "thread": None, "files": 0,
        "guild_id": "g1", "channel_id": "c1",
    } for i in range(n)]


def main():
    # 1. 연동된 CLI 가 없으면 통계 결산으로 강등된다 (실제 CLI 는 안 부른다)
    import db
    real_accounts = db.local_accounts
    db.local_accounts = lambda: []
    try:
        assert not digest.cli_ready()
        assert digest.summarize(rows(), "아이디어", "2026-08-27") is None, "CLI 없이 부름"
        text = digest.stats(rows(), "아이디어", "2026-08-27")
        assert "일일 결산" in text and "6건" in text, text
        assert "김가람 — 3건" in text, text
        assert "discord.com/channels/g1/c1/100" in text, text
        assert digest.stats([], "아이디어", "2026-08-27") is None
    finally:
        db.local_accounts = real_accounts

    # 2. 모델별 프로바이더
    conn = db.connect()
    try:
        assert digest.model() == digest.DEFAULT_MODEL
        assert digest.spec()["provider"] == "anthropic"
        db.set_setting(conn, "digest_model", "gpt-5.6-terra")
        assert digest.model() == "gpt-5.6-terra"
        assert digest.spec()["provider"] == "openai"
        # 모르는 모델은 anthropic 으로 떨어뜨려 최소한 죽지는 않게
        assert digest.spec("없는모델")["provider"] == "anthropic"
        # 고른 모델의 제공자에 맞는 CLI 를 고른다
        real = db.local_accounts
        db.local_accounts = lambda: [
            {"agent": "claude", "identity": "a", "home": "A"},
            {"agent": "codex", "identity": "b", "home": "B"},
        ]
        try:
            assert digest.linked_cli()["agent"] == "codex", "terra 인데 claude 를 고름"
            db.set_setting(conn, "digest_model", "claude-opus-5")
            assert digest.linked_cli()["agent"] == "claude"
        finally:
            db.local_accounts = real
        db.set_setting(conn, "digest_model", None)
    finally:
        conn.close()

    # 2b. CLI 호출 — API 키를 쓰지 않고 연동된 계정 홈으로 부른다
    import subprocess as _sp

    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["env"] = kw.get("env") or {}
        seen["cwd"] = kw.get("cwd")
        seen["stdin"] = kw.get("input")
        seen["shell"] = kw.get("shell")
        return _sp.CompletedProcess(cmd, 0, "요약본", "")

    real_run, real_accounts = _sp.run, db.local_accounts
    _sp.run = fake_run
    db.local_accounts = lambda: [{"agent": "claude", "identity": "me@x.com", "home": "HOME"}]
    try:
        assert digest.cli_ready()
        assert digest._cli("시스템", "본문") == "요약본"
        cmd = seen["cmd"]
        assert cmd[0].lower().endswith(("claude", "claude.exe")), cmd[0]
        assert "-p" in cmd, cmd
        assert "--append-system-prompt" in cmd and "시스템" in cmd
        # 본문은 stdin 으로 — 인자로 주면 Windows 32KB 한계와 줄바꿈에 걸린다
        assert seen["stdin"] == "본문", seen["stdin"]
        assert "본문" not in cmd, "본문이 명령행 인자로 들어감"
        assert not seen.get("shell"), "shell 을 씀 — 인용 사고 위험"
        assert seen["env"]["CLAUDE_CONFIG_DIR"] == "HOME", "계정 홈이 안 물림"
        # 수집된 메시지는 신뢰할 수 없는 입력이라 도구가 막혀 있어야 한다
        assert "--disallowed-tools" in cmd
        for t in ("Read", "Write", "Bash", "WebFetch"):
            assert t in cmd, t
        assert seen["cwd"] and "abst-cli-" in str(seen["cwd"]), "빈 임시 폴더에서 안 돌림"
        # API 키는 어디에도 안 쓴다
        assert not hasattr(digest, "_anthropic") and not hasattr(digest, "_post")

        # 실패는 조용히 삼키지 않고 올린다
        _sp.run = lambda cmd, **kw: _sp.CompletedProcess(cmd, 1, "", "boom")
        try:
            digest._cli("s", "p")
            raise AssertionError("실패를 안 잡음")
        except RuntimeError as e:
            assert "boom" in str(e)

        # CLI 가 없으면 묻기는 안내를 돌려준다
        db.local_accounts = lambda: []
        assert not digest.cli_ready()
        text, err = digest.ask([{"role": "user", "content": "?"}], "자료")
        assert text is None and "연동된 CLI" in err
    finally:
        _sp.run, db.local_accounts = real_run, real_accounts

    # 3. 보존 기간 — 지난 것만 지우고, 0 이면 아무것도 안 지운다
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    conn = db.connect()
    try:
        db.add_channel(conn, "c1", "g1", "t")

        def m(i, days_ago):
            return SimpleNamespace(
                id=str(i), guild=SimpleNamespace(id="g1"),
                channel=SimpleNamespace(id="c1", name="t",
                                        type=SimpleNamespace(name="text"), parent=None),
                author=SimpleNamespace(id="u1", display_name="a"), content="x",
                created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
                edited_at=None, reference=None, attachments=[])

        for i, ago in enumerate([1, 30, 100, 200]):
            db.upsert_message(conn, m(i, ago))
        assert db.retention_days(conn) == db.DEFAULT_RETENTION_DAYS
        assert db.purge_old(conn) == 2, "90일 초과 2건이 안 지워짐"
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
        db.set_setting(conn, "retention_days", "0")
        assert db.purge_old(conn) == 0, "0(무제한)인데 지움"
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
        db.set_setting(conn, "retention_days", None)
    finally:
        conn.close()

    # 4. 디스코드 2000자 제한에 맞춰 줄 단위로 자른다
    long = "\n".join("줄 %d 짧은 내용" % i for i in range(400))
    parts = digest.chunks(long)
    assert len(parts) > 1
    assert all(len(p) <= 1900 for p in parts), [len(p) for p in parts]
    assert "\n".join(parts) == long, "자르면서 내용이 바뀜"
    assert digest.chunks("한 줄") == ["한 줄"]

    # 5. 어제 날짜는 KST 기준
    from datetime import datetime, timedelta
    assert digest.yesterday_kst() == (datetime.now(db.KST).date() - timedelta(days=1)).isoformat()

    for f in ("data/_test_digest.db", "data/_test_digest.db-wal", "data/_test_digest.db-shm"):
        if os.path.exists(f):
            os.remove(f)
    print("test_digest: 통과")


if __name__ == "__main__":
    main()
