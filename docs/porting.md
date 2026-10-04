# Wiring up a different system

The pipeline is not MediBot-specific. See the [README](../README.md) for what it does and
[design.md](design.md) for why.

---


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

**The evaluation set is per-target.** Two of the four RAGAS metrics need reference
answers, and those are ground truth for one specific system.

---
