import json

import pytest

from fact_form_importer.config import AppConfig
from fact_form_importer.ingest.workbook_profiler import WorkbookProfile
from fact_form_importer.ingest.workbook_reader import IngestResult
from fact_form_importer.models.court_submission import CourtSubmission
from fact_form_importer.models.source import SourceMetadata
from fact_form_importer.processing import _court_lookup, load_fact_api_services, process_workbook
from fact_form_importer.validators.fact_api_courts import CourtReference, CourtSlugSuggestion
from fact_form_importer.validators.os_addresses import AddressVerificationBatch
from fact_form_importer.validators.vocabularies import Vocabularies


def test_process_workbook_publishes_archive_and_latest_outputs(tmp_path, monkeypatch):
    source = tmp_path / "forms.csv"
    source.write_text("unused")
    submission = CourtSubmission(
        source=SourceMetadata(source_row_number=2), court_slug="example-court", status="processed"
    )
    profile = WorkbookProfile(
        source_path=source, sheet_name=None, row_count=1, column_count=1, columns=[]
    )

    monkeypatch.setattr("fact_form_importer.processing.profile_workbook", lambda path: profile)

    def fake_ingest(input_path, output_path):
        (output_path / "submissions_raw.json").write_text("[]")
        (output_path / "submissions_cleaned.json").write_text("[]")
        (output_path / "ingest_summary.json").write_text("{}")
        return IngestResult(submissions=[submission])

    monkeypatch.setattr("fact_form_importer.processing.ingest_workbook", fake_ingest)
    monkeypatch.setattr(
        "fact_form_importer.processing.load_fact_api_services",
        lambda **kwargs: (_vocabularies(), "test", lambda slug: True, None),
    )
    monkeypatch.setattr("fact_form_importer.processing.new_run_id", lambda: "run-1")
    monkeypatch.setattr("fact_form_importer.processing._court_lookup", lambda config: None)

    result = process_workbook(
        source, tmp_path / "out", config=AppConfig(config_dir=tmp_path / "config")
    )

    assert result.archive.archive_path.name == "run-1"
    assert (result.archive.archive_path / "api_readiness_report.json").exists()
    assert (result.archive.archive_path / "address_verification_report.json").exists()
    assert (result.archive.archive_path / "llm_actions_review.json").exists()
    assert (result.archive.archive_path / "fact_import_payload.json").exists()
    assert (
        result.duplicate_review_workbook_path
        == result.archive.archive_path / "duplicate_forms_review.xlsx"
    )
    assert result.duplicate_review_workbook_path.exists()
    assert (tmp_path / "out" / "import_summary.json").exists()
    summary = json.loads((result.archive.archive_path / "import_summary.json").read_text())
    assert summary["source_file"] == "forms.csv"
    assert summary["api_manifest_pending_action_count"] == 0
    assert (
        json.loads((result.archive.archive_path / "submissions_cleaned.json").read_text())[0][
            "status"
        ]
        == "processed"
    )


def test_process_workbook_runs_explicit_address_verification_and_records_metrics(
    tmp_path, monkeypatch
):
    source = tmp_path / "forms.csv"
    source.write_text("unused")
    submission = CourtSubmission(
        source=SourceMetadata(source_row_number=2), court_slug="example-court", status="processed"
    )
    profile = WorkbookProfile(
        source_path=source, sheet_name=None, row_count=1, column_count=1, columns=[]
    )
    monkeypatch.setenv("FACT_DATA_API_BASE_URL", "https://fact.example.test")
    monkeypatch.setenv("FACT_DATA_API_BEARER_TOKEN", "token")
    monkeypatch.setattr("fact_form_importer.processing.profile_workbook", lambda path: profile)
    monkeypatch.setattr(
        "fact_form_importer.processing.ingest_workbook",
        lambda input_path, output_path: _fake_ingest(output_path, submission),
    )
    monkeypatch.setattr(
        "fact_form_importer.processing.load_fact_api_services",
        lambda **kwargs: (_vocabularies(), "fact_data_api", lambda slug: True, None),
    )
    monkeypatch.setattr("fact_form_importer.processing.new_run_id", lambda: "verification-run")
    monkeypatch.setattr("fact_form_importer.processing._court_lookup", lambda config: None)
    calls = []
    monkeypatch.setattr(
        "fact_form_importer.processing.verify_addresses_with_fact_api",
        lambda submissions, config: (
            calls.append(submissions) or AddressVerificationBatch(enabled=True)
        ),
    )

    result = process_workbook(
        source,
        tmp_path / "out",
        verify_addresses=True,
        config=AppConfig(config_dir=tmp_path / "config"),
    )

    summary = result.output.summary
    report = json.loads(result.address_verification_report_path.read_text())
    assert len(calls) == 1
    assert calls[0][0] is submission
    assert summary["address_verification_enabled"] is True
    assert summary["address_verification_count"] == 0
    assert report["enabled"] is True


