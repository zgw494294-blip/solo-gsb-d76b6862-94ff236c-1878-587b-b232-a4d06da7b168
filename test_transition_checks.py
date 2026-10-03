"""End-to-end checks for the per-transition executability/blocking-reasons feature."""

import threading

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" :: {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def create(protocol):
    r = client.post("/v1/sessions", json=protocol)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def run(sid, tid, ver):
    return client.post(f"/v1/sessions/{sid}/transitions",
                       json={"transition_id": tid, "expected_version": ver})


def checks(sid, ver):
    return client.post(f"/v1/sessions/{sid}/transition-checks",
                       json={"expected_version": ver})


# Protocol built so several transitions fail multiple requirements at once.
#
# States: A 0->1->2(final), B 0->1->2(final).
#   s1: A send m   0->1   (enabled at the initial configuration)
#   s2: A send n   1->1   (needs A=1 and the free A->B slot)
#   r1: B receive m 1->1  (needs B=1 and m in A->B; wrong source at B=0)
#   r2: B receive m 0->1  (fires when A->B holds m)
#   x:  B send q   0->1   (B->A channel, independent)
P = {
    "A": {"states": [0, 1, 2], "initial": 0, "finals": [2]},
    "B": {"states": [0, 1, 2], "initial": 0, "finals": [2]},
    "transitions": [
        {"id": "s1", "from": "A", "to": "B", "action": "send", "message": "m",
         "source": 0, "target": 1},
        {"id": "s2", "from": "A", "to": "B", "action": "send", "message": "n",
         "source": 1, "target": 1},
        {"id": "r1", "from": "A", "to": "B", "action": "receive", "message": "m",
         "source": 1, "target": 1},
        {"id": "r2", "from": "A", "to": "B", "action": "receive", "message": "m",
         "source": 0, "target": 1},
        {"id": "x", "from": "B", "to": "A", "action": "send", "message": "q",
         "source": 0, "target": 1},
    ],
}

# 1. initial configuration: all ids ascending, session/version echoed
sid = create(P)
r = checks(sid, 0)
check("initial 200", r.status_code == 200, r.text)
b = r.json()
check("initial echo", b["session_id"] == sid and b["version"] == 0, b)
ids = [item["transition_id"] for item in b["transitions"]]
check("all ids ascending", ids == ["r1", "r2", "s1", "s2", "x"], ids)
by_id = {item["transition_id"]: item for item in b["transitions"]}

check("s1 enabled, no reasons",
      by_id["s1"]["enabled"] is True and by_id["s1"]["reasons"] == [], by_id["s1"])
check("x enabled (B->A send)",
      by_id["x"]["enabled"] is True and by_id["x"]["reasons"] == [], by_id["x"])
check("r2 enabled at B=0 empty queue? no",
      by_id["r2"]["enabled"] is False, by_id["r2"])

# r2 (receive on B, source 0): state matches, but A->B is empty -> only the
# empty-queue reason, and no message mismatch on top of it.
check("r2 empty receive queue only",
      by_id["r2"]["reasons"] == [{
          "error": "receive_queue_empty", "direction": "A_to_B",
          "actual": None, "required": "m"}], by_id["r2"]["reasons"])

# r1 (receive on B, source 1): B is at 0 AND A->B is empty -> both reasons.
r1_reasons = by_id["r1"]["reasons"]
check("r1 two reasons", len(r1_reasons) == 2, r1_reasons)
check("r1 state mismatch", r1_reasons[0] == {
    "error": "state_mismatch", "endpoint": "B",
    "actual": 0, "required": 1}, r1_reasons)
check("r1 empty queue (no duplicate mismatch)", r1_reasons[1] == {
    "error": "receive_queue_empty", "direction": "A_to_B",
    "actual": None, "required": "m"}, r1_reasons)

