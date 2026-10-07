from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Symbol:
    id: str
    kind: str
    name: str
    qualified_name: str
    file: str
    start_line: int
    end_line: int
    parent_id: str | None = None
    module_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "qualifiedName": self.qualified_name,
            "file": self.file,
            "startLine": self.start_line,
            "endLine": self.end_line,
            "parentId": self.parent_id,
            "moduleId": self.module_id,
            "details": self.details,
        }


@dataclass
class Relationship:
    source_id: str
    target_id: str | None
    type: str
    status: str
    confidence: str
    label: str
    file: str
    line: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "sourceId": self.source_id,
            "targetId": self.target_id,
            "type": self.type,
            "status": self.status,
            "confidence": self.confidence,
            "label": self.label,
            "file": self.file,
            "line": self.line,
        }


@dataclass
class FileUnit:
    path: str
    rel_path: str
    module_name: str
    source: str
    tree: Any | None
    error: str | None = None

