# Guardrail & Evaluation Pipeline

A safety and quality layer that sits **in front of** a chatbot and answers three questions
on every request:

- *Is this prompt safe to process?*
- *Is this answer safe to show?*
- *Is answer quality holding up over time?*

It does not replace the chatbot and it does not live inside it. It stands between the user
and the system, so the checks cannot be skipped, and it keeps a record of what it decided
so that "did this leak?" is a log query instead of an afternoon.

Its first target is **[MediBot](https://github.com/LijiAlex/medibot)**, a role-aware
hospital assistant. MediBot is the thing being watched, not part of this project.

---

## Why a separate layer

Three incidents motivate the design:

1. An assistant answered a dosage question with a number it invented, and nobody could
   reconstruct what it had actually retrieved before answering.
2. A ticket claimed a chatbot showed billing data to a non-billing role, and there was no
   way to confirm or rule it out.
3. A model was swapped, answer quality dropped, and it took two weeks of complaints for
   anyone to notice.

None of those is fixed by a better chatbot. They are fixed by something *outside* it that
watches, records and blocks — and by that something being independent, because a system
cannot certify itself.

---

## Status

| Phase | What it adds | State |
|---|---|---|
| **A0** | Repo, the target contract, two adapters, the proxy | **done** — 73 tests |
| A1 | MediBot returns the passages it retrieved, its token usage and its timings | next |
| A2 | MediBot's internal steps join this pipeline's trace | planned |
| A3 | MediBot's own UI routed through here, so the guardrails cannot be bypassed | planned |
| B1–B3 | The guardrails themselves, input and output, on AWS Bedrock | planned |
| C1–C2 | Tracing and a structured event log | planned |
| D1–D5 | Labelled evaluation set, heuristics, RAGAS, an LLM judge, online sampling | planned |
| E1–E2 | The consolidated report, and this README completed | planned |

**Today the pipeline forwards and translates. It does not yet block anything.** A0 exists
so that each later step is a small change to something that already runs and has tests,
rather than a large thing invented all at once.

---

## How a request flows

```
  browser / curl
        │  POST /chat   Authorization: Bearer <token>
        ▼
  ┌──────────────── this pipeline (:9000) ────────────────┐
  │  1. [B2] input guardrail   ← not built yet              │
  │  2. ask the target, forwarding the token ┐              │
  │  3. [B3] output guardrail  ← not built yet              │
  │        uses the role the target reported │              │
  │  4. render the answer in the target's shape             │
  └─────────────────────────────────────────┼──────────────┘
                                            ▼
                              ┌──── MediBot (:8000) ────┐
                              │  retrieve → rerank →    │
                              │  generate               │
                              └─────────────────────────┘
```

Two things worth knowing about that picture:

**The pipeline never imports the target.** It speaks HTTP through an *adapter* — a small
class that knows one system's endpoints and field names. Nothing else in the codebase
knows MediBot exists.

**Identity comes from the target, not from the caller.** The caller's token is forwarded
untouched, because only the target can verify its own credentials — the pipeline never
inspects it and holds no user database. The role the target reports *with its answer* is
what the output guardrail uses, so a client cannot claim to be an admin by saying so.

The input guardrail runs before any of that, and does not need to know who is asking:
prompt injection, off-topic abuse and role-override attempts are all properties of the
text being sent.

> **principal** — who is asking, in whatever words the target uses. MediBot calls them
> roles (`doctor`, `nurse`, `billing_executive`…). The pipeline says "principal" because
> not every system has roles, and those that do spell it differently.

---

## The other half: evaluation

Guardrails run on **every request**. Evaluation does not — it is a script you run, against
a fixed set of questions whose right answers are known in advance. A real user's question
has no known right answer, so there is nothing to score it against.

```
  evaluation set          ≥15 labelled questions: normal, adversarial, role-refusals,
        │                 and one deliberately wrong-but-confident answer
        ▼
  for each question
        │
        ├──► service.handle(...)      ← the SAME guarded path the API uses,
        │                               so this evaluates the guarded system
        ▼
  ┌─────────────────────────────────────────────────────────────┐
  │  1. heuristics     rules, no model, run FIRST so a broken    │
  │                    system fails before any tokens are spent  │
  │                    e.g. every answer cites a source; a       │
  │                    restricted query is actually refused      │
  │                                                              │
  │  2. RAGAS          faithfulness · answer relevancy ·         │
  │                    context precision · context recall        │
  │                    (did it invent things? did retrieval      │
  │                     find the right passages?)                │
  │                                                              │
  │  3. LLM judge      a different model, on a different         │
  │                    provider, scoring against a written       │
  │                    rubric and explaining its reasoning       │
  └─────────────────────────────────────────────────────────────┘
        │
        ▼
  one report      per-metric scores against a pass threshold, which specific
                  checks failed, and how many requests the guardrails blocked
```

**Why the judge is a different model on a different provider.** A system grading its own
output is the problem this project exists to solve. The target answers on Groq; the judge
runs Claude on AWS Bedrock. Different model, different provider, different company.

**Why the runner shares `service.handle` rather than calling the target directly.** If it
went straight to the target, it would be evaluating an unguarded system and reporting
scores for something users never see.

### Three cadences, not two

| | when it runs | the question it answers |
|---|---|---|
| **Guardrails** | every request, inline | is *this* answer safe to show? |
| **Evaluation** | on demand — before a release, after a model swap | is quality holding up? |
| **Online sampling** *(D5)* | a small share of live traffic, scored after the answer is sent | has quality drifted without anyone noticing? |

The third is the answer to incident 3 above: a model was swapped, quality dropped, and
two weeks passed before anyone knew.

---

## Quick start

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

**1. Start the target.** In the MediBot repository — it has its own setup first (a Groq
API key, and a one-off step to build its search index; see its README):

```bash
cd backend && uv run uvicorn medibot.api.app:app --port 8000
```

**2. Start the pipeline**, in a second terminal:

```bash
uv run uvicorn guardrail_eval_pipeline.api.app:app --port 9000
```

```bash
curl localhost:9000/health
# {"status":"ok","target":"medibot","endpoint":"http://localhost:8000"}
```

**3. Sign in and ask something.** `/chat` requires a token; there is no anonymous path.

```bash
TOKEN=$(curl -s -X POST localhost:9000/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"nurse.priya","password":"nurse.priya-demo"}' | jq -r .token)

curl -s -X POST localhost:9000/chat \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"question":"Show me all insurance billing codes"}' | jq
```

```json
{
  "answer": "This looks like a question for billing documents, which a nurse cannot read...",
  "sources": [],
  "retrieval_type": "hybrid_rag",
  "role": "nurse",
  "sql": null,
  "refusal": "role"
}
```

MediBot ships five demo accounts. Each password is the username plus `-demo`:

| Principal | Username | Can read |
|---|---|---|
| `doctor` | `dr.mehta` | general, clinical, nursing |
| `nurse` | `nurse.priya` | general, nursing |
| `billing_executive` | `billing.ravi` | general, billing — and the claims database |
| `technician` | `tech.anand` | general, equipment |
| `admin` | `admin.sys` | everything |

> **The first question will take around 17 seconds.** MediBot loads two models on its
> first request. Subsequent questions take under a second. Nothing is wrong.

That refusal comes from **MediBot**, not from this pipeline — a nurse may not read billing
documents, and the target enforces that itself. Once the guardrails are built, a block by
*this* pipeline will look different: a generic answer and `"refusal": "blocked"`, with the
reason written only to the log.

### Endpoints

| | |
|---|---|
| `GET /health` | Is the pipeline running, and which target is wired in |
| `POST /login` | Forwarded to the target unchanged. Exists so a browser has one address for the whole API |
| `GET /collections/{role}` | Forwarded to the target |
| `POST /chat` | The guarded path. Requires `Authorization: Bearer <token>` |

`/chat` answers in **the target's own response shape**, so MediBot's existing UI keeps
working when it is pointed here. Internally everything speaks one generic shape; the
target's vocabulary is spoken only at the edge.

### Configuration

Everything has a working default; nothing needs setting to run the quick start.

| Variable | Default | What it does |
|---|---|---|
| `GEP_TARGET_CONFIG` | `targets/medibot.yaml` | Which system to watch. Point it at another file to switch targets |
| `GEP_ALLOWED_ORIGINS` | `http://localhost:3000` | Comma-separated browser origins allowed to call this pipeline |
| `MEDIBOT_PASSWORD_SUFFIX` | from the target's YAML | Overrides the demo-password convention without editing the config file |

The target's address, its accounts and its capabilities live in
`targets/<name>.yaml` rather than in environment variables, because they describe a
system rather than a deployment.

### Tests

```bash
.venv/bin/python -m pytest        # 73 tests, under a second
```

They run with no target, no model and no network — see *StubTarget* below.

---

## Wiring up a different system

The pipeline is not MediBot-specific. Supporting another chatbot means two files and one
line, and no change to any guardrail, evaluator or report.

**1. A config file**, `targets/<name>.yaml`:

```yaml
name: scheduling
adapter: scheduling
endpoint: http://scheduling.internal:8080
principals:
  clinician: svc-clinician      # key = what the target reports as its principal
```

**2. An adapter**, `src/guardrail_eval_pipeline/adapters/<name>.py`, translating that
system's answers into the shared shape:

```python
class SchedulingTarget:
    name = "scheduling"

    def ask(self, question, *, principal=None, token=None, trace_headers=None):
        body = self._post(question, token)
        return TargetResponse(
            answer=body["reply"],                                  # it calls it "reply"
            contexts=[Retrieved(text=c) for c in body["snippets"]],
            citations=body.get("refs", []),
            refused=body.get("declined", False),
            raw=body,
        )
```

**3. One line** in `adapters/__init__.py`:

```python
ADAPTERS = {"medibot": MediBotTarget, "scheduling": SchedulingTarget}
```

### Optional capabilities

`ask` is the only required method. Three more are optional, and a target that omits one
has that capability reported as **unavailable** rather than quietly passing:

| Method | Gives you | Without it |
|---|---|---|
| `render(result, *, withhold)` | answers in the target's own shape, so its UI keeps working | a generic shape is returned instead |
| `forward(method, path, ...)` | `/login` and `/collections` passthrough | those endpoints answer 501 |
| `allowed_scopes(principal)` | the access-zone leak check | that check reports unavailable |

**Honest limit:** the eval set can never be generic. Two of the four RAGAS metrics need
reference answers, and those are ground truth for one specific system.

---

## Design notes

**Ground truth lives here, not in the target's repository.** If the questions and expected
answers sat beside the system being judged by them, it would be writing its own exam —
which is the problem this project exists to solve.

**The target knows how to be asked; the pipeline decides what correct means.** Transport,
authentication and response shape belong to the adapter. Questions, thresholds and
expected answers belong here.

**A missing capability is reported, never scored zero.** A zero meaning "we could not
look" is indistinguishable from one meaning "it failed", and only one of those is the
target's fault.

**`StubTarget`** is a second adapter that answers from a dictionary. It is not a demo: it
lets every component be tested with no target running, no tokens spent against a daily
cap, and no contention for MediBot's embedded vector store, which allows one process at a
time. It is also what shows the shared contract is not MediBot's shape under another name.

---

## Troubleshooting

**`/chat` returns 401.** There is no anonymous path. Sign in at `/login` first and send
the token as `Authorization: Bearer <token>`.

**`/chat` or `/login` returns 502.** The pipeline is running but the target is not
reachable. Check `GET /health` for the endpoint it is trying, and that the target is up.

**`/chat` returns 401 after working for a while.** The target's token expired. Sign in
again. Tokens the pipeline mints for itself are renewed automatically; a caller's own
token is not, because only its owner can replace it.

**The target will not start, or its tests fail while it is running.** MediBot uses an
embedded vector store that allows one process at a time. Stop its server before running
its test suite, and vice versa. This pipeline's own tests are unaffected — they use
`StubTarget` and never touch it.

**The first request takes about 17 seconds.** See the note above: the target is loading
its models. It is not a hang.

---

## Layout

```
targets/<name>.yaml                  which systems we watch, and how to reach them
src/guardrail_eval_pipeline/
  contracts.py                       the shared vocabulary every component reads
  config.py                          reads a target's YAML
  service.py                         the guarded path, shared by the API and the eval runner
  adapters/<name>.py                 how to talk to one system, and translate its answers
  api/app.py                         the HTTP front door
tests/                               73 tests, all offline
```

---

## Still to come

These sections are required for submission and will be filled in as the phases land:

- **Adversarial guardrail test cases**, with the verdicts the guardrails actually returned
- **A sample evaluation report**, taken from a real run rather than written by hand
- **Tool substitutions**, and why each was made
