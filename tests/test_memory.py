from jev_runner.memory import MemoryStore, scrub_pii


def store(tmp_path):
    return MemoryStore(tmp_path / "memory.db")


def test_scrub_pii_strips_digit_runs_and_masks_keeps_words():
    assert scrub_pii("transfer to CR 5101 0200 0094 9234 0743") == "transfer to CR #### #### #### #### ####"
    assert scrub_pii("account ••••0743 confirmed") == "account ••• confirmed"
    assert scrub_pii("avoid the Quiero sheet") == "avoid the Quiero sheet"
    assert scrub_pii("") == "" and scrub_pii(None) == ""


def test_add_and_query_lesson_by_keyword(tmp_path):
    s = store(tmp_path)
    s.add_lesson(target="android", app="net.bac.sbe.android",
                 goal="pay the farm planilla", body="use SINPE Movil, avoid the Quiero sheet")
    s.add_lesson(target="android", app="com.whatsapp",
                 goal="forward an invoice", body="long-press the message then Forward")
    hits = s.query_lessons(target="android", app="net.bac.sbe.android", goal="pay planilla")
    assert len(hits) == 1 and "SINPE" in hits[0]["body"]


def test_query_scoped_by_app(tmp_path):
    s = store(tmp_path)
    s.add_lesson(target="android", app="com.whatsapp", goal="forward", body="Forward trick")
    s.add_lesson(target="android", app="net.bac.sbe.android", goal="forward", body="bank forward")
    hits = s.query_lessons(target="android", app="com.whatsapp", goal="forward")
    assert [h["body"] for h in hits] == ["Forward trick"]


def test_query_falls_back_to_recent_when_no_match(tmp_path):
    s = store(tmp_path)
    s.add_lesson(target="chrome", goal="a", body="first")
    s.add_lesson(target="chrome", goal="b", body="second")
    hits = s.query_lessons(target="chrome", goal="zzzzz nothing matches", k=5)
    assert [h["body"] for h in hits] == ["second", "first"]  # newest first


def test_ingest_scrubs_pii_before_storing(tmp_path):
    s = store(tmp_path)
    s.add_lesson(target="android", goal="pay 88244852", body="sent to phone 88244852")
    hit = s.query_lessons(target="android", goal="pay", k=1)[0]
    assert "88244852" not in hit["body"] and "88244852" not in hit["goal_text"]
    assert "####" in hit["body"]


def test_record_run_and_roundtrip(tmp_path):
    s = store(tmp_path)
    s.record_run(target="android", app="net.bac.sbe.android", goal="pay 148000 to 88244852",
                 status="done", verified=True, steps=7, duration_ms=5200, loop_events=0)
    row = s._conn.execute("SELECT * FROM runs").fetchone()
    assert row["status"] == "done" and row["verified"] == 1 and row["steps"] == 7
    assert "148000" not in row["goal"] and "88244852" not in row["goal"]  # scrubbed


def test_match_query_is_fts_safe(tmp_path):
    # punctuation / FTS operators in the goal must not raise
    s = store(tmp_path)
    s.add_lesson(target="chrome", goal="x", body="safe")
    hits = s.query_lessons(target="chrome", goal='pay "OR" AND (NEAR) * : ^ -', k=3)
    assert isinstance(hits, list)  # did not raise
