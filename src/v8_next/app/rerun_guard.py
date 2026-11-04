"""One key-scoped refusal for a re-execution of an already-registered run identity.

Both receipt-chain CLIs (``benchmark-portfolio`` and ``benchmark``) compute a run
key, and both used to re-execute the whole window -- receipt, ledger append,
certificate, forensic HTML, window manifest COMPLETED -- and only then die inside
``ResearchStore.record_run`` with an uncaught ``ValueError: registered run identity
cannot be rewritten`` (exit 1).

The store is right and stays unchanged: the registered payload folds in the sha256
of every artifact, including the benchmark ledger the re-execution appends to, so a
second registration for one run key can never match the first. What has to change is
where the CLI decides: refuse *before* the first write, with a named reason and an
exit code of its own, instead of discovering it after the artifacts are on disk.

The rule is key-scoped: a run key the store registers nothing for runs and registers
normally, with or without ``--allow-rerun``.
"""

from __future__ import annotations

from pathlib import Path

from v8_next.evaluation.store import ResearchStore

# Named refusal for a run whose identity is already registered in this output dir's
# ResearchStore: the registered payload folds in the sha256 of every artifact,
# including the benchmark ledger a re-execution appends to, so the second
# registration can never match and must not be attempted.
RERUN_WOULD_REWRITE_REGISTERED_RUN_IDENTITY = "RERUN_WOULD_REWRITE_REGISTERED_RUN_IDENTITY"
# Exit code for a run the CLI refused to start. 0 = ran and verified, 1 = ran and
# did not verify, 2 = bad input or invalid data, so a refusal needs its own code.
REFUSED_EXIT_CODE = 3


def registered_run_payload(out_dir: Path, run_key_digest: str) -> str | None:
    """The payload this output dir's run store holds for ``run_key_digest``.

    ``None`` when the store registers no such run -- including when the store file
    does not exist yet, since this check must not create the store (or any other
    artifact) for a run that has not registered anything. The store's ``runs``
    table is the authority here: the window manifest alone cannot distinguish a
    registered run from one whose registration never happened.
    """
    store_path = out_dir / "runs" / "runs.sqlite"
    if not store_path.is_file():
        return None
    store = ResearchStore(store_path)
    try:
        row = store.get_run(run_key_digest)
    finally:
        store.close()
    return None if row is None else row[0]


def refusal_message(out_dir: Path, run_key_digest: str) -> str:
    """The one sentence both CLIs print when they refuse to start a re-execution."""
    return (
        f"{RERUN_WOULD_REWRITE_REGISTERED_RUN_IDENTITY}: run identity {run_key_digest} "
        f"is already registered in {out_dir / 'runs' / 'runs.sqlite'} and a "
        "re-execution can never be re-registered under it (its payload binds the "
        "ledger it appends to). Refusing before the window manifest, the ledger "
        "append or any other artifact is written; a deliberate re-execution needs a "
        "fresh --output-dir."
    )
