from concurrent.futures import ThreadPoolExecutor

from fact_form_importer.execution.ledger import ExecutionLedgerStore
from fact_form_importer.execution.models import (
    ActionExecutionState,
    CourtExecutionState,
    ExecutionLedger,
)


def _court(slug: str, status: str = "succeeded") -> CourtExecutionState:
    return CourtExecutionState(
        court_slug=slug,
        actions={
            f"{slug}-1": ActionExecutionState(
                action_id=f"{slug}-1",
                status=status,
            )
        },
    )


def test_court_checkpoints_use_independent_atomic_shards(tmp_path):
    store = ExecutionLedgerStore(tmp_path)
    store.save(ExecutionLedger(run_id="run-1", courts={"alpha": _court("alpha")}))

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(
            executor.map(
                lambda court: store.save_court("run-1", court),
                [_court("bravo"), _court("charlie")],
            )
        )

    # A checkpoint does not repeatedly expand/rewrite the consolidated file.
    consolidated = store.path_for("run-1").read_text(encoding="utf-8")
    assert "bravo" not in consolidated
    assert "charlie" not in consolidated
    assert len(list(store.court_directory_for("run-1").glob("*.json"))) == 2

    loaded = store.load("run-1")
    assert set(loaded.courts) == {"alpha", "bravo", "charlie"}


def test_new_consolidated_save_supersedes_older_court_shard(tmp_path):
    store = ExecutionLedgerStore(tmp_path)
    store.save_court("run-1", _court("alpha", "failed"))
    loaded = store.load("run-1")
    loaded.courts["alpha"] = _court("alpha", "planned")

    store.save(loaded)

    assert store.load("run-1").courts["alpha"].actions["alpha-1"].status == "planned"
