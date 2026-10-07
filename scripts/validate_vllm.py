"""Reproducible real-source acceptance check; no vLLM dependencies required."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import subprocess
import sys
import time


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    engine = Path(__file__).resolve().parents[1] / "backend" / "run.py"
    process = subprocess.Popen(
        [sys.executable, "-u", str(engine), "--db", str(args.db.resolve())],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8",
    )
    sequence = 0
    progress = 0

    def request(method: str, **params: object) -> object:
        nonlocal sequence, progress
        sequence += 1
        assert process.stdin and process.stdout
        process.stdin.write(json.dumps({"id": sequence, "method": method, "params": params}) + "\n")
        process.stdin.flush()
        while line := process.stdout.readline():
            message = json.loads(line)
            if message.get("method") == "progress":
                progress += 1
                continue
            if message.get("id") != sequence:
                raise RuntimeError(f"Unexpected response: {message}")
            if "error" in message:
                raise RuntimeError(message["error"])
            return message["result"]
        raise RuntimeError(f"Backend stopped: {process.poll()}")

    checks: dict[str, bool] = {}
    evidence: dict[str, object] = {}
    started = time.perf_counter()
    try:
        summary = request("index", root=str(root))
        classes = request("symbols", kind="class", query="Scheduler", limit=1000)
        assert isinstance(classes, list)
        scheduler = next(s for s in classes if s["qualifiedName"].endswith("v1.core.sched.scheduler.Scheduler"))
        detail = request("inspect", id=scheduler["id"])
        checks["find_scheduler"] = bool(scheduler)
        children = detail["children"]
        checks["members_methods_and_file"] = (
            any(s["kind"] == "member" and s["name"] == "kv_cache_manager" for s in children)
            and any(s["kind"] == "method" and s["name"] == "schedule" for s in children)
            and Path(scheduler["file"]).is_file()
        )
        targets = {s["id"]: s for s in detail["related"]}
        owns = [e for e in detail["outgoing"] if e["type"] == "owns"]
        checks["scheduler_owns_kv_cache_manager"] = any(
            targets.get(e["targetId"], {}).get("name") == "KVCacheManager" for e in owns
        )
        inheritance = request("graph", id=scheduler["id"], types=["inherits"], depth=1)
        checks["inheritance_includes_focus"] = any(n["id"] == scheduler["id"] for n in inheritance["nodes"])
        schedule = next(s for s in children if s["name"] == "schedule" and s["kind"] == "method")
        graph = request("graph", id=schedule["id"], types=["calls"], direction="out", depth=1, limit=300)
        checks["schedule_call_graph"] = bool(graph["edges"])
        allocate = [s for s in graph["nodes"] if s["qualifiedName"].endswith("KVCacheManager.allocate_slots")]
        checks["allocate_slots_resolved"] = bool(allocate)
        checks["allocate_slots_source_location"] = bool(allocate) and all(
            Path(s["file"]).is_file()
            and "def allocate_slots(" in "\n".join(Path(s["file"]).read_text(encoding="utf-8").splitlines()[s["startLine"] - 1:s["startLine"] + 3])
            for s in allocate
        )
        checks["search_kv_cache_manager"] = any(
            s["name"] == "KVCacheManager" for s in request("symbols", query="KVCacheManager", kind="class")
        )
        context = request("context", file=schedule["file"], line=schedule["startLine"] + 1)
        checks["current_symbol_context"] = context is not None and context["id"] == schedule["id"]
        checks["progress_from_separate_process"] = progress > 0
        evidence = {"scheduler": scheduler, "schedule": schedule, "allocateSlots": allocate,
                    "schedulerOwns": owns, "callEdges": graph["edges"], "graphTruncated": graph["truncated"]}
        for name in ["EngineCore", "Executor"]:
            found = request("symbols", query=name, kind="class")
            checks[f"find_{name}"] = bool(found)
            evidence[name] = [{k: s[k] for k in ("id", "qualifiedName", "file", "startLine")} for s in found[:10]]
        commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        report = {"source": str(root), "commit": commit, "platform": platform.platform(),
                  "python": sys.version, "elapsedSeconds": round(time.perf_counter() - started, 3),
                  "summary": summary, "checks": checks, "evidence": evidence}
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"checks": checks, "summary": summary, "report": str(args.report)}, ensure_ascii=False, indent=2))
        request("shutdown")
        process.wait(timeout=10)
        if not all(checks.values()):
            sys.exit(1)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


if __name__ == "__main__":
    main()
