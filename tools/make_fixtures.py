"""Generate a deterministic corpus of realistic documents with ground-truth labels.

Dev-only tool. Requires the `dev` extra (reportlab, python-docx); the cairn runtime
itself stays stdlib-only.

    python tools/make_fixtures.py --to fixtures/

Writes the corpus plus `manifest.json`, mapping each relative path to the category
the rules are expected to assign. Tests score against that manifest, which is also
the baseline Phase 5's classifier has to beat.

All identifiers here are deliberately fake (000-00-0000, Acme Corp). Never seed this
with anything resembling real PII.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sys
from pathlib import Path

SEED = 20260826

# --------------------------------------------------------------------------
# Document bodies. Keyword-bearing and realistic enough that full-text search
# on the .txt/.md/.csv members is a genuine test rather than a filename match.
# --------------------------------------------------------------------------

BODIES: dict[str, str] = {
    "1040": """Department of the Treasury - Internal Revenue Service
Form 1040  U.S. Individual Income Tax Return  2023

Name: Jordan A. Sample        SSN: 000-00-0000
Filing status: Single

Wages, salaries, tips (Form W-2, box 1) .............. 84,500.00
Taxable interest ......................................... 312.44
Adjusted gross income .................................. 84,812.44
Standard deduction ..................................... 13,850.00
Taxable income ......................................... 70,962.44
Total tax .............................................. 11,204.00
Federal income tax withheld ............................ 12,880.00
REFUND ................................................. 1,676.00
""",
    "w2": """2023 Form W-2  Wage and Tax Statement
Employer: Acme Corp, 100 Anvil Way, Springfield
Employer EIN: 00-0000000
Employee: Jordan A. Sample   SSN: 000-00-0000

Box 1  Wages, tips, other compensation ......... 84,500.00
Box 2  Federal income tax withheld ............. 12,880.00
Box 3  Social security wages ................... 84,500.00
Box 4  Social security tax withheld ............. 5,239.00
Box 5  Medicare wages and tips ................. 84,500.00
Box 6  Medicare tax withheld .................... 1,225.25
""",
    "passport": """UNITED STATES OF AMERICA - PASSPORT
Type P   Code USA   Passport No. X00000000
Surname: SAMPLE
Given names: JORDAN A
Nationality: UNITED STATES OF AMERICA
Date of birth: 01 JAN 1990
Place of birth: SPRINGFIELD, U.S.A.
Date of issue: 15 MAR 2021    Date of expiration: 14 MAR 2031
Authority: United States Department of State
This is a scanned copy retained for identity verification purposes.
""",
    "bank": """ACME FEDERAL CREDIT UNION
Monthly Account Statement - March 2024
Account holder: Jordan A. Sample
Checking account ending 0000    Routing 000000000

Opening balance ..................... 4,182.19
Total deposits ...................... 6,404.00
Total withdrawals ................... 5,918.73
Closing balance ..................... 4,667.46

Date        Description                     Amount
03/01/2024  DIRECT DEPOSIT ACME CORP      +3,202.00
03/04/2024  MORTGAGE PAYMENT              -1,845.00
03/12/2024  UTILITIES - SPRINGFIELD PWR     -164.22
""",
    "lease": """RESIDENTIAL LEASE AGREEMENT

This Lease Agreement is entered into between Acme Property Holdings LLC
("Landlord") and Jordan A. Sample ("Tenant") for the premises located at
42 Sample Street, Unit 3, Springfield.

1. TERM. The lease term begins 01 June 2024 and ends 31 May 2025.
2. RENT. Tenant shall pay monthly rent of $1,850.00 due on the first day
   of each month.
3. SECURITY DEPOSIT. Tenant has deposited $1,850.00 as security.
4. UTILITIES. Tenant is responsible for electricity, gas, and internet.
5. TERMINATION. Either party may terminate with sixty (60) days notice.

IN WITNESS WHEREOF the parties have executed this agreement.
""",
    "offer": """Acme Corp
100 Anvil Way, Springfield

15 April 2024

Dear Jordan,

We are delighted to extend an offer of employment for the position of
Senior Systems Engineer, reporting to the Director of Infrastructure.

Base salary: $132,000 per year, paid semi-monthly.
Signing bonus: $10,000, payable in the first full pay cycle.
Equity: 4,000 restricted stock units vesting over four years.
Start date: 13 May 2024.

This offer is contingent on successful completion of a background check.
Please sign and return by 26 April 2024.

