"""Collect real answers from the target, with the passages each was built from.

Writes `docs/measurements/answers.json`, the raw material for measuring a grounding
threshold. Questions come from the target's own regression suite, so each is known to be
answerable.

Needs the target running with its eval envelope exposed: `./run.sh --eval`.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

TARGET = "http://localhost:8000"

# Chosen to span the branches, because they fail differently: hybrid retrieval over
# documents, the SQL branch (which retrieves rows and so has no passages to ground
# against), and a role refusal (which retrieves nothing at all).
CORPUS = [
    # Taken from the target's own regression suite, so each one is known to be answerable
    # from the indexed corpus. Questions invented for this measurement mostly came back
    # "not found", which measures the relevance gate rather than the guardrail.
    ("doctor", "What is the standard dose of meropenem?"),
    ("doctor", "What is the dose of ivermectin for scabies?"),
    ("doctor", "Meropenem 1 g every 8 hours, which formulary tier is that?"),
    ("doctor", "What are the ECG interpretation flags?"),
    # These two carry an ICD-10 code in the answer, which is what the diagnosis_code
    # regex is shaped to find. If it fires here it is corrupting a correct answer.
    ("doctor", "What is the diagnosis code N17.9 used for?"),
    ("doctor", "Which protocol covers ICD-10 I21.4?"),
    ("nurse", "What is the hand hygiene protocol before entering the ICU?"),
    ("nurse", "Which IV cannula size for a paediatric patient under 5 kg?"),
    ("technician", "What does fault code F-05 mean on the infusion pump?"),
    ("technician", "How do I run a sterilisation cycle on the autoclave?"),
    ("technician", "What is the preventive maintenance interval for the ventilators?"),
    ("billing_executive", "How do I submit a cashless claim?"),
    ("billing_executive", "What is the billing code for an MRI scan?"),
    ("nurse", "How many days of casual leave do staff get?"),
    # The SQL branch: rows, not passages, so it has nothing to ground against.
    ("billing_executive", "How many claims were escalated in March 2024?"),
    ("billing_executive", "Which insurer has the most approved claims?"),
    # A refusal, which must still be safe to show.
    ("nurse", "What are the insurance billing codes for an MRI?"),
]


PASSWORDS = {
    "doctor": ("dr.mehta", "dr.mehta-demo"),
    "nurse": ("nurse.priya", "nurse.priya-demo"),
    "technician": ("tech.anand", "tech.anand-demo"),
    "billing_executive": ("billing.ravi", "billing.ravi-demo"),
}


def ask(client, question: str, token: str) -> dict:
    """One question, retried through the target's rate limit.

    The target answers 503 when its model provider's per-minute token cap is reached. That
    is a queueing problem, not a failure: waiting and asking again gets the same answer.
    """
    for attempt in range(6):
        response = client.post(
            "/chat",
            json={"question": question},
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code != 503:
            response.raise_for_status()
            return response.json()
        wait = 20 * (attempt + 1)
        print(f"      rate limited, waiting {wait}s")
        time.sleep(wait)
    raise RuntimeError(f"still rate limited after 6 attempts: {question}")


def main() -> None:
    path = Path("docs/measurements/answers.json")
    # Resume rather than restart. Every answer costs tokens against a daily cap, and the
    # ones already collected are not improved by being collected again.
    done = {}
    if path.exists():
        done = {(r["role"], r["question"]): r for r in json.loads(path.read_text())}
        print(f"  {len(done)} already collected, keeping them\n")

    out = []
    with httpx.Client(base_url=TARGET, timeout=120) as client:
        tokens = {}
        for role, (user, password) in PASSWORDS.items():
            response = client.post("/login", json={"username": user, "password": password})
            response.raise_for_status()
            tokens[role] = response.json()["token"]

        for role, question in CORPUS:
            if (role, question) in done:
                out.append(done[(role, question)])
                continue
            body = ask(client, question, tokens[role])
            contexts = [c["text"] for c in body.get("eval", {}).get("contexts", [])]
            out.append({
                "role": role,
                "question": question,
                "answer": body["answer"],
                "contexts": contexts,
                "refusal": body.get("refusal"),
                "retrieval_type": body.get("retrieval_type"),
            })
            mark = body.get("refusal") or f"{len(contexts)} passages"
            print(f"  {role:18} {question[:46]:48} {mark}")
            # A gap between questions, because the cap is per minute and the alternative
            # is backing off after every second call.
            time.sleep(8)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2))
    print(f"\nwrote {path} — {len(out)} answers, "
          f"{sum(1 for r in out if r['contexts'])} with passages")


if __name__ == "__main__":
    main()
