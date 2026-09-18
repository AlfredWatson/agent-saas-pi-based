from __future__ import annotations

import re
import unicodedata


def normalize_entity(name: str) -> str:
    normalized = unicodedata.normalize("NFKC", name).casefold().strip()
    normalized = re.sub(r"[^\w\s-]", " ", normalized, flags=re.UNICODE)
    return re.sub(r"\s+", " ", normalized).strip()


def canonical_key(entity_type: str, name: str) -> str:
    return f"{normalize_entity(entity_type)}:{normalize_entity(name)}"


def merge_property_maps(target: dict, incoming: dict) -> dict:
    result = dict(target)
    for key, value in incoming.items():
        if key not in result:
            result[key] = value
            continue
        current = result[key]
        values = current if isinstance(current, list) else [current]
        additions = value if isinstance(value, list) else [value]
        for item in additions:
            if item not in values:
                values.append(item)
        result[key] = values[0] if len(values) == 1 else values
    return result


def reciprocal_rank_fusion(
    rankings: list[tuple[list[str], float]], *, rrf_k: int = 60
) -> list[tuple[str, float]]:
    scores: dict[str, float] = {}
    for ranking, weight in rankings:
        for rank, item_id in enumerate(ranking, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + weight / (rrf_k + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))
