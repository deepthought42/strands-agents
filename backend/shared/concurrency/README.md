# shared.concurrency

Small, dependency-light concurrency primitives shared across agent teams.
Importing this package pulls in only the Python standard library.

## `BackgroundHeartbeat`

One driver for the "daemon thread runs a callable on an interval until stopped"
pattern that several teams had independently hand-rolled (a Temporal
`activity.heartbeat()` keep-alive, the founder spec-generation job heartbeat, the
SE job-store heartbeat thread, the SE and job-service stale-job monitors, the
blogging pipeline heartbeat, and the blogging event-bus reaper). The driver is
generic — it knows nothing about Temporal or the job service; it just calls a
`beat` callable every `interval_s` until stopped.

```python
from shared.concurrency import BackgroundHeartbeat

# Externally controlled (context manager owns start + stop):
with BackgroundHeartbeat(activity.heartbeat, 30.0, copy_context=True):
    do_long_blocking_work()

# Fire-and-forget, self-terminating via a predicate:
BackgroundHeartbeat(
    lambda: client.heartbeat(job_id),
    120.0,
    should_continue=lambda: job_is_active(job_id),
    on_error=lambda exc: logger.warning("hb %s: %s", job_id, exc),
).start()

# Fire-and-forget with a caller-held stop handle, beating immediately on start:
stop = threading.Event()
BackgroundHeartbeat(sweep, 60.0, beat_first=True, stop_event=stop).start()
# ... later, from anywhere: stop.set()
```

Parameters cover the axes the original copies differed on:

- `should_continue` — optional predicate checked each tick; returning `False`
  exits the thread on its own (no external stop needed).
- `beat_first` — run one beat before the first wait (e.g. a stale-job sweep that
  should fire immediately on startup) instead of the default wait-then-beat.
- `copy_context` — snapshot `contextvars.copy_context()` and run **both** the beat
  and `should_continue` inside it (so a Temporal activity handle is visible in the
  beater thread). The two run in the same context — there is no asymmetry.
- `on_error` — invoked on any beat/predicate exception; default swallows. A
  raising beat or predicate never kills the loop.
- `stop_event` — inject a caller-owned `threading.Event` so the caller keeps a raw
  stop handle (an injected event is never cleared on `start()`).
- `join_timeout` — bound on `stop()`'s join.

`start()` is idempotent; `is_alive()` reports whether the beater thread is
running; `stop()` is safe to call before `start()` or twice.

### Single liveness owner (coding-team activity)

`software_engineering_team/temporal/activities.py::execute_coding_team_activity`
relies on the background beater as the *sole* liveness mechanism for the whole
run; the orchestrator's update callback only persists progress and does **not**
also heartbeat. This keeps "who keeps the activity alive" unambiguous.

## `parallel_map`

One driver for the "fan a per-item function across a bounded `ThreadPoolExecutor`"
pattern that several teams had each hand-rolled with subtly different ordering,
error, and **context-propagation** semantics. The decisive correctness issue is
the last one: a raw `ThreadPoolExecutor` does **not** copy contextvars into its
workers, so every fan-out site has to remember to wrap submissions in
`contextvars.copy_context().run(...)` or it silently drops the LLM attribution /
request-id contextvars (see `llm_service.attribution`). Routing all fan-out
through this helper fixes worker bounds, exception propagation, and context
propagation once — and new sites get context propagation for free.

```python
from shared.concurrency import parallel_map

# Common case — bounded, order-preserving, context propagated, None skipped:
results = parallel_map(prospects, run_one, max_workers=8)

# Each task gets its own copy_context(), so this propagates by default. Opt out
# only for CPU-only work that explicitly wants no propagation:
sums = parallel_map(rows, crunch, max_workers=4, propagate_context=False)

# Completion order + a failure hook (e.g. flip an "abandoned" progress flag
# before pending tasks are cancelled):
outcomes = parallel_map(
    chunks, review_one, max_workers=4, preserve_order=False,
    skip_none=False, on_first_exception=mark_abandoned,
)
```

Parameters cover the axes the original copies differed on:

- `max_workers` — the pool is sized at `min(max_workers, len(items))`, so a small
  batch never spins up idle threads. Empty input short-circuits to `[]`.
- `preserve_order` — return results aligned to input order (default) or in
  completion order.
- `skip_none` — filter `None` results out (the "return `None` to skip this item"
  convention, default) or keep them positionally.
- `propagate_context` — run each task inside a fresh `contextvars.copy_context()`
  (default) so the caller's attribution/request-id reach the worker.
- `on_first_exception` — optional zero-arg callback fired exactly once, on the
  first worker exception, **before** pending tasks are cancelled and the
  exception re-raises.
- `timeout` / `on_timeout` — optional per-item wall-clock budget in seconds,
  measured from when that item's `fn` actually starts running (not from
  submission, so queued-behind-a-busy-worker time is never charged). A task
  that reaches or exceeds it is **degraded, not aborted**: its result becomes `None`
  (`on_timeout(item)` is invoked first, if given) while the rest of the batch
  keeps running unaffected. Default `None` disables timeouts entirely and
  runs the original code path unchanged.

Error policy is a single, documented **fast-fail**: the first worker exception is
observed in completion order (never delayed behind a slower earlier task),
pending tasks are cancelled (`cancel_futures=True`), and the exception propagates
with its original traceback while already-running tasks finish in the background.
A per-item `timeout` is independent of this: it degrades just that one item and
never trips the fast-fail path — unless a *different* item also raises a real
exception, in which case fast-fail still wins.

```python
# Per-item timeout: a hung item degrades to None instead of blocking the batch,
# with a hook to log/record which item timed out:
results = parallel_map(
    groups, verify_one, max_workers=4, skip_none=False,
    timeout=60.0, on_timeout=lambda item: logger.warning("verify timed out: %s", item),
)
```

Migrated callers: the sales pod's per-prospect / decision-maker / dossier
fan-outs (`sales_team/orchestrator.py`), the blog research agent's document
scoring and summarization (`blogging/blog_research_agent/agent.py`), and the SE
code-review coordinator's per-chunk map (`code_review_agent/coordinator.py`).

## `LatestValueFlusher`

A single-slot mailbox drained by one daemon writer thread, for moving a
synchronous, possibly-slow write off a thread that holds a lock other threads
need — the case that motivated it: `coding_team/orchestrator.py`'s task-graph
mutators used to call a job-service HTTP write synchronously while holding
`TaskGraphService`'s lock, serializing concurrent implementation workers on the
sum of their write latencies even though their LLM/build/lint work ran in
parallel.

```python
from shared.concurrency import LatestValueFlusher

with LatestValueFlusher(job_client.update_job, name="job-persist") as flusher:
    flusher.enqueue({"status_text": "working"})  # never blocks
    ...
    flusher.drain()  # block until the above (or a fresher payload) has landed
```

Because the destination write is assumed to be **overwrite, not append**
semantics (e.g. the job service's `update_job` does a shallow JSONB merge —
last write wins per field), `enqueue()` never queues more than one payload: a
burst of mutations before the writer thread catches up coalesces into a single
write of the latest state, not N sequential writes.

- `enqueue(payload)` — replace the pending payload; never blocks on the writer.
- `drain(timeout=None)` — block until idle (no pending payload, no write in
  flight); the mechanism that makes ordering safe when a caller needs to do its
  own synchronous write afterward without a stale background write landing
  after it.
- `on_error` — invoked on any writer exception; default logs and swallows. A
  raising writer never kills the loop.
- `start()`/`is_alive()`/context-manager use mirror `BackgroundHeartbeat`.
  `stop()` drains first — **unbounded**, not bounded by `join_timeout` — so a
  payload enqueued just before shutdown is guaranteed to land rather than
  possibly being abandoned mid-write by a writer slower than `join_timeout`
  (e.g. an HTTP client with its own longer timeout/retry budget);
  `join_timeout` only bounds the final thread join once draining is done.

## `KeyedLockManager`

A per-key mutual-exclusion registry: concurrent callers that name the same
key are serialized against each other, while callers touching disjoint keys
proceed fully concurrently — the "lock only what actually conflicts" pattern,
as opposed to a single global lock that would serialize everything.

