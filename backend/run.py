from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from backend.engine import Engine
else:  # pragma: no cover
    from .engine import Engine


def main() -> int:
    # The extension speaks UTF-8 regardless of the Windows console code page.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    args = parser.parse_args()

    def notify(payload: dict[str, object]) -> None:
        print(json.dumps({"method": "progress", "params": payload}, ensure_ascii=False), flush=True)

    engine = Engine(args.db, notify)
    for line in sys.stdin:
        if not line.strip():
            continue
        request_id = None
        try:
            request = json.loads(line)
            request_id = request.get("id")
            method = request.get("method")
            params = request.get("params") or {}
            if method == "index":
                result = engine.index(params)
            elif method == "summary":
                result = engine.summary()
            elif method == "symbols":
                result = engine.symbols(params)
            elif method == "inspect":
                result = engine.inspect(params)
            elif method == "context":
                result = engine.context(params)
            elif method == "graph":
                result = engine.graph(params)
            elif method == "moduleGraph":
                result = engine.module_graph(params)
            elif method == "classGraph":
                result = engine.class_graph(params)
            elif method == "explainContext":
                result = engine.explain_context(params)
            elif method == "shutdown":
                print(json.dumps({"id": request_id, "result": True}), flush=True)
                return 0
            else:
                raise ValueError(f"unknown method: {method}")
            print(json.dumps({"id": request_id, "result": result}, ensure_ascii=False), flush=True)
        except Exception as exc:
            print(json.dumps({"id": request_id, "error": {"message": str(exc)}}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

