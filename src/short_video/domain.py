from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class RequestError(ValueError):
    pass


@dataclass(frozen=True)
class VideoRequest:
    request_id: str
    topic: str
    audience: str
    platform: str
    target_duration_sec: int | None
    style: str = "自然口语"
    notes: str = ""
    missing_fields: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: Any) -> "VideoRequest":
        if not isinstance(data, dict):
            raise RequestError("input must be a JSON object")
        request_id = str(data.get("request_id") or "").strip()
        if not request_id:
            raise RequestError("request_id is required")
        topic = str(data.get("topic") or "").strip()
        platform = str(data.get("platform") or "").strip()
        duration_raw = data.get("target_duration_sec")
        duration: int | None = None
        missing: list[str] = []
        if not topic:
            missing.append("topic")
        if not platform:
            missing.append("platform")
        try:
            if duration_raw is not None and str(duration_raw).strip() != "":
                duration = int(duration_raw)
        except (TypeError, ValueError):
            duration = None
        if duration is None or duration <= 0:
            missing.append("target_duration_sec")
        return cls(
            request_id=request_id,
            topic=topic,
            audience=str(data.get("audience") or "通用受众").strip() or "通用受众",
            platform=platform,
            target_duration_sec=duration,
            style=str(data.get("style") or "自然口语").strip() or "自然口语",
            notes=str(data.get("notes") or ""),
            missing_fields=tuple(missing),
        )

    @property
    def needs_input(self) -> bool:
        return bool(self.missing_fields)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "topic": self.topic,
            "audience": self.audience,
            "platform": self.platform,
            "target_duration_sec": self.target_duration_sec,
            "style": self.style,
            "notes": self.notes,
            "missing_fields": list(self.missing_fields),
        }


def load_request(path: str | Path) -> VideoRequest:
    return VideoRequest.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def _tokens(text: str) -> set[str]:
    text = text.lower()
    tokens: set[str] = set(re.findall(r"[a-z0-9_]+", text))
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(run) == 1:
            tokens.add(run)
        for size in (2, 3):
            for index in range(0, len(run) - size + 1):
                tokens.add(run[index : index + size])
    return tokens


@dataclass
class MaterialStore:
    materials: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, data_dir: str | Path) -> "MaterialStore":
        raw = json.loads((Path(data_dir) / "materials.json").read_text(encoding="utf-8"))
        materials = raw.get("materials") if isinstance(raw, dict) else raw
        if not isinstance(materials, list):
            raise RequestError("materials.json must contain a materials list")
        return cls(materials=materials)

    def search(self, query: str) -> list[dict[str, Any]]:
        query_tokens = _tokens(query)
        if not query_tokens:
            return []
        hits: list[dict[str, Any]] = []
        for material in self.materials:
            text_tokens = _tokens(
                " ".join(
                    [
                        str(material.get("title") or ""),
                        " ".join(str(fact) for fact in material.get("facts", [])),
                        " ".join(str(tag) for tag in material.get("tags", [])),
                    ]
                )
            )
            score = len(query_tokens & text_tokens)
            if score > 0:
                hits.append(
                    {
                        "id": material["id"],
                        "title": material.get("title"),
                        "facts": list(material.get("facts", [])),
                        "score": score,
                    }
                )
        hits.sort(key=lambda item: (-item["score"], item["id"]))
        return hits

    def get(self, material_id: str) -> dict[str, Any] | None:
        return next((item for item in self.materials if item.get("id") == material_id), None)


@dataclass
class BrandRules:
    banned_claims: list[str] = field(default_factory=list)
    max_duration_sec: int = 60
    min_duration_sec: int = 15
    require_source: bool = True

    @classmethod
    def load(cls, data_dir: str | Path) -> "BrandRules":
        raw = json.loads((Path(data_dir) / "brand_rules.json").read_text(encoding="utf-8"))
        return cls(
            banned_claims=list(raw.get("banned_claims") or []),
            max_duration_sec=int(raw.get("max_duration_sec") or 60),
            min_duration_sec=int(raw.get("min_duration_sec") or 15),
            require_source=bool(raw.get("require_source", True)),
        )


@dataclass
class Templates:
    base: list[dict[str, Any]] = field(default_factory=list)
    compact: list[dict[str, Any]] = field(default_factory=list)
    compressed: list[dict[str, Any]] = field(default_factory=list)
    min_duration_sec: int = 28

    @classmethod
    def load(cls, data_dir: str | Path) -> "Templates":
        raw = json.loads((Path(data_dir) / "templates.json").read_text(encoding="utf-8"))
        return cls(
            base=list(raw.get("base") or []),
            compact=list(raw.get("compact") or []),
            compressed=list(raw.get("compressed") or []),
            min_duration_sec=int(raw.get("min_duration_sec") or 28),
        )