Sincerely,
Dana Reyes, Head of Talent
""",
    "license": """STATE OF SPRINGFIELD - DEPARTMENT OF MOTOR VEHICLES
DRIVER LICENSE RENEWAL NOTICE

License number: D0000000
Name: JORDAN A SAMPLE
Class: C          Endorsements: NONE
Expires: 01 JAN 2027

Your driver license expires soon. To renew, present this notice, proof of
identity, and proof of residency at any DMV office. Renewal fee: $38.00.
Vision screening is required for applicants over 40.
""",
    "medical": """SPRINGFIELD FAMILY MEDICINE
Patient Visit Summary

Patient: Jordan A. Sample      DOB: 01/01/1990      MRN: 0000000
Date of service: 08 February 2024
Provider: Dr. Alex Morgan, MD

Chief complaint: Annual physical examination.
Vitals: BP 118/74, HR 66, Temp 98.4F, BMI 23.1
Assessment: Healthy adult. No acute findings.
Plan: Routine lab panel ordered (CBC, lipid panel). Follow up in 12 months.
Immunizations up to date. Prescription refilled: none.
""",
    "transcript": """SPRINGFIELD STATE UNIVERSITY
OFFICIAL ACADEMIC TRANSCRIPT

Student: Jordan A. Sample          Student ID: 00000000
Degree awarded: Bachelor of Science in Computer Science
Conferred: 15 May 2012             Cumulative GPA: 3.71

Course                                   Credits   Grade
CS 240  Data Structures                     4       A
CS 350  Operating Systems                   4       A-
CS 411  Database Systems                    3       A
MATH 301 Linear Algebra                     3       B+
PHYS 210 Classical Mechanics                4       B
""",
    "receipt": """ANVIL HARDWARE - SPRINGFIELD
Receipt #000184        12 July 2024

2x  Cordless drill bit set        $ 24.98
1x  Extension cord, 25ft          $ 18.50
3x  Wood screws, 100ct            $ 11.97
                    Subtotal      $ 55.45
                    Tax (6.5%)    $  3.60
                    TOTAL         $ 59.05

Paid: VISA ending 0000.  Returns accepted within 30 days with receipt.
""",
    "insurance": """ACME MUTUAL INSURANCE
Automobile Policy Declarations

Policyholder: Jordan A. Sample
Policy number: AP-0000000       Term: 01 JAN 2024 - 01 JAN 2025
Vehicle: 2018 sedan, VIN 00000000000000000

Coverage                          Limit            Premium
Bodily injury liability      250,000/500,000       $412.00
Property damage liability          100,000         $188.00
Comprehensive (deductible $500)                    $146.00
Collision (deductible $500)                        $233.00
                                  Total annual premium $979.00
""",
    "invoice": """INVOICE

Sample Consulting LLC              Invoice #: 0000-014
                                   Date: 03 September 2024
Bill to: Acme Corp                 Terms: Net 30
         100 Anvil Way, Springfield

Description                       Hours    Rate      Amount
Systems architecture review        18.0   185.00    3,330.00
Migration planning workshop         6.0   185.00    1,110.00
                                          Subtotal  4,440.00
                                          TOTAL DUE 4,440.00

Remit payment within 30 days of the invoice date.
""",
    "recipe": """Weeknight Tomato Soup

Serves 4. Takes about 35 minutes start to finish.

Ingredients
  2 tbsp olive oil
  1 yellow onion, diced
  3 cloves garlic, sliced thin
  1 28oz can whole peeled tomatoes
  2 cups vegetable stock
  1/4 cup heavy cream
  Salt, black pepper, a pinch of sugar

Method
  Sweat the onion in the oil over medium heat until translucent, about
  8 minutes. Add garlic, cook one minute more. Add tomatoes, crushing
  them by hand, and the stock. Simmer 20 minutes. Blend until smooth,
  stir in the cream, season aggressively.
""",
    "notes": """# Sprint planning notes - 14 Aug

Attendees: Dana, Priya, Sam, me

## Carried over
- Flaky integration test on the queue consumer. Priya to look.
- Docs for the new retry semantics are still a stub.

## This sprint
- Split the ingest worker out of the monolith (Sam, 5pt)
- Add structured logging to the scheduler (me, 3pt)
- Spike: is the nightly reindex still needed? (Dana, 2pt)

## Decisions
- We are NOT upgrading the ORM this cycle. Revisit in September.
- Staging gets the new alerting rules first, prod follows in a week.
""",
    "readme": """# left-pad

String left pad.

## Install

    npm install left-pad

