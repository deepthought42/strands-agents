# SE Review Gate Evaluation Corpus

Labeled cases pairing a diff (or file set) with the findings the review gates
are expected to produce on it. This is the ground truth the golden-set
evaluation harness scores against.

Governing specifications, all under [`../docs/`](../docs/):

| Doc | What it fixes |
|---|---|
| [`CORPUS_CASE_FORMAT.md`](../docs/CORPUS_CASE_FORMAT.md) | The case and label schema, and the 31-value closed defect-class vocabulary |
| [`GATE_FINDING_MATCHING_RULE.md`](../docs/GATE_FINDING_MATCHING_RULE.md) | When a produced finding counts as matching a label |
| [`CORPUS_FALSE_POSITIVE_RESISTANCE.md`](../docs/CORPUS_FALSE_POSITIVE_RESISTANCE.md) | How a case declares that a gate must *not* report something |
| [`CORPUS_SELECTION_PLAN.md`](../docs/CORPUS_SELECTION_PLAN.md) | The designed distribution these cases fill |
| [`GATE_FINDING_INVENTORY.md`](../docs/GATE_FINDING_INVENTORY.md) | What the gates actually emit |

## What is here today

**40 case directories (`CASE-0005`–`CASE-0044`), carrying 43 `must_find`
labels.** This is the recall half of the corpus. The
false-positive-resistance half is authored separately and is not present yet,
so the corpus is not yet complete against the selection plan's 60-case target.

`CASE-0001`–`CASE-0004` are permanently reserved for the worked examples in
the format and false-positive-resistance specifications. They are deliberately
not materialized here: case identifiers are never reused or renumbered, so
reserving them is cleaner than renumbering these cases later.

### Layout

```
cases/CASE-NNNN/
  case.yaml      # identifier, title, language, stack, gates, mode, origin
  labels.yaml    # the expected_findings list
  diff.patch     # mode: diff
  files/         # mode: files
```

### The `origin` block

Each `case.yaml` carries an `origin` block recording provenance, documented in
[`CORPUS_CASE_FORMAT.md`](../docs/CORPUS_CASE_FORMAT.md) §1:

```yaml
origin:
  sourcing: real        # real | invented
  commit: 0040820       # short SHA of the fix commit; real sourcing only
  note: >               # why no real example was available; invented only
    ...
```

For a real-sourced case, `diff.patch` is the **inverse** of that fix commit,
reduced to its production files — the diff that introduces the defect rather
than the one that removed it. Line numbers in the labels are the defect's
position in the post-diff file, which for these cases is the state of the file
at the fix commit's parent. Real cases are pinned to their origin commit and
are not synchronized with a moving `main`.

## Achieved distribution

| Measure | Value |
|---|---|
| Case directories | 40 |
| `must_find` labels | 43 |
| Cases from real history | 27 (68%) |
| Invented cases | 13 (32%) |
| Labels from real history | 30 of 43 (70%) |
| Backend-primary cases | 32 (80%) |
| Frontend-primary cases | 8 (20%) |

### Per-class must-find counts against the selection plan

Every class matches its target in `CORPUS_SELECTION_PLAN.md` §3 exactly; no
class is over- or under-supplied.

| Class | Target | Actual | | Class | Target | Actual |
|---|---|---|---|---|---|---|
| `naming` | 1 | 1 | | `injection` | 1 | 1 |
| `structure` | 1 | 1 | | `xss` | 1 | 1 |
| `logic` | 3 | 3 | | `csrf` | 1 | 1 |
| `spec-compliance` | 1 | 1 | | `auth` | 2 | 2 |
| `standards` | 2 | 2 | | `crypto` | 1 | 1 |
| `integration` | 1 | 1 | | `insecure-deserialization` | 1 | 1 |
| `testing` | 1 | 1 | | `ssrf` | 1 | 1 |
| `architecture` | 1 | 1 | | `off-by-one` | 1 | 1 |
| `refactor` | 1 | 1 | | `race-condition` | 3 | 3 |
| `maintainability` | 1 | 1 | | `resource-leak` | 2 | 2 |
| `side-effects` | 1 | 1 | | `null-deref` | 3 | 3 |
| `documentation` | 1 | 1 | | `integer-overflow` | 1 | 1 |
| `missing-import` | 1 | 1 | | `unvalidated-input` | 3 | 3 |
| `wrong-path` | 1 | 1 | | `missing-error-handling` | 2 | 2 |
| `type-error` | 1 | 1 | | `inconsistent-state` | 1 | 1 |
| `syntax-error` | 1 | 1 | | | | |

