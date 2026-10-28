"""Bounded, per-product continuation context and conservative model-result comparisons."""

from collections import Counter

from app.history import read_request, read_result
from app.matching_context import MATCHING_PROMPT_VERSION
from app.supplements import applicable_documents


def assessment_summary(product):
    eligibility = product.eligibility.model_dump(mode="json")
    return {
        "eligibility": {key: eligibility[key] for key in ("status", "assessment_basis", "explanation", "verification_needs")},
        "conditions": [{key: condition.get(key) for key in (
            "condition_id", "source_quote", "source_quote_verified", "status", "explanation",
            "required_material", "verification_needs", "assessment_basis",
        )} for condition in eligibility["condition_results"]],
    }


def continuation_context(runs, request, catalog):
    latest = {}
    for run in runs.values():
        if run.get("result"):
            for product in read_result(run["result"]).matches:
                latest[product.product_id] = (run, product)
    contexts = {}
    for product in catalog.products:
        previous = latest.get(product.product_id)
        if not previous:
            continue
        run, old_product = previous
        # A changed product contract needs a fresh evaluation, not old clause identities.
        if old_product.application_conditions != product.application_conditions or not run["snapshot"].get("request"):
            continue
        before = {d.document_id: d.model_dump(mode="json") for d in
                  applicable_documents(read_request(run["snapshot"]["request"]), product)}
        after = {d.document_id: d.model_dump(mode="json") for d in applicable_documents(request, product)}
        added = [key for key in after if key not in before]
        updated = [key for key in after if key in before and after[key] != before[key]]
        removed = [key for key in before if key not in after]
        previous_prompt_version = run["result"].get("versions", {}).get("prompt")
        contexts[product.product_id] = {
            "previous_prompt_version": previous_prompt_version,
            "current_prompt_version": MATCHING_PROMPT_VERSION,
            "policy_changed": previous_prompt_version != MATCHING_PROMPT_VERSION,
            "base_run_id": run["id"], "previous_assessment": assessment_summary(old_product),
            "added_document_ids": added, "updated_document_ids": updated, "removed_document_ids": removed,
            "focus_condition_quotes": list(dict.fromkeys(
                after[key]["human_supplement"]["condition_quote"] for key in added + updated
                if after[key].get("human_supplement") and after[key]["human_supplement"].get("condition_quote")
            )),
        }
    return contexts


def comparisons_for(run):
    if not run.get("result") or not run["snapshot"].get("continuations"):
        return {}
    return {product.product_id: comparison_for(run, product.product_id, product)
            for product in read_result(run["result"]).matches}


def comparison_for(run, product_id, product=None):
    context = run["snapshot"].get("continuations", {}).get(product_id)
    if not context or not run.get("result"):
        return None
    if product is None:
        product = next((p for p in read_result(run["result"]).matches if p.product_id == product_id), None)
    if product is None:
        return None
    current = assessment_summary(product)
    before = context["previous_assessment"]
    old_counts = Counter(c["source_quote"] for c in before["conditions"])
    new_counts = Counter(c["source_quote"] for c in current["conditions"])
    old = {c["source_quote"]: c for c in before["conditions"]}
    changes = []
    paired = set()
    for condition in current["conditions"]:
        quote = condition["source_quote"]
        previous = old.get(quote)
        # Model IDs can drift; pair only unique, verified, identical original quotes.
        comparable = (previous and old_counts[quote] == new_counts[quote] == 1 and
                      previous["source_quote_verified"] and condition["source_quote_verified"])
        if comparable:
            paired.add(quote)
        changes.append({"source_quote": quote, "condition_id": condition["condition_id"],
                        "before": previous["status"] if comparable else None,
                        "after": condition["status"], "explanation": condition["explanation"],
                        "required_material": condition["required_material"]})
    return {
        "base_run_id": context["base_run_id"], "before": before["eligibility"]["status"],
        "after": current["eligibility"]["status"], "changes": changes,
        "unpaired_previous_count": sum(c["source_quote"] not in paired for c in before["conditions"]),
        "added_document_ids": context["added_document_ids"],
        "updated_document_ids": context["updated_document_ids"],
        "removed_document_ids": context["removed_document_ids"],
    }
