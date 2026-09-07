"""Keyed lazy-initialization primitive with per-key double-checked locking.

:class:`KeyedLazyRegistry` consolidates the hand-rolled "value = registry.get(key);
if value is None: with lock: value = registry.get(key); if value is None:
registry[key] = build()" idiom that
``branding_team/api/conversation.py::_get_or_create_phase_cache`` and
``branding_team/api/main.py::_get_brand_cache`` used to hand-roll — both are now
thin wrappers over this class, shown as the :meth:`~KeyedLazyRegistry.get_or_create`
usage example below. ``llm_service/rate_limiter.py::_get_team_semaphore`` still
hand-rolls its own copy of the same idiom (see ``shared/concurrency/README.md``
for the full picture). This class is the dict-keyed sibling of
:class:`~shared.concurrency.lazy_singleton.LazySingleton`, and shares that class's
factory contract verbatim so the two primitives can be reasoned about together.

Unlike a shared-lock call site, which serializes every key's construction
through one lock, this registry delegates mutual exclusion to
:class:`~shared.concurrency.keyed_lock_manager.KeyedLockManager`: a slow factory
for one key never delays first construction for another. It is stdlib-only apart
from that sibling primitive.
"""

from __future__ import annotations

from typing import Callable, Dict, Generic, Hashable, Optional, TypeVar

from shared.concurrency.keyed_lock_manager import KeyedLockManager

__all__ = ["KeyedLazyRegistry"]

K = TypeVar("K", bound=Hashable)
_T = TypeVar("_T")


