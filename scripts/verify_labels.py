"""Check the evaluation set against the target's own source documents.

    python scripts/verify_labels.py --chunks /tmp/chunks.json

Ground truth that nobody checked is just confident writing. Every `must_mention` fragment
has to appear in the document the case names, which catches the two mistakes that matter:
a fact written from memory, and a fact attributed to the wrong document.

The chunks come from the target, and are dumped with the target's own environment because
reading its store needs its storage library — which this pipeline deliberately does not
depend on. From the target repository, with it stopped:

    backend/.venv/bin/python -c "
    import json; from qdrant_client import QdrantClient
    pts, _ = QdrantClient(path='store/qdrant').scroll('medibot', limit=2000, with_payload=True)
    json.dump([{'source_document': p.payload['metadata'].get('source_document'),
                'text': p.payload['page_content']} for p in pts], open('/tmp/chunks.json','w'))"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from guardrail_eval_pipeline.dataset import load


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--set", default="evaluation/medibot.yaml")
    parser.add_argument("--chunks", default="/tmp/chunks.json")
    args = parser.parse_args()

    chunks = json.loads(Path(args.chunks).read_text())
    by_document: dict[str, str] = {}
    for chunk in chunks:
        document = chunk.get("source_document") or "?"
        by_document[document] = by_document.get(document, "") + "\n" + chunk.get("text", "")

    dataset = load(args.set)
    problems = 0
    for case in dataset.cases:
        if not case.must_mention:
            continue
        if case.source not in by_document:
            print(f"  MISSING DOC    {case.id}: names {case.source!r}, which is not indexed")
            problems += 1
            continue
        for fragment in case.must_mention:
            if fragment.lower() not in by_document[case.source].lower():
                print(f"  NOT IN SOURCE  {case.id}: {fragment!r} absent from {case.source}")
                problems += 1

    anchored = [c for c in dataset.cases if c.must_mention]
    print(f"{sum(len(c.must_mention) for c in anchored)} fragments across {len(anchored)} cases — "
          f"{'all traceable to their source' if not problems else f'{problems} problem(s)'}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
