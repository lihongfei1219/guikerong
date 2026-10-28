"""Find the product clause addressed by a professional explanation."""

import re
import unicodedata
from difflib import SequenceMatcher


def normalize(text: str) -> str:
    return "".join(char for char in unicodedata.normalize("NFKC", text).casefold() if char.isalnum())


def similarity(query: str, text: str) -> float:
    if query in text:
        return 1.0
    if len(query) < 4:
        return 0.0
    matcher = SequenceMatcher(None, query, text, autojunk=False)
    best = matcher.ratio()
    # Compare nearby passages too, so a short query can match a long clause.
    for block in sorted(matcher.get_matching_blocks(), key=lambda block: block.size, reverse=True)[:3]:
        if not block.size:
            continue
        start = max(0, block.b - block.a)
        margin = max(2, len(query) // 4)
        for left, right in ((start, start + len(query)), (max(0, start - margin), start + len(query) + margin)):
            best = max(best, SequenceMatcher(None, query, text[left:right], autojunk=False).ratio())
    return best


def match_condition(query: str, conditions: str) -> str | None:
    """Return the original matching passage; an unmatched query stays out of model input."""
    normalized = normalize(query)
    if len(normalized) < 2:
        return None
    clauses = [part.strip() for part in re.split(r"[；;。\r\n]+", conditions) if part.strip()]
    if conditions not in clauses:
        clauses.append(conditions)
    threshold = 0.85 if len(normalized) < 8 else 0.78
    best_clause, best_score = None, 0.0
    for clause in clauses:
        text = normalize(clause)
        if not text:
            continue
        score = similarity(normalized, text)
        if score > best_score:
            best_clause, best_score = clause, score
        if score == 1.0:
            break
    return best_clause if best_score >= threshold else None


def matching_knowledge(item: dict, product) -> dict | None:
    if item.get("product_id") and item["product_id"] != product.product_id:
        return None
    query = item.get("condition_quote", "")
    if not query:
        return dict(item) if not item.get("product_id") else None
    matched = match_condition(query, product.application_conditions)
    if matched is None:
        return None
    return {**item, "matched_condition": matched}