### Frontend share: a hand-off for the other half

The corpus-wide target is 28% frontend. This half lands **8 of 40 (20%)**, so
the false-positive-resistance half needs roughly **9 of its ~24 cases (≈37%)
frontend** for the corpus as a whole to reach 28%.

That shortfall is real and is recorded rather than papered over. Frontend
supply in this repository's shipped-and-fixed history is genuinely thinner
than 28%, and the two cross-stack invented classes that could plausibly go
either way (`integer-overflow`, `type-error`) are already authored as
frontend. Inventing further frontend cases purely to move the ratio would
produce exactly the "whatever was easy to find" corpus the selection plan was
written to prevent.

### Substitutions from the selection plan

Two of the plan's cited commits did not survive contact with their actual
diffs. Both substitutions are recorded here rather than forced.

- **`standards` (frontend), `CASE-0012`.** The plan cites `1308c1d` for a
  native `alert()` replaced by the application's snackbar convention. That
  commit's net squashed diff contains no `alert()` — the call had already been
  removed by `086a4894`, which is the commit that actually performs the
  substitution. `CASE-0012` is sourced from `086a4894`.
- **`architecture`, `CASE-0015`.** The plan cites `ae3ccf70` for a
  team-agnostic platform test importing a domain team package. That pull
  request introduced *and* fixed the violation within itself, so the violating
  state was squashed away and never reached `main`; the net diff is
  boundary-clean and does not invert to the defect. `CASE-0015` is authored as
  an invented case modelled on the boundary `ae3ccf70` documents, and is
  marked `sourcing: invented` with that reason.

No path was genericized for sensitivity: real-sourced cases use their true
repository-relative paths throughout, and no fixture embeds a credential,
token, key, or other private value.

## Case index