def test_process_workbook_excludes_superseded_duplicates_before_address_work(
    tmp_path, monkeypatch
):
    source = tmp_path / "forms.csv"
    source.write_text("unused")
    submissions = [
        CourtSubmission(
            source=SourceMetadata(
                source_row_number=2, completion_time="2026-01-01T12:00:00"
            ),
            court_slug="duplicate-court",
        ),
        CourtSubmission(
            source=SourceMetadata(
                source_row_number=3, completion_time="2026-01-02T12:00:00"
            ),
            court_slug="duplicate-court",
        ),
    ]
    profile = WorkbookProfile(
        source_path=source, sheet_name=None, row_count=2, column_count=1, columns=[]
    )
    monkeypatch.setenv("FACT_DATA_API_BASE_URL", "https://fact.example.test")
    monkeypatch.setenv("FACT_DATA_API_BEARER_TOKEN", "token")
    monkeypatch.setattr("fact_form_importer.processing.profile_workbook", lambda path: profile)

    def fake_ingest(input_path, output_path):
        (output_path / "submissions_raw.json").write_text("[]")
        (output_path / "submissions_cleaned.json").write_text("[]")
        (output_path / "ingest_summary.json").write_text("{}")
        return IngestResult(submissions=submissions)

    monkeypatch.setattr("fact_form_importer.processing.ingest_workbook", fake_ingest)
    monkeypatch.setattr(
        "fact_form_importer.processing.load_fact_api_services",
        lambda **kwargs: (_vocabularies(), "fact_data_api", lambda slug: True, None),
    )
    monkeypatch.setattr("fact_form_importer.processing.new_run_id", lambda: "duplicate-run")
    monkeypatch.setattr("fact_form_importer.processing._court_lookup", lambda config: None)
    checked = []
    monkeypatch.setattr(
        "fact_form_importer.processing.verify_addresses_with_fact_api",
        lambda values, config: checked.extend(values) or AddressVerificationBatch(enabled=True),
    )

    result = process_workbook(
        source,
        tmp_path / "out",
        verify_addresses=True,
        config=AppConfig(config_dir=tmp_path / "config"),
    )

    assert [submission.source.source_row_number for submission in checked] == [3]
    selection = json.loads(
        (result.archive.archive_path / "submission_selection.json").read_text()
    )
    archived = json.loads(
        (result.archive.archive_path / "submissions_cleaned.json").read_text()
    )
    assert selection["authoritative_source_row_numbers"] == [3]
    assert archived[0]["status"] == "skipped"
    assert archived[0]["superseded_by_source_row_number"] == 3


def test_process_workbook_selects_latest_after_fact_canonicalises_slug_aliases(
    tmp_path, monkeypatch
):
    source = tmp_path / "forms.csv"
    source.write_text("unused")
    submissions = [
        CourtSubmission(
            source=SourceMetadata(
                source_row_number=2, completion_time="2026-01-01T12:00:00"
            ),
            court_slug_raw="Llanelli Law Court",
            court_slug="llanelli-law-court",
        ),
        CourtSubmission(
            source=SourceMetadata(
                source_row_number=3, completion_time="2026-01-02T12:00:00"
            ),
            court_slug_raw="Llanelli-law-courts",
            court_slug="llanelli-law-courts",
        ),
    ]
    profile = WorkbookProfile(
        source_path=source, sheet_name=None, row_count=2, column_count=1, columns=[]
    )
    monkeypatch.setenv("FACT_DATA_API_BASE_URL", "https://fact.example.test")
    monkeypatch.setenv("FACT_DATA_API_BEARER_TOKEN", "token")
    monkeypatch.setattr("fact_form_importer.processing.profile_workbook", lambda path: profile)
    monkeypatch.setattr(
        "fact_form_importer.processing.ingest_workbook",
        lambda input_path, output_path: _fake_ingest_many(output_path, submissions),
    )

    def exists(slug):
        return slug == "llanelli-law-courts"

    def suggest(slug, raw):
        if slug != "llanelli-law-court":
            return None
        return CourtSlugSuggestion(
            submitted_slug=slug,
            suggested_slug="llanelli-law-courts",
            suggested_court_name="Llanelli Law Courts",
            confidence=1.0,
            query=str(raw),
            reason="Exact FaCT alias",
        )

    monkeypatch.setattr(
        "fact_form_importer.processing.load_fact_api_services",
        lambda **kwargs: (_vocabularies(), "fact_data_api", exists, suggest),
    )
    monkeypatch.setattr("fact_form_importer.processing.new_run_id", lambda: "alias-run")
    monkeypatch.setattr("fact_form_importer.processing._court_lookup", lambda config: None)

    result = process_workbook(
        source, tmp_path / "out", config=AppConfig(config_dir=tmp_path / "config")
    )

    selection = json.loads(
        (result.archive.archive_path / "submission_selection.json").read_text()
    )
    archived = json.loads(
        (result.archive.archive_path / "submissions_cleaned.json").read_text()
    )
    assert selection["duplicate_court_count"] == 1
    assert selection["authoritative_source_row_numbers"] == [3]
    assert archived[0]["court_slug"] == "llanelli-law-courts"
    assert archived[0]["status"] == "skipped"
    assert all(
        issue["code"] != "DUPLICATE_COURT_SLUG"
        for submission in archived
        for issue in submission["issues"]
    )


