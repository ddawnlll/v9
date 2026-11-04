"""Tests for the books/ → BenchmarkCase registry (literature loop entry)."""

from pathlib import Path

from v8_next.books.registry import build_case, coverage, get_mapping

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_exemplar_mapping_resolves_to_executable_case():
    m = get_mapping("squeeze-swing")
    assert m.filename.endswith(".pdf")
    assert "squeeze_swing" in m.experts
    case = build_case("squeeze-swing")
    assert case.case_id == "BC-BOOKS-SQUEEZE-01"
    assert case.allowed_populations == ("BurnedDiagnosticReal",)
    assert case.strategy_config.grammar_policy == "range-breakout-48-v1"


def _books_library_present(books_dir: Path) -> bool:
    return books_dir.is_dir() and any(books_dir.iterdir())


def test_mapped_filenames_exist_in_library():
    books_dir = REPO_ROOT / "books"
    if not _books_library_present(books_dir):
        import pytest

        pytest.skip("books/ library not present or not checked out")
    from v8_next.books.registry import MAPPINGS

    missing = [m.filename for m in MAPPINGS if not (books_dir / m.filename).is_file()]
    assert missing == [], f"registry cites absent books: {missing}"


def test_coverage_reports_mapped_vs_unmapped():
    books_dir = REPO_ROOT / "books"
    if not _books_library_present(books_dir):
        import pytest

        pytest.skip("books/ library not present or not checked out")
    cov = coverage(books_dir)
    assert cov["total"] >= 100
    assert cov["mapped"] >= 4
    assert cov["unmapped"] > 0
    assert "squeeze-swing" in cov["mapping_ids"]
