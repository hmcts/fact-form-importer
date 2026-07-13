"""Reusable end-to-end workbook processing for the CLI and local web UI."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from fact_form_importer.config import AppConfig, load_default_field_rules
from fact_form_importer.execution.fact_api import FactApiExecutionClient
from fact_form_importer.ingest.workbook_profiler import profile_to_json, profile_workbook
from fact_form_importer.ingest.workbook_reader import ingest_workbook
from fact_form_importer.llm.client import validate_openai_config
from fact_form_importer.llm.pipeline import LlmUsageMetrics, normalise_submissions_with_llm
from fact_form_importer.output.archive import ArchiveResult, publish_run_archive, stage_path
from fact_form_importer.output.duplicates_workbook import write_duplicate_review_workbook
from fact_form_importer.output.fact_api_manifest import build_fact_api_import_manifest
from fact_form_importer.output.logs import OutputResult, new_run_id, write_processing_outputs
from fact_form_importer.output.nsu_workbook import write_nsu_review_workbook
from fact_form_importer.output.submitters import SubmitterOutputResult, write_submitter_outputs
from fact_form_importer.validators.base import clear_validation_issues
from fact_form_importer.validators.business_rules import validate_all_submissions
from fact_form_importer.validators.fact_api_courts import (
    CourtReference,
    court_slug_exists_in_fact_api,
    lookup_court_by_slug_in_fact_api,
    suggest_court_slug_in_fact_api,
)
from fact_form_importer.validators.fact_api_vocabularies import load_vocabularies_from_fact_api
from fact_form_importer.validators.os_addresses import (
    AddressVerificationBatch,
    verify_submission_addresses,
)
from fact_form_importer.validators.vocabularies import Vocabularies, load_vocabularies


@dataclass(frozen=True)
class ProcessingResult:
    run_id: str
    archive: ArchiveResult
    output: OutputResult
    review_workbook_path: Path
    duplicate_review_workbook_path: Path
    submitters: SubmitterOutputResult
    address_verification_report_path: Path


def process_workbook(
    input_path: Path,
    output_root: Path,
    *,
    allow_local_vocabularies: bool = False,
    use_llm: bool = False,
    verify_addresses: bool = False,
    source_name: str | None = None,
    config: AppConfig | None = None,
) -> ProcessingResult:
    """Create a complete immutable run archive without sending API write requests."""

    app_config = config or AppConfig()
    if use_llm and not app_config.llm_enabled:
        raise ValueError("--use-llm requires LLM_ENABLED=true in .env")
    if use_llm:
        validate_openai_config(app_config, command_name="run --use-llm")
    if verify_addresses and (
        not app_config.fact_data_api_base_url or not app_config.fact_data_api_bearer_token
    ):
        raise ValueError("--verify-addresses requires FACT_DATA_API_BASE_URL and FACT_DATA_API_BEARER_TOKEN")

    run_id = new_run_id()
    staging = stage_path(output_root, run_id)
    try:
        staging.mkdir(parents=True, exist_ok=False)
        workbook_profile = profile_workbook(input_path)
        ingest_result = ingest_workbook(input_path=input_path, output_path=staging)
        vocabularies, vocabulary_source, court_slug_exists, court_slug_suggester = (
            load_fact_api_services(
                config=app_config,
                allow_local_vocabularies=allow_local_vocabularies,
            )
        )
        submissions = validate_all_submissions(
            ingest_result.submissions,
            vocabularies,
            court_slug_exists=court_slug_exists,
            court_slug_suggester=court_slug_suggester,
        )
        address_verifications = AddressVerificationBatch(enabled=False)
        if verify_addresses:
            address_verifications = verify_addresses_with_fact_api(submissions, app_config)
        llm_metrics = LlmUsageMetrics()
        if use_llm:
            llm_options = {
                "field_rules": load_default_field_rules(app_config),
                "vocabularies": vocabularies,
                "config": app_config,
            }
            if verify_addresses:
                llm_options["address_verifications"] = address_verifications
            llm_result = normalise_submissions_with_llm(
                submissions,
                **llm_options,
            )
            submissions = llm_result.submissions
            llm_metrics = llm_result.metrics
        if use_llm or verify_addresses:
            clear_validation_issues(submissions)
            submissions = validate_all_submissions(
                submissions,
                vocabularies,
                court_slug_exists=court_slug_exists,
                court_slug_suggester=court_slug_suggester,
            )

        # Ingestion writes this file before validation. Replace it with the final
        # batch so archive/UI status tables agree with all review outputs.
        (staging / "submissions_cleaned.json").write_text(
            json.dumps(
                [submission.model_dump(mode="json") for submission in submissions],
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        (staging / "profile.json").write_text(profile_to_json(workbook_profile) + "\n", encoding="utf-8")
        (staging / "address_verification_report.json").write_text(
            json.dumps(address_verifications.as_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        # Local-vocabulary fallback is an inspection mode. Do not make a second
        # API dependency attempt to resolve court UUIDs after that fallback.
        court_lookup = _court_lookup(app_config) if vocabulary_source == "fact_data_api" else None
        manifest_result = build_fact_api_import_manifest(
            submissions,
            run_id=run_id,
            vocabularies=vocabularies,
            court_lookup=court_lookup,
            address_verifications=address_verifications if verify_addresses else None,
        )
        (staging / "api_readiness_report.json").write_text(
            json.dumps(manifest_result.manifest.model_dump(mode="json"), indent=2, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )
        output_result = write_processing_outputs(
            submissions=submissions,
            ingest_result=ingest_result,
            workbook_profile=workbook_profile,
            output_path=staging,
            run_id=run_id,
            vocabulary_source=vocabulary_source,
            llm_enabled=app_config.llm_enabled,
            llm_requested=use_llm,
            llm_metrics=llm_metrics.as_dict(app_config.openai_model if use_llm else None),
            api_manifest_metrics=manifest_result.metrics,
            source_name=source_name or input_path.name,
            vocabularies=vocabularies,
            court_lookup=court_lookup,
            address_verification_metrics=address_verifications.summary_metrics(),
        )
        review_workbook_path = write_nsu_review_workbook(
            submissions=submissions,
            output_path=staging,
            summary=output_result.summary,
            address_verifications=address_verifications.verifications,
        )
        duplicate_review_workbook_path = write_duplicate_review_workbook(
            submissions=submissions,
            output_path=staging,
            summary=output_result.summary,
        )
        submitters = write_submitter_outputs(submissions=submissions, output_path=staging)
        archive = publish_run_archive(
            output_root=output_root,
            staging_path=staging,
            run_id=run_id,
            source_name=source_name or input_path.name,
            summary=output_result.summary,
        )
        return ProcessingResult(
            run_id=run_id,
            archive=archive,
            output=output_result,
            review_workbook_path=archive.archive_path / review_workbook_path.name,
            duplicate_review_workbook_path=archive.archive_path / duplicate_review_workbook_path.name,
            submitters=SubmitterOutputResult(
                json_path=archive.archive_path / submitters.json_path.name,
                workbook_path=archive.archive_path / submitters.workbook_path.name,
                user_count=submitters.user_count,
                excluded_user_count=submitters.excluded_user_count,
            ),
            address_verification_report_path=archive.archive_path / "address_verification_report.json",
        )
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise


def load_fact_api_services(
    config: AppConfig | None = None,
    allow_local_vocabularies: bool = False,
) -> tuple[
    Vocabularies | None,
    str,
    Callable[[str], bool] | None,
    Callable[[str, str | None], object] | None,
]:
    """Load validated FaCT reference data and read-only court validation services."""

    app_config = config or AppConfig()
    path = app_config.vocabularies_path
    local_vocabularies = load_vocabularies(path) if path.exists() else None

    if not app_config.fact_data_api_base_url or not app_config.fact_data_api_bearer_token:
        if allow_local_vocabularies:
            return local_vocabularies, "local_json" if local_vocabularies else "none", None, None
        missing = (
            "FACT_DATA_API_BASE_URL"
            if not app_config.fact_data_api_base_url
            else "FACT_DATA_API_BEARER_TOKEN"
        )
        raise ValueError(f"{missing} is required for run")

    def court_slug_exists(court_slug: str) -> bool:
        try:
            return court_slug_exists_in_fact_api(
                court_slug=court_slug,
                base_url=app_config.fact_data_api_base_url or "",
                bearer_token=app_config.fact_data_api_bearer_token or "",
            )
        except Exception as exc:
            raise ValueError(f"Unable to validate court slug '{court_slug}' against FaCT API: {exc}") from exc

    def court_slug_suggester(court_slug: str, raw_value: str | None):
        try:
            return suggest_court_slug_in_fact_api(
                court_slug=court_slug,
                raw_value=raw_value,
                base_url=app_config.fact_data_api_base_url or "",
                bearer_token=app_config.fact_data_api_bearer_token or "",
            )
        except Exception as exc:
            raise ValueError(f"Unable to suggest court slug for '{court_slug}' against FaCT API: {exc}") from exc

    try:
        return (
            load_vocabularies_from_fact_api(
                base_url=app_config.fact_data_api_base_url,
                bearer_token=app_config.fact_data_api_bearer_token,
                fallback=local_vocabularies,
            ),
            "fact_data_api",
            court_slug_exists,
            court_slug_suggester,
        )
    except Exception as exc:
        if not allow_local_vocabularies or local_vocabularies is None:
            raise ValueError(f"Unable to load FaCT API vocabularies: {exc}") from exc
        return local_vocabularies, "local_json_fallback_after_fact_data_api_error", None, None


def verify_addresses_with_fact_api(
    submissions,
    config: AppConfig,
) -> AddressVerificationBatch:
    """Use FaCT's authenticated OS proxy once per unique postcode in this run.

    The importer deliberately does not read an OS credential or call Ordnance
    Survey directly. FaCT owns that integration and its response is retained
    as review evidence for the run.
    """

    from urllib.parse import quote

    client = FactApiExecutionClient(config)
    try:
        return verify_submission_addresses(
            submissions,
            lambda postcode: client.get(f"/search/address/v1/postcode/{quote(postcode, safe='')}"),
            min_interval_seconds=config.os_address_min_interval_seconds,
        )
    finally:
        client.close()


def _court_lookup(config: AppConfig) -> Callable[[str], CourtReference | None] | None:
    if not config.fact_data_api_base_url or not config.fact_data_api_bearer_token:
        return None
    cache: dict[str, CourtReference | None] = {}

    def lookup(court_slug: str) -> CourtReference | None:
        if court_slug not in cache:
            try:
                cache[court_slug] = lookup_court_by_slug_in_fact_api(
                    court_slug=court_slug,
                    base_url=config.fact_data_api_base_url or "",
                    bearer_token=config.fact_data_api_bearer_token or "",
                )
            except Exception as exc:
                raise ValueError(
                    f"Unable to resolve FaCT court UUID for '{court_slug}': {type(exc).__name__}"
                ) from exc
        return cache[court_slug]

    return lookup
