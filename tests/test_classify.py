"""The rules baseline.

This module measures the keyword/regex classifier against the fixture manifest
and writes the numbers to `metrics.json`. Phase 5's trained classifier has to
beat these on the same held-out set -- and if it doesn't, the honest thing is
to say so in the README rather than quietly reporting the better number.

The folder-signal ablation matters for that comparison. A corpus filed into
`tax/` and `medical/` folders makes any classifier that reads folder names look
strong, so both numbers are reported: with the signal (how it ships) and
without (what the text alone supports).
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pytest

from cairn import classify, extract

from conftest import MANIFEST

METRICS_PATH = Path(__file__).resolve().parents[1] / "metrics.json"

# Floors, not targets. They exist to catch regressions, and they sit a little
# below the measured values so a small rule edit does not fail the build.
MIN_ACCURACY_WITH_FOLDERS = 0.90
MIN_ACCURACY_TEXT_ONLY = 0.75


def predict(corpus: Path, rel: str, *, use_folder_signal: bool) -> str:
    full = corpus / rel
    body = extract.extract_text(full)
    return classify.categorize(
        extract.name_tokens(full),
        str(full),
        body,
        ext=full.suffix.lower(),
        use_folder_signal=use_folder_signal,
    ).category


def score_all(corpus: Path, *, use_folder_signal: bool) -> dict:
    tp: dict[str, int] = defaultdict(int)
    fp: dict[str, int] = defaultdict(int)
    fn: dict[str, int] = defaultdict(int)
    wrong: list[tuple[str, str, str]] = []

    for rel, expected in sorted(MANIFEST.items()):
        got = predict(corpus, rel, use_folder_signal=use_folder_signal)
        if got == expected:
            tp[expected] += 1
        else:
            fp[got] += 1
            fn[expected] += 1
            wrong.append((rel, expected, got))

    per_category = {}
    for cat in sorted(set(MANIFEST.values())):
        precision = tp[cat] / (tp[cat] + fp[cat]) if (tp[cat] + fp[cat]) else 0.0
        recall = tp[cat] / (tp[cat] + fn[cat]) if (tp[cat] + fn[cat]) else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        per_category[cat] = {
            "support": tp[cat] + fn[cat],
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
        }

    total = len(MANIFEST)
    correct = sum(tp.values())
    macro_f1 = sum(m["f1"] for m in per_category.values()) / len(per_category)

    return {
        "n": total,
        "correct": correct,
        "accuracy": round(correct / total, 4),
        "macro_f1": round(macro_f1, 4),
        "per_category": per_category,
        "misclassified": [{"path": p, "expected": e, "got": g} for p, e, g in wrong],
    }


def render(title: str, m: dict) -> str:
    lines = [
        "",
        f"  {title}",
        f"  {'-' * len(title)}",
        f"  accuracy {m['accuracy']:.1%}  ({m['correct']}/{m['n']})    macro-F1 {m['macro_f1']:.3f}",
        "",
        f"  {'category':<12}{'n':>4}{'prec':>8}{'rec':>8}{'F1':>8}",
    ]
    for cat, s in sorted(m["per_category"].items()):
        lines.append(f"  {cat:<12}{s['support']:>4}{s['precision']:>8.2f}{s['recall']:>8.2f}{s['f1']:>8.2f}")
    if m["misclassified"]:
        lines.append("")
        lines.append("  misclassified:")
        for w in m["misclassified"]:
            lines.append(f"    {w['path']}  expected {w['expected']}, got {w['got']}")
    return "\n".join(lines)


@pytest.fixture(scope="module")
def baseline(corpus: Path) -> dict:
    with_folders = score_all(corpus, use_folder_signal=True)
    text_only = score_all(corpus, use_folder_signal=False)
    payload = {
        "corpus_size": len(MANIFEST),
        "rules": len(classify.RULES),
        "with_folder_signal": with_folders,
        "text_only": text_only,
        "caveat": (
            "SATURATED BENCHMARK -- NOT a valid Phase 5 comparison point. The fixture "
            "bodies and the regex rules were written by the same hand, so the rules match "
            "vocabulary they were built against and score near-perfectly. Real documents "
            "carry vocabulary the rules have never seen. A trustworthy baseline needs the "
            "hand-labelled set of real documents that Phase 5 builds."
        ),
    }
    METRICS_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def test_rules_baseline(baseline, capsys):
    """Measure, report, and hold a floor. The printed table is the deliverable."""
    with capsys.disabled():
        print(render("rules baseline, with folder signal (as shipped)", baseline["with_folder_signal"]))
        print(render("rules baseline, text and filename only", baseline["text_only"]))
        print(f"\n  written to {METRICS_PATH.name}\n")

    assert baseline["with_folder_signal"]["accuracy"] >= MIN_ACCURACY_WITH_FOLDERS
    assert baseline["text_only"]["accuracy"] >= MIN_ACCURACY_TEXT_ONLY


def test_folder_signal_helps_but_is_not_the_whole_story(baseline):
    """If text-only collapses, the benchmark is measuring folder names."""
    with_folders = baseline["with_folder_signal"]["accuracy"]
    text_only = baseline["text_only"]["accuracy"]
    assert with_folders >= text_only
    assert text_only > 0.6, "the classifier is leaning entirely on folder names"


def test_metrics_carry_the_saturation_caveat(baseline):
    """Guard against the 100% number being quoted as if it meant something.

    The fixture bodies and the rules share an author, so this corpus cannot
    falsify the rules. The caveat travels with the metrics file so the number
    is never read without it.
    """
    assert "SATURATED BENCHMARK" in baseline["caveat"]
    if baseline["text_only"]["accuracy"] >= 0.99:
        assert baseline["caveat"], "a perfect score demands the caveat be present"


# -- behaviour of the scoring itself ----------------------------------------


def test_unmatched_documents_fall_through_to_other():
    result = classify.categorize("shopping list", "/home/x/shopping list.txt", "milk eggs bread")
    assert result.category == classify.OTHER
    assert result.confidence == 0.0


def test_identity_outranks_receipt_in_value():
    assert classify.IMPORTANCE[classify.IDENTITY] > classify.IMPORTANCE[classify.RECEIPT]


def test_score_penalizes_transient_locations():
    body = "passport, date of expiration, department of state"
    keep = classify.categorize("passport scan", "/home/x/Documents/passport scan.pdf", body, ext=".pdf")
    junk = classify.categorize("passport scan", "/home/x/Downloads/passport scan.pdf", body, ext=".pdf")
    assert keep.category == junk.category == classify.IDENTITY
    assert keep.score > junk.score


def test_confidence_drops_when_categories_compete():
    clear = classify.categorize(
        "form 1040", "/x/form 1040.pdf", "form 1040 internal revenue service taxable income tax return"
    )
    muddy = classify.categorize("mixed", "/x/mixed.txt", "lease agreement patient diagnosis form 1040 transcript")
    assert clear.confidence > muddy.confidence


def test_matched_rules_are_reported_for_explainability(scanned, corpus: Path):
    row = scanned.conn.execute(
        "SELECT matched_rules FROM files WHERE name = '2023_Form_1040.pdf'"
    ).fetchone()
    matched = json.loads(row["matched_rules"])
    assert matched, "a classified file should record why"
    assert any(m.startswith("tax:") for m in matched)


def test_every_category_has_at_least_one_rule():
    covered = {r.category for r in classify.RULES}
    expected = set(classify.CATEGORIES) - {classify.OTHER}
    assert expected <= covered
