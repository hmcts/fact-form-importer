"""Rate-limited, auditable Ordnance Survey address verification.

The importer deliberately calls the existing FaCT Data API address-search
endpoint rather than Ordnance Survey directly.  FaCT owns the OS credential and
the resulting verification can be reviewed with the rest of an import run.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from time import monotonic, sleep
from threading import Lock
from typing import Any, Literal, Protocol

from fact_form_importer.cleaners.postcodes import normalise_uk_postcode
from fact_form_importer.models.court_submission import Address, CourtSubmission, sync_cleaned_snapshot
from fact_form_importer.models.issues import Issue
from fact_form_importer.validators.base import add_issue_once

ADDRESS_OS_NORMALISED = "ADDRESS_OS_NORMALISED"
ADDRESS_OS_VERIFIED = "ADDRESS_OS_VERIFIED"
ADDRESS_OS_REVIEW_REQUIRED = "ADDRESS_OS_REVIEW_REQUIRED"
ADDRESS_OS_LOOKUP_UNAVAILABLE = "ADDRESS_OS_LOOKUP_UNAVAILABLE"

# 1.25 seconds stays within OS's 50-request/minute development/trial limit.
# Live projects can opt into 0.11 seconds through AppConfig, leaving headroom
# below the documented 600-request/minute limit.
DEFAULT_OS_MIN_INTERVAL_SECONDS = 1.25
AUTO_MATCH_THRESHOLD = 0.98
AUTO_MATCH_MARGIN = 0.05
MAX_REPORTED_CANDIDATES = 10

VerificationStatus = Literal[
    "auto_normalised",
    "verified",
    "review_required",
    "invalid_postcode",
    "unsupported_postcode_region",
    "no_os_result",
    "unavailable",
    "missing_postcode",
]

_ADDRESS_WORD_PATTERN = re.compile(r"[a-z0-9]+")
_CARE_OF_PATTERN = re.compile(r"\bc\s*/\s*o\b", re.IGNORECASE)
_UNSUPPORTED_POSTCODE_PREFIXES = re.compile(
    r"^(?:ZE|KW|IV|HS|PH|AB|DD|PA|FK|G\d|KY|KA|DG|EH|ML|TD|BT|IM|JE|GY)",
    re.IGNORECASE,
)


class LookupResponse(Protocol):
    """The small response surface needed from a FaCT API HTTP client."""

    status_code: int
    body: Any


@dataclass(frozen=True)
class OsAddressCandidate:
    uprn: str | None
    address: str | None
    organisation_name: str | None
    building_number: str | None
    building_name: str | None
    thoroughfare_name: str | None
    post_town: str | None
    postcode: str | None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "uprn": self.uprn,
            "address": self.address,
            "organisation_name": self.organisation_name,
            "building_number": self.building_number,
            "building_name": self.building_name,
            "thoroughfare_name": self.thoroughfare_name,
            "post_town": self.post_town,
            "postcode": self.postcode,
        }

    def proposed_address(self, source: Address) -> dict[str, str | None] | None:
        """Return a conservative FaCT address field mapping from OS DPA data."""

        premises = _join_parts(self.building_number, self.building_name, self.thoroughfare_name)
        organisation = _normalise_text(self.organisation_name)
        if organisation and premises:
            line_1, line_2 = organisation, premises
        elif organisation:
            line_1, line_2 = organisation, None
        elif premises:
            line_1, line_2 = premises, None
        else:
            # ADDRESS is useful review evidence but is not reliably split into
            # FaCT's two address lines, so never use it as a blind replacement.
            return None

        town = _normalise_text(self.post_town) or source.town_or_city
        postcode = _normalise_text(self.postcode) or source.postcode
        if not line_1 or not town or not postcode:
            return None
        return {
            "line_1": line_1,
            "line_2": line_2,
            "town_or_city": town,
            "county": source.county,
            "postcode": postcode,
        }


@dataclass
class AddressVerification:
    source_row_number: int
    court_slug: str | None
    address_index: int
    postcode: str | None
    status: VerificationStatus
    message: str
    original_address: dict[str, Any]
    proposed_address: dict[str, Any] | None = None
    selected_candidate: OsAddressCandidate | None = None
    candidates: list[OsAddressCandidate] = field(default_factory=list)
    match_score: float | None = None
    score_margin: float | None = None
    match_type: str | None = None
    llm_suggestion: dict[str, Any] | None = None

    @property
    def is_action_blocking(self) -> bool:
        return self.status in {
            "review_required",
            "invalid_postcode",
            "unsupported_postcode_region",
            "no_os_result",
            "missing_postcode",
        }

    def action_reason(self) -> str | None:
        if not self.is_action_blocking:
            return None
        return f"Address verification requires review: {self.message}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_row_number": self.source_row_number,
            "court_slug": self.court_slug,
            "address_index": self.address_index,
            "postcode": self.postcode,
            "status": self.status,
            "message": self.message,
            "original_address": self.original_address,
            "proposed_address": self.proposed_address,
            "selected_candidate": self.selected_candidate.as_dict() if self.selected_candidate else None,
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "match_score": self.match_score,
            "score_margin": self.score_margin,
            "match_type": self.match_type,
            "llm_suggestion": self.llm_suggestion,
        }


@dataclass
class AddressVerificationBatch:
    verifications: list[AddressVerification] = field(default_factory=list)
    enabled: bool = False
    unique_postcode_lookups: int = 0
    cache_hits: int = 0
    rate_limit_retries: int = 0

    def for_address(self, submission: CourtSubmission, address_index: int) -> AddressVerification | None:
        return next(
            (
                verification
                for verification in self.verifications
                if verification.source_row_number == submission.source.source_row_number
                and verification.address_index == address_index
            ),
            None,
        )

    def action_reason_for(self, submission: CourtSubmission, address_index: int) -> str | None:
        verification = self.for_address(submission, address_index)
        return verification.action_reason() if verification else None

    def action_evidence_for(self, submission: CourtSubmission, address_index: int) -> dict[str, Any] | None:
        verification = self.for_address(submission, address_index)
        return verification.as_dict() if verification else None

    def llm_candidates_for(self, submission: CourtSubmission) -> list[dict[str, Any]]:
        """Return only unresolved candidate data permitted in a row-level LLM request."""

        requests = []
        for verification in self.verifications:
            if verification.source_row_number != submission.source.source_row_number:
                continue
            if verification.status != "review_required" or not verification.candidates:
                continue
            original = verification.original_address
            requests.append(
                {
                    "address_index": verification.address_index,
                    # Postcode is deliberately absent: it is deterministic and
                    # every candidate came from the same postcode lookup.
                    "submitted_address": {
                        "line_1": original.get("line_1"),
                        "line_2": original.get("line_2"),
                        "town_or_city": original.get("town_or_city"),
                        "county": original.get("county"),
                    },
                    "candidates": [
                        {
                            "uprn": candidate.uprn,
                            "organisation_name": candidate.organisation_name,
                            "building_number": candidate.building_number,
                            "building_name": candidate.building_name,
                            "thoroughfare_name": candidate.thoroughfare_name,
                            "post_town": candidate.post_town,
                        }
                        for candidate in verification.candidates
                        if candidate.uprn
                    ],
                }
            )
        return [request for request in requests if request["candidates"]]

    def record_llm_suggestion(
        self,
        submission: CourtSubmission,
        address_index: int,
        uprn: str | None,
        confidence: str,
        needs_human_review: bool,
        reason: str,
    ) -> bool:
        verification = self.for_address(submission, address_index)
        if verification is None or verification.status != "review_required":
            return False
        allowed_uprns = {candidate.uprn for candidate in verification.candidates if candidate.uprn}
        if uprn is not None and uprn not in allowed_uprns:
            return False
        verification.llm_suggestion = {
            "uprn": uprn,
            "confidence": confidence,
            "needs_human_review": needs_human_review,
            "reason": reason,
        }
        return True

    def as_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for verification in self.verifications:
            counts[verification.status] = counts.get(verification.status, 0) + 1
        return {
            "enabled": self.enabled,
            "unique_postcode_lookups": self.unique_postcode_lookups,
            "cache_hits": self.cache_hits,
            "rate_limit_retries": self.rate_limit_retries,
            "counts_by_status": dict(sorted(counts.items())),
            "action_blocking_count": sum(
                verification.is_action_blocking for verification in self.verifications
            ),
            "verifications": [verification.as_dict() for verification in self.verifications],
        }

    def summary_metrics(self) -> dict[str, int | bool]:
        report = self.as_dict()
        counts = report["counts_by_status"]
        action_blocking_verifications = [
            verification for verification in self.verifications if verification.is_action_blocking
        ]
        return {
            "address_verification_enabled": self.enabled,
            "address_verification_count": len(self.verifications),
            "address_verification_unique_postcode_lookups": self.unique_postcode_lookups,
            "address_verification_cache_hits": self.cache_hits,
            "address_verification_rate_limit_retries": self.rate_limit_retries,
            "address_verification_auto_normalised_count": int(counts.get("auto_normalised", 0)),
            "address_verification_verified_count": int(counts.get("verified", 0)),
            "address_verification_review_required_count": int(counts.get("review_required", 0)),
            "address_verification_no_os_result_count": int(counts.get("no_os_result", 0)),
            "address_verification_invalid_postcode_count": int(counts.get("invalid_postcode", 0)),
            "address_verification_unsupported_postcode_region_count": int(
                counts.get("unsupported_postcode_region", 0)
            ),
            "address_verification_missing_postcode_count": int(counts.get("missing_postcode", 0)),
            "address_verification_action_blocking_count": int(report["action_blocking_count"]),
            "address_verification_action_blocking_submission_count": len(
                {verification.source_row_number for verification in action_blocking_verifications}
            ),
            "address_verification_unavailable_count": int(counts.get("unavailable", 0)),
        }


class RateLimitedPostcodeLookup:
    """Cache unique postcode lookups and protect the upstream OS rate limit."""

    def __init__(
        self,
        lookup: Callable[[str], LookupResponse],
        *,
        min_interval_seconds: float = DEFAULT_OS_MIN_INTERVAL_SECONDS,
        now: Callable[[], float] = monotonic,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        self.lookup = lookup
        self.min_interval_seconds = min_interval_seconds
        self.now = now
        self.sleeper = sleeper
        self.cache: dict[str, LookupResponse] = {}
        self.last_request_at: float | None = None
        self.unique_postcode_lookups = 0
        self.cache_hits = 0
        self.rate_limit_retries = 0
        self._lock = Lock()

    def set_lookup(self, lookup: Callable[[str], LookupResponse]) -> None:
        """Replace the transport callback while retaining rate-limit state.

        The execution layer can use a short-lived HTTP client per operation,
        whereas an import run uses one callback for its whole lifetime. Keeping
        the cache and the most recent request time here protects the common
        FaCT/OS endpoint in both cases.
        """

        with self._lock:
            self.lookup = lookup

    def get(self, postcode: str) -> LookupResponse:
        # Hold the lock across the request. This is deliberately conservative:
        # it prevents two UI requests from bypassing the one-request-per-second
        # upstream limit at the same time.
        with self._lock:
            if postcode in self.cache:
                self.cache_hits += 1
                return self.cache[postcode]

            response = self._request(postcode)
            if response.status_code == 429:
                self.rate_limit_retries += 1
                self.sleeper(max(self.min_interval_seconds, 2.0))
                response = self._request(postcode)
            self.cache[postcode] = response
            return response

    def _request(self, postcode: str) -> LookupResponse:
        if self.last_request_at is not None:
            remaining = self.min_interval_seconds - (self.now() - self.last_request_at)
            if remaining > 0:
                self.sleeper(remaining)
        response = self.lookup(postcode)
        self.last_request_at = self.now()
        self.unique_postcode_lookups += 1
        return response


def verify_submission_addresses(
    submissions: list[CourtSubmission],
    lookup: Callable[[str], LookupResponse],
    *,
    min_interval_seconds: float = DEFAULT_OS_MIN_INTERVAL_SECONDS,
    now: Callable[[], float] = monotonic,
    sleeper: Callable[[float], None] = sleep,
) -> AddressVerificationBatch:
    """Verify imported addresses against FaCT's existing OS lookup endpoint.

    Only a unique, high-confidence OS candidate updates a submission.  Every
    other outcome stays in the review artifact and can block only the address
    action that needs manual attention.
    """

    limiter = RateLimitedPostcodeLookup(
        lookup,
        min_interval_seconds=min_interval_seconds,
        now=now,
        sleeper=sleeper,
    )
    batch = AddressVerificationBatch(enabled=True)

    for submission in submissions:
        changed = False
        for address in submission.addresses:
            verification, did_change = _verify_address(submission, address, limiter)
            batch.verifications.append(verification)
            changed = changed or did_change
        if changed:
            sync_cleaned_snapshot(submission)

    batch.unique_postcode_lookups = limiter.unique_postcode_lookups
    batch.cache_hits = limiter.cache_hits
    batch.rate_limit_retries = limiter.rate_limit_retries
    return batch


def _verify_address(
    submission: CourtSubmission,
    address: Address,
    limiter: RateLimitedPostcodeLookup,
) -> tuple[AddressVerification, bool]:
    original = address.model_dump(mode="json")
    field = f"addresses[{address.index}]"
    postcode_result = normalise_uk_postcode(address.postcode, f"{field}.postcode")
    postcode = postcode_result.value
    if postcode is None:
        return _verification(
            submission, address, None, "missing_postcode", "Address has no postcode to verify", original
        ), False
    if any(issue.code == "INVALID_POSTCODE" for issue in postcode_result.issues):
        return _verification(
            submission,
            address,
            postcode,
            "invalid_postcode",
            "Postcode is not valid enough for an Ordnance Survey lookup",
            original,
        ), False
    if _UNSUPPORTED_POSTCODE_PREFIXES.match(postcode.replace(" ", "")):
        return _verification(
            submission,
            address,
            postcode,
            "unsupported_postcode_region",
            "Postcode region is not supported by the FaCT API",
            original,
        ), False

    try:
        response = limiter.get(postcode)
    except Exception as exc:
        _add_lookup_unavailable_issue(submission, field, address.postcode, type(exc).__name__)
        return _verification(
            submission,
            address,
            postcode,
            "unavailable",
            "FaCT/Ordnance Survey lookup was unavailable; address was not changed",
            original,
        ), False

    if response.status_code != 200:
        message = _response_message(response.body)
        if response.status_code in {400, 404}:
            result = _verification(
                submission,
                address,
                postcode,
                "no_os_result",
                f"FaCT/Ordnance Survey returned no address result{': ' + message if message else ''}",
                original,
            )
            _add_review_issue(submission, field, original, result.message)
            return result, False
        _add_lookup_unavailable_issue(submission, field, address.postcode, f"HTTP {response.status_code}")
        return _verification(
            submission,
            address,
            postcode,
            "unavailable",
            f"FaCT/Ordnance Survey lookup was unavailable (HTTP {response.status_code})",
            original,
        ), False

    candidates = _parse_candidates(response.body)
    if not candidates:
        result = _verification(
            submission,
            address,
            postcode,
            "no_os_result",
            "FaCT/Ordnance Survey returned no usable address candidates",
            original,
        )
        _add_review_issue(submission, field, original, result.message)
        return result, False

    selected, score, margin, match_type = _select_candidate(address, candidates)
    if selected is None:
        result = _verification(
            submission,
            address,
            postcode,
            "review_required",
            "No unique, high-confidence Ordnance Survey address match was found",
            original,
            candidates=candidates,
            match_score=score,
            score_margin=margin,
        )
        _add_review_issue(submission, field, original, result.message)
        return result, False

    proposed = selected.proposed_address(address)
    if proposed is None:
        result = _verification(
            submission,
            address,
            postcode,
            "review_required",
            "An Ordnance Survey candidate matched but could not be safely mapped to FaCT address fields",
            original,
            selected_candidate=selected,
            candidates=candidates,
            match_score=score,
            score_margin=margin,
            match_type=match_type,
        )
        _add_review_issue(submission, field, original, result.message)
        return result, False

    changed = any(getattr(address, key) != value for key, value in proposed.items())
    if changed:
        for key, value in proposed.items():
            setattr(address, key, value)
        verification = _verification(
            submission,
            address,
            postcode,
            "auto_normalised",
            "Address was normalised from a unique, high-confidence Ordnance Survey match",
            original,
            proposed_address=address.model_dump(mode="json"),
            selected_candidate=selected,
            candidates=candidates,
            match_score=score,
            score_margin=margin,
            match_type=match_type,
        )
        add_issue_once(
            submission,
            Issue(
                field=field,
                code=ADDRESS_OS_NORMALISED,
                severity="warning",
                message=verification.message,
                raw_value=original,
                cleaned_value={
                    "uprn": selected.uprn,
                    "match_score": score,
                    "match_type": match_type,
                    "address": verification.proposed_address,
                },
            ),
        )
        return verification, True

    verification = _verification(
        submission,
        address,
        postcode,
        "verified",
        "Address matched a unique Ordnance Survey candidate without needing changes",
        original,
        proposed_address=address.model_dump(mode="json"),
        selected_candidate=selected,
        candidates=candidates,
        match_score=score,
        score_margin=margin,
        match_type=match_type,
    )
    add_issue_once(
        submission,
        Issue(
            field=field,
            code=ADDRESS_OS_VERIFIED,
            severity="info",
            message=verification.message,
            raw_value=original,
            cleaned_value={"uprn": selected.uprn, "match_score": score, "match_type": match_type},
        ),
    )
    return verification, False


def _verification(
    submission: CourtSubmission,
    address: Address,
    postcode: str | None,
    status: VerificationStatus,
    message: str,
    original: dict[str, Any],
    **kwargs: Any,
) -> AddressVerification:
    return AddressVerification(
        source_row_number=submission.source.source_row_number,
        court_slug=submission.court_slug,
        address_index=address.index,
        postcode=postcode,
        status=status,
        message=message,
        original_address=original,
        **kwargs,
    )


def _parse_candidates(body: Any) -> list[OsAddressCandidate]:
    if not isinstance(body, dict) or not isinstance(body.get("results"), list):
        return []
    candidates = []
    seen = set()
    for result in body["results"]:
        if not isinstance(result, dict):
            continue
        dpa = result.get("DPA") or result.get("dpa")
        if not isinstance(dpa, dict):
            continue
        candidate = OsAddressCandidate(
            uprn=_normalise_text(dpa.get("UPRN") or dpa.get("uprn")),
            address=_normalise_text(dpa.get("ADDRESS") or dpa.get("address")),
            organisation_name=_normalise_text(dpa.get("ORGANISATION_NAME") or dpa.get("organisationName")),
            building_number=_normalise_text(dpa.get("BUILDING_NUMBER") or dpa.get("buildingNumber")),
            building_name=_normalise_text(dpa.get("BUILDING_NAME") or dpa.get("buildingName")),
            thoroughfare_name=_normalise_text(dpa.get("THOROUGHFARE_NAME") or dpa.get("thoroughfareName")),
            post_town=_normalise_text(dpa.get("POST_TOWN") or dpa.get("postTown")),
            postcode=_normalise_text(dpa.get("POSTCODE") or dpa.get("postcode")),
        )
        identity = candidate.uprn or candidate.address
        if not identity or identity in seen:
            continue
        seen.add(identity)
        candidates.append(candidate)
        if len(candidates) == MAX_REPORTED_CANDIDATES:
            break
    return candidates


def _select_candidate(
    address: Address, candidates: list[OsAddressCandidate]
) -> tuple[OsAddressCandidate | None, float | None, float | None, str | None]:
    scored = sorted(
        ((_candidate_score(address, candidate), candidate) for candidate in candidates),
        key=lambda item: item[0][0],
        reverse=True,
    )
    if not scored:
        return None, None, None, None
    (score, match_type), candidate = scored[0]
    runner_up = scored[1][0][0] if len(scored) > 1 else 0.0
    margin = round(score - runner_up, 4)
    if score >= AUTO_MATCH_THRESHOLD and (len(scored) == 1 or margin >= AUTO_MATCH_MARGIN):
        return candidate, score, margin, match_type
    return None, score, margin, None


def _candidate_score(address: Address, candidate: OsAddressCandidate) -> tuple[float, str]:
    # DPA does not reliably return a county, so county must not prevent an
    # otherwise exact court address from being recognised.
    source = _canonical_address(address.line_1, address.line_2, address.town_or_city)
    candidate_values = [
        _canonical_address(candidate.organisation_name, candidate.post_town),
        _canonical_address(
            candidate.organisation_name,
            candidate.building_number,
            candidate.building_name,
            candidate.thoroughfare_name,
            candidate.post_town,
        ),
        _canonical_address(candidate.address),
    ]
    candidate_values = [value for value in candidate_values if value]
    if not source or not candidate_values:
        return 0.0, "insufficient_data"

    source_tokens = set(source.split())
    source_town = _canonical_address(address.town_or_city)
    best_score = 0.0
    best_type = "similarity"
    for value in candidate_values:
        candidate_tokens = set(value.split())
        sequence = SequenceMatcher(None, source, value).ratio()
        coverage = len(source_tokens & candidate_tokens) / len(source_tokens)
        town_matches = not source_town or source_town in value
        if town_matches and source == value:
            score, match_type = 1.0, "canonical_exact_match"
        elif town_matches and source_tokens <= candidate_tokens and len(source_tokens) >= 2:
            score, match_type = 0.99, "canonical_token_match"
        else:
            score, match_type = round(0.7 * coverage + 0.3 * sequence, 4), "similarity"
        if score > best_score:
            best_score, best_type = score, match_type
    return best_score, best_type


def _canonical_address(*values: object) -> str:
    text = " ".join(str(value) for value in values if value)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = _CARE_OF_PATTERN.sub("care of", text)
    text = text.replace("&", " and ").lower()
    # Address abbreviations regularly differ between a Forms export and DPA.
    words = _ADDRESS_WORD_PATTERN.findall(text)
    aliases = {"st": "street", "rd": "road", "ave": "avenue", "ct": "court"}
    return " ".join(aliases.get(word, word) for word in words)


def _join_parts(*values: str | None) -> str | None:
    parts = [_normalise_text(value) for value in values]
    joined = " ".join(part for part in parts if part)
    return joined or None


def _normalise_text(value: object) -> str | None:
    if value is None:
        return None
    collapsed = re.sub(r"\s+", " ", str(value)).strip()
    return collapsed or None


def _response_message(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    message = body.get("message") or body.get("error")
    return message.strip()[:500] if isinstance(message, str) and message.strip() else None


def _add_review_issue(
    submission: CourtSubmission, field: str, original: dict[str, Any], message: str
) -> None:
    add_issue_once(
        submission,
        Issue(
            field=field,
            code=ADDRESS_OS_REVIEW_REQUIRED,
            severity="warning",
            message=message,
            raw_value=original,
            cleaned_value=None,
        ),
    )


def _add_lookup_unavailable_issue(
    submission: CourtSubmission, field: str, raw_value: object, detail: str
) -> None:
    add_issue_once(
        submission,
        Issue(
            field=field,
            code=ADDRESS_OS_LOOKUP_UNAVAILABLE,
            severity="info",
            message="FaCT/Ordnance Survey lookup was unavailable; address was not changed",
            raw_value=raw_value,
            cleaned_value={"detail": detail},
        ),
    )
