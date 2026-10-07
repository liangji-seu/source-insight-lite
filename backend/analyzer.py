from __future__ import annotations

import ast
import hashlib
import os
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

from .models import FileUnit, Relationship, Symbol


def _stable_id(*parts: object) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def _dotted(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        left = _dotted(node.value)
        return f"{left}.{node.attr}" if left else node.attr
    return None


def _annotation(node: ast.AST | None) -> str | None:
    return ast.unparse(node) if node is not None else None


def _doc(node: ast.AST) -> str | None:
    return ast.get_docstring(node, clean=False)


def _end_line(node: ast.AST) -> int:
    return int(getattr(node, "end_lineno", getattr(node, "lineno", 1)))


@dataclass
class Analysis:
    symbols: list[Symbol] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)


class Analyzer:
    def __init__(self, units: list[FileUnit], progress: Callable[[int, int, str], None] | None = None):
        self.units = units
        self.progress = progress
        self.analysis = Analysis()
        self.by_id: dict[str, Symbol] = {}
        self.by_qname: dict[str, list[Symbol]] = defaultdict(list)
        self.by_file_module: dict[str, Symbol] = {}
        self.children: dict[str, list[Symbol]] = defaultdict(list)
        self.imports: dict[str, dict[str, tuple[str, str | None]]] = defaultdict(dict)
        self.class_types: dict[str, str] = {}
        self.member_types: dict[tuple[str, str], str] = {}
        self.local_types: dict[tuple[str, str], str] = {}
        self.class_nodes: dict[str, ast.ClassDef] = {}
        self.callable_nodes: list[tuple[Symbol, ast.AST, str | None]] = []
        self._module_qnames: dict[str, Symbol] = {}

    def run(self) -> Analysis:
        self._collect_symbols()
        self._collect_imports()
        self._collect_inheritance_and_ownership()
        self._collect_calls()
        self._collect_overrides()
        return self.analysis

    def _add(self, symbol: Symbol) -> None:
        self.analysis.symbols.append(symbol)
        self.by_id[symbol.id] = symbol
        self.by_qname[symbol.qualified_name].append(symbol)
        if symbol.parent_id:
            self.children[symbol.parent_id].append(symbol)
        if symbol.kind == "module":
            self.by_file_module[symbol.file] = symbol
            self._module_qnames[symbol.qualified_name] = symbol

    def _symbol(self, kind: str, name: str, qname: str, unit: FileUnit, node: ast.AST,
                parent: Symbol | None, details: dict[str, Any] | None = None) -> Symbol:
        module_id = (parent.id if parent and parent.kind == "module" else parent.module_id) if parent else None
        if kind == "module":
            module_id = None
        symbol = Symbol(
            id=_stable_id(unit.path, kind, qname, getattr(node, "lineno", 1)),
            kind=kind, name=name, qualified_name=qname, file=unit.path,
            start_line=int(getattr(node, "lineno", 1)), end_line=_end_line(node),
            parent_id=parent.id if parent else None, module_id=module_id,
            details=details or {},
        )
        if kind == "module":
            symbol.end_line = max(1, len(unit.source.splitlines()))
        self._add(symbol)
        return symbol

    def _collect_symbols(self) -> None:
        total = len(self.units)
        for done, unit in enumerate(self.units, 1):
            if unit.tree is None:
                continue
            module = self._symbol("module", unit.module_name.rsplit(".", 1)[-1], unit.module_name,
                                  unit, unit.tree, None,
                                  {"docstring": _doc(unit.tree), "imports": []})
            visitor = SymbolVisitor(self, unit, module)
            visitor.visit(unit.tree)
            if self.progress:
                self.progress(done, total, unit.path)

    def _collect_imports(self) -> None:
        by_module = {u.module_name: u for u in self.units}
        for unit in self.units:
            if unit.tree is None:
                continue
            module = self.by_file_module[unit.path]
            names = self.imports[module.id]
            detail_names: list[dict[str, str | None]] = []
            for node in ast.iter_child_nodes(unit.tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        bound = alias.asname or alias.name.split(".")[0]
                        names[bound] = (alias.name, None)
                        detail_names.append({"name": alias.name, "alias": alias.asname})
                        target = self._resolve_module(alias.name, by_module)
                        self.analysis.relationships.append(Relationship(
                            module.id, target.id if target else None, "imports",
                            "resolved" if target else "unresolved", "high" if target else "low",
                            alias.name, unit.path, node.lineno))
                elif isinstance(node, ast.ImportFrom):
                    base = self._resolve_from_module(unit.module_name, node, by_module,
                                                     unit.rel_path.replace("\\", "/").endswith("__init__.py"))
                    for alias in node.names:
                        if alias.name == "*":
                            continue
                        bound = alias.asname or alias.name
                        dotted = f"{base}.{alias.name}" if base else alias.name
                        names[bound] = (dotted, alias.name)
                        detail_names.append({"name": dotted, "alias": alias.asname})
                        target = self._resolve_qname(dotted) or self._resolve_module(dotted, by_module)
                        self.analysis.relationships.append(Relationship(
                            module.id, target.id if target else None, "imports",
                            "resolved" if target else "unresolved", "high" if target else "low",
                            dotted, unit.path, node.lineno))
            module.details["imports"] = detail_names

    def _resolve_module(self, name: str, by_module: dict[str, FileUnit]) -> Symbol | None:
        if name in self._module_qnames:
            return self._module_qnames[name]
        # Importing a package submodule may have a file symbol with an exact qname.
        return self._module_qnames.get(name)

    def _resolve_from_module(self, current: str, node: ast.ImportFrom,
                             by_module: dict[str, FileUnit], current_is_package: bool = False) -> str | None:
        # A relative import starts in the current module's containing package.
        if node.level == 0:
            return node.module
        parts = current.split(".") if current_is_package else current.split(".")[:-1]
        for _ in range(max(0, node.level - 1)):
            if parts:
                parts.pop()
        if node.module:
            parts.extend(node.module.split("."))
        candidate = ".".join(parts)
        if candidate in by_module or candidate in self._module_qnames:
            return candidate
        # A relative import from a module file is based on its containing package.
        if node.level and parts:
            return candidate
        return node.module

    def _resolve_qname(self, qname: str, kind: str | None = None) -> Symbol | None:
        candidates = [symbol for symbol in self.by_qname.get(qname, [])
                      if kind is None or symbol.kind == kind]
        return candidates[0] if len(candidates) == 1 else None

    def _resolve_name(self, name: str, module: Symbol, kind: str | None = None) -> Symbol | None:
        target = self._resolve_qname(name, kind)
        if target:
            return target
        return self._resolve_qname(f"{module.qualified_name}.{name}", kind)

    def _relationship(self, source: Symbol, target: Symbol | None, typ: str, status: str,
                      confidence: str, label: str, file: str, line: int) -> None:
        self.analysis.relationships.append(Relationship(source.id, target.id if target else None,
                                                         typ, status, confidence, label, file, line))

    def _collect_inheritance_and_ownership(self) -> None:
        for class_id, node in self.class_nodes.items():
            cls = self.by_id[class_id]
            module = self.by_id[cls.module_id] if cls.module_id else cls
            for base in node.bases:
                text = _dotted(base) or ast.unparse(base)
                target = self._resolve_expr_type(text, module, cls)
                self._relationship(cls, target, "inherits", "resolved" if target else "unresolved",
                                   "high" if target else "low", text, cls.file, base.lineno)
        for (class_id, member_name), type_name in self.member_types.items():
            cls = self.by_id[class_id]
            target = self._resolve_expr_type(type_name, self.by_id[cls.module_id] if cls.module_id else cls, cls)
            member = next((s for s in self.children[class_id] if s.name == member_name and s.kind == "member"), None)
            if member:
                member.details["inferredType"] = type_name
                self._relationship(cls, target, "owns", "inferred" if target else "unresolved",
                                   "medium" if target else "low", member_name, cls.file, member.start_line)
            
    def _resolve_expr_type(self, text: str, module: Symbol, cls: Symbol | None = None) -> Symbol | None:
        short = text.rsplit(".", 1)[-1]
        imported = self.imports.get(module.id, {}).get(short)
        if imported:
            text = imported[0]
        target = self._resolve_qname(text, "class")
        if target:
            return target
        if "." in text:
            return None
        return self._resolve_name(short, module, "class")

    def _collect_overrides(self) -> None:
        for symbol in [s for s in self.analysis.symbols if s.kind == "class"]:
            node = self.class_nodes.get(symbol.id)
            if not node:
                continue
            own_methods = {s.name: s for s in self.children[symbol.id] if s.kind == "method"}
            for base in node.bases:
                target_cls = self._resolve_expr_type(_dotted(base) or ast.unparse(base),
                                                     self.by_id[symbol.module_id] if symbol.module_id else symbol, symbol)
                if not target_cls:
                    continue
                for method_name, method in own_methods.items():
                    parent_method = next((s for s in self.children[target_cls.id]
                                          if s.kind == "method" and s.name == method_name), None)
                    if parent_method:
                        self._relationship(method, parent_method, "overrides", "resolved", "high",
                                           method_name, method.file, method.start_line)

    def _collect_calls(self) -> None:
        for symbol, node, class_id in self.callable_nodes:
            module = self.by_id[symbol.module_id] if symbol.module_id else symbol
            cls = self.by_id[class_id] if class_id else None
            visitor = CallVisitor(self, symbol, module, cls)
            body = getattr(node, "body", [])
            for statement in body:
                visitor.visit(statement)

    def resolve_call(self, expr: ast.AST, source: Symbol, module: Symbol,
                     cls: Symbol | None, locals_map: dict[str, str]) -> tuple[Symbol | None, str, str]:
        if isinstance(expr, ast.Name):
            if expr.id in locals_map:
                typ = self._resolve_expr_type(locals_map[expr.id], module, cls)
                return typ, "inferred", "medium"
            imported = self.imports.get(module.id, {}).get(expr.id)
            if imported:
                target = self._resolve_qname(imported[0]) or self._resolve_expr_type(imported[0], module, cls)
                return target, "resolved" if target else "unresolved", "high" if target else "low"
            target = self._resolve_name(expr.id, module)
            return target, "resolved" if target else "unresolved", "high" if target else "low"
        if isinstance(expr, ast.Attribute):
            base_type, status, confidence = self._infer_expr_type(expr.value, module, cls, locals_map)
            if base_type:
                target = next((s for s in self.children.get(base_type.id, []) if s.name == expr.attr
                               and s.kind in {"method", "function", "member"}), None)
                return target, "inferred" if target and status == "inferred" else ("resolved" if target else "unresolved"), confidence if target else "low"
            # self.method is resolved only through current class; no global bare method guesses.
            return None, "unresolved", "low"
        return None, "dynamic", "low"

    def _infer_expr_type(self, expr: ast.AST, module: Symbol, cls: Symbol | None,
                         locals_map: dict[str, str]) -> tuple[Symbol | None, str, str]:
        if isinstance(expr, ast.Name):
            if expr.id == "self" and cls:
                return cls, "resolved", "high"
            if expr.id in locals_map:
                target = self._resolve_expr_type(locals_map[expr.id], module, cls)
                return target, "inferred" if target else "unresolved", "medium" if target else "low"
            imported = self.imports.get(module.id, {}).get(expr.id)
            if imported:
                target = self._resolve_expr_type(imported[0], module, cls) or self._resolve_qname(imported[0])
                return target, "inferred" if target else "unresolved", "medium" if target else "low"
            return None, "unresolved", "low"
        if isinstance(expr, ast.Attribute):
            owner, _, conf = self._infer_expr_type(expr.value, module, cls, locals_map)
            if owner and cls and owner.id == cls.id:
                typ = self.member_types.get((cls.id, expr.attr))
                target = self._resolve_expr_type(typ, module, cls) if typ else None
                return target, "inferred" if target else "unresolved", "medium" if target else "low"
            if owner:
                target = next((s for s in self.children.get(owner.id, []) if s.name == expr.attr), None)
                return target, "inferred" if target else "unresolved", conf if target else "low"
        return None, "dynamic", "low"


class SymbolVisitor(ast.NodeVisitor):
    def __init__(self, analyzer: Analyzer, unit: FileUnit, module: Symbol):
        self.a = analyzer
        self.unit = unit
        self.module = module
        self.stack: list[Symbol] = [module]
        self.class_stack: list[Symbol] = []

    @property
    def parent(self) -> Symbol:
        return self.stack[-1]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qname = f"{self.parent.qualified_name}.{node.name}"
        details = {"docstring": _doc(node), "bases": [_dotted(x) or ast.unparse(x) for x in node.bases],
                   "decorators": [ast.unparse(x) for x in node.decorator_list]}
        symbol = self.a._symbol("class", node.name, qname, self.unit, node, self.parent, details)
        self.a.class_nodes[symbol.id] = node
        self.stack.append(symbol); self.class_stack.append(symbol)
        for statement in node.body:
            self.visit(statement)
        self.class_stack.pop(); self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function(node)

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        kind = "method" if self.class_stack and self.parent.id == self.class_stack[-1].id else "function"
        qname = f"{self.parent.qualified_name}.{node.name}"
        args = []
        all_args = list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs)
        for arg in all_args:
            args.append({"name": arg.arg, "annotation": _annotation(arg.annotation)})
        details = {"docstring": _doc(node), "decorators": [ast.unparse(x) for x in node.decorator_list],
                   "parameters": args, "returnAnnotation": _annotation(node.returns)}
        symbol = self.a._symbol(kind, node.name, qname, self.unit, node, self.parent, details)
        class_id = self.class_stack[-1].id if kind == "method" else None
        self.a.callable_nodes.append((symbol, node, class_id))
        if kind == "method" and class_id:
            self._collect_method_members(symbol, node, self.class_stack[-1])
        # Do not visit function bodies for symbol discovery here: nested definitions are
        # collected explicitly so member discovery can stay scoped to the owning method.
        self.stack.append(symbol)
        for statement in node.body:
            if isinstance(statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                self.visit(statement)
        self.stack.pop()

    def _collect_method_members(self, method: Symbol, node: ast.FunctionDef | ast.AsyncFunctionDef,
                                cls: Symbol) -> None:
        for statement in node.body:
            for item in _direct_scope_nodes(statement):
                target = item.target if isinstance(item, ast.AnnAssign) else None
                value = item.value if isinstance(item, ast.AnnAssign) else item.value
                if isinstance(item, ast.Assign):
                    targets = item.targets
                else:
                    targets = [target] if target is not None else []
                for assigned in targets:
                    if isinstance(assigned, ast.Attribute) and isinstance(assigned.value, ast.Name) and assigned.value.id == "self":
                        type_name = _annotation(item.annotation) if isinstance(item, ast.AnnAssign) else _constructor_name(value)
                        self._member(cls, assigned.attr, assigned, type_name)

    def _member(self, cls: Symbol, name: str, node: ast.AST, inferred: str | None) -> None:
        existing = next((s for s in self.a.children[cls.id] if s.kind == "member" and s.name == name), None)
        if existing:
            if inferred:
                existing.details["inferredType"] = inferred
            if (cls.id, name) not in self.a.member_types and inferred:
                self.a.member_types[(cls.id, name)] = inferred
            return
        details = {"inferredType": inferred} if inferred else {}
        member = self.a._symbol("member", name, f"{cls.qualified_name}.{name}", self.unit, node, cls, details)
        if inferred:
            self.a.member_types[(cls.id, name)] = inferred

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        # Class-level annotations are members; module-level uppercase annotations are constants.
        if self.class_stack and self.parent.id == self.class_stack[-1].id and isinstance(node.target, ast.Name):
            self._member(self.class_stack[-1], node.target.id, node, _annotation(node.annotation))
        elif not self.class_stack and isinstance(node.target, ast.Name) and node.target.id.isupper():
            self.a._symbol("constant", node.target.id, f"{self.module.qualified_name}.{node.target.id}",
                           self.unit, node, self.module, {"annotation": _annotation(node.annotation)})

    def visit_Assign(self, node: ast.Assign) -> None:
        if not self.class_stack and any(isinstance(t, ast.Name) and t.id.isupper() for t in node.targets):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    self.a._symbol("constant", target.id, f"{self.module.qualified_name}.{target.id}",
                                   self.unit, node, self.module, {"inferredType": _constructor_name(node.value)})
        if self.class_stack and self.parent.id == self.class_stack[-1].id:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self._member(self.class_stack[-1], target.id, target, _constructor_name(node.value))


def _direct_scope_nodes(statement: ast.stmt) -> list[ast.Assign | ast.AnnAssign]:
    result: list[ast.Assign | ast.AnnAssign] = []

    class AssignmentVisitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            return

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            return

        def visit_Lambda(self, node: ast.Lambda) -> None:
            return

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            return

        def visit_Assign(self, node: ast.Assign) -> None:
            result.append(node)
            self.generic_visit(node)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            result.append(node)
            self.generic_visit(node)

    AssignmentVisitor().visit(statement)
    return result


def _constructor_name(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    return None


class CallVisitor(ast.NodeVisitor):
    def __init__(self, analyzer: Analyzer, source: Symbol, module: Symbol, cls: Symbol | None):
        self.a = analyzer
        self.source = source
        self.module = module
        self.cls = cls
        self.locals: dict[str, str] = {}

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_Assign(self, node: ast.Assign) -> None:
        typ = _constructor_name(node.value)
        if typ:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.locals[target.id] = typ
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        typ = _constructor_name(node.value) or _annotation(node.annotation)
        if typ and isinstance(node.target, ast.Name):
            self.locals[node.target.id] = typ
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        target, status, confidence = self.a.resolve_call(node.func, self.source, self.module,
                                                         self.cls, self.locals)
        label = _dotted(node.func) or ast.unparse(node.func)
        self.a._relationship(self.source, target, "calls", status, confidence, label,
                             self.source.file, node.lineno)
        self.generic_visit(node)