# s2 (send on A, source 1): A is at 0 AND A->B free -> state mismatch only.
check("s2 state mismatch only", by_id["s2"]["reasons"] == [{
    "error": "state_mismatch", "endpoint": "A",
    "actual": 0, "required": 1}], by_id["s2"]["reasons"])

# Enabled ids agree with the session snapshot.
snapshot = client.get(f"/v1/sessions/{sid}").json()
enabled_here = sorted(t for t in ids if by_id[t]["enabled"])
check("enabled matches snapshot",
      enabled_here == snapshot["enabled_transition_ids"],
      (enabled_here, snapshot["enabled_transition_ids"]))

# 2. fire s1 (A->B now holds "m", A=1): exercise occupancy and head mismatch
r = run(sid, "s1", 0)
assert r.status_code == 200, r.text
b = checks(sid, 1).json()
by_id = {item["transition_id"]: item for item in b["transitions"]}
check("echo version 1", b["version"] == 1, b)

# s2 (send on A): now A=1 (state ok) but A->B is occupied -> occupancy only,
# carrying the held message and the required empty slot.
check("s2 occupancy only", by_id["s2"]["reasons"] == [{
    "error": "send_queue_occupied", "direction": "A_to_B",
    "actual": "m", "required": None}], by_id["s2"]["reasons"])

# s1 (send on A, source 0): A moved to 1 AND A->B occupied -> both reasons.
check("s1 now has both blockers",
      [reason["error"] for reason in by_id["s1"]["reasons"]]
      == ["state_mismatch", "send_queue_occupied"], by_id["s1"]["reasons"])
check("s1 reasons values", by_id["s1"]["reasons"] == [
    {"error": "state_mismatch", "endpoint": "A", "actual": 1, "required": 0},
    {"error": "send_queue_occupied", "direction": "A_to_B",
     "actual": "m", "required": None}], by_id["s1"]["reasons"])

# r1 (receive on B, source 1): B=0 (state wrong) but A->B holds m (matches)
# -> state mismatch only; the matching message is not reported.
check("r1 state mismatch only with matching head", by_id["r1"]["reasons"] == [{
    "error": "state_mismatch", "endpoint": "B",
    "actual": 0, "required": 1}], by_id["r1"]["reasons"])

# r2 (receive on B, source 0): state matches, head is m -> enabled.
check("r2 enabled after send",
      by_id["r2"]["enabled"] is True and by_id["r2"]["reasons"] == [], by_id["r2"])

# x (B->A send) stays enabled: the occupied A->B slot is a different channel.
check("x still enabled on other channel",
      by_id["x"]["enabled"] is True and by_id["x"]["reasons"] == [], by_id["x"])

snapshot = client.get(f"/v1/sessions/{sid}").json()
enabled_here = sorted(t for t in ids if by_id[t]["enabled"])
check("enabled matches snapshot after send",
      enabled_here == snapshot["enabled_transition_ids"],
      (enabled_here, snapshot["enabled_transition_ids"]))

# 3. message mismatch: occupy B->A with "q", a B->A receive wanting "n"
M = {
    "A": {"states": [0, 1], "initial": 0, "finals": [1]},
    "B": {"states": [0, 1], "initial": 0, "finals": [1]},
    "transitions": [
        {"id": "sendQ", "from": "B", "to": "A", "action": "send", "message": "q",
         "source": 0, "target": 1},
        {"id": "recvN", "from": "B", "to": "A", "action": "receive", "message": "n",
         "source": 0, "target": 1},
        {"id": "recvQ", "from": "B", "to": "A", "action": "receive", "message": "q",
         "source": 0, "target": 1},
    ],
}
sm = create(M)
run(sm, "sendQ", 0)
b = checks(sm, 1).json()
mm = {item["transition_id"]: item for item in b["transitions"]}
# recvN executes on B, reads the B->A slot: B=0 ok, head is q != n -> mismatch.
check("message mismatch reported", mm["recvN"]["reasons"] == [{
    "error": "message_mismatch", "direction": "B_to_A",
    "actual": "q", "required": "n"}], mm["recvN"]["reasons"])
