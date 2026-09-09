"""Feature dictionary backed by a version-controlled JSON file."""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..models import FeatureMatch


_TOKEN_PATTERN = re.compile(r"[0-9A-Za-z가-힣_-]+")


def _tokens(value: str) -> set[str]:
    return {token.casefold() for token in _TOKEN_PATTERN.findall(value) if len(token) > 1}


class JsonFeatureCatalog:
    """Search deploy-time product capabilities without model or ERP access."""

    def __init__(self, source_path: str | Path):
        self.source_path = Path(source_path)
        raw = json.loads(self.source_path.read_text(encoding="utf-8"))
        self._features = [FeatureMatch.model_validate(entry) for entry in raw]

    def search(self, query: str, *, limit: int = 5) -> list[FeatureMatch]:
        # A deterministic overlap score is enough for this menu-sized catalog
        # and avoids embedding latency for stable product capabilities.
        query_tokens = _tokens(query)
        normalized_query = query.casefold().strip()
        scored: list[tuple[int, FeatureMatch]] = []
        for feature in self._features:
            title = feature.title.casefold()
            searchable = " ".join(
                [feature.title, feature.summary, *feature.keywords, *feature.steps]
            )
            feature_tokens = _tokens(searchable)
            score = len(query_tokens & feature_tokens) * 4
            if normalized_query and normalized_query in searchable.casefold():
                score += 10
            if any(keyword.casefold() in normalized_query for keyword in feature.keywords):
                score += 6
            if title in normalized_query:
                score += 8
            if score:
                scored.append((score, feature))
        scored.sort(key=lambda pair: (-pair[0], pair[1].title))
        return [feature for _, feature in scored[:limit]]

    def all(self) -> list[FeatureMatch]:
        return list(self._features)
