# Wiring up a different system

Supporting another chatbot means two files and one line, and no change to any guardrail,
evaluator or report. See the [README](../README.md) for what the pipeline does and
[design.md](design.md) for how each layer works.

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
from guardrail_eval_pipeline.contracts import Retrieved, TargetResponse


class SchedulingTarget:
    name = "scheduling"

    # Adapters are built as ADAPTERS[config.adapter](config), so this signature is
    # part of the contract.
    def __init__(self, config):
        self.config = config

    def ask(self, question, *, principal=None, token=None, trace_headers=None):
        body = self._post(question, token)
        return TargetResponse(
            answer=body["reply"],                                  # it calls it "reply"
            contexts=[Retrieved(text=s["text"], scope=s.get("zone"))
                      for s in body["snippets"]],
            citations=body.get("refs", []),
            refused=body.get("declined", False),
            principal=body["role"],       # what the OUTPUT guardrail checks against
            grounded=True,                # the answer came from passages, so ground it
            raw=body,
        )
```

Two of those fields are easy to leave out and quietly weaken the layer. `principal` is what
the scope check compares a passage's zone against; `grounded` is what tells the output
guardrail the answer should have passages, without which grounding and the citation check
report unavailable instead of running. `scope` on each passage is optional — omit it and
the access-zone check reports unavailable rather than failing.

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

**The evaluation set is per-target.** Two of the four RAGAS metrics need reference
answers, and those are ground truth for one specific system.

---
