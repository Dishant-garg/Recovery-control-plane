"""Audit hash chain: tamper-evidence and the derived SQLite mirror."""

from __future__ import annotations

import json

from rcp.audit import GENESIS, AuditLog, read_all, sync_mirror, verify


def test_chain_verifies(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    for i in range(5):
        log.append("decision", {"event_id": f"evt_{i}", "outcome": "selected"}, ts=1000 + i)

    ok, msg = verify(log.path)
    assert ok, msg
    assert "5 records" in msg


def test_tampered_body_is_detected(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.append("decision", {"amount_paise": 100}, ts=1000)
    log.append("decision", {"amount_paise": 200}, ts=1001)

    records = read_all(path)
    records[0]["body"]["amount_paise"] = 999_999
    path.write_text("\n".join(json.dumps(r, sort_keys=True, separators=(",", ":"))
                              for r in records) + "\n")

    ok, msg = verify(path)
    assert not ok
    assert "seq 0" in msg


def test_deleted_record_breaks_the_chain(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    for i in range(4):
        log.append("action", {"i": i}, ts=1000 + i)

    records = read_all(path)
    del records[1]
    path.write_text("\n".join(json.dumps(r, sort_keys=True, separators=(",", ":"))
                              for r in records) + "\n")

    ok, msg = verify(path)
    assert not ok


def test_append_resumes_across_process_restarts(tmp_path):
    path = tmp_path / "audit.jsonl"
    AuditLog(path).append("a", {"n": 1}, ts=1)
    AuditLog(path).append("b", {"n": 2}, ts=2)   # fresh object, same file
    AuditLog(path).append("c", {"n": 3}, ts=3)

    ok, msg = verify(path)
    assert ok, msg
    assert [r["seq"] for r in read_all(path)] == [0, 1, 2]


def test_mirror_is_derived_and_idempotent(conn, tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    for i in range(3):
        log.append("decision", {"i": i}, ts=1000 + i, ref_id=f"dec_{i}")

    assert sync_mirror(conn, path) == 3
    assert sync_mirror(conn, path) == 0, "re-syncing must not duplicate rows"

    log.append("decision", {"i": 3}, ts=1003, ref_id="dec_3")
    assert sync_mirror(conn, path) == 1

    rows = conn.execute("SELECT seq, kind, ref_id FROM audit_mirror ORDER BY seq").fetchall()
    assert [r["seq"] for r in rows] == [0, 1, 2, 3]
    assert rows[3]["ref_id"] == "dec_3"


def test_mirror_is_append_only(conn, tmp_path):
    path = tmp_path / "audit.jsonl"
    AuditLog(path).append("decision", {"i": 0}, ts=1000)
    sync_mirror(conn, path)

    try:
        conn.execute("UPDATE audit_mirror SET kind = 'forged'")
    except Exception as exc:
        assert "append-only" in str(exc)
    else:
        raise AssertionError("audit_mirror must not be mutable")


def test_reopening_a_log_continues_the_chain(tmp_path):
    """The default. A restarted process must not renumber from zero."""
    path = tmp_path / "audit.jsonl"
    AuditLog(path).append("decision", {"i": 0}, ts=1000)
    AuditLog(path).append("decision", {"i": 1}, ts=1001)

    records = read_all(path)
    assert [r["seq"] for r in records] == [0, 1]
    assert verify(path)[0]


def test_reset_truncates_and_restarts_the_chain(tmp_path):
    """Only legitimate when the database the log describes is also replaced.

    `eval/run.py` forks a fresh per-arm database every run. Without this the
    log accumulated records from every previous run -- verifying cleanly the
    whole time while describing databases that no longer existed. Measured
    before the fix: 136,644 audit records against 1,049 rows in `decisions`.
    """
    path = tmp_path / "audit.jsonl"
    for i in range(5):
        AuditLog(path).append("decision", {"i": i}, ts=1000 + i)
    assert len(read_all(path)) == 5

    AuditLog(path, reset=True).append("decision", {"i": 0}, ts=2000)

    records = read_all(path)
    assert len(records) == 1, "reset must discard the previous run entirely"
    assert records[0]["seq"] == 0
    assert records[0]["prev_hash"] == GENESIS
    assert verify(path)[0]


def test_reset_on_a_missing_file_is_not_an_error(tmp_path):
    log = AuditLog(tmp_path / "fresh.jsonl", reset=True)
    log.append("decision", {"i": 0}, ts=1000)
    assert len(read_all(log.path)) == 1