def test_process_workbook_requires_fact_api_configuration_for_address_verification(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("FACT_DATA_API_BASE_URL", raising=False)
    monkeypatch.delenv("FACT_DATA_API_BEARER_TOKEN", raising=False)

    with pytest.raises(ValueError, match="--verify-addresses requires"):
        process_workbook(
            tmp_path / "forms.csv",
            tmp_path / "out",
            verify_addresses=True,
            config=AppConfig(config_dir=tmp_path / "config"),
        )


def test_process_workbook_removes_failed_staging_directory(tmp_path, monkeypatch):
    source = tmp_path / "forms.csv"
    source.write_text("unused")
    monkeypatch.setattr("fact_form_importer.processing.new_run_id", lambda: "run-fail")
    monkeypatch.setattr(
        "fact_form_importer.processing.profile_workbook",
        lambda path: (_ for _ in ()).throw(ValueError("bad")),
    )

    with pytest.raises(ValueError, match="bad"):
        process_workbook(source, tmp_path / "out", config=AppConfig(config_dir=tmp_path / "config"))

    assert not (tmp_path / "out" / ".staging" / "run-fail").exists()


def test_process_workbook_does_not_resolve_court_ids_for_offline_vocabulary_runs(
    tmp_path, monkeypatch
):
    source = tmp_path / "forms.csv"
    source.write_text("unused")
    submission = CourtSubmission(
        source=SourceMetadata(source_row_number=2), court_slug="example-court", status="processed"
    )
    profile = WorkbookProfile(
        source_path=source, sheet_name=None, row_count=1, column_count=1, columns=[]
    )
    monkeypatch.setattr("fact_form_importer.processing.profile_workbook", lambda path: profile)
    monkeypatch.setattr(
        "fact_form_importer.processing.ingest_workbook",
        lambda input_path, output_path: _fake_ingest(output_path, submission),
    )
    monkeypatch.setattr(
        "fact_form_importer.processing.load_fact_api_services",
        lambda **kwargs: (
            _vocabularies(),
            "local_json_fallback_after_fact_data_api_error",
            None,
            None,
        ),
    )
    monkeypatch.setattr("fact_form_importer.processing.new_run_id", lambda: "offline-run")
    monkeypatch.setattr(
        "fact_form_importer.processing._court_lookup",
        lambda config: (_ for _ in ()).throw(AssertionError("must not call FaCT API")),
    )

    result = process_workbook(
        source, tmp_path / "out", config=AppConfig(config_dir=tmp_path / "config")
    )

    payload = json.loads((result.archive.archive_path / "fact_import_payload.json").read_text())
    assert payload["records"][0]["courtId"] is None


def _fake_ingest(output_path, submission):
    (output_path / "submissions_raw.json").write_text("[]")
    (output_path / "submissions_cleaned.json").write_text("[]")
    (output_path / "ingest_summary.json").write_text("{}")
    return IngestResult(submissions=[submission])


def _fake_ingest_many(output_path, submissions):
    (output_path / "submissions_raw.json").write_text("[]")
    (output_path / "submissions_cleaned.json").write_text("[]")
    (output_path / "ingest_summary.json").write_text("{}")
    return IngestResult(submissions=submissions)


def test_load_fact_api_services_handles_offline_fallback_and_missing_config(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "vocabularies.example.json").write_text(
        json.dumps({"areas_of_law": [{"code": "civil", "name": "Civil"}]})
    )
    monkeypatch.delenv("FACT_DATA_API_BASE_URL", raising=False)
    monkeypatch.delenv("FACT_DATA_API_BEARER_TOKEN", raising=False)
    config = AppConfig(config_dir=config_dir)

    vocabularies, source, exists, suggester = load_fact_api_services(
        config, allow_local_vocabularies=True
    )
    assert source == "local_json"
    assert vocabularies.value_in_vocab("Civil", "areas_of_law")
    assert exists is None and suggester is None

    with pytest.raises(ValueError, match="FACT_DATA_API_BASE_URL"):
        load_fact_api_services(config)


def test_load_fact_api_services_exercises_online_callbacks_and_fallback(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "vocabularies.example.json").write_text(
        json.dumps({"areas_of_law": [{"code": "civil", "name": "Civil"}]})
    )
    monkeypatch.setenv("FACT_DATA_API_BASE_URL", "https://fact.example.test")
    monkeypatch.setenv("FACT_DATA_API_BEARER_TOKEN", "token")
    config = AppConfig(config_dir=config_dir)
    calls = []
    monkeypatch.setattr(
        "fact_form_importer.processing.load_vocabularies_from_fact_api",
        lambda **kwargs: _vocabularies(),
    )
    monkeypatch.setattr(
        "fact_form_importer.processing.court_slug_exists_in_fact_api",
        lambda **kwargs: calls.append("exists") or True,
    )
    monkeypatch.setattr(
        "fact_form_importer.processing.suggest_court_slug_in_fact_api",
        lambda **kwargs: calls.append("suggest") or "suggestion",
    )

    vocabularies, source, exists, suggester = load_fact_api_services(config)
    assert vocabularies.version == "test"
    assert source == "fact_data_api"
    assert exists("example-court") is True
    assert suggester("example-court", "Example Court") == "suggestion"
    assert calls == ["exists", "suggest"]

    monkeypatch.setattr(
        "fact_form_importer.processing.load_vocabularies_from_fact_api",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )
    fallback, source, exists, suggester = load_fact_api_services(
        config, allow_local_vocabularies=True
    )
    assert fallback.value_in_vocab("Civil", "areas_of_law")
    assert source == "local_json_fallback_after_fact_data_api_error"
    assert exists is None and suggester is None


def test_fact_api_callbacks_and_court_lookup_wrap_errors_and_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("FACT_DATA_API_BASE_URL", "https://fact.example.test")
    monkeypatch.setenv("FACT_DATA_API_BEARER_TOKEN", "token")
    config = AppConfig(config_dir=tmp_path / "config")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "vocabularies.example.json").write_text(
        json.dumps({"areas_of_law": [{"code": "civil", "name": "Civil"}]})
    )
    monkeypatch.setattr(
        "fact_form_importer.processing.load_vocabularies_from_fact_api",
        lambda **kwargs: _vocabularies(),
    )
    monkeypatch.setattr(
        "fact_form_importer.processing.court_slug_exists_in_fact_api",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("down")),
    )
    _, _, exists, _ = load_fact_api_services(config)
    with pytest.raises(ValueError, match="Unable to validate"):
        exists("example-court")

    calls = []
    monkeypatch.setattr(
        "fact_form_importer.processing.lookup_court_by_slug_in_fact_api",
        lambda **kwargs: (
            calls.append(kwargs["court_slug"]) or CourtReference("court-id", kwargs["court_slug"])
        ),
    )
    lookup = _court_lookup(config)
    assert lookup("example-court").court_id == "court-id"
    assert lookup("example-court").court_id == "court-id"
    assert calls == ["example-court"]

    monkeypatch.setattr(
        "fact_form_importer.processing.lookup_court_by_slug_in_fact_api",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("down")),
    )
    with pytest.raises(ValueError, match="Unable to resolve"):
        _court_lookup(config)("other-court")


def _vocabularies():
    return Vocabularies(
        version="test",
        vocabularies={
            "areas_of_law": [{"code": "civil", "name": "Civil", "api_id": "area-id"}],
            "court_types": [{"code": "county", "name": "County Court", "api_id": "type-id"}],
            "opening_hour_types": [
                {"code": "court_open", "name": "Court open", "api_id": "opening-id"}
            ],
            "contact_description_types": [
                {"code": "enquiries", "name": "Enquiries", "api_id": "contact-id"}
            ],
        },
    )