Motivating use case: the SE code-v2 gated execution loop
(`software_engineering_team/shared/phases/execution.py`) accumulates every
microtask's output into a shared `all_files: Dict[str, str]` dict and writes
it to a shared `repo_path` git worktree. Independent microtasks in the same
scheduled wave run concurrently via `parallel_map` with `wait_for_stragglers=True`
so a stop-on-review-failure does not return while a sibling is still writing
the worktree. The pool is capped by `SE_EXECUTION_WAVE_CONCURRENCY` (default 4),
not wave size. Generation runs unlocked; write through review, docs, and
rollback then hold a per-run worktree lock because review tools (build/lint)
observe the whole repo and review/docs can introduce paths that were not in
the initial generation set. Per-path `KeyedLockManager` locks still
serialize overlapping snapshot/write/merge under that exclusive section;
keys are physical (`realpath`) paths, so `shared.py` and `./shared.py`
serialize against each other.

```python
from shared.concurrency import KeyedLockManager

file_locks: KeyedLockManager[str] = KeyedLockManager()  # one instance per task run

# Keys are physical (realpath) paths so aliases of one file serialize together.
with file_locks.lock(physical_lock_keys):
    write_repo_text_files(repo_path, microtask_files)
    all_files.update(microtask_files)
```

- `lock(keys)` — a context manager that acquires every key in `keys` (any
  hashable) for the duration of the `with` block, deduplicating repeated keys
  first. An empty batch is a no-op.
- Disjoint key sets never block each other; overlapping keys are fully
  serialized — whichever caller acquires second observes every side effect
  the first caller made under the lock (no interleaving, no dropped update).
- Batch acquisition is deadlock-safe **regardless of the order keys are
  passed in**: every key is assigned a global order the first time this
  manager ever sees it, and `lock()` always acquires a batch sorted by that
  order — so two callers locking `["a", "b"]` and `["b", "a"]` concurrently
  can never deadlock on each other.
- Not reentrant: a thread calling `lock()` for a key it already holds from an
  outer, not-yet-exited `lock()` call raises `RuntimeError` immediately
  instead of deadlocking silently.
- A key's lock is never removed once created — this manager is meant to be
  constructed once and reused for an entire run (the same lifetime as the
  `all_files` dict it is intended to guard), not created per call.

## `LazySingleton`

A single-slot "build at most once, even under concurrent first access"
primitive, consolidating the hand-rolled double-checked-locking idiom that
`branding_team/store.py::get_default_store`,
`branding_team/api/main.py::_get_assistant_agent`, and
`shared/coro_runner.py`'s worker-pool singleton each used to hand-roll — all
three are now thin wrappers over this class.

```python
from shared.concurrency import LazySingleton

_store: LazySingleton[BrandingStore] = LazySingleton()  # module-scope, one per value

def get_default_store() -> BrandingStore:
    return _store.get_or_create(BrandingStore)
```

- `get_or_create(factory)` — returns the constructed value, calling
  `factory()` to build it on the first call to succeed; every other call,
  concurrent or not, returns that exact same object without invoking its own
  `factory`.
- A raising `factory` propagates the exception to that caller and leaves the
  instance unconstructed, so the next call retries — this is what lets
  `_get_assistant_agent`'s existing "raise `HTTPException`, retry on the next
  request" contract carry over unchanged.
- A `factory` that also needs a one-time side effect (e.g. `atexit`
  registration) is just a closure — `get_or_create` doesn't care what
  `factory` does beyond returning the value.
- `None` is reserved as the "not yet constructed" sentinel, so `factory` must
  never return `None` — the same assumption the hand-rolled call sites above
  already made. Enforced: a `None`-returning `factory` raises `ValueError`
  immediately rather than silently caching `None` and re-running `factory`
  on every later call.
- `factory` must not call `get_or_create` on the same instance, directly or
  transitively — the internal lock isn't reentrant and is held for the
  duration of `factory`, so re-entry deadlocks the calling thread.

## `KeyedLazyRegistry`

