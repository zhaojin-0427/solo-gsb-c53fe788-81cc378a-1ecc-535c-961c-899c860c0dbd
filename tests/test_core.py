"""Adjudication semantics tests, each running against a fresh SQLite file."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import core, db  # noqa: E402


def fresh_conn():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return db.init_db(path), path


def task(tid, deps=(), def_v=1, src_v=1, prev_def_v=None, prev_src_v=None,
         artifact=None, dynamic=False, read_digests=None):
    """Build an import item; deps are upstream ids whose artifacts are 'art:<id>'."""
    prev_def_v = def_v if prev_def_v is None else prev_def_v
    prev_src_v = src_v if prev_src_v is None else prev_src_v
    read_digests = read_digests or {}
    dep_list = [{"upstream": u, "read_digest": read_digests.get(u, f"art:{u}")}
                for u in deps]
    return {
        "id": tid,
        "definition": f"def:{tid}:v{def_v}",
        "prev_definition": f"def:{tid}:v{prev_def_v}",
        "source_digest": f"src:{tid}:v{src_v}",
        "prev_source_digest": f"src:{tid}:v{prev_src_v}",
        "artifact_digest": artifact or f"art:{tid}",
        "static_deps": [] if dynamic else dep_list,
        "dynamic_deps": dep_list if dynamic else [],
    }


def states(conn):
    return {r["task_id"]: r["state"]
            for r in conn.execute("SELECT task_id, state FROM tasks")}


def test_import_all_matching_is_reusable():
    conn, _ = fresh_conn()
    core.import_graph(conn, [task("a"), task("b", ["a"]), task("c", ["b"])])
    assert states(conn) == {"a": "reusable", "b": "reusable", "c": "reusable"}


def test_changed_source_makes_executable_downstream_waits():
    conn, _ = fresh_conn()
    core.import_graph(conn, [
        task("a", src_v=2, prev_src_v=1),   # sources changed
        task("b", ["a"]),
        task("c", ["b"]),
    ])
    s = states(conn)
    assert s["a"] == "executable"
    assert s["b"] == "waiting"
    assert s["c"] == "waiting"
    row = conn.execute("SELECT reason, depth FROM tasks WHERE task_id='a'").fetchone()
    assert row["reason"] == "source_changed" and row["depth"] == 0


def test_upstream_rerun_same_artifact_keeps_downstream_reusable():
    conn, _ = fresh_conn()
    core.import_graph(conn, [
        task("a", src_v=2, prev_src_v=1), task("b", ["a"]), task("c", ["b"]),
    ])
    claim, err = core.claim(conn, "a")
    assert err is None
    status, _ = core.report_result(conn, "a", claim["input_fingerprint"],
                                   reads=[], artifact_digest="art:a")  # unchanged
    assert status == "accepted"
    s = states(conn)
    assert s == {"a": "reusable", "b": "reusable", "c": "reusable"}


def test_upstream_artifact_change_invalidates_downstream():
    conn, _ = fresh_conn()
    core.import_graph(conn, [
        task("a", src_v=2, prev_src_v=1), task("b", ["a"]), task("c", ["b"]),
    ])
    claim, _ = core.claim(conn, "a")
    status, _ = core.report_result(conn, "a", claim["input_fingerprint"],
                                   reads=[], artifact_digest="art:a:NEW")
    assert status == "accepted"
    s = states(conn)
    assert s["a"] == "reusable"
    assert s["b"] == "executable"   # read digest no longer matches
    assert s["c"] == "waiting"      # b not settled
    row = conn.execute(
        "SELECT reason, reason_task_id, depth FROM tasks WHERE task_id='b'").fetchone()
    assert row["reason"] == "upstream_changed"
    assert row["reason_task_id"] == "a"
    # chain c -> b -> a
    chain = core.reason_chain(conn, "c")
    assert [n["task_id"] for n in chain] == ["c", "b", "a"]


def test_completed_task_rereport_rejected_even_with_old_fingerprint():
    """The first report flips the task reusable without changing its
    fingerprint, so a duplicate report carrying that same fingerprint used to
    be accepted again -- potentially with a different artifact, silently
    rewriting the downstream plan.  It must now be refused as stale."""
    conn, _ = fresh_conn()
    core.import_graph(conn, [
        task("a", src_v=2, prev_src_v=1), task("b", ["a"]),
    ])
    claim_a, _ = core.claim(conn, "a")
    status, _ = core.report_result(conn, "a", claim_a["input_fingerprint"],
                                   [], "art:a:X")
    assert status == "accepted"
    before_seq = conn.execute("SELECT MAX(seq) FROM changes").fetchone()[0]
    before_b = conn.execute("SELECT * FROM tasks WHERE task_id='b'").fetchone()

    # re-report with the still-valid claim fingerprint but another artifact
    status, detail = core.report_result(conn, "a", claim_a["input_fingerprint"],
                                        [], "art:a:Y")
    assert status == "stale"
    assert detail["reason"] == "already_reported"

    # task, downstream plan and change seq are all untouched
    assert conn.execute(
        "SELECT artifact_digest FROM tasks WHERE task_id='a'").fetchone()[0] \
        == "art:a:X"
    after_b = conn.execute("SELECT * FROM tasks WHERE task_id='b'").fetchone()
    assert dict(before_b) == dict(after_b)
    assert conn.execute("SELECT MAX(seq) FROM changes").fetchone()[0] \
        == before_seq


def test_stale_report_rejected_without_state_change():
    conn, _ = fresh_conn()
    core.import_graph(conn, [
        task("a", src_v=2, prev_src_v=1), task("b", ["a"], src_v=2, prev_src_v=1),
    ])
    # a completes with a new artifact -> b unblocks (own sources also changed)
    claim_a, _ = core.claim(conn, "a")
    core.report_result(conn, "a", claim_a["input_fingerprint"], [], "art:a:X")
    claim_b, err = core.claim(conn, "b")
    assert err is None                      # b's own sources changed: executable
    # a's artifact moves on again without a new claim (e.g. a newer upstream
    # build): the fingerprint handed to b is now stale
    conn.execute("UPDATE tasks SET artifact_digest='art:a:Y' WHERE task_id='a'")
    core._propagate(conn, core._ChangeLog(conn), ["a"])
    before = conn.execute("SELECT * FROM tasks WHERE task_id='b'").fetchone()
    before_seq = conn.execute("SELECT MAX(seq) FROM changes").fetchone()[0]
    status, detail = core.report_result(conn, "b", claim_b["input_fingerprint"],
                                        [{"upstream": "a", "digest": "art:a:X"}],
                                        "art:b:NEW")
    assert status == "stale"
    assert detail["reason"] == "fingerprint_mismatch"
    after = conn.execute("SELECT * FROM tasks WHERE task_id='b'").fetchone()
    assert dict(before) == dict(after)      # no state change at all
    assert conn.execute("SELECT MAX(seq) FROM changes").fetchone()[0] \
        == before_seq                        # no change rows either


def test_wrong_read_digest_rejected_without_side_effects():
    """reads are validated against the upstream's *current* artifact digest;
    merely naming an existing upstream is not enough."""
    conn, _ = fresh_conn()
    core.import_graph(conn, [
        task("x", src_v=2, prev_src_v=1),
        task("a", ["x"], dynamic=True, src_v=2, prev_src_v=1),
    ])
    # x reruns and produces a different artifact -> a is still waiting
    claim_x, _ = core.claim(conn, "x")
    core.report_result(conn, "x", claim_x["input_fingerprint"], [], "art:x:v2")
    # a becomes executable; a stale worker still believes x's old digest
    claim_a, _ = core.claim(conn, "a")
    before_seq = conn.execute("SELECT MAX(seq) FROM changes").fetchone()[0]
    status, detail = core.report_result(
        conn, "a", claim_a["input_fingerprint"],
        [{"upstream": "x", "digest": "art:x"}], "art:a:NEW")
    assert status == "stale"
    assert detail["reason"] == "read_digest_mismatch"
    assert detail["upstreams"] == ["x"]
    # nothing was written: the stale dynamic row survives unchanged, the task
    # stays executable and no new change row was appended
    dep = conn.execute(
        "SELECT kind, read_digest FROM deps WHERE task_id='a'").fetchone()
    assert dep["kind"] == "dynamic" and dep["read_digest"] == "art:x"
    assert states(conn)["a"] == "executable"
    assert conn.execute("SELECT MAX(seq) FROM changes").fetchone()[0] \
        == before_seq


def test_static_read_keeps_static_identity_and_refreshes_digest():
    """A static dependency that appears in this run's reads stays a static
    row; its historical read_digest is updated and no duplicate dynamic row is
    inserted for the same key."""
    conn, _ = fresh_conn()
    core.import_graph(conn, [
        task("x", src_v=2, prev_src_v=1),
        task("a", ["x"], src_v=2, prev_src_v=1,
             read_digests={"x": "art:x:OLD"}),
    ])
    claim_x, _ = core.claim(conn, "x")
    core.report_result(conn, "x", claim_x["input_fingerprint"], [], "art:x")
    # stored read digest was the stale "art:x:OLD" -> a executable
    assert states(conn)["a"] == "executable"
    claim_a, _ = core.claim(conn, "a")
    status, _ = core.report_result(
        conn, "a", claim_a["input_fingerprint"],
        [{"upstream": "x", "digest": "art:x"}], "art:a:NEW")
    assert status == "accepted"
    deps = conn.execute(
        "SELECT upstream_id, kind, read_digest FROM deps WHERE task_id='a'").fetchall()
    assert [(d["upstream_id"], d["kind"], d["read_digest"]) for d in deps] == \
        [("x", "static", "art:x")]
    assert states(conn)["a"] == "reusable"


def test_mixed_static_and_dynamic_reads_share_key_safely():
    """Static dep x plus newly discovered dynamic dep y reported together:
    x stays static (digest refreshed), y is the only dynamic row -- the old
    code attempted to insert x twice and failed with a primary-key error."""
    conn, _ = fresh_conn()
    core.import_graph(conn, [
        task("x", src_v=2, prev_src_v=1), task("y"),
        task("a", ["x"], src_v=2, prev_src_v=1,
             read_digests={"x": "art:x:OLD"}),
    ])
    claim_x, _ = core.claim(conn, "x")
    core.report_result(conn, "x", claim_x["input_fingerprint"], [], "art:x")
    claim_a, _ = core.claim(conn, "a")
    status, _ = core.report_result(
        conn, "a", claim_a["input_fingerprint"],
        [{"upstream": "x", "digest": "art:x"},
         {"upstream": "y", "digest": "art:y"}],
        "art:a:NEW")
    assert status == "accepted"
    deps = conn.execute(
        "SELECT upstream_id, kind, read_digest FROM deps WHERE task_id='a' "
        "ORDER BY upstream_id").fetchall()
    assert [(d["upstream_id"], d["kind"], d["read_digest"]) for d in deps] == \
        [("x", "static", "art:x"), ("y", "dynamic", "art:y")]


def test_cycle_report_rolls_back():
    conn, _ = fresh_conn()
    core.import_graph(conn, [
        task("a", src_v=2, prev_src_v=1), task("b", ["a"]),
    ])
    claim, _ = core.claim(conn, "a")
    # a reports it dynamically read b -> cycle a -> b -> a
    status, detail = core.report_result(
        conn, "a", claim["input_fingerprint"],
        [{"upstream": "b", "digest": "art:b"}], "art:a:NEW")
    assert status == "cycle"
    # everything rolled back
    assert conn.execute(
        "SELECT COUNT(*) FROM deps WHERE task_id='a' AND kind='dynamic'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT artifact_digest FROM tasks WHERE task_id='a'").fetchone()[0] == "art:a"
    assert states(conn)["a"] == "executable"


def test_missing_dependency_report_rolls_back():
    conn, _ = fresh_conn()
    core.import_graph(conn, [task("a", src_v=2, prev_src_v=1)])
    claim, _ = core.claim(conn, "a")
    status, detail = core.report_result(
        conn, "a", claim["input_fingerprint"],
        [{"upstream": "ghost", "digest": "x"}], "art:a:NEW")
    assert status == "missing_dependency"
    assert detail["missing"] == ["ghost"]
    assert states(conn)["a"] == "executable"
    assert conn.execute("SELECT COUNT(*) FROM deps").fetchone()[0] == 0


def test_dynamic_deps_replaced_by_actual_reads():
    conn, _ = fresh_conn()
    core.import_graph(conn, [
        task("x"), task("y"),
        task("a", ["x"], dynamic=True, src_v=2, prev_src_v=1),
    ])
    claim, _ = core.claim(conn, "a")
    status, _ = core.report_result(
        conn, "a", claim["input_fingerprint"],
        [{"upstream": "y", "digest": "art:y"}], "art:a:NEW")
    assert status == "accepted"
    deps = conn.execute(
        "SELECT upstream_id, kind, read_digest FROM deps WHERE task_id='a'").fetchall()
    assert [(d["upstream_id"], d["kind"], d["read_digest"]) for d in deps] == \
        [("y", "dynamic", "art:y")]
    # y's artifact later changes -> a is invalidated through its dynamic dep
    conn.execute("UPDATE tasks SET artifact_digest='art:y:2' WHERE task_id='y'")
    core._propagate(conn, core._ChangeLog(conn), ["y"])
    assert states(conn)["a"] == "executable"


def plan_counts(conn):
    return {state: n for state, n in core.plan_since(conn, 0)["counts"].items()
            if n}


def test_report_reading_unsettled_new_upstream_waits_not_reusable():
    """A legitimate report may dynamically read an upstream that is itself
    still executable.  The report is accepted, but the task must wait for
    that upstream instead of turning reusable -- and downstream tasks whose
    old inputs still match must not be released early."""
    conn, _ = fresh_conn()
    core.import_graph(conn, [
        task("x", src_v=2, prev_src_v=1),   # x: sources changed -> executable
        task("a", src_v=2, prev_src_v=1),   # a: sources changed -> executable
        task("b", ["a"]),                   # b: read a's current artifact
    ])
    assert states(conn) == {"x": "executable", "a": "executable",
                            "b": "waiting"}
    claim_a, _ = core.claim(conn, "a")
    # a dynamically reads x's *registered* artifact while x is not done yet;
    # a reruns but produces the same artifact digest, so b's inputs all match
    status, _ = core.report_result(
        conn, "a", claim_a["input_fingerprint"],
        [{"upstream": "x", "digest": "art:x"}], "art:a")
    assert status == "accepted"
    s = states(conn)
    assert s["a"] == "waiting"      # x unsettled: a's fresh reads are provisional
    assert s["b"] == "waiting"      # b must not be released early
    row = conn.execute(
        "SELECT reason, reason_task_id, depth FROM tasks WHERE task_id='a'"
    ).fetchone()
    assert row["reason"] == "upstream_waiting"
    assert row["reason_task_id"] == "x"
    assert row["depth"] == 1
    row = conn.execute(
        "SELECT reason, reason_task_id, depth FROM tasks WHERE task_id='b'"
    ).fetchone()
    assert row["reason"] == "upstream_waiting"
    assert row["reason_task_id"] == "a"
    assert row["depth"] == 2
    # counts and change log agree with the stored states
    assert plan_counts(conn) == {"executable": 1, "waiting": 2}
    # chain b -> a -> x (root cause: x's sources changed)
    chain = core.reason_chain(conn, "b")
    assert [n["task_id"] for n in chain] == ["b", "a", "x"]
    # x finishes with the SAME artifact digest -> a and b reusable again
    claim_x, _ = core.claim(conn, "x")
    status, _ = core.report_result(conn, "x", claim_x["input_fingerprint"],
                                   [], "art:x")
    assert status == "accepted"
    assert states(conn) == {"x": "reusable", "a": "reusable", "b": "reusable"}
    assert plan_counts(conn) == {"reusable": 3}


def test_unsettled_upstream_completes_with_different_artifact():
    """Same setup, but the new upstream finishes with a *different* artifact:
    the task becomes executable (its recorded read is stale) and the
    downstream keeps waiting."""
    conn, _ = fresh_conn()
    core.import_graph(conn, [
        task("x", src_v=2, prev_src_v=1),
        task("a", src_v=2, prev_src_v=1),
        task("b", ["a"]),
    ])
    claim_a, _ = core.claim(conn, "a")
    status, _ = core.report_result(
        conn, "a", claim_a["input_fingerprint"],
        [{"upstream": "x", "digest": "art:x"}], "art:a")
    assert status == "accepted"
    assert states(conn)["a"] == "waiting"
    claim_x, _ = core.claim(conn, "x")
    status, _ = core.report_result(conn, "x", claim_x["input_fingerprint"],
                                   [], "art:x:v2")   # different digest
    assert status == "accepted"
    s = states(conn)
    assert s["x"] == "reusable"
    assert s["a"] == "executable"   # recorded read art:x is now stale
    assert s["b"] == "waiting"      # a not settled
    row = conn.execute(
        "SELECT reason, reason_task_id, depth FROM tasks WHERE task_id='a'"
    ).fetchone()
    assert row["reason"] == "upstream_changed"
    assert row["reason_task_id"] == "x"
    assert row["depth"] == 1
    assert plan_counts(conn) == {"reusable": 1, "executable": 1, "waiting": 1}
    # a can be claimed again and completes against x's new artifact
    claim_a2, err = core.claim(conn, "a")
    assert err is None
    status, _ = core.report_result(
        conn, "a", claim_a2["input_fingerprint"],
        [{"upstream": "x", "digest": "art:x:v2"}], "art:a")
    assert status == "accepted"
    assert states(conn) == {"x": "reusable", "a": "reusable", "b": "reusable"}


def test_report_reading_waiting_upstream_propagates_depth():
    """The new dynamic upstream may itself be waiting on something else;
    the reason chain must point through it with the correct depth."""
    conn, _ = fresh_conn()
    core.import_graph(conn, [
        task("root", src_v=2, prev_src_v=1),     # executable (depth 0)
        task("mid", ["root"]),                   # waiting on root (depth 1)
        task("a", src_v=2, prev_src_v=1),        # executable
    ])
    claim_a, _ = core.claim(conn, "a")
    status, _ = core.report_result(
        conn, "a", claim_a["input_fingerprint"],
        [{"upstream": "mid", "digest": "art:mid"}], "art:a:NEW")
    assert status == "accepted"
    row = conn.execute(
        "SELECT state, reason, reason_task_id, depth FROM tasks "
        "WHERE task_id='a'").fetchone()
    assert row["state"] == "waiting"
    assert row["reason"] == "upstream_waiting"
    assert row["reason_task_id"] == "mid"
    assert row["depth"] == 2                     # through mid to root
    chain = core.reason_chain(conn, "a")
    assert [n["task_id"] for n in chain] == ["a", "mid", "root"]


def test_restart_consistency():
    conn, path = fresh_conn()
    core.import_graph(conn, [
        task("a", src_v=2, prev_src_v=1), task("b", ["a"]), task("c", ["b"]),
    ])
    claim, _ = core.claim(conn, "a")
    core.report_result(conn, "a", claim["input_fingerprint"], [], "art:a:NEW")
    plan_before = core.plan_since(conn, 0)
    conn.close()
    # reopen the same database file: verdicts must be identical
    conn2 = db.init_db(path)
    plan_after = core.plan_since(conn2, 0)
    assert plan_before == plan_after
    assert states(conn2) == {"a": "reusable", "b": "executable", "c": "waiting"}
    conn2.close()
    os.unlink(path)


def test_import_cycle_rejected():
    conn, _ = fresh_conn()
    a = task("a", ["b"])
    b = task("b", ["a"])
    try:
        core.import_graph(conn, [a, b])
        assert False, "expected ValueError"
    except ValueError as e:
        assert "cycle" in str(e)


def test_import_missing_dep_rejected():
    conn, _ = fresh_conn()
    try:
        core.import_graph(conn, [task("a", ["ghost"])])
        assert False, "expected ValueError"
    except ValueError as e:
        assert "missing" in str(e)


def test_plan_returns_only_changes_since_seq():
    conn, _ = fresh_conn()
    core.import_graph(conn, [
        task("a", src_v=2, prev_src_v=1), task("b", ["a"]),
    ])
    plan0 = core.plan_since(conn, 0)
    assert len(plan0["changes"]) == 2
    claim, _ = core.claim(conn, "a")
    core.report_result(conn, "a", claim["input_fingerprint"], [], "art:a:NEW")
    plan1 = core.plan_since(conn, plan0["seq"])
    changed_ids = {c["task_id"] for c in plan1["changes"]}
    assert changed_ids == {"a", "b"}
    assert plan1["counts"]["executable"] == 1
    plan2 = core.plan_since(conn, plan1["seq"])
    assert plan2["changes"] == []


def test_shortest_chain_prefers_shallower_upstream():
    conn, _ = fresh_conn()
    # deep chain: d1 changed -> m1 -> m2 -> t ; shallow: s1 changed -> t
    core.import_graph(conn, [
        task("d1", src_v=2, prev_src_v=1),
        task("m1", ["d1"]),
        task("m2", ["m1"]),
        task("s1", src_v=2, prev_src_v=1),
        task("t", ["m2", "s1"]),
    ])
    row = conn.execute(
        "SELECT reason_task_id, depth FROM tasks WHERE task_id='t'").fetchone()
    assert row["reason_task_id"] == "s1"   # depth 1 beats depth 3
    assert row["depth"] == 1
