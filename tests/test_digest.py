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
    # 1. 통계 결산은 키 없이도 나온다
    key = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        assert digest.summarize(rows(), "아이디어", "2026-08-27") is None, "키 없이 LLM 을 부름"
        text = digest.stats(rows(), "아이디어", "2026-08-27")
        assert "일일 결산" in text and "6건" in text, text
        assert "김가람 — 3건" in text, text
        assert "discord.com/channels/g1/c1/100" in text, text
        assert digest.stats([], "아이디어", "2026-08-27") is None
    finally:
        if key:
            os.environ["ANTHROPIC_API_KEY"] = key

    # 2. 모델별 프로바이더와 필요한 키가 맞물린다
    import db
    conn = db.connect()
    try:
        assert digest.model() == digest.DEFAULT_MODEL
        assert digest.spec()["provider"] == "anthropic"
        db.set_setting(conn, "digest_model", "gpt-5.6-terra")
        assert digest.model() == "gpt-5.6-terra"
        assert digest.spec()["provider"] == "openai"
        # 모르는 모델은 anthropic 으로 떨어뜨려 최소한 죽지는 않게
        assert digest.spec("없는모델")["provider"] == "anthropic"
        saved = {k: os.environ.pop(k, None) for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")}
        try:
            assert not digest.key_present("claude-opus-5")
            assert not digest.key_present("gpt-5.6-terra")
            os.environ["OPENAI_API_KEY"] = "x"
            assert digest.key_present("gpt-5.6-terra")
            assert not digest.key_present("claude-opus-5"), "프로바이더 키가 섞임"
        finally:
            os.environ.pop("OPENAI_API_KEY", None)
            for k, v in saved.items():
                if v:
                    os.environ[k] = v
        db.set_setting(conn, "digest_model", None)
    finally:
        conn.close()

    # 2b. 수제 HTTP 의 요청/응답 모양. 여기가 틀리면 결산이 조용히 통계로 강등된다.
    import io
    import json as _json
    import urllib.request

    sent = {}

    def fake_urlopen(req, timeout=None):
        sent["url"] = req.full_url
        sent["headers"] = {k.lower(): v for k, v in req.header_items()}
        sent["body"] = _json.loads(req.data.decode("utf-8"))
        return io.BytesIO(_json.dumps(sent["reply"]).encode("utf-8"))

    real = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        sent["reply"] = {"content": [{"type": "thinking", "thinking": "..."},
                                     {"type": "text", "text": "요약본"}],
                         "stop_reason": "end_turn"}
        os.environ["ANTHROPIC_API_KEY"] = "k"
        assert digest._anthropic("claude-opus-5", "대화") == "요약본"
        assert sent["url"].endswith("/v1/messages")
        assert sent["headers"]["x-api-key"] == "k"
        assert sent["headers"]["anthropic-version"] == "2023-06-01"
        assert sent["body"]["model"] == "claude-opus-5"
        assert sent["body"]["thinking"] == {"type": "adaptive"}
        assert "budget_tokens" not in _json.dumps(sent["body"]), "제거된 파라미터가 남음"
        assert sent["body"]["messages"][0]["content"].endswith("대화")

        # 거절은 조용히 넘기지 않는다
        sent["reply"] = {"content": [], "stop_reason": "refusal"}
        try:
            digest._anthropic("claude-opus-5", "x")
            raise AssertionError("거절을 못 잡음")
        except RuntimeError:
            pass

        sent["reply"] = {"output": [{"type": "message", "content": [
            {"type": "output_text", "text": "테라 요약"}]}]}
        os.environ["OPENAI_API_KEY"] = "k2"
        assert digest._openai("gpt-5.6-terra", "대화") == "테라 요약"
        assert sent["url"].endswith("/v1/responses")
        assert sent["headers"]["authorization"] == "Bearer k2"
        assert sent["body"]["reasoning"] == {"effort": "medium"}
    finally:
        urllib.request.urlopen = real
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ.pop("OPENAI_API_KEY", None)

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
