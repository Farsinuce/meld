"""WorkerPool run-control: the stop flag, the per-run stagger epoch, and the
governor admission hook (src/workers.py).

These are the three things that were silently wrong: `_stopped` was written by
nobody, the "first job" stagger fired once per THREAD (so runs 2..N started in
lockstep), and there was no way for a scheduler to gate a worker's start. Every
test here runs in-process - no subprocesses, no arnis, no flask.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.workers import WorkerPool  # noqa: E402

STARTING = "Starting…"


def _collecting_pool(max_workers: int, expect: int, sink: list):
    """Pool whose runner records what each job saw, and an Event that fires once
    `expect` jobs have run."""
    pool = WorkerPool(max_workers=max_workers)
    pool.stagger_seconds = 0.0          # no real sleeping unless a test asks for it
    done = threading.Event()
    lock = threading.Lock()

    def runner(job, state):
        with lock:
            sink.append({"cell": job.get("cell_key"), "worker": state["worker_id"],
                         "message": state.get("message")})
            if len(sink) >= expect:
                done.set()
        return True

    pool.configure(runner)
    return pool, done


# -- stop flag ---------------------------------------------------------------
def test_stop_sets_flag_and_is_readable():
    pool = WorkerPool(max_workers=2)
    assert pool.is_stopped is False
    assert pool._stopped is False          # server.py greps this legacy attribute
    pool.stop()
    assert pool.is_stopped is True
    assert pool._stopped is True


def test_stopped_pool_does_not_pick_up_already_queued_jobs():
    ran = []
    pool, done = _collecting_pool(2, 1, ran)
    pool.stop()
    # Two ways in - straight into the queue, and through submit(). Neither may wake a
    # stopped pool, and neither may eat the queue (clear() is what drains it).
    with pool._cv:
        pool._queue.append({"cell_key": "a"})
        pool._cv.notify_all()
    pool.submit({"cell_key": "b"})
    assert not done.wait(0.4)
    assert ran == []
    assert pool.queue_size() == 2


def test_submit_does_not_clear_the_stop_flag():
    """Finding 14. submit() used to un-stop the pool, so a Stop landing between the caller's
    gate check and the first submit was ERASED: the cells ran (or were killed mid-flight) with
    the pool looking un-stopped, _on_complete read is_stopped False, called the failure
    transient and re-queued the run Stop had just killed. Queueing work must never clear a
    Stop - only new_run_epoch() and resume() do that."""
    ran = []
    pool, done = _collecting_pool(2, 1, ran)
    pool.submit({"cell_key": "warm"})
    assert done.wait(5.0)
    pool.stop()
    time.sleep(0.15)                       # let the idle threads notice and exit
    assert pool.is_stopped is True

    done.clear()
    ran.clear()
    pool.submit({"cell_key": "after-stop"})
    assert pool.is_stopped is True, "submit() must not erase a Stop"
    assert not done.wait(0.4), "a stopped pool must not run newly submitted cells"
    assert ran == []
    assert pool.queue_size() == 1          # the job waits; clear() is what drains it

    pool.resume()                          # the explicit un-stop brings the workers back
    assert pool.is_stopped is False
    assert done.wait(5.0)
    assert [r["cell"] for r in ran] == ["after-stop"]


def test_stop_racing_the_caller_gate_still_holds_for_the_whole_batch():
    """The exact race: caller checks the gate (open), Stop lands, caller submits its batch.
    Every cell must stay unrun and the pool must still read stopped, so the server labels
    them "stopped by user" instead of retrying them."""
    ran = []
    pool, done = _collecting_pool(4, 1, ran)
    assert pool.is_stopped is False        # caller's gate check: open
    pool.stop()                            # ...Stop lands here
    for i in range(4):
        pool.submit({"cell_key": "c%d" % i})
    assert not done.wait(0.4)
    assert ran == []
    assert pool.is_stopped is True
    assert pool.queue_size() == 4

    # And a run started afterwards picks the queue back up (new_run_epoch clears the flag).
    pool.new_run_epoch()
    assert pool.is_stopped is False
    assert done.wait(5.0)


def test_resume_and_new_run_epoch_clear_the_stop_flag():
    pool = WorkerPool(max_workers=1)
    pool.stop()
    pool.resume()
    assert pool.is_stopped is False
    pool.stop()
    pool.new_run_epoch()
    assert pool.is_stopped is False


def test_stop_does_not_break_clear_or_terminate_all():
    pool = WorkerPool(max_workers=1)
    pool.stop()
    pool._queue.append({"cell_key": "x"})
    assert pool.clear() == 1
    assert pool.terminate_all() == 0       # no published processes -> nothing to kill


# -- stagger epoch -----------------------------------------------------------
def test_stagger_rearms_once_per_run_epoch():
    """One long-lived worker, three runs. The stagger decision must be taken once per
    worker per RUN. Before the epoch it was taken once per THREAD, so runs 2..N started
    every worker in lockstep - exactly the sawtooth the stagger exists to prevent."""
    seen = []
    armed = []
    pool = WorkerPool(max_workers=1)
    pool.stagger_seconds = 0.01
    pool.stagger_adaptive = False
    pool._first_job_delay = lambda worker_id: (armed.append(worker_id), 0.01)[1]
    done = threading.Event()

    def runner(job, state):
        seen.append(state.get("message"))
        done.set()
        return True

    pool.configure(runner)

    def one_run(new_epoch):
        if new_epoch:
            pool.new_run_epoch()
        done.clear()
        pool.submit({"cell_key": "c"})
        assert done.wait(5.0)

    one_run(False)                          # run 1: the thread's first job ever
    assert len(armed) == 1
    one_run(True)                           # run 2: same thread, new epoch
    assert len(armed) == 2, "run 2 must re-arm the stagger on the reused worker thread"
    one_run(True)                           # run 3
    assert len(armed) == 3

    # Same worker, same run, second job: no re-stagger.
    done.clear()
    pool.submit({"cell_key": "c2"})
    assert done.wait(5.0)
    assert len(armed) == 3, "the stagger fires once per worker per run, not per job"
    assert seen[-1] == STARTING, seen


def test_stagger_message_visible_for_a_delayed_worker():
    """A worker with a non-zero offset advertises it (worker 0's offset is always 0)."""
    seen = []
    pool = WorkerPool(max_workers=1)
    pool.stagger_seconds = 0.01
    pool.stagger_adaptive = False
    pool._first_job_delay = lambda worker_id: 0.02
    done = threading.Event()
    pool.configure(lambda job, state: (seen.append(state.get("message")), done.set(), True)[2])
    pool.submit({"cell_key": "c"})
    assert done.wait(5.0)
    assert seen[0].startswith("Staggered start"), seen


def test_new_run_epoch_bumps_and_resets_the_ewma():
    pool = WorkerPool(max_workers=1)
    pool.record_completion(42.0)
    assert pool.avg_cell_s > 0
    e0 = pool.run_epoch
    e1 = pool.new_run_epoch()
    assert e1 == e0 + 1 == pool.run_epoch
    assert pool.avg_cell_s == 0.0, "cell-time history must not leak across runs/projects"


def test_first_job_delay_shape_unchanged():
    pool = WorkerPool(max_workers=8)
    pool.stagger_seconds = 1.5
    pool.stagger_adaptive = False
    assert pool._first_job_delay(0) == 0.0
    assert pool._first_job_delay(3) == 4.5
    assert pool._first_job_delay(20) == pool._first_job_delay(8)   # capped
    pool.stagger_seconds = 0.0
    assert pool._first_job_delay(5) == 0.0


# -- governor admission hook -------------------------------------------------
def test_admit_cb_is_called_with_worker_id_and_active_count():
    calls = []
    ran = []
    pool, done = _collecting_pool(1, 2, ran)

    def admit(worker_id, active):
        calls.append((worker_id, active))
        return "go"

    pool.admit_cb = admit
    pool.submit({"cell_key": "a"})
    pool.submit({"cell_key": "b"})
    assert done.wait(5.0)
    assert len(calls) == 2
    assert all(w == 0 for w, _ in calls)
    # Single worker: the caller is discounted, so it never counts itself as active.
    assert all(a == 0 for _, a in calls)


def test_admit_cb_sees_peers_as_active():
    hold = threading.Event()
    seen_active = []
    pool = WorkerPool(max_workers=2)
    pool.stagger_seconds = 0.0
    started = threading.Semaphore(0)
    done = threading.Event()
    n = []

    def runner(job, state):
        started.release()
        hold.wait(5.0)
        n.append(job["cell_key"])
        if len(n) >= 2:
            done.set()
        return True

    pool.configure(runner)
    pool.admit_cb = lambda worker_id, active: (seen_active.append(active), "go")[1]
    pool.submit({"cell_key": "a"})
    assert started.acquire(timeout=5.0)     # worker A is inside the runner
    pool.submit({"cell_key": "b"})
    assert started.acquire(timeout=5.0)     # worker B got admitted while A was busy
    hold.set()
    assert done.wait(5.0)
    assert seen_active[0] == 0 and max(seen_active) == 1, seen_active


def test_admit_cb_replaces_the_stagger_sleep():
    ran = []
    pool, done = _collecting_pool(1, 1, ran)
    pool.stagger_seconds = 30.0            # would hang this test if it were still honored
    pool.stagger_adaptive = False
    pool.admit_cb = lambda worker_id, active: "go"
    t0 = time.monotonic()
    pool.new_run_epoch()
    pool.submit({"cell_key": "a"})
    assert done.wait(5.0)
    assert time.monotonic() - t0 < 3.0
    assert ran[0]["message"] == STARTING    # admission message is cleared before the job runs


def test_admit_cb_can_block_and_then_release():
    gate = threading.Event()
    ran = []
    pool, done = _collecting_pool(1, 1, ran)
    pool.admit_cb = lambda worker_id, active: (gate.wait(5.0), "go")[1]
    pool.submit({"cell_key": "a"})
    time.sleep(0.2)
    assert ran == []
    assert pool.get_states()[0]["message"] == "waiting for admission"
    gate.set()
    assert done.wait(5.0)


def test_admit_cb_exception_does_not_wedge_the_pool():
    ran = []
    pool, done = _collecting_pool(1, 1, ran)

    def admit(worker_id, active):
        raise RuntimeError("governor exploded")

    pool.admit_cb = admit
    pool.submit({"cell_key": "a"})
    assert done.wait(5.0), "a raising admit_cb must not stop the job from running"


def test_no_admit_cb_is_todays_behavior():
    ran = []
    pool, done = _collecting_pool(2, 4, ran)
    assert pool.admit_cb is None
    for i in range(4):
        pool.submit({"cell_key": "c%d" % i})
    assert done.wait(5.0)
    assert sorted(r["cell"] for r in ran) == ["c0", "c1", "c2", "c3"]


def test_completion_callback_and_states_still_work():
    """Guard the untouched contract: on_complete fires, states go back to idle."""
    seen = []
    pool = WorkerPool(max_workers=1)
    pool.stagger_seconds = 0.0
    done = threading.Event()
    pool.configure(lambda job, state: True,
                   lambda job, ok, err: (seen.append((job["cell_key"], ok, err)), done.set()))
    pool.submit({"cell_key": "a"})
    assert done.wait(5.0)
    assert seen == [("a", True, {})]
    deadline = time.monotonic() + 5.0
    while pool.is_running() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert pool.is_running() is False
