from __future__ import annotations

import os
from collections import defaultdict, deque
from typing import Any

from .storage import Store


class ClassGraph:
    """Build a bounded class-level graph from indexed symbols and relationships."""

    def __init__(self, store: Store):
        self.store = store

    def build(self, params: dict[str, Any]) -> dict[str, Any]:
        requested_id = str(params.get("id", ""))
        requested_file = str(params.get("file", ""))
        depth = min(max(int(params.get("depth", 1)), 1), 3)
        limit = min(max(int(params.get("limit", 80)), 1), 300)
        with self.store.connect() as db:
            symbols = db.execute("SELECT id, kind, parent_id, name FROM symbols").fetchall()
            parent = {row["id"]: row["parent_id"] for row in symbols}
            names = {row["id"]: row["name"] for row in symbols}
            kind = {row["id"]: row["kind"] for row in symbols}
            classes = {
                row["id"]: self.store.symbol(row)
                for row in db.execute("SELECT * FROM symbols WHERE kind = 'class' ORDER BY file, start_line, id")
            }
            if requested_id:
                focus = classes.get(requested_id)
                if focus is None:
                    raise ValueError(f"classGraph id is not a class: {requested_id}")
                focus_ids = [requested_id]
            elif requested_file:
                path = os.path.abspath(requested_file)
                collation = " COLLATE NOCASE" if os.name == "nt" else ""
                focus_ids = [row["id"] for row in db.execute(
                    "SELECT id FROM symbols WHERE kind = 'class' AND file = ?" + collation + " "
                    "ORDER BY start_line, id", (path,))]
                if not focus_ids:
                    raise ValueError(f"no classes found in file: {path}")
            else:
                raise ValueError("classGraph requires id or file")

            owner: dict[str, str | None] = {}
            for symbol_id in kind:
                current = symbol_id
                seen: set[str] = set()
                while current and current not in seen:
                    seen.add(current)
                    if kind.get(current) == "class":
                        owner[symbol_id] = current
                        break
                    current = parent.get(current)
                else:
                    owner[symbol_id] = None
            local_ids: dict[str, list[str]] = defaultdict(list)
            for symbol_id, class_id in owner.items():
                if class_id:
                    local_ids[class_id].append(symbol_id)
            nodes: dict[str, dict[str, Any]] = {}
            truncated = False
            for class_id in focus_ids:
                if len(nodes) >= limit:
                    truncated = True
                    break
                nodes[class_id] = classes[class_id]
            queue: deque[tuple[str, int]] = deque((class_id, 0) for class_id in nodes)
            grouped: dict[tuple[str, str | None, str], list[dict[str, Any]]] = defaultdict(list)
            seen_candidates: set[int] = set()
            while queue:
                class_id, current_depth = queue.popleft()
                if current_depth >= depth:
                    continue
                for row in self._candidate_rows(db, local_ids.get(class_id, []), seen_candidates):
                    source_class = owner.get(row["source_id"])
                    target_class = owner.get(row["target_id"]) if row["target_id"] else None
                    relation_type = row["type"]
                    if relation_type not in {"calls", "inherits"} or not source_class:
                        continue
                    if target_class == source_class:
                        continue
                    if target_class and target_class not in classes:
                        target_class = None
                    # An unresolved/module-level function target has no class
                    # endpoint. Preserve it as evidence without inventing one.
                    grouped[(source_class, target_class, relation_type)].append({
                        "row": row, "targetName": (names.get(row["target_id"])
                            if row["target_kind"] == "method" else
                            f"构造 {names.get(row['target_id'], '')}" if row["target_kind"] == "class" else None),
                    })
                    neighbor = target_class if source_class == class_id else source_class
                    if neighbor and neighbor not in nodes:
                        if len(nodes) >= limit:
                            truncated = True
                        else:
                            nodes[neighbor] = classes[neighbor]
                            queue.append((neighbor, current_depth + 1))

            edges = self._aggregate(grouped)
            known = set(nodes)
            edges = [edge for edge in edges if edge["sourceId"] in known and
                     (edge["targetId"] is None or edge["targetId"] in known)]
            if len(edges) > limit:
                edges = edges[:limit]
                truncated = True
            return {"nodes": list(nodes.values()), "edges": edges, "truncated": truncated}

    @staticmethod
    def _candidate_rows(db: Any, symbol_ids: list[str], seen: set[int]) -> list[Any]:
        if not symbol_ids:
            return []
        placeholders = ",".join("?" for _ in symbol_ids)
        args = tuple(symbol_ids) + tuple(symbol_ids)
        rows = db.execute(
            f"""SELECT r.rowid, r.source_id, r.target_id, r.type, r.status,
                       r.confidence, r.label, r.file, r.line
                       ,target.kind AS target_kind
                  FROM relationships r
                  LEFT JOIN symbols target ON target.id = r.target_id
                 WHERE r.type IN ('calls', 'inherits')
                   AND r.rowid IN (
                       SELECT rowid FROM relationships WHERE source_id IN ({placeholders})
                       UNION
                       SELECT rowid FROM relationships WHERE target_id IN ({placeholders})
                   )
                 ORDER BY r.rowid""", args).fetchall()
        result = []
        for row in rows:
            if row["rowid"] not in seen:
                seen.add(row["rowid"])
                result.append(row)
        return result

    @staticmethod
    def _aggregate(grouped: dict[tuple[str, str | None, str], list[dict[str, Any]]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for (source_id, target_id, relation_type), evidence in grouped.items():
            statuses = {str(item["row"]["status"]) for item in evidence}
            status = ("unresolved" if statuses & {"unresolved", "dynamic"} else
                      "inferred" if "inferred" in statuses else "resolved")
            confidence = ("low" if any(item["row"]["confidence"] == "low" for item in evidence) else
                          "medium" if any(item["row"]["confidence"] == "medium" for item in evidence) else "high")
            first = evidence[0]["row"]
            if relation_type == "calls":
                labels: list[str] = []
                for item in evidence:
                    label = item["targetName"]
                    if not label:
                        continue
                    if label not in labels:
                        labels.append(label)
                if not labels:
                    labels.append("未解析")
                shown = labels[:3]
                if len(labels) > 3:
                    shown.append(f"+{len(labels) - 3}")
                label = "调用 " + ", ".join(shown) + f" ×{len(evidence)}"
            else:
                label = f"继承 ×{len(evidence)}"
            result.append({"sourceId": source_id, "targetId": target_id, "type": relation_type,
                           "status": status, "confidence": confidence, "label": label,
                           "file": first["file"], "line": first["line"]})
        return result
