from __future__ import annotations

import copy
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from .storage import Store


class ModuleGraph:
    """Build a small module/package graph directly from the persisted index."""

    def __init__(self, store: Store):
        self.store = store

    def build(self, params: dict[str, Any]) -> dict[str, Any]:
        requested_id = str(params.get("id", ""))
        depth = min(max(int(params.get("depth", 1)), 1), 3)
        limit = min(max(int(params.get("limit", 80)), 1), 300)
        with self.store.connect() as db:
            modules = {
                row["id"]: self.store.symbol(row)
                for row in db.execute("SELECT * FROM symbols WHERE kind = 'module' ORDER BY qualified_name, id")
            }
            module_nodes, packages, children, parents = self._package_nodes(modules)
            package_by_id = {package["id"]: package for package in packages.values()}
            focus = self._focus(requested_id, modules, packages)
            if focus is None:
                raise ValueError(f"unknown module or package id: {requested_id}")

            nodes: dict[str, dict[str, Any]] = {focus["id"]: focus}
            edges: list[dict[str, Any]] = []
            seen_edges: set[tuple[Any, ...]] = set()
            queue: deque[tuple[str, int]] = deque([(focus["id"], 0)])
            truncated = False
            while queue:
                current_id, current_depth = queue.popleft()
                current = package_by_id.get(current_id) or module_nodes.get(current_id)
                if current is None or current_depth >= depth:
                    continue

                # Package focus is intentionally structural. Its first level shows
                # immediate contents without expanding all repository dependencies.
                for child_id in children.get(current_id, []):
                    self._add_edge(edges, seen_edges, current_id, child_id, "contains", "resolved", "high", "包含", current)
                    self._enqueue(child_id, current_depth + 1, nodes, queue, module_nodes, package_by_id, limit,
                                  lambda: self._mark_truncated())
                parent_id = parents.get(current_id)
                if parent_id:
                    parent_node = package_by_id.get(parent_id) or module_nodes.get(parent_id) or current
                    self._add_edge(edges, seen_edges, parent_id, current_id, "contains", "resolved", "high", "包含", parent_node)
                    self._enqueue(parent_id, current_depth + 1, nodes, queue, module_nodes, package_by_id, limit,
                                  lambda: self._mark_truncated())

                if current.get("details", {}).get("isPackage") and not current.get("file"):
                    continue
                for edge in self._module_relationships(db, current_id, module_nodes):
                    key = (edge["sourceId"], edge["targetId"], edge["type"], edge["label"])
                    if key not in seen_edges:
                        seen_edges.add(key)
                        edges.append(edge)
                    neighbor_id = edge["targetId"] if edge["sourceId"] == current_id else edge["sourceId"]
                    if neighbor_id and neighbor_id not in nodes:
                        if len(nodes) < limit:
                            target = package_by_id.get(neighbor_id) or module_nodes.get(neighbor_id)
                            if target:
                                nodes[neighbor_id] = target
                                queue.append((neighbor_id, current_depth + 1))
                        else:
                            truncated = True
                    if len(edges) > limit:
                        truncated = True
                        break
                if len(edges) > limit:
                    break

            if len(nodes) > limit:
                nodes = dict(list(nodes.items())[:limit])
                truncated = True
            if len(edges) > limit:
                edges = edges[:limit]
                truncated = True
            node_ids = set(nodes)
            filtered = [edge for edge in edges if edge["sourceId"] in node_ids and
                        (edge["targetId"] is None or edge["targetId"] in node_ids)]
            if len(filtered) != len(edges):
                truncated = True
            return {"nodes": list(nodes.values()), "edges": filtered, "truncated": truncated}

    def _package_nodes(self, modules: dict[str, dict[str, Any]]) -> tuple[
            dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, list[str]], dict[str, str]]:
        module_nodes = dict(modules)
        packages: dict[str, dict[str, Any]] = {}
        for module in modules.values():
            qname = module["qualifiedName"]
            if Path(module["file"]).name == "__init__.py":
                package = copy.deepcopy(module)
                package["details"] = dict(package.get("details") or {})
                package["details"]["isPackage"] = True
                packages[qname] = package
            parts = qname.split(".")
            for index in range(1, len(parts)):
                package_name = ".".join(parts[:index])
                packages.setdefault(package_name, self._synthetic_package(package_name))

        children: dict[str, list[str]] = defaultdict(list)
        parents: dict[str, str] = {}
        package_ids = {package["id"] for package in packages.values()}
        for package_name, package in packages.items():
            parent_name = package_name.rsplit(".", 1)[0] if "." in package_name else None
            if parent_name and parent_name in packages:
                parent_id = packages[parent_name]["id"]
                children[parent_id].append(package["id"])
                parents[package["id"]] = parent_id
        for module in modules.values():
            if module["id"] in package_ids:
                continue
            qname = module["qualifiedName"]
            parent_name = qname.rsplit(".", 1)[0] if "." in qname else None
            if parent_name and parent_name in packages:
                parent_id = packages[parent_name]["id"]
                children[parent_id].append(module["id"])
                parents[module["id"]] = parent_id
        labels = {node["id"]: node["qualifiedName"] for node in packages.values()}
        labels.update({node_id: node["qualifiedName"] for node_id, node in module_nodes.items()})
        for values in children.values():
            values.sort(key=lambda item: (labels.get(item, ""), item))
        return module_nodes, packages, children, parents

    @staticmethod
    def _synthetic_package(qname: str) -> dict[str, Any]:
        return {"id": f"package:{qname}", "kind": "module", "name": qname.rsplit(".", 1)[-1],
                "qualifiedName": qname, "file": "", "startLine": 0, "endLine": 0,
                "parentId": None, "moduleId": None, "details": {"isPackage": True}}

    @staticmethod
    def _focus(requested_id: str, modules: dict[str, dict[str, Any]],
               packages: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
        if requested_id.startswith("package:"):
            qname = requested_id[len("package:"):]
            return packages.get(qname)
        package_node = next((package for package in packages.values() if package["id"] == requested_id), None)
        if package_node is not None:
            return package_node
        return modules.get(requested_id)

    def _module_relationships(self, db: Any, module_id: str,
                              module_nodes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        symbol_ids = [module_id]
        symbol_ids.extend(row["id"] for row in db.execute(
            "SELECT id FROM symbols WHERE module_id = ?", (module_id,)).fetchall())
        placeholders = ",".join("?" for _ in symbol_ids)
        args = tuple(symbol_ids) + tuple(symbol_ids)
        rows = db.execute(
            f"""SELECT r.type, r.status, r.confidence, r.label, r.file, r.line,
                      CASE WHEN source.kind = 'module' THEN source.id ELSE source.module_id END AS source_module_id,
                      CASE WHEN target.kind = 'module' THEN target.id ELSE target.module_id END AS target_module_id
                 FROM relationships r
                 JOIN symbols source ON source.id = r.source_id
                 LEFT JOIN symbols target ON target.id = r.target_id
                WHERE r.type IN ('imports', 'calls')
                  AND r.rowid IN (
                    SELECT rowid FROM relationships WHERE source_id IN ({placeholders})
                    UNION
                    SELECT rowid FROM relationships WHERE target_id IN ({placeholders})
                  )
                ORDER BY r.rowid""", args).fetchall()
        grouped: dict[tuple[str, str | None, str], list[Any]] = defaultdict(list)
        for row in rows:
            source_id = row["source_module_id"]
            target_id = row["target_module_id"]
            if not source_id or source_id == target_id:
                continue
            if source_id not in module_nodes:
                continue
            if target_id not in module_nodes:
                target_id = None
            grouped[(source_id, target_id, row["type"])].append(row)

        result: list[dict[str, Any]] = []
        for (source_id, target_id, relation_type), evidence in grouped.items():
            statuses = {str(row["status"]) for row in evidence}
            if "unresolved" in statuses or "dynamic" in statuses:
                status = "unresolved"
            elif "inferred" in statuses:
                status = "inferred"
            else:
                status = "resolved"
            confidence = "low" if any(row["confidence"] == "low" for row in evidence) else \
                ("medium" if any(row["confidence"] == "medium" for row in evidence) else "high")
            label = ("调用" if relation_type == "calls" else "导入") + f" ×{len(evidence)}"
            first = evidence[0]
            result.append({"sourceId": source_id, "targetId": target_id, "type": relation_type,
                           "status": status, "confidence": confidence, "label": label,
                           "file": first["file"], "line": first["line"]})
        return result

    @staticmethod
    def _add_edge(edges: list[dict[str, Any]], seen: set[tuple[Any, ...]], source_id: str,
                  target_id: str | None, relation_type: str, status: str, confidence: str,
                  label: str, source: dict[str, Any]) -> None:
        key = (source_id, target_id, relation_type, label)
        if key in seen:
            return
        seen.add(key)
        edges.append({"sourceId": source_id, "targetId": target_id, "type": relation_type,
                      "status": status, "confidence": confidence, "label": label,
                      "file": source.get("file", ""), "line": source.get("startLine", 0)})

    @staticmethod
    def _enqueue(node_id: str, depth: int, nodes: dict[str, dict[str, Any]], queue: deque[tuple[str, int]],
                 module_nodes: dict[str, dict[str, Any]], packages: dict[str, dict[str, Any]],
                 limit: int, mark_truncated: Any) -> None:
        if node_id in nodes:
            return
        node = packages.get(node_id) or module_nodes.get(node_id)
        if node is None:
            return
        if len(nodes) >= limit:
            mark_truncated()
            return
        nodes[node_id] = node
        queue.append((node_id, depth))

    @staticmethod
    def _mark_truncated() -> None:
        # Kept as a callback target for the bounded enqueue path. The final
        # edge/node filtering determines the externally visible truncation bit.
        return None

