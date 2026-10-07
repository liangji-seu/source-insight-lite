from __future__ import annotations

import os
import time
from collections import deque
from typing import Any, Callable

from .analyzer import Analyzer
from .class_graph import ClassGraph
from .module_graph import ModuleGraph
from .scanner import scan
from .storage import Store


class Engine:
    def __init__(self, db_path: str, progress: Callable[[dict[str, Any]], None] | None = None):
        self.store = Store(db_path)
        self.progress = progress

    def index(self, params: dict[str, Any]) -> dict[str, Any]:
        root = os.path.abspath(os.path.expanduser(str(params.get("root", ""))))
        if not os.path.isdir(root) and not (os.path.isfile(root) and root.endswith(".py")):
            raise ValueError(f"root is not a Python file or directory: {root}")
        started = time.perf_counter()

        def report(completed: int, total: int, file: str) -> None:
            if self.progress:
                self.progress({"completed": completed, "total": total, "file": file})

        units, errors = scan(root, params.get("excludes"), report)
        analysis = Analyzer(units).run()
        errors.extend(analysis.errors)
        elapsed = int((time.perf_counter() - started) * 1000)
        summary = {
            "root": root, "files": len(units), "symbols": len(analysis.symbols),
            "relationships": len(analysis.relationships), "errors": errors, "elapsedMs": elapsed,
        }
        self.store.replace(analysis.symbols, analysis.relationships, errors, summary)
        return summary

    def summary(self) -> dict[str, Any] | None:
        if not os.path.exists(self.store.db_path):
            return None
        with self.store.connect() as db:
            row = db.execute("SELECT value_json FROM meta WHERE key = 'summary'").fetchone()
            if not row:
                return None
            import json
            return json.loads(row[0])

    def symbols(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        limit = min(max(int(params.get("limit", 1000)), 1), 10000)
        offset = max(int(params.get("offset", 0)), 0)
        clauses: list[str] = []
        args: list[Any] = []
        if params.get("kind"):
            clauses.append("kind = ?"); args.append(str(params["kind"]))
        if "parentId" in params and params.get("parentId") is not None:
            clauses.append("parent_id = ?"); args.append(str(params["parentId"]))
        if params.get("file"):
            clauses.append("file = ? COLLATE NOCASE"); args.append(os.path.abspath(str(params["file"])))
        if params.get("query"):
            clauses.append("(name LIKE ? OR qualified_name LIKE ?)")
            value = f"%{params['query']}%"; args.extend([value, value])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.store.connect() as db:
            rows = db.execute(f"SELECT * FROM symbols{where} ORDER BY file, start_line, id LIMIT ? OFFSET ?",
                              (*args, limit, offset)).fetchall()
            return [self.store.symbol(row) for row in rows]

    def inspect(self, params: dict[str, Any]) -> dict[str, Any]:
        sid = str(params.get("id", ""))
        with self.store.connect() as db:
            symbol = self.store.symbol(db.execute("SELECT * FROM symbols WHERE id = ?", (sid,)).fetchone())
            if symbol is None:
                raise ValueError(f"unknown symbol id: {sid}")
            children = [self.store.symbol(row) for row in db.execute(
                "SELECT * FROM symbols WHERE parent_id = ? ORDER BY start_line, id", (sid,))]
            outgoing = [self.store.relationship(row) for row in db.execute(
                "SELECT * FROM relationships WHERE source_id = ? ORDER BY line, rowid", (sid,))]
            incoming = [self.store.relationship(row) for row in db.execute(
                "SELECT * FROM relationships WHERE target_id = ? ORDER BY line, rowid", (sid,))]
            ids = {sid}
            ids.update(r["targetId"] for r in outgoing if r["targetId"])
            ids.update(r["sourceId"] for r in incoming if r["sourceId"])
            placeholders = ",".join("?" for _ in ids)
            related = [self.store.symbol(row) for row in db.execute(
                f"SELECT * FROM symbols WHERE id IN ({placeholders}) AND id != ? ORDER BY file, start_line",
                (*ids, sid)).fetchall()] if len(ids) > 1 else []
            return {"symbol": symbol, "children": children, "outgoing": outgoing,
                    "incoming": incoming, "related": related}

    def context(self, params: dict[str, Any]) -> dict[str, Any] | None:
        file = os.path.abspath(str(params.get("file", "")))
        line = int(params.get("line", 1))
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM symbols WHERE file = ? AND start_line <= ? AND end_line >= ?",
                              (file, line, line)).fetchall()
            symbols = [self.store.symbol(row) for row in rows]
            return min(symbols, key=lambda x: (x["endLine"] - x["startLine"], -x["startLine"])) if symbols else None

    def graph(self, params: dict[str, Any]) -> dict[str, Any]:
        sid = str(params.get("id", ""))
        types = ([str(x) for x in params["types"]]
                 if "types" in params and params.get("types") is not None else None)
        direction = str(params.get("direction", "both"))
        depth = min(max(int(params.get("depth", 1)), 1), 3)
        limit = min(max(int(params.get("limit", 100)), 1), 300)
        with self.store.connect() as db:
            first = db.execute("SELECT * FROM symbols WHERE id = ?", (sid,)).fetchone()
            if first is None:
                raise ValueError(f"unknown symbol id: {sid}")
            nodes: dict[str, dict[str, Any]] = {sid: self.store.symbol(first)}
            edges: list[dict[str, Any]] = []
            seen_edges: set[tuple[Any, ...]] = set()
            queue: deque[tuple[str, int]] = deque([(sid, 0)])
            truncated = False
            while queue:
                current, current_depth = queue.popleft()
                if current_depth >= depth:
                    continue
                predicates: list[str] = []
                args: list[Any] = []
                if direction in {"out", "both"}:
                    predicates.append("source_id = ?"); args.append(current)
                if direction in {"in", "both"}:
                    predicates.append("target_id = ?"); args.append(current)
                if not predicates:
                    predicates.append("source_id = ?"); args.append(current)
                type_sql = " AND type IN (" + ",".join("?" for _ in types) + ")" if types else ""
                if types is not None and not types:
                    continue
                if types:
                    args.extend(types)
                rows = db.execute("SELECT rowid, * FROM relationships WHERE (" + " OR ".join(predicates) + ")" +
                                  type_sql + " ORDER BY rowid", args).fetchall()
                for row in rows:
                    edge_key = (row["source_id"], row["target_id"], row["type"], row["line"], row["label"])
                    if edge_key in seen_edges:
                        continue
                    seen_edges.add(edge_key)
                    edge = self.store.relationship(row)
                    edges.append(edge)
                    if len(edges) > limit:
                        truncated = True
                        break
                    other = row["target_id"] if row["source_id"] == current else row["source_id"]
                    if other and other not in nodes:
                        target_row = db.execute("SELECT * FROM symbols WHERE id = ?", (other,)).fetchone()
                        if target_row and len(nodes) < limit + 1:
                            nodes[other] = self.store.symbol(target_row)
                            queue.append((other, current_depth + 1))
                        elif target_row:
                            truncated = True
                if truncated:
                    break
            if len(edges) > limit:
                edges = edges[:limit]
            if len(nodes) > limit:
                nodes = dict(list(nodes.items())[:limit]); truncated = True
            node_ids = set(nodes)
            filtered_edges = [edge for edge in edges
                              if edge["sourceId"] in node_ids and
                              (edge["targetId"] is None or edge["targetId"] in node_ids)]
            if len(filtered_edges) != len(edges):
                truncated = True
            edges = filtered_edges
            return {"nodes": list(nodes.values()), "edges": edges, "truncated": truncated}

    def module_graph(self, params: dict[str, Any]) -> dict[str, Any]:
        return ModuleGraph(self.store).build(params)

    def class_graph(self, params: dict[str, Any]) -> dict[str, Any]:
        return ClassGraph(self.store).build(params)

    def explain_context(self, params: dict[str, Any]) -> dict[str, Any]:
        sid = str(params.get("id", ""))
        with self.store.connect() as db:
            symbol = self.store.symbol(db.execute("SELECT * FROM symbols WHERE id = ?", (sid,)).fetchone())
            if symbol is None:
                raise ValueError(f"unknown symbol id: {sid}")
            parent = self.store.symbol(db.execute("SELECT * FROM symbols WHERE id = ?", (symbol["parentId"],)).fetchone()) if symbol["parentId"] else None
            children = [self.store.symbol(row) for row in db.execute(
                "SELECT * FROM symbols WHERE parent_id = ? ORDER BY start_line, id LIMIT 100", (sid,))]
            outgoing = [self.store.relationship(row) for row in db.execute(
                "SELECT * FROM relationships WHERE source_id = ? ORDER BY line LIMIT 50", (sid,))]
            incoming = [self.store.relationship(row) for row in db.execute(
                "SELECT * FROM relationships WHERE target_id = ? ORDER BY line LIMIT 50", (sid,))]
            context = {
                "symbol": symbol, "parent": parent,
                "children": children,
                "outgoing": outgoing, "incoming": incoming,
                "instruction": "Explain this symbol's role and the evidence-backed relationships shown here. Keep uncertainty explicit.",
            }
            import json
            return {"symbol": symbol, "context": json.dumps(context, ensure_ascii=False, indent=2)[:20000]}

