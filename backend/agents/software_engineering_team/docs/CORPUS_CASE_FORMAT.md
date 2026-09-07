# SE Review Gate Corpus Case and Finding-Label Format

## Purpose

A golden-set evaluation harness (#7578) needs a corpus of diffs with
labeled expected findings, and a scorer that can tell whether a gate's
finding matches a label. Both are unusable unless two people labeling the
same diff produce the same labels. This document specifies:

1. The **case format** — what a corpus entry is.
2. The **finding-label schema** — how an expected finding is written down.
3. A **closed defect-class vocabulary**, justified line by line against
   [`GATE_FINDING_INVENTORY.md`](GATE_FINDING_INVENTORY.md) (the factual
   catalogue of what the code-review, QA, security, and
   `false_positive_filter` gates actually emit today).
4. Named exclusions — inventory content deliberately left out, and why.
5. Two worked examples.

**Out of scope for this document:** the matching rule that decides whether
a produced finding satisfies a label (file/line tolerance, defect-class
equivalence across gates) is a separate, later specification. So is
elaborating false-positive-resistance patterns beyond the `must_not_find`
polarity defined below, and building the corpus itself. No gate prompt,
model, or logic changes accompany this document.

## Why a flat "one taxonomy" design doesn't work

The inventory establishes four facts that shape everything below:

- Code review is the **only** in-scope gate with a closed, LLM-enforced
  defect-category enum (13 values) and a reliably structured `file_path` +
  `line`/`start_line`. Its own real example labels a SQL-injection defect as
  `category: "logic"` — the enum has **no `security` value at all**
  (`GATE_FINDING_INVENTORY.md` §1).
- QA's `BugReport` has **no category field whatsoever** — only prose-named
  bug patterns in its prompt — and **no numeric line field ever**; only
  `fix_build` mode even gets a structured `file_path` (§2).
- Security's `SecurityVulnerability.category` is **free text**, not a closed
  enum, and `location` is a single free-text field with **no structured
  file_path or line field at all** (§3).
- `false_positive_filter` **emits no finding shape of its own** — it only
  removes entries from the code-review gate's own `CodeReviewIssue` list,
  and never relabels or modifies a surviving finding (§4).

Consequently: a single "defect class = the gate's own category string"
design fails for QA and security (neither has a closed one), and a single
location schema fails too (only code review is reliably file+line
addressable). The design below resolves both by keying every label to a
single emitting gate, and by treating the label's location as ground truth
authored from the fixture rather than a copy of what a gate would output.

## 1. Case format

A case is one corpus entry: an identifier, the file set or diff under
review, its language/stack, which gates it targets, and its list of
expected findings.

```yaml
case_id: CASE-0001          # stable identifier — see "Case identifier policy" below
title: <short human summary>
language: python             # or typescript, etc.
stack: fastapi                # free text, e.g. fastapi, angular, temporal-worker
gates: [code_review, security]   # subset of {code_review, qa, security} — which gates this case scores
mode: diff                    # "diff" or "files" — see below
origin:                       # where the case came from — see "Provenance" below
  sourcing: real              # real | invented
  commit: 0040820             # short SHA of the origin commit; required when sourcing: real
expected_findings:
  - <label>                    # zero or more; see §2
  - <label>
```

- **`mode: diff`** — the file set is expressed as a unified diff
  (`diff.patch`) applied against an implicit or included baseline. Preferred
  for code-review-targeted cases, since the code-review gate's own
  `pre_existing`/`omission` fields exist specifically to distinguish
  in-diff findings from pre-existing ones (`GATE_FINDING_INVENTORY.md` §1)
  — a diff-shaped case is what makes that distinction meaningful.
- **`mode: files`** — the file set is a plain tree of full file contents
  (`files/`). Use this when a case is about a gate's judgment on code as it
  stands, not about what changed.

Every gate listed in `gates` is scored for this case; `gates` need not
equal the set of gates referenced by `expected_findings`. A gate listed
with no corresponding labels means that gate is expected to report nothing
on this fixture (a clean-fixture / false-positive-resistance case for that
gate), not that the gate is unscored. The reverse direction is a hard
constraint, not a convention: every gate referenced by a label in
`expected_findings` **must** appear in `gates` — a label whose `gate` is
not listed makes the case malformed, since `gates` is the case's own
declaration of "which gates this case scores."

### Physical layout

Not created by this document (building the corpus is #7587's job), but
specified here so that story has a fixed target:

```
backend/agents/software_engineering_team/eval_corpus/
  cases/
    CASE-0001/
      case.yaml       # the case body above (expected_findings omitted here when split out)
      labels.yaml       # optional: expected_findings as a top-level list of labels, same shape as §2
      diff.patch         # mode: diff
      # or
      files/              # mode: files
        app/main.py
```

`labels.yaml`, when present, holds exactly the `expected_findings` list —
`case.yaml` then omits that key. This split is optional; a case is free to
keep `expected_findings` inline in `case.yaml` instead.

YAML is chosen because it is diff-friendly line by line in a pull-request
review, matching the "human-reviewable in a PR" requirement.

### Provenance

`origin` is a required top-level field. It records where the case's fixture
came from, so a reader can tell evidence grounded in a defect this repository
actually shipped from a plausible construction, without leaving the case file:

- **`sourcing`** is `real` or `invented`.
- **`commit`** is the short SHA of the origin commit and is required when
  `sourcing: real`, omitted otherwise. That commit is the *fix* — the ground
  truth for what the defect was and where it lived. A real case's `diff.patch`
  is therefore the inverse of that commit, reduced to the files carrying the
  defect, and its labels' line numbers are positions in the post-diff file
  (the state of the file at the fix commit's parent). A real case stays pinned
  to its origin commit; it is not resynchronized as the repository moves on.
- **`note`** is required when `sourcing: invented` and states why no real
  example was available — a search that came back empty, or a structural
  reason the class cannot survive in merged history. "No example was found"
  alone is not a reason.

A case whose fixture is authored rather than derived is not second-class, but
it is a weaker grade of evidence, and this field is what keeps that difference
visible to anyone reading a metric computed over the corpus.

### Case identifier policy

- `case_id` is `CASE-NNNN`, zero-padded, assigned sequentially at creation.
- Once merged, a `case_id` is **never reused or renumbered**, even if the
  case is later retired — a retired case is deleted or marked, but its ID
  is not recycled onto a different case. This is what lets a metrics report
  name the exact case that regressed, permanently.

## 2. Finding-label schema

A label is a single expected finding **for a single gate**. When the same
real defect is one that multiple gates are expected to catch, it gets one
label per gate — because, as the facts above show, the gates don't share a
defect taxonomy, so the "same" defect is described differently by each
gate's own real output (see the worked multi-gate example in §5).

```yaml
label_id: L1                       # stable within the case (not globally unique)
gate: code_review                   # one of: code_review | qa | security
defect_class: logic                 # closed vocabulary — see §3
severity: critical                  # critical | high | medium | low | info — required when polarity: must_find, must be omitted when polarity: must_not_find
polarity: must_find                 # must_find | must_not_find
file_path: app/main.py              # required — ground truth location, authored from the fixture
line: 42                            # int, or null for a file-wide/structural finding
line_end: null                      # int, for a line range/span; null when the finding is single-line
description: >
  SQL built via f-string concatenation of the `user_id` query param;
  parameterize the query.
```

Field notes:

- **`gate`** is a closed 3-value enum: `code_review`, `qa`, `security`.
  `false_positive_filter` is not a value here — see §4.
- **`defect_class`** must be one of the closed vocabulary values in §3. It
  is required on **both** polarities, unlike `severity`. On a
  `must_not_find` label, `defect_class` names the class a false positive
  about this region would be filed under — i.e., the class the gate must
  not assign here — not the class of the benign code actually present.
- **`severity`** uses one vocabulary across all three gates —
  `critical | high | medium | low | info` — because that union is
  consistent with what all three gates actually document
  (`GATE_FINDING_INVENTORY.md` §1 `CodeReviewIssueSeverity`, §2 prompt
  set, §3 prompt set). It is **required** on a `must_find` label and
  **must be omitted** (not merely optional) on a `must_not_find` label,
  since there is no finding whose severity to state.
- **`file_path`** / **`line`** / **`line_end`** are always present,
  regardless of which gate the label targets. They describe where the
  ground-truth defect (or decoy) actually is in the fixture — the label is
  authored by whoever wrote the fixture, not derived from a gate's output.
  `line: null` denotes a file-wide/structural finding with no single anchor
  line, mirroring the inventory's own documented semantics for
  `CodeReviewIssue.line` (§1). `line_end` sets an inclusive span: for a
  single-line finding, `line_end` equals `line`; when `line` is `null`
  (file-wide/structural), `line_end` is also `null`. Whenever `line` is
  non-null, `line_end >= line`.

  This is a deliberate difference from code review's own field naming
  (`line` + `start_line`, where the inventory flags that `line` acts as the
  *end* line of a span — §1, "Location fields"): the corpus schema uses
  the unambiguous `line`/`line_end` instead of reproducing that inversion.
  Mapping the corpus's clear fields onto each gate's own native fields
  (including QA's and security's free-text locations, which the inventory's
  "Constraint on matching" notes say cannot be reliably parsed to file+line
  — §2, §3) is the matching rule's job, not this schema's.
- **`description`** is a human-readable statement of the ground truth (for
  `must_find`) or of why the region is a deliberate decoy (for
  `must_not_find`). It is not matched against a gate's finding text — that
  would reintroduce prose-similarity matching, which the parent epic (#7586)
  explicitly rules out.

## 3. Closed defect-class vocabulary

31 values, built as a justified union across the three in-scope gates — not
invented independently of them. Each entry cites the inventory section it
is drawn from.

### Group A — code review's own closed category enum (12 of 13 values)

Verbatim from `_ChunkReviewIssueCategory` (`GATE_FINDING_INVENTORY.md` §1),
**excluding `general`** (see §4).

| class | inventory citation |
|---|---|
| `naming` | §1, `_ChunkReviewIssueCategory` |
| `structure` | §1, `_ChunkReviewIssueCategory` |
| `logic` | §1, `_ChunkReviewIssueCategory`; real example: SQL-injection finding tagged `category: "logic"` |
| `spec-compliance` | §1, `_ChunkReviewIssueCategory` |
| `standards` | §1, `_ChunkReviewIssueCategory` |
| `integration` | §1, `_ChunkReviewIssueCategory` |
| `testing` | §1, `_ChunkReviewIssueCategory` |
| `architecture` | §1, `_ChunkReviewIssueCategory`; also the architecture-consistency pass's restricted 2-value set |
| `refactor` | §1, `_ChunkReviewIssueCategory`; also the architecture-consistency pass's restricted set |
| `maintainability` | §1, `_ChunkReviewIssueCategory` |
| `side-effects` | §1, `_ChunkReviewIssueCategory`; also the side-effect pass's restricted 2-value set |
| `documentation` | §1, `_ChunkReviewIssueCategory`; also the side-effect pass's restricted set |

### Group B — security's named vulnerability classes (7 values)

From the security agent's **"Your expertise"** prompt block
(`GATE_FINDING_INVENTORY.md` §3), which the inventory distinguishes from
its separate "Methodology" block (attack surfaces to walk, not defect
kinds — see §4).

| class | inventory citation |
|---|---|
| `injection` | §3, expertise block: "injection (SQL/NoSQL/command)"; real example: `category: "injection"`, "Command injection in run()" |
| `xss` | §3, expertise block |
| `csrf` | §3, expertise block |
| `auth` | §3, expertise block: "authentication/authorization flaws" |
| `crypto` | §3, expertise block: "cryptographic issues (weak algorithms, hardcoded secrets)" |
| `insecure-deserialization` | §3, expertise block |
| `ssrf` | §3, expertise block |

### Group C — QA's prose-named bug patterns (8 values)

From `qa_agent/prompts.py:39-48` as catalogued in
`GATE_FINDING_INVENTORY.md` §2. QA has no category field, so this
grounding is prompt-line citation rather than a validated enum — a weaker
grade of evidence than Groups A and B, stated here explicitly rather than
overstated as equivalent.

| class | inventory citation |
|---|---|
| `off-by-one` | §2: "off-by-one errors" |
| `race-condition` | §2: "race conditions" |
| `resource-leak` | §2: "resource leaks" |
| `null-deref` | §2: "null/None dereferencing" |
| `integer-overflow` | §2: "integer overflow/type coercion" |
| `unvalidated-input` | §2: "unvalidated external input" |
| `missing-error-handling` | §2: "missing I/O error handling" |
| `inconsistent-state` | §2: "inconsistent state after partial failure" |

QA's prompt also names "SQL injection via string formatting" (§2). This is
deliberately **not** a ninth Group C value — it maps onto Group B's
`injection`; a `gate: qa` label uses `defect_class: injection` for that
pattern.

### Group D — QA's `fix_build`-mode root causes (4 values)

From `qa_agent/prompts.py:100` as catalogued in
`GATE_FINDING_INVENTORY.md` §2.

| class | inventory citation |
|---|---|
| `missing-import` | §2, `fix_build` mode root causes |
| `wrong-path` | §2, `fix_build` mode root causes |
| `type-error` | §2, `fix_build` mode root causes |
| `syntax-error` | §2, `fix_build` mode root causes |

A `gate: qa` label using a Group D class implies the case is exercising
QA's `fix_build` mode specifically.

## 4. Named exclusions

Every exclusion below is inventory content that was considered and left
out of the vocabulary or the `gate` enum, with a reason.

| excluded | reason |
|---|---|
| `general` category (code review) | Code review's own overflow bucket (`GATE_FINDING_INVENTORY.md` §1), not a distinguishable defect kind. A curated label should always name something specific; the raw gate may still emit `general` in production, but the corpus never assigns it as ground truth. |
| Lint gate (`linting_tool_agent` / `LintIssue`) | The inventory itself scopes this out ("a fifth finding-producing gate not inventoried here", §"Purpose"). Its `rule` field is an open, tool-defined namespace (arbitrary linter rule codes), not closed-enumerable. Deferred to a future revision if harness scope grows to include it. |
| devops's `change_review_agent` / `ReviewFinding` | Also scoped out by the inventory (§"Purpose"). Its shape is a field-renaming of the same code-review engine output (`devops_maintainability` profile), not a new taxonomy — no new class would be needed if a future revision adds this gate as its own `gate` enum value. |
| `false_positive_filter` as a `gate` value | It emits no finding shape of its own — it only removes entries from `CodeReviewIssue` and never modifies survivors (`GATE_FINDING_INVENTORY.md` §4). Its drop-precision metric is computed at the runner level from `code_review` labels plus the filter's keep/drop decision on the raw code-review output; no distinct label shape is needed. |
| "OWASP Top 10" as a class | An umbrella/reference term for a category list (§3, expertise block), not itself one defect kind. Its members are already enumerated individually in Group B. |
| "dependency CVEs" as a class | Named only in security's **Methodology** block as an attack surface to investigate (§3), not in its expertise block of vulnerability classes. A real dependency-CVE finding is filed under whichever Group B class it actually is (e.g. a vulnerable crypto library → `crypto`). |
| QA's `acceptance_evidence` mode | Per the inventory, that mode's output schema omits `bugs_found` entirely — "an `acceptance_evidence` run produces evidence records, not findings" (§2, quoted directly). Excluded from `gates` targeting; a case cannot expect findings from that mode. |
| `ArchitectureConsistencyFindingLLM` / `SideEffectImpactFindingLLM` as separate shapes | Both are target-only schemas not validated at runtime today (§1); their real, coerced output already lands in `CodeReviewIssue` with categories already covered by Group A (`architecture`/`refactor` and `side-effects`/`documentation` respectively). `gate: code_review` labels cover both passes without a new class. |

## 5. Worked examples

### Example 1 — single-gate case

```yaml
case_id: CASE-0001
title: Unclear helper name shadows a stdlib module
language: python
stack: fastapi
gates: [code_review]
mode: diff
expected_findings:
  - label_id: L1
    gate: code_review
    defect_class: naming
    severity: low
    polarity: must_find
    file_path: app/utils/json.py
    line: 1
    line_end: 1
    description: >
      New helper module named `json.py` shadows the stdlib `json` module
      for any sibling file that does `import json`.
```

`diff.patch` for `CASE-0001` (the file set under review — see "Physical
layout" in §1):

```diff
--- /dev/null
+++ b/app/utils/json.py
@@ -0,0 +1,4 @@
+import json as _stdlib_json
+
+def dumps_sorted(obj):
+    return _stdlib_json.dumps(obj, sort_keys=True)
```

### Example 2 — multi-gate case, same defect, one label per gate

Demonstrates the one-label-per-gate design: the same underlying SQL
injection is described three different ways because the three gates name
defects with three different vocabularies (`GATE_FINDING_INVENTORY.md`
§1, §2, §3).

```yaml
case_id: CASE-0002
title: User-supplied id concatenated into a raw SQL query
language: python
stack: fastapi
gates: [code_review, security, qa]
mode: diff
expected_findings:
  - label_id: L1
    gate: code_review
    defect_class: logic
    severity: critical
    polarity: must_find
    file_path: app/repositories/user_repo.py
    line: 27
    line_end: 27
    description: >
      Query built via f-string concatenation of `user_id` from the request
      path; parameterize the query.

  - label_id: L2
    gate: security
    defect_class: injection
    severity: critical
    polarity: must_find
    file_path: app/repositories/user_repo.py
    line: 27
    line_end: 27
    description: >
      SQL injection via unparameterized string interpolation of untrusted
      request input.

  - label_id: L3
    gate: qa
    defect_class: unvalidated-input
    severity: high
    polarity: must_find
    file_path: app/repositories/user_repo.py
    line: 27
    line_end: 27
    description: >
      `user_id` from the request path is used to build a query with no
      validation of its shape before it reaches the query builder.
```

`diff.patch` for `CASE-0002` (the file set under review — see "Physical
layout" in §1):

```diff
--- a/app/repositories/user_repo.py
+++ b/app/repositories/user_repo.py
@@ -26,3 +26,3 @@ class UserRepository:
     def find_by_id(self, user_id: str) -> Optional[User]:
-        query = "SELECT * FROM users WHERE id = %s"
-        return self._db.execute(query, (user_id,)).fetchone()
+        query = f"SELECT * FROM users WHERE id = {user_id}"
+        return self._db.execute(query).fetchone()
```

Line 26 (the `def` line) is unchanged context; line 27 in the new file is
the added `query = f"..."` line each of L1–L3 above points at.