class KeyedLazyRegistry(Generic[K, _T]):
    """A value per key, each lazily built at most once even under concurrent first access.

    Intended lifetime: construct one instance at module scope (or as an instance
    attribute) per logical registry, and call :meth:`get_or_create` from every
    access point instead of hand-rolling the dict-plus-lock double-checked-locking
    check. Values are never evicted, so this is for registries whose key space is
    bounded in practice (one entry per brand, per conversation, per team) — not a
    general-purpose cache. A registry that needs eviction wants a bounded/LRU
    structure instead, not this primitive.

    Usage (the first example is the real
    ``branding_team/api/conversation.py::_get_or_create_phase_cache`` call site;
    the second is illustrative for a closure-style factory)::

        _phase_caches: KeyedLazyRegistry[str, PhaseOutputCache] = KeyedLazyRegistry()

        def _get_or_create_phase_cache(conversation_id: str) -> PhaseOutputCache:
            return _phase_caches.get_or_create(conversation_id, PhaseOutputCache)

        # A factory needing constructor arguments is just a closure — the
        # primitive doesn't care how the value is built. Held as an instance
        # attribute here rather than at module scope, since the value depends on
        # per-instance configuration:
        class TeamRateLimiter:
            def __init__(self, per_team_limit: int) -> None:
                self._team_semaphores: KeyedLazyRegistry[str, threading.BoundedSemaphore] = (
                    KeyedLazyRegistry()
                )
                self.per_team_limit = per_team_limit

            def _get_team_semaphore(self, team: str) -> threading.BoundedSemaphore:
                return self._team_semaphores.get_or_create(
                    team, lambda: threading.BoundedSemaphore(self.per_team_limit)
                )

    Preconditions:
        - Every ``key`` passed to :meth:`get_or_create` is hashable (the bound on
          ``K``), since keys index both the value dict and the underlying
          :class:`~shared.concurrency.keyed_lock_manager.KeyedLockManager`. Its
          ``__hash__``/``__eq__`` are consistent, side-effect-free, and do not
          call back into this registry — they run on the unlocked fast-path
          lookup described in the Invariants, outside any lock this class holds.
        - Every ``factory`` takes no arguments and either returns a ``_T`` or
          raises.
        - ``factory`` never returns ``None`` — ``None`` is this class's internal
          sentinel for "no value for this key yet", exactly as the hand-rolled
          call sites it replaces already relied on (e.g. ``cache is None`` after
          a ``dict.get``), and exactly as
          :class:`~shared.concurrency.lazy_singleton.LazySingleton` requires.
        - A caller does not assume a *specific* call's ``factory`` ran just
          because that call returned — once some call's ``factory`` has already
          constructed that key's value, later calls for that key return it
          without invoking their own ``factory`` at all.
        - ``factory`` does not call :meth:`get_or_create` on this same instance
          for the key it is building, directly or transitively: the per-key lock
          is not reentrant and is held for the duration of ``factory``.
        - ``factory`` does not call :meth:`get_or_create` on this same instance
          for a key that was seen earlier *and is not yet built* — either
          because its own ``factory`` previously raised, or because another
          thread is building it right now. The in-flight case raises too; it
          does not wait for that thread's build to finish. The underlying manager
          assigns each key a global order at first sight and refuses to nest a
          lower-order acquisition under a higher-order one, which is what rules
          out an A-builds-B/B-builds-A deadlock cycle.

          Two nestings are always fine, and are the common cases: building a key
          this registry has never seen (it is assigned a higher order, so the
          rule permits it), and reading a key that is already built (it returns
          on the unlocked fast path below and never acquires its lock at all, so
          the ordering rule is never consulted).

          What is left is the narrow case above, and there the permitted set
          still depends on the order this registry first saw the two keys — an
          acyclic nesting can be accepted in one process and refused in another
          that touched the keys in the opposite order. Building keyed values
          from independent factories rather than from each other avoids the
          question entirely.
        - Both nesting violations raise ``RuntimeError`` rather than deadlocking
          silently — inherited from
          :class:`~shared.concurrency.keyed_lock_manager.KeyedLockManager`, and
          the reason that primitive is reused here instead of a bare per-key
          ``Lock``: trading a rejected-but-safe nesting for two classes of
          silent hang is the better bargain for a shared primitive.
        - The value a ``factory`` returns is itself safe for other threads to
          use. This registry guarantees only that a key's value is published
          once and fully constructed, never that operations on it are
          thread-safe.

    Postconditions:
        - The first ``factory`` invocation to complete without raising for a
          given key constructs that key's value exactly once; every call to
          :meth:`get_or_create` for that key — whether it arrives before, during,
          or after that construction — returns that exact same object, never a
          second instance, even under concurrent first calls for the key.
        - Distinct keys are fully independent: a value constructed for one key is
          never returned for another, and a ``factory`` failure for one key
          leaves every other key's value untouched.
        - "Exactly once" constrains *successful* construction only. While a key
          has no value, concurrent callers for it each run their own ``factory``
          in turn — serialized, never overlapping — until one succeeds, so a
          ``factory`` that always raises is retried once per caller by design.

    Invariants:
        - At most one ``factory`` runs per key at a time (serialized by that
          key's lock), while factories for *distinct* keys run fully
          concurrently. This is the property the shared-lock call sites lack and
          the reason this class delegates to
          :class:`~shared.concurrency.keyed_lock_manager.KeyedLockManager` rather
          than holding a single lock across construction.
        - No value is ever evicted or replaced once stored, so a key's value is
          stable for this instance's lifetime and memory grows with the number of
          distinct keys ever seen — the same unbounded-registry tradeoff the two
          migrated call sites' comments already document, and the same one the
          underlying manager makes for its per-key locks.
        - Each key's slot is single-assignment: written only by the thread
          holding that key's lock, only after ``factory`` returned a non-``None``
          value, and never overwritten or removed. That, not merely dict
          atomicity, is what makes the lock-free fast-path read *correct* rather
          than only fast. A single ``dict`` lookup and a single ``dict`` store
          are atomic with respect to one another (CPython serializes them;
          free-threaded builds lock the dict internally), so the read observes
          exactly one of two things: the key's one final, fully-constructed
          value, or nothing — never a torn intermediate. Observing nothing is
          never wrong, only slower: that caller falls through to the locked path
          and re-checks the slot under the key's lock *before* deciding to
          build, so a stale fast-path miss costs one uncontended lock
          acquisition and can never cause a second construction. This is the
          same assumption the hand-rolled code the two migrated call sites used
          to have relied on. :meth:`__contains__` and :meth:`__getitem__` are lock-free
          reads over the same slot and lean on this same guarantee: once either
          observes a key present, that observation is permanent and the value
          returned is that key's one final object, never a partial one.
    """

    def __init__(self) -> None:
        # Per-key mutual exclusion, delegated rather than hand-rolled: this is
        # what keeps one key's slow factory from blocking another key's first
        # construction, and what turns a re-entrant factory into a RuntimeError
        # instead of a silent self-deadlock.
        self._locks: KeyedLockManager[K] = KeyedLockManager()
        self._values: Dict[K, _T] = {}

    def get_or_create(self, key: K, factory: Callable[[], _T]) -> _T:
        """Return ``key``'s value, building it via ``factory`` on first success.

        Preconditions:
            ``key`` is hashable; ``factory`` takes no arguments and returns a
            non-``None`` ``_T`` or raises. On nesting, see the class
            Preconditions, which state the rule authoritatively — in short,
            ``factory`` must not call :meth:`get_or_create` on this same
            instance for ``key`` itself, nor for a key seen earlier *and not yet
            built* (its ``factory`` either previously raised or is in flight on
            another thread); both raise
            ``RuntimeError`` from the underlying
            :class:`~shared.concurrency.keyed_lock_manager.KeyedLockManager`
            rather than deadlocking. Building a key this registry has not seen,
            and reading one that is already built, are both always permitted —
            a built key returns on the unlocked fast path below and never
            acquires its lock, so the ordering rule is never consulted.

        Postconditions:
            See the class Postconditions: exactly-once construction per key
            across concurrent first calls for that key, and independence between
            keys. If ``factory`` raises, the exception propagates to that caller
            unchanged and ``key`` remains unconstructed, so a later call retries
            by invoking its own ``factory`` again rather than caching the
            failure — matching the raise-then-retry contract
            :class:`~shared.concurrency.lazy_singleton.LazySingleton` already
            has. Raises ``ValueError`` — likewise without caching anything, so a
            later call still retries — if ``factory`` returns ``None``, since a
            silently cached ``None`` would violate the "returns that exact same
            object" postcondition on every later call for ``key`` with no way to
            diagnose why.
        """
        value: Optional[_T] = self._values.get(key)
        if value is None:
            with self._locks.lock([key]):
                value = self._values.get(key)
                if value is None:
                    value = factory()
                    if value is None:
                        raise ValueError(
                            f"factory for key {key!r} returned None; None is reserved as the "
                            "'no value for this key yet' sentinel for KeyedLazyRegistry"
                        )
                    self._values[key] = value
        return value

    def __contains__(self, key: object) -> bool:
        """Whether ``key`` already has a built value.

        Preconditions:
            None beyond ``key`` being a hashable object — it need not be a
            ``K`` this registry has ever seen.
        Postconditions:
            Returns ``True`` iff some call's ``factory`` has already completed
            for ``key``. This is a point-in-time snapshot, but per the class
            Invariants a ``True`` result is permanent: this instance never
            evicts, so a later :meth:`__getitem__` for the same ``key`` always
            succeeds and returns that identical value. A ``False`` result means
            only "not built *yet*" — another thread may be constructing it
            right now, or no one has asked for it at all — never "never will
            be." Does not invoke any ``factory`` and never blocks on a key's
            lock.
        """
        return key in self._values

    def __getitem__(self, key: K) -> _T:
        """Return ``key``'s already-built value without constructing it.

        Preconditions:
            ``key in self`` — this is a read of an existing slot, not a
            build-or-read like :meth:`get_or_create`.
        Postconditions:
            Returns the exact object some call's ``factory`` built for ``key``,
            identical to what every :meth:`get_or_create` call for that key
            returns. Raises ``KeyError`` if ``key`` has no value yet — never
            ``None``, since ``self._values`` only ever holds fully-constructed,
            non-``None`` values (see class Preconditions on ``factory``). Does
            not invoke any ``factory`` and never blocks on a key's lock.
        """
        return self._values[key]