check("matching head enabled",
      mm["recvQ"]["enabled"] is True and mm["recvQ"]["reasons"] == [], mm["recvQ"])

# 4. completed configuration: all enabled=false and only "completed" reported
Q = {
    "A": {"states": [0, 1], "initial": 0, "finals": [1]},
    "B": {"states": [0, 1], "initial": 0, "finals": [1]},
    "transitions": [
        {"id": "t1", "from": "A", "to": "B", "action": "send", "message": "m",
         "source": 0, "target": 1},
        {"id": "t4", "from": "A", "to": "B", "action": "receive", "message": "m",
         "source": 0, "target": 1},
    ],
}
done = create(Q)
run(done, "t1", 0)
run(done, "t4", 1)
b = checks(done, 2).json()
check("completed echo", b["session_id"] == done and b["version"] == 2, b)
check("completed all disabled",
      all(item["enabled"] is False for item in b["transitions"]), b)
check("completed reports only session_completed",
      [item["reasons"] for item in b["transitions"]]
      == [[{"error": "session_completed"}], [{"error": "session_completed"}]],
      b["transitions"])
# No state/queue reasons leak into the completion report.
errors = {reason["error"] for item in b["transitions"] for reason in item["reasons"]}
check("completed reason set", errors == {"session_completed"}, errors)

# 5. protocol with no transitions -> empty list
empty = create({
    "A": {"states": [0], "initial": 0, "finals": [0]},
    "B": {"states": [0], "initial": 0, "finals": [0]},
    "transitions": [],
})
b = checks(empty, 0).json()
check("no transitions -> []", b["transitions"] == [], b)
check("no transitions still echo", b["session_id"] == empty and b["version"] == 0, b)

# 6. unknown session -> 404; stale/future version -> 409 with current_version
r = client.post("/v1/sessions/does-not-exist/transition-checks",
                json={"expected_version": 0})
check("unknown 404", r.status_code == 404, (r.status_code, r.text))
check("unknown body", r.json() == {"detail": "unknown session"}, r.json())
r = checks(sid, 0)
check("stale 409", r.status_code == 409, (r.status_code, r.text))
check("stale current_version",
      r.json() == {"detail": {"error": "version_conflict", "current_version": 1}},
      r.json())
r = checks(sid, 99)
check("future version 409", r.status_code == 409
      and r.json()["detail"]["current_version"] == 1, r.text)
r = client.post("/v1/sessions/does-not-exist/transition-checks",
                json={"expected_version": 99})
check("unknown beats stale 404", r.status_code == 404, r.status_code)

# 7. body validation -> 422
r = client.post(f"/v1/sessions/{sid}/transition-checks", json={})
check("missing version 422", r.status_code == 422, r.status_code)
r = client.post(f"/v1/sessions/{sid}/transition-checks",
                json={"expected_version": -1})
check("negative version 422", r.status_code == 422, r.status_code)
r = client.post(f"/v1/sessions/{sid}/transition-checks",
                json={"expected_version": 1.5})
check("non-integer version 422", r.status_code == 422, r.status_code)
r = client.post(f"/v1/sessions/{sid}/transition-checks",
                json={"expected_version": 1, "extra": 1})
check("extra field 422", r.status_code == 422, r.status_code)

# 8. read-only: checking never writes or consumes a version
before = client.get(f"/v1/sessions/{sid}").json()
checks(sid, 1)
checks(sid, 1)
after = client.get(f"/v1/sessions/{sid}").json()
check("no write", before["version"] == after["version"] == 1
      and before["trace"] == after["trace"], (before, after))

# 9. locked session can still be checked; the lock is never a reported blocker
r = client.put(f"/v1/sessions/{sid}/lock",
               json={"expected_version": 1, "locked": True})
