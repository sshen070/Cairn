"""Rule-based classification and valuation. The Phase 5 baseline.

Rules are *data*: a flat list of (category, weight, pattern) that can be
diffed, unit-tested, and pointed at in a README. When the trained classifier
arrives it has to beat the numbers this file produces on the same held-out set.
If it doesn't, that gets written down rather than papered over.

Two knobs matter for honest measurement:

- `use_folder_signal` -- people really do file documents in `tax/` and
  `medical/` folders, so the folder name is a legitimate feature. But it also
  makes any benchmark whose corpus is organized that way look easy. Both
  numbers get reported, with and without.

- Category confidence and *importance* are separate. A receipt can be
  confidently a receipt and still not be worth much storage. `score` blends
  confidence with how much the category is worth keeping.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

TAX = "tax"
FINANCIAL = "financial"
IDENTITY = "identity"
LEGAL = "legal"
MEDICAL = "medical"
EDUCATION = "education"
EMPLOYMENT = "employment"
RECEIPT = "receipt"
OTHER = "other"

CATEGORIES = (TAX, FINANCIAL, IDENTITY, LEGAL, MEDICAL, EDUCATION, EMPLOYMENT, RECEIPT, OTHER)

# How much a correctly-identified document of each category is worth keeping.
# Identity documents top the list because they are simultaneously the hardest to
# replace and the least frequently touched -- the case the whole project rests on.
IMPORTANCE: dict[str, float] = {
    IDENTITY: 1.00,
    TAX: 0.95,
    LEGAL: 0.90,
    MEDICAL: 0.85,
    FINANCIAL: 0.80,
    EDUCATION: 0.70,
    EMPLOYMENT: 0.70,
    RECEIPT: 0.40,
    OTHER: 0.10,
}


@dataclass(frozen=True)
class Rule:
    category: str
    weight: float
    pattern: re.Pattern[str]
    label: str


def _r(category: str, weight: float, source: str) -> Rule:
    return Rule(category, weight, re.compile(source, re.IGNORECASE | re.UNICODE), source)


# fmt: off
RULES: tuple[Rule, ...] = (
    # --- tax ---------------------------------------------------------------
    _r(TAX, 3.0, r"\bform\s*1040\b"), _r(TAX, 2.5, r"\b1040\b"),
    _r(TAX, 3.0, r"\bw-?2\b"), _r(TAX, 2.5, r"\b1099\b"),
    _r(TAX, 3.0, r"internal revenue service"), _r(TAX, 2.0, r"\birs\b"),
    _r(TAX, 3.0, r"wage and tax statement"), _r(TAX, 2.5, r"tax return"),
    _r(TAX, 2.5, r"taxable income"), _r(TAX, 2.0, r"adjusted gross income"),
    _r(TAX, 2.0, r"tax withheld"), _r(TAX, 2.0, r"standard deduction"),
    _r(TAX, 1.5, r"\btax(es)?\b"), _r(TAX, 1.5, r"\befin\b|\bein\b"),

    # --- financial ---------------------------------------------------------
    _r(FINANCIAL, 3.0, r"bank statement"), _r(FINANCIAL, 3.0, r"account statement"),
    _r(FINANCIAL, 2.5, r"\binvoice\b"), _r(FINANCIAL, 2.5, r"credit union"),
    _r(FINANCIAL, 2.0, r"checking account"), _r(FINANCIAL, 2.0, r"routing\b"),
    _r(FINANCIAL, 2.0, r"closing balance|opening balance"),
    _r(FINANCIAL, 2.0, r"direct deposit"), _r(FINANCIAL, 2.0, r"amount due|total due"),
    _r(FINANCIAL, 2.0, r"remit payment"), _r(FINANCIAL, 2.0, r"\bbudget\b"),
    _r(FINANCIAL, 2.0, r"\bmileage\b"), _r(FINANCIAL, 1.5, r"\bstatement\b"),
    _r(FINANCIAL, 1.5, r"withdrawals?\b"), _r(FINANCIAL, 1.0, r"\bnet\s*30\b"),

    # --- identity ----------------------------------------------------------
    _r(IDENTITY, 3.5, r"\bpassport\b"), _r(IDENTITY, 3.0, r"driver'?s?[\s_-]*licen[sc]e"),
    _r(IDENTITY, 3.0, r"birth certificate"), _r(IDENTITY, 3.0, r"social security card"),
    _r(IDENTITY, 2.5, r"date of expiration"), _r(IDENTITY, 2.5, r"department of state"),
    _r(IDENTITY, 2.5, r"licen[sc]e renewal"), _r(IDENTITY, 2.5, r"department of motor vehicles|\bdmv\b"),
    _r(IDENTITY, 2.0, r"identity verification"), _r(IDENTITY, 2.0, r"licen[sc]e number"),
    _r(IDENTITY, 1.5, r"place of birth"), _r(IDENTITY, 0.5, r"\b\d{3}-\d{2}-\d{4}\b"),

    # --- legal -------------------------------------------------------------
    _r(LEGAL, 3.0, r"lease agreement"), _r(LEGAL, 2.5, r"\blease\b"),
    _r(LEGAL, 3.0, r"in witness whereof"), _r(LEGAL, 2.5, r"landlord"),
    _r(LEGAL, 2.5, r"\btenant\b"), _r(LEGAL, 2.5, r"security deposit"),
    _r(LEGAL, 3.0, r"policy declarations"), _r(LEGAL, 2.5, r"policyholder"),
    _r(LEGAL, 2.0, r"\binsurance\b"), _r(LEGAL, 2.0, r"\bdeductible\b"),
    _r(LEGAL, 2.0, r"bodily injury|liability\b"), _r(LEGAL, 1.5, r"\bpremium\b"),
    _r(LEGAL, 1.5, r"\bagreement\b"), _r(LEGAL, 1.5, r"\bnotar(y|ized)\b"),

    # --- medical -----------------------------------------------------------
    _r(MEDICAL, 3.0, r"visit summary"), _r(MEDICAL, 3.0, r"chief complaint"),
    _r(MEDICAL, 2.5, r"\bpatient\b"), _r(MEDICAL, 2.5, r"\bdiagnos(is|es|tic)\b"),
    _r(MEDICAL, 2.5, r"\bprescription\b"), _r(MEDICAL, 2.5, r"medical record"),
    _r(MEDICAL, 2.5, r"immuni[sz]ation"), _r(MEDICAL, 2.0, r"\bmrn\b"),
    _r(MEDICAL, 2.0, r"\bvitals\b"), _r(MEDICAL, 2.0, r"family medicine|\bclinic\b"),
    # Bare "md" matches the Markdown extension far more often than a doctor.
    _r(MEDICAL, 1.5, r"\bphysician\b|\bm\.d\."),

    # --- education ---------------------------------------------------------
    _r(EDUCATION, 3.5, r"academic transcript"), _r(EDUCATION, 3.0, r"\btranscript\b"),
    _r(EDUCATION, 2.5, r"\bgpa\b"), _r(EDUCATION, 2.5, r"degree awarded"),
    # "master" alone matches the git branch name in every .gitignore on disk,
    # so the degree words need their qualifier. Found by scanning a real drive.
    _r(EDUCATION, 2.5, r"\bdiploma\b"), _r(EDUCATION, 2.0, r"bachelor of|master of|master's degree|doctorate"),
    _r(EDUCATION, 2.0, r"\buniversity\b|\bcollege\b"), _r(EDUCATION, 1.5, r"\bsemester\b"),
    _r(EDUCATION, 1.5, r"\bcredits\b"), _r(EDUCATION, 1.5, r"\benrollment\b"),

    # --- employment --------------------------------------------------------
    _r(EMPLOYMENT, 3.5, r"offer letter"), _r(EMPLOYMENT, 3.0, r"offer of employment"),
    _r(EMPLOYMENT, 3.0, r"r[ée]sum[ée]"), _r(EMPLOYMENT, 3.0, r"履歴書"),
    _r(EMPLOYMENT, 3.0, r"curriculum vitae"), _r(EMPLOYMENT, 2.5, r"signing bonus"),
    _r(EMPLOYMENT, 2.5, r"restricted stock"), _r(EMPLOYMENT, 2.5, r"base salary"),
    _r(EMPLOYMENT, 2.0, r"\bpayroll\b"), _r(EMPLOYMENT, 2.0, r"background check"),
    _r(EMPLOYMENT, 1.5, r"start date"), _r(EMPLOYMENT, 1.5, r"\bemployment\b"),

    # --- receipt -----------------------------------------------------------
    _r(RECEIPT, 3.0, r"\breceipt\b"), _r(RECEIPT, 2.5, r"returns accepted"),
    _r(RECEIPT, 2.0, r"\bsubtotal\b"), _r(RECEIPT, 2.0, r"order confirmation"),
    _r(RECEIPT, 1.5, r"\bpaid:\s"), _r(RECEIPT, 1.0, r"\bpurchase\b"),
)
# fmt: on

# Patterns that argue *against* a category rather than for one.
#
# Every source tree carries a LICENSE file, and its contract vocabulary --
# "agreement", "liability", "copyright" -- reads exactly like a personal legal
# document to a keyword matcher. On a real drive this was 31 of 39 `legal`
# hits. Software licensing is a distinct thing and it is recognisable, so it
# gets recognised explicitly instead of being tuned around.
DEMOTIONS: tuple[Rule, ...] = (
    _r(LEGAL, 8.0, r"permission is hereby granted, free of charge"),
    _r(LEGAL, 8.0, r"redistributions? (of source|in binary)"),
    _r(LEGAL, 8.0, r"apache license|gnu general public license|mozilla public license|\bmit license\b"),
    _r(LEGAL, 8.0, r"bsd licen[sc]e|isc licen[sc]e|\bcopyleft\b"),
    _r(LEGAL, 6.0, r"warranties of merchantability"),
    _r(LEGAL, 4.0, r"software is provided \"?as is\"?"),
)

# Folder names that stand in for a category when someone has filed by hand.
FOLDER_SIGNAL: dict[str, str] = {
    "tax": TAX, "taxes": TAX,
    "financial": FINANCIAL, "finance": FINANCIAL, "finances": FINANCIAL, "banking": FINANCIAL,
    "identity": IDENTITY, "ids": IDENTITY,
    "legal": LEGAL, "contracts": LEGAL, "insurance": LEGAL,
    "medical": MEDICAL, "health": MEDICAL,
    "education": EDUCATION, "school": EDUCATION, "university": EDUCATION,
    "employment": EMPLOYMENT, "work": EMPLOYMENT, "career": EMPLOYMENT,
    "receipt": RECEIPT, "receipts": RECEIPT,
}
FOLDER_WEIGHT = 2.5

# Body matches count more than once, but with heavy diminishing returns -- a
# word repeated forty times is not forty times the evidence.
NAME_MULTIPLIER = 2.0
PATH_MULTIPLIER = 1.0
BODY_MULTIPLIER = 1.0
MAX_BODY_HITS = 3

DOCUMENT_EXTENSIONS = frozenset({".pdf", ".docx", ".doc", ".txt", ".md", ".rtf", ".odt", ".csv", ".xlsx"})
LOW_VALUE_PATH = re.compile(r"[\\/](downloads|temp|tmp|cache|node_modules|appdata)[\\/]", re.IGNORECASE)


@dataclass(frozen=True)
class Classification:
    category: str
    confidence: float
    score: float
    matched: tuple[str, ...]

    @property
    def explain(self) -> str:
        return ", ".join(self.matched[:5]) if self.matched else "no rules matched"


def categorize(
    name: str,
    path: str,
    body: str = "",
    *,
    ext: str | None = None,
    use_folder_signal: bool = True,
) -> Classification:
    """Score `name`/`path`/`body` against the rules and pick a category."""
    totals: dict[str, float] = dict.fromkeys(CATEGORIES, 0.0)
    matched: list[tuple[float, str]] = []

    name_part = name
    # Only the directory portion, so filename hits are not counted twice.
    dir_part = str(Path(path).parent)
    body_part = body or ""

    for rule in RULES:
        hit = 0.0
        if rule.pattern.search(name_part):
            hit += rule.weight * NAME_MULTIPLIER
        if rule.pattern.search(dir_part):
            hit += rule.weight * PATH_MULTIPLIER
        if body_part:
            n = min(len(rule.pattern.findall(body_part)), MAX_BODY_HITS)
            if n:
                hit += rule.weight * BODY_MULTIPLIER * (1 + 0.5 * (n - 1))
        if hit:
            totals[rule.category] += hit
            matched.append((hit, f"{rule.category}:{rule.label}"))

    haystack = f"{name_part}\n{dir_part}\n{body_part}"
    for rule in DEMOTIONS:
        if rule.pattern.search(haystack):
            totals[rule.category] -= rule.weight
            matched.append((-rule.weight, f"-{rule.category}:{rule.label}"))

    if use_folder_signal:
        for part in Path(dir_part).parts:
            cat = FOLDER_SIGNAL.get(part.lower())
            if cat:
                totals[cat] += FOLDER_WEIGHT
                matched.append((FOLDER_WEIGHT, f"{cat}:folder={part}"))

    best = max(CATEGORIES, key=lambda c: totals[c])
    best_total = totals[best]

    if best_total <= 0:
        return Classification(OTHER, 0.0, IMPORTANCE[OTHER], ())

    # Confidence is the winner's share of all evidence, so a document matching
    # three categories equally is reported as uncertain rather than confident.
    # Only positive evidence counts toward the denominator; a demotion should
    # not inflate the winner's share by shrinking the total.
    all_total = sum(v for v in totals.values() if v > 0) or 1.0
    share = best_total / all_total
    strength = min(best_total / 8.0, 1.0)
    confidence = round(min(share * 0.6 + strength * 0.4, 1.0), 4)

    score = _value_score(best, confidence, path, ext)
    matched.sort(reverse=True)
    return Classification(best, confidence, score, tuple(label for _, label in matched))


def _value_score(category: str, confidence: float, path: str, ext: str | None) -> float:
    """How much this file is worth keeping, 0..1.

    Deliberately not a function of access time. Last-access timestamps are
    polluted by antivirus and indexing sweeps, so they are advisory context for
    *which* files to look at, never evidence of what a file is worth.
    """
    score = IMPORTANCE[category] * (0.5 + 0.5 * confidence)
    if ext and ext in DOCUMENT_EXTENSIONS:
        score += 0.05
    if LOW_VALUE_PATH.search(path):
        score -= 0.10
    return round(max(0.0, min(score, 1.0)), 4)
