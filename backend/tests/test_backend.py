from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from backend.engine import Engine


class BackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="architecture-backend-", dir=Path.cwd()))
        (self.root / "pkg").mkdir()
        (self.root / "pkg" / "__init__.py").write_text(
            "from .dep import Dep as Exported\n", encoding="utf-8")
        (self.root / "pkg" / "base.py").write_text(
            "class Base:\n    def run(self):\n        return 1\n", encoding="utf-8")
        (self.root / "pkg" / "dep.py").write_text(
            "def helper():\n    return 1\n\nclass Dep:\n    def work(self):\n        return helper()\n", encoding="utf-8")
        (self.root / "pkg" / "main.py").write_text(
            "from .base import Base\nfrom .dep import Dep, helper as h\n\nVALUE = 1\n\n"
            "class Child(Base):\n"
            "    def __init__(self):\n"
            "        self.dep: Dep\n"
            "        def nested():\n"
            "            self.not_a_member = Dep()\n"
            "    def run(self):\n"
            "        self.dep.work()\n"
            "        h()\n"
            "        h()\n", encoding="utf-8")
        (self.root / "pkg" / "classes.py").write_text(
            "class B:\n    def bar(self):\n        pass\n\n"
            "class A:\n    def foo(self):\n        self.b = B()\n        self.b.bar()\n\n"
            "class C:\n    def use(self):\n        b = B()\n        b.bar()\n", encoding="utf-8")
        (self.root / "bad.py").write_text("class broken(:\n", encoding="utf-8")
        (self.root / "other.py").write_text("class Base:\n    pass\n", encoding="utf-8")
        (self.root / "dups.py").write_text(
            "class Dup:\n    pass\n\nclass Dup:\n    pass\n\ndef use():\n    return Dup()\n", encoding="utf-8")
        (self.root / "ignored").mkdir()
        (self.root / "ignored" / "skip.py").write_text("class Skip: pass\n", encoding="utf-8")
        self.db = self.root / "index.sqlite"

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_index_graph_context_and_persistence(self) -> None:
        engine = Engine(str(self.db))
        result = engine.index({"root": str(self.root)})
        self.assertEqual(result["files"], 9)
        self.assertEqual(len(result["errors"]), 1)
        symbols = engine.symbols({"query": "Child"})
        child = next(x for x in symbols if x["kind"] == "class")
        modules = engine.symbols({"kind": "module", "query": "pkg.main"})
        self.assertEqual(child["moduleId"], modules[0]["id"])
        self.assertGreaterEqual(modules[0]["endLine"], 13)
        file_symbols = engine.symbols({"kind": "class", "file": str(self.root / "pkg" / "main.py").lower()})
        self.assertTrue(any(item["qualifiedName"] == "pkg.main.Child" for item in file_symbols))
        inspected = engine.inspect({"id": child["id"]})
        self.assertTrue(any(x["name"] == "dep" for x in inspected["children"]))
        dep_member = next(x for x in inspected["children"] if x["name"] == "dep")
        self.assertEqual(dep_member["details"]["inferredType"], "Dep")
        rels = inspected["outgoing"]
        self.assertTrue(any(x["type"] == "inherits" and x["status"] == "resolved" for x in rels))
        self.assertTrue(any(x["type"] == "owns" and x["label"] == "dep"
                            and next((r["name"] for r in inspected["related"] if r["id"] == x["targetId"]), None) == "Dep"
                            for x in rels))
        self.assertFalse(any(x["type"] == "owns" and x["targetId"] in
                             {child_item["id"] for child_item in inspected["children"]} for x in rels))
        inferred_calls = [x for x in engine.inspect({"id": next(s["id"] for s in inspected["children"] if s["name"] == "run")})["outgoing"] if x["type"] == "calls"]
        self.assertTrue(any(x["label"] == "self.dep.work" and x["status"] == "inferred" for x in inferred_calls))
        self.assertTrue(any(x["label"] == "h" and x["status"] == "resolved" for x in inferred_calls))
        package = next(s for s in engine.symbols({"kind": "module", "query": "pkg"})
                       if s["qualifiedName"] == "pkg")
        self.assertTrue(any(x["type"] == "imports" and x["label"].endswith("pkg.dep.Dep") for x in
                            engine.inspect({"id": package["id"]})["outgoing"]))
        graph = engine.graph({"id": child["id"], "types": ["inherits", "owns"], "direction": "out"})
        self.assertTrue(graph["nodes"])
        self.assertTrue(any(edge["label"] == "dep" for edge in graph["edges"]))
        self.assertTrue(all(node["kind"] == "class" for node in graph["nodes"]))
        context = engine.context({"file": str(self.root / "pkg" / "main.py"), "line": 8})
        self.assertIsNotNone(context)
        self.assertEqual(context["name"], "dep")
        self.assertEqual(engine.summary()["root"], str(self.root.resolve()))

        limited = engine.graph({"id": child["id"], "direction": "out", "depth": 1, "limit": 1})
        limited_ids = {x["id"] for x in limited["nodes"]}
        self.assertTrue(all(edge["targetId"] is None or edge["targetId"] in limited_ids
                            for edge in limited["edges"]))
        self.assertEqual(engine.graph({"id": child["id"], "types": [], "direction": "out"})["edges"], [])

        dup_classes = engine.symbols({"kind": "class", "query": "dups.Dup"})
        self.assertEqual(len(dup_classes), 2)
        use = next(x for x in engine.symbols({"kind": "function", "query": "dups.use"}))
        self.assertTrue(any(x["type"] == "calls" and x["status"] == "unresolved"
                            for x in engine.inspect({"id": use["id"]})["outgoing"]))

    def test_nested_function_does_not_create_self_member(self) -> None:
        engine = Engine(str(self.db))
        engine.index({"root": str(self.root)})
        members = engine.symbols({"kind": "member", "query": "not_a_member"})
        self.assertEqual(members, [])

    def test_excludes_support_relative_globs(self) -> None:
        engine = Engine(str(self.db))
        result = engine.index({"root": str(self.root), "excludes": ["ignored/**"]})
        self.assertEqual(result["files"], 8)
        self.assertEqual(engine.symbols({"query": "Skip"}), [])

    def test_qualified_unknown_base_does_not_guess_local_class(self) -> None:
        path = self.root / "qualified.py"
        path.write_text("class Base:\n    pass\n\nclass Child(unknown.Base):\n    pass\n", encoding="utf-8")
        engine = Engine(str(self.db))
        engine.index({"root": str(self.root)})
        child = next(x for x in engine.symbols({"kind": "class", "query": "qualified.Child"}))
        inherits = [x for x in engine.inspect({"id": child["id"]})["outgoing"] if x["type"] == "inherits"]
        self.assertEqual(len(inherits), 1)
        self.assertIsNone(inherits[0]["targetId"])

    def test_module_graph_aggregates_calls_imports_and_packages(self) -> None:
        engine = Engine(str(self.db))
        engine.index({"root": str(self.root)})
        main = next(s for s in engine.symbols({"kind": "module", "query": "pkg.main"})
                    if s["qualifiedName"] == "pkg.main")
        graph = engine.module_graph({"id": main["id"], "depth": 1, "limit": 80})
        self.assertTrue(all(node["kind"] == "module" for node in graph["nodes"]))
        self.assertTrue(any(edge["type"] == "imports" and edge["label"] == "导入 ×2" for edge in graph["edges"]))
        self.assertTrue(any(edge["type"] == "calls" and edge["label"].startswith("调用 ×") for edge in graph["edges"]))
        node_ids = {node["id"] for node in graph["nodes"]}
        self.assertTrue(all(edge["targetId"] is None or edge["targetId"] in node_ids for edge in graph["edges"]))

        package_graph = engine.module_graph({"id": "package:ignored", "depth": 1})
        self.assertEqual(package_graph["nodes"][0]["id"], "package:ignored")
        self.assertTrue(package_graph["nodes"][0]["details"]["isPackage"])
        self.assertTrue(any(edge["type"] == "contains" and edge["targetId"] != "package:ignored"
                            for edge in package_graph["edges"]))
        pkg = next(s for s in engine.symbols({"kind": "module", "query": "pkg"})
                   if s["qualifiedName"] == "pkg")
        real_package_graph = engine.module_graph({"id": pkg["id"], "depth": 1})
        self.assertTrue(real_package_graph["nodes"][0]["details"]["isPackage"])
        self.assertTrue(any(edge["type"] == "contains" for edge in real_package_graph["edges"]))
        self.assertTrue(any(edge["type"] == "imports" for edge in real_package_graph["edges"]))
        limited = engine.module_graph({"id": main["id"], "limit": 1})
        limited_ids = {node["id"] for node in limited["nodes"]}
        self.assertTrue(all(edge["targetId"] is None or edge["targetId"] in limited_ids
                            for edge in limited["edges"]))

        isolated = next(s for s in engine.symbols({"kind": "module", "query": "other"})
                        if s["qualifiedName"] == "other")
        isolated_graph = engine.module_graph({"id": isolated["id"]})
        self.assertEqual([node["id"] for node in isolated_graph["nodes"]], [isolated["id"]])
        self.assertEqual(isolated_graph["edges"], [])

        class_file = str(self.root / "pkg" / "classes.py")
        class_graph = engine.class_graph({"file": class_file, "depth": 1})
        self.assertTrue(all(node["kind"] == "class" for node in class_graph["nodes"]))
        self.assertEqual({node["name"] for node in class_graph["nodes"]}, {"A", "B", "C"})
        call_edges = [edge for edge in class_graph["edges"] if edge["type"] == "calls"]
        self.assertTrue(any("bar" in edge["label"] and edge["status"] == "inferred"
                            for edge in call_edges))
        b_id = next(node["id"] for node in class_graph["nodes"] if node["name"] == "B")
        self.assertTrue(any(edge["targetId"] == b_id for edge in call_edges))
        self.assertTrue(all(edge["sourceId"] != edge["targetId"] for edge in call_edges))
        a_id = next(node["id"] for node in class_graph["nodes"] if node["name"] == "A")
        self.assertTrue(engine.class_graph({"id": a_id})["nodes"])
        limited_classes = engine.class_graph({"file": class_file, "limit": 1})
        limited_class_ids = {node["id"] for node in limited_classes["nodes"]}
        self.assertTrue(limited_classes["truncated"])
        self.assertTrue(all(edge["targetId"] is None or edge["targetId"] in limited_class_ids
                            for edge in limited_classes["edges"]))
        with self.assertRaises(ValueError):
            engine.class_graph({"id": main["id"]})


if __name__ == "__main__":
    unittest.main()