assert r.status_code == 200, r.text
r = checks(sid, 2)
check("locked still checkable 200", r.status_code == 200, (r.status_code, r.text))
check("locked no lock reason",
      all(reason["error"] != "session_locked"
          for item in r.json()["transitions"] for reason in item["reasons"]),
      r.json())

# 10. protocol replacement: checks follow the new definitions
rp = create(P)
run(rp, "s1", 0)  # version 1, A->B holds "m", A=1
new_protocol = {
    **P,
    "transitions": [
        {**t, **({"message": "ZZ"} if t["id"] == "r2" else {})}
        for t in P["transitions"]
    ],
}
r = client.put(f"/v1/sessions/{rp}/protocol",
               json={"expected_version": 1, "protocol": new_protocol})
check("replace 200", r.status_code == 200, r.text)
b = checks(rp, 2).json()
rr2 = {item["transition_id"]: item for item in b["transitions"]}["r2"]
# r2 now requires "ZZ" but the slot still holds "m" -> message mismatch under
# the new definition, not enabled.
check("new definition applied",
      not rr2["enabled"] and rr2["reasons"] == [{
          "error": "message_mismatch", "direction": "A_to_B",
          "actual": "m", "required": "ZZ"}], rr2)
r = checks(rp, 1)
check("old version after replace 409", r.status_code == 409
      and r.json()["detail"]["current_version"] == 2, r.text)

# 11. concurrency: checks vs execution on one version give the whole
#     named-version report or 409, never a mixed/partial snapshot.
target = create(P)
run(target, "s1", 0)  # version 1: A=1, A->B holds "m"
results = {}
barrier = threading.Barrier(8)


def check_worker(tag):
    barrier.wait()
    rr = checks(target, 1)
    if rr.status_code == 200:
        items = {i["transition_id"]: i for i in rr.json()["transitions"]}
        fingerprint = {
            tid: (i["enabled"], tuple(x["error"] for x in i["reasons"]))
            for tid, i in sorted(items.items())
        }
        results[tag] = (200, fingerprint)
    else:
        results[tag] = (rr.status_code, None)


def execute_worker():
    barrier.wait()
    results["execute"] = (run(target, "r2", 1).status_code, None)


threads = [threading.Thread(target=check_worker, args=(f"chk{i}",))
           for i in range(7)]
threads.append(threading.Thread(target=execute_worker))
for t in threads:
    t.start()
for t in threads:
    t.join()

check("concurrent execute 200", results["execute"][0] == 200, results)
for i in range(7):
    code, snapshot = results[f"chk{i}"]
    check(f"concurrent check{i} 200/409", code in (200, 409), results)
# Every 200 is the whole v1 verdict: s1/r1/s2 disabled, r2/x enabled. A 200
# snapshot never reflects the later r2 execution.
    if code == 200:
        check(f"concurrent check{i} whole snapshot", snapshot == {
            "r1": (False, ("state_mismatch",)),
            "r2": (True, ()),
            "s1": (False, ("state_mismatch", "send_queue_occupied")),
            "s2": (False, ("send_queue_occupied",)),
            "x": (True, ()),
        }, snapshot)

final = client.get(f"/v1/sessions/{target}").json()
check("session advanced exactly once", final["version"] == 2
      and [s["transition_id"] for s in final["trace"]] == ["s1", "r2"],
      (final["version"], final["trace"]))
post = checks(target, 2).json()
check("post-commit report recomputed",
      all(item["transition_id"] for item in post["transitions"])
      and post["version"] == 2, post)

# 12. old endpoints still work
r = client.get("/healthz")
check("healthz intact", r.status_code == 200)
r = client.post("/v1/sessions", json=Q)
check("sessions create intact", r.status_code == 201)
r = client.post("/v1/deadlocks", json=Q)
check("deadlocks intact", r.status_code == 200)
r = client.post(f"/v1/sessions/{sid}/message-tracks", json={"expected_version": 2})
check("message-tracks intact", r.status_code == 200)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    raise SystemExit(1)
print("ALL TRANSITION-CHECK CHECKS PASSED")
