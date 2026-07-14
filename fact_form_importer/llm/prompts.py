"""Prompt construction for LLM-assisted field normalisation."""

from __future__ import annotations

import json

from fact_form_importer.llm.schemas import LlmNormalisationRequest


SYSTEM_PROMPT = """You normalise selected court form fields for HMCTS FaCT import review.
Return only structured JSON matching the requested schema.
Use only the fields, vocabularies, and rules provided in the request.
Do not infer facts that are not present in the input.
Do not invent courts, floors, rooms, entrances, routes, services, or availability.
If a value is unclear, vague, contradictory, or cannot be safely mapped, keep it for human review.
For controlled vocabularies, return only exact allowed vocabulary values.
For address candidates, choose only a UPRN supplied in the request, or null.
Address candidate selections are advisory and must not reconstruct or invent an address.
Treat submitted_address.line_1 as a weaker matching signal: court or building
names there are often incomplete or slightly inaccurate. Do not reduce address
confidence solely because of a plausible line_1 discrepancy when the remaining
submitted address fields consistently identify one supplied candidate. Still
return null or require review when non-line_1 details conflict, more than one
candidate remains plausible, or a match would require inventing information.
Return one normalised_fields item for every selected field. If no safe value can
be returned, set that field's value to null and set its needs_human_review flag
appropriately. Put field uncertainty on that field, not only on the aggregate
response. Address-candidate ambiguity belongs in address_matches, not in the
aggregate response-level review flag.
"""


def build_llm_input(request: LlmNormalisationRequest) -> str:
    payload = request.model_dump(mode="json")
    return (
        "Normalise this court form record. "
        "Only return fields present in the request.\n\n"
        f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
    )