| Case | Title | Class(es) | Gate(s) | Sourcing | Origin | Stack |
|---|---|---|---|---|---|---|
| CASE-0005 | Helper module name shadows the stdlib logging module | `naming` | code_review | invented | — | backend |
| CASE-0006 | Response model defined inline in agent.py instead of models.py | `structure` | code_review | invented | — | backend |
| CASE-0007 | Null recommendation stringified to the literal "None" | `logic`, `null-deref` | code_review, qa | real | 0040820 | backend |
| CASE-0008 | Excluded-company match uses substring instead of word boundaries | `logic` | code_review | real | 766c6e5 | backend |
| CASE-0009 | aria-label bound as a host attribute on mat-checkbox | `logic` | code_review | real | 3fd49f5 | frontend |
| CASE-0010 | Endpoint returns a shape the documented response contract forbids | `spec-compliance` | code_review | invented | — | backend |
| CASE-0011 | Cache root read from an environment variable that is never set | `standards` | code_review | real | c6169cd | backend |
| CASE-0012 | Native blocking alert() used for an error toast | `standards` | code_review | real | 086a4894 | frontend |
| CASE-0013 | Factory overwrites its own injected llm_client | `integration` | code_review | real | e325ad8 | backend |
| CASE-0014 | Teardown success spec passes without exercising the subscribe path | `testing` | code_review | real | 0175839 | frontend |
| CASE-0015 | Team-agnostic platform test imports a domain team package | `architecture` | code_review | invented | — | backend |
| CASE-0016 | Review-gated execution loop hand-inlined in each team orchestrator | `refactor` | code_review | real | 51810fd5 | backend |
| CASE-0017 | Redundant `or` fallback masks a valid falsy value | `maintainability` | code_review | real | 66bc52d5 | backend |
| CASE-0018 | Crash handler leaves the dedicated error field unset | `side-effects`, `inconsistent-state` | code_review, qa | real | c8cbbb7 | backend |
| CASE-0019 | Docstring documents a precondition the function never enforces | `documentation` | code_review | real | c0db42b | backend |
| CASE-0020 | SQL table identifier interpolated with no identifier validation | `injection` | security | real | 27691e3 | backend |
| CASE-0021 | Trigger description interpolated unescaped into trusted SVG | `xss` | security | real | 7e11ddc | frontend |
| CASE-0022 | State-changing endpoint accepts a cookie-authenticated cross-origin form post | `csrf` | security | invented | — | backend |
| CASE-0023 | Webhook signature verification skipped when the secret is unset | `auth` | security | real | f1f605b | backend |
| CASE-0024 | Repository-relative path joined without containment check | `auth`, `unvalidated-input` | qa, security | real | c5a9017 | backend |
| CASE-0025 | Encryption key falls back to a hard-coded literal | `crypto` | security | invented | — | backend |
| CASE-0026 | Cached payload deserialized with pickle | `insecure-deserialization` | security | invented | — | backend |
| CASE-0027 | User-configured URL fetched with no destination restriction | `ssrf` | security | invented | — | backend |
| CASE-0028 | Progress counter can grow past the next phase's fixed value | `off-by-one` | qa | real | 70f16f7 | backend |
| CASE-0029 | Cancellation check and status write are not atomic | `race-condition` | qa | real | cb8aded | backend |
| CASE-0030 | Lock released between session enumeration and write-back | `race-condition` | qa | real | f5eb3e9 | backend |
| CASE-0031 | Read-then-delete is not atomic, so pop can return stale data | `race-condition` | qa | real | 56c2fcd | backend |
| CASE-0032 | Flowchart click listeners are added without ever being removed | `resource-leak` | qa | real | caed749 | frontend |
| CASE-0033 | Component subscribes throughout with no teardown on destroy | `resource-leak` | qa | real | 9ac88a3 | frontend |
| CASE-0034 | OHLC values coerced with float() without a null guard | `null-deref` | qa | real | c0e71da | backend |
| CASE-0035 | Explicit null field stringified into the literal "None" | `null-deref` | qa | real | fd3b9a0 | backend |
| CASE-0036 | Identifier from an external API truncated by JS number precision | `integer-overflow` | qa | invented | — | frontend |
| CASE-0037 | Externally supplied agent id used directly as a path component | `unvalidated-input` | qa | real | 3a31cee | backend |
| CASE-0038 | One store method skips the path validation its siblings apply | `unvalidated-input` | qa | real | 0436308 | backend |
| CASE-0039 | Bare except returns None, erasing the difference from not-found | `missing-error-handling` | qa | real | 4f1b84e | backend |
| CASE-0040 | Catch-all reported as a specific, unrelated failure | `missing-error-handling` | qa | real | 87a02c2 | backend |
| CASE-0041 | Module uses a name it never imports | `missing-import` | qa | invented | — | backend |
| CASE-0042 | Template path points at a directory that does not exist | `wrong-path` | qa | invented | — | backend |
| CASE-0043 | Discriminated union member accessed on the wrong branch | `type-error` | qa | invented | — | frontend |
| CASE-0044 | Unclosed bracket in a dictionary literal | `syntax-error` | qa | invented | — | backend |

## Limits worth knowing before reading a metric derived from this

These are inherited from `CORPUS_SELECTION_PLAN.md` §7 and hold for the cases
here:

- Fifteen classes carry a single `must_find` label, so one miss swings that
  class's measured recall from 100% to 0%. The corpus confirms a gate catches
  *an* instance, not that it generalizes.
- The matching rule skips the defect-class check for `qa` labels entirely,
  because `BugReport` has no category field. The Group C and D class counts
  are a stratification tool for authoring variety, not a dimension the runner
  can score.
- `severity` is never compared by the matching rule, so nothing here measures
  whether a gate rates a correctly-found defect at the right severity.
- Free-text `qa` and `security` locations frequently resolve to a bare
  basename, which by the matching rule never matches a fuller relative path.
  These cases use true repository paths, so recall for those two gates will
  read pessimistically. That is a real property of the gates' output shape,
  not an artifact to be tuned away by shortening paths.
- Thirteen cases are invented rather than drawn from history — a structurally
  weaker grade of evidence. Each states in its `origin.note` why no real
  example was available.