## Usage

    const leftPad = require('left-pad');
    leftPad('foo', 5);       // "  foo"
    leftPad('foo', 5, '0');  // "00foo"

## License

MIT
""",
    "travel": """Trip notes - Springfield to Riverton, October

Booked the 07:15 train, arrives 11:40. Seat 14A, quiet car.
Hotel: Riverton Inn, two nights, confirmation 000000. Check-in after 15:00.

Things to actually do
  - the covered bridge walk, early, before the tour buses
  - that bookshop on Mill Street someone mentioned
  - dinner at the place by the water, needs a reservation

Pack: rain shell, the good camera, charger for it this time.
""",
}

CSV_BODIES: dict[str, str] = {
    "budget": """month,category,description,amount
2024-01,housing,rent payment,1850.00
2024-01,utilities,electricity,164.22
2024-01,groceries,supermarket,412.88
2024-02,housing,rent payment,1850.00
2024-02,utilities,electricity,151.06
2024-02,groceries,supermarket,388.14
2024-03,housing,rent payment,1850.00
2024-03,insurance,auto premium,979.00
""",
    "mileage": """date,start,end,miles,purpose
2024-03-04,Springfield,Riverton,58.2,client visit
2024-03-11,Springfield,Anvil Plant,12.7,site inspection
2024-04-02,Springfield,Riverton,58.2,client visit
2024-04-19,Springfield,Capital City,140.5,conference
""",
}

# (relative path, body key, expected category)
# Categories: tax financial identity legal medical education employment receipt other
PDF_DOCS = [
    ("tax/2023_Form_1040.pdf", "1040", "tax"),
    ("tax/W2_Acme_2023.pdf", "w2", "tax"),
    ("identity/passport_scan.pdf", "passport", "identity"),
    ("financial/bank_statement_mar2024.pdf", "bank", "financial"),
    ("financial/invoice_0000-014.pdf", "invoice", "financial"),
    ("medical/visit_summary_feb2024.pdf", "medical", "medical"),
    ("education/university_transcript.pdf", "transcript", "education"),
    ("misc/Ünïcödé_résumé.pdf", "offer", "employment"),
    ("misc/履歴書_2024.pdf", "offer", "employment"),
]

DOCX_DOCS = [
    ("legal/lease_agreement_signed.docx", "lease", "legal"),
    ("employment/offer_letter_Acme.docx", "offer", "employment"),
    ("identity/drivers_license_renewal.docx", "license", "identity"),
    ("legal/insurance_policy_auto.docx", "insurance", "legal"),
]

TEXT_DOCS = [
    ("tax/tax_notes_2023.txt", "1040", "tax"),
    ("tax/w2_summary.txt", "w2", "tax"),
    ("financial/bank_statement_mar2024.txt", "bank", "financial"),
    ("financial/invoice_0000-014.txt", "invoice", "financial"),
    ("legal/lease_terms_summary.txt", "lease", "legal"),
    ("legal/insurance_declarations.txt", "insurance", "legal"),
    ("employment/offer_letter_Acme.txt", "offer", "employment"),
    ("medical/visit_summary_feb2024.txt", "medical", "medical"),
    ("education/transcript_copy.txt", "transcript", "education"),
    ("identity/passport_details.txt", "passport", "identity"),
    ("identity/license_renewal_notice.txt", "license", "identity"),
    ("receipts/anvil_hardware_jul2024.txt", "receipt", "receipt"),
    ("notes/recipe_tomato_soup.txt", "recipe", "other"),
    ("notes/travel_riverton.txt", "travel", "other"),
    ("notes/sprint_planning_aug14.md", "notes", "other"),
    ("notes/left-pad_README.md", "readme", "other"),
]

CSV_DOCS = [
    ("financial/budget_2024.csv", "budget", "financial"),
    ("financial/mileage_log.csv", "mileage", "financial"),
]


def _write_pdf(path: Path, title: str, body: str) -> None:
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.units import inch
    from reportlab.pdfgen import canvas

    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=LETTER)
    c.setTitle(title)
    width, height = LETTER
    y = height - inch

    c.setFont("Helvetica-Bold", 13)
    c.drawString(inch, y, title)
    y -= 24
    c.setFont("Courier", 9)
    for line in body.splitlines():
        if y < inch:
            c.showPage()
            c.setFont("Courier", 9)
            y = height - inch
        # Courier is Latin-1 only; keep non-encodable glyphs from aborting the draw.
        c.drawString(inch, y, line.encode("latin-1", "replace").decode("latin-1"))
        y -= 12
    c.save()


def _write_docx(path: Path, title: str, body: str) -> None:
    from docx import Document

    path.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()
    doc.add_heading(title, level=1)
    for block in body.split("\n\n"):
        doc.add_paragraph(block.strip())
    doc.save(str(path))


def _write_text(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8", newline="\n")


def _title_from(rel: str) -> str:
    return Path(rel).stem.replace("_", " ").replace("-", " ").title()


def build(root: Path) -> dict[str, str]:
    """Write the corpus under `root`. Returns {relative_path: expected_category}."""
    random.seed(SEED)
    manifest: dict[str, str] = {}

    for rel, key, cat in PDF_DOCS:
        _write_pdf(root / rel, _title_from(rel), BODIES[key])
        manifest[rel] = cat

    for rel, key, cat in DOCX_DOCS:
        _write_docx(root / rel, _title_from(rel), BODIES[key])
        manifest[rel] = cat

    for rel, key, cat in TEXT_DOCS:
        _write_text(root / rel, BODIES[key])
        manifest[rel] = cat

    for rel, key, cat in CSV_DOCS:
        _write_text(root / rel, CSV_BODIES[key])
        manifest[rel] = cat

    _build_edge_cases(root, manifest)
    return manifest


def _build_edge_cases(root: Path, manifest: dict[str, str]) -> None:
    """Files whose only job is to make the walker prove it survives them.

    These are excluded from classification scoring (not added to the manifest)
    except where the expected category is unambiguous.
    """
    edge = root / "edge"
    edge.mkdir(parents=True, exist_ok=True)

    # Zero-byte file.
    (edge / "empty.txt").write_bytes(b"")

    # No extension at all.
    _write_text(edge / "LICENSE", "MIT License\n\nPermission is hereby granted, free of charge...\n")

    # Deeply nested tree (>= 6 levels).
    # Named without a category word in the path, so the label rests on body text
    # alone -- the folder-signal ablation in test_classify needs cases like this.
    deep = edge / "a" / "b" / "c" / "d" / "e" / "f"
    _write_text(deep / "buried_hardware_receipt.txt", BODIES["receipt"])
    manifest["edge/a/b/c/d/e/f/buried_hardware_receipt.txt"] = "receipt"

    # A path longer than 260 characters. Built from nested segments so no single
    # component exceeds the 255-char per-component limit.
    seg = "longpath_segment_" + "x" * 40
    long_dir = edge / "long"
    for _ in range(5):
        long_dir = long_dir / seg
    _write_text(long_dir / "deeply_nested_document.txt", BODIES["notes"])

    # Byte-identical content under two different names, in two different trees.
    # This is the dedup proof: the store must hold exactly one object for both.
    dup_body = BODIES["insurance"]
    _write_text(edge / "dup" / "policy_original.txt", dup_body)
    _write_text(edge / "dup" / "nested" / "policy_copy_backup.txt", dup_body)

    # Directories the skip lists must exclude. If these ever show up in a scan,
    # the skip logic has regressed.
    _write_text(root / "node_modules" / "left-pad" / "index.js", "module.exports = leftPad;\n")
    _write_text(root / "node_modules" / "left-pad" / "package.json", '{"name":"left-pad"}\n')
    _write_text(root / ".git" / "config", "[core]\n\trepositoryformatversion = 0\n")
    _write_text(root / "__pycache__" / "stale.pyc.txt", "not really bytecode\n")

    # Binary-ish noise that must not be treated as a document.
    (root / "misc").mkdir(parents=True, exist_ok=True)
    (root / "misc" / "setup.exe").write_bytes(b"MZ\x90\x00" + bytes(random.randrange(256) for _ in range(2048)))
    (root / "misc" / "screenshot.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + bytes(random.randrange(256) for _ in range(1024))
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--to", type=Path, required=True, help="output directory for the corpus")
    ap.add_argument("--clean", action="store_true", help="remove the target directory first")
    args = ap.parse_args(argv)

    root: Path = args.to
    if args.clean and root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    try:
        manifest = build(root)
    except ImportError as exc:
        print(f"error: fixture generation needs the dev extra: {exc}", file=sys.stderr)
        print('       pip install -e ".[dev]"', file=sys.stderr)
        return 2

    (root / "manifest.json").write_text(
        json.dumps(dict(sorted(manifest.items())), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    total = sum(len(files) for _, _, files in os.walk(root))
    print(f"wrote {total} files to {root}  ({len(manifest)} labelled in manifest.json)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