The dict-keyed sibling of `LazySingleton`: one lazily-built value *per key*,
consolidating the hand-rolled dict-plus-lock double-checked-locking idiom. Two
call sites — `branding_team/api/conversation.py::_get_or_create_phase_cache`
and `branding_team/api/main.py::_get_brand_cache` — are already thin wrappers
over it; `llm_service/rate_limiter.py::_get_team_semaphore` still hand-rolls
its own copy of the same idiom.

```python
from shared.concurrency import KeyedLazyRegistry

_phase_caches: KeyedLazyRegistry[str, PhaseOutputCache] = KeyedLazyRegistry()

def _get_or_create_phase_cache(conversation_id: str) -> PhaseOutputCache:
    return _phase_caches.get_or_create(conversation_id, PhaseOutputCache)

# A value needing constructor arguments is just a closure. Held as an instance
# attribute here, since the value depends on per-instance configuration:
class TeamSemaphorePool:
    def __init__(self, per_team_limit: int) -> None:
        self.per_team_limit = per_team_limit
        self._team_semaphores: KeyedLazyRegistry[str, threading.BoundedSemaphore] = (
            KeyedLazyRegistry()
        )

    def _get_team_semaphore(self, team: str) -> threading.BoundedSemaphore:
        return self._team_semaphores.get_or_create(
            team, lambda: threading.BoundedSemaphore(self.per_team_limit)
        )
```

- `get_or_create(key, factory)` — returns that key's value, calling `factory()`
  to build it on the first call for the key to succeed; every other call for
  that key, concurrent or not, returns that exact same object without invoking
  its own `factory`.
- **Distinct keys never block each other's construction.** This is the one
  behavioural difference from the call sites above, which funnel every key
  through a single shared lock: there, a slow `factory` for one key stalls first
  construction for every other key. Here each key has its own lock, delegated to
  `KeyedLockManager`, and no lock is held across another key's `factory`.
- Raise-and-retry is per key: a raising `factory` propagates to that caller and
  leaves *that key* unbuilt, so the next call for it retries — while every other
  key's value, and its retryability, is untouched. "Build exactly once" is a
  claim about *successful* construction; concurrent callers for a key whose
  `factory` always raises each get their own turn, serialized.
- `None` is the per-key "not built yet" sentinel, so `factory` must never return
  `None` — the same rule, and the same `ValueError`, as `LazySingleton`. The
  error message names the offending key.
- `key in registry` and `registry[key]` are lock-free reads of an
  already-built slot — neither invokes `factory` or blocks on a key's lock.
  `in` answers "has some call's `factory` already completed for this key?";
  once `True`, it's permanent, since values are never evicted. `registry[key]`
  returns that exact same object, or raises `KeyError` if the key isn't built
  yet (never `None` — `None` is never a stored value). Use these only to check
  or read a key you expect is already populated; to get-or-build, call
  `get_or_create`.
- Re-entrancy is rejected loudly rather than hanging: a `factory` that calls
  `get_or_create` for the key it is building raises `RuntimeError`, and so does
  one that reaches for a key this registry saw *earlier and that is not yet
  built* — whether its own factory previously raised, or it is still being built
  right now on another thread (the inherited `KeyedLockManager` lock ordering,
  which is what forecloses an A-builds-B/B-builds-A cycle). Note the in-flight
  case raises rather than waiting for the other thread's build to finish. Nesting
  into a key that is brand new, or one that is already built, is always fine:
  the first is assigned a higher order, and the second returns on the unlocked
  fast path without touching its lock. For that remaining narrow case, whether a
  nesting is permitted depends on first-sight order, so it can be accepted in
  one process and refused in another — build keyed values from independent
  factories, not from each other.
- Neither values nor per-key locks are ever evicted, so memory grows with the
  number of distinct keys ever seen — the same accepted tradeoff as
  `KeyedLockManager`, and the same one the never-evicted caches this is meant to
  hold already make. A registry that needs eviction wants a bounded/LRU
  structure, not this.
- Reach for `KeyedLockManager` instead when you want to hold a key's lock across
  *your own* critical section, or lock several keys at once; reach for this when
  you want keyed state built once and handed back.
