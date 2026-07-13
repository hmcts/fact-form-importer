import json

import pytest

from fact_form_importer.output.archive import (
    LATEST_RUN_FILE,
    RUN_MANIFEST_FILE,
    list_run_archives,
    load_run_archive,
    publish_run_archive,
    stage_path,
)


def test_publish_run_archive_creates_immutable_archive_and_latest_mirror(tmp_path):
    output_root = tmp_path / "out"
    staging = stage_path(output_root, "20260710T120000Z-test")
    staging.mkdir(parents=True)
    (staging / "import_summary.json").write_text('{"processed_count": 1}')
    (staging / "nsu_cleaned_review.xlsx").write_bytes(b"review")

    result = publish_run_archive(
        output_root=output_root,
        staging_path=staging,
        run_id="20260710T120000Z-test",
        source_name="forms.csv",
        summary={"processed_count": 1},
    )

    assert result.archive_path.name == "20260710T120000Z-test"
    assert not staging.exists()
    assert (result.archive_path / RUN_MANIFEST_FILE).exists()
    assert (output_root / "import_summary.json").exists()
    assert json.loads((output_root / LATEST_RUN_FILE).read_text())["run_id"] == result.run_id
    archive = load_run_archive(output_root, result.run_id)
    assert archive is not None
    assert archive["manifest"]["source_name"] == "forms.csv"
    assert {item["name"] for item in archive["manifest"]["artifacts"]} == {
        "import_summary.json",
        "nsu_cleaned_review.xlsx",
    }


def test_list_run_archives_ignores_incomplete_or_invalid_directories(tmp_path):
    output_root = tmp_path / "out"
    (output_root / "final" / "incomplete").mkdir(parents=True)
    invalid = output_root / "final" / "invalid"
    invalid.mkdir()
    (invalid / RUN_MANIFEST_FILE).write_text("not json")

    assert list_run_archives(output_root) == []
    assert list_run_archives(tmp_path / "missing-output") == []

    wrong_name = output_root / "final" / "wrong-name"
    wrong_name.mkdir()
    (wrong_name / RUN_MANIFEST_FILE).write_text('{"run_id": "different"}')
    assert list_run_archives(output_root) == []


def test_publish_run_archive_rejects_existing_archive_or_missing_staging(tmp_path):
    output_root = tmp_path / "out"
    existing = output_root / "final" / "run-1"
    existing.mkdir(parents=True)

    with pytest.raises(ValueError, match="already exists"):
        publish_run_archive(output_root, tmp_path / "missing", "run-1", "forms.csv", {})

    with pytest.raises(ValueError, match="does not exist"):
        publish_run_archive(output_root, tmp_path / "missing", "run-2", "forms.csv", {})
