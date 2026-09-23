# Test fixture provenance

Every fixture containing statutory or regulatory text in this directory is a verbatim
excerpt of a public-domain U.S. government publication. Nothing here is hand-written
legal text. Synthetic fixtures used only to exercise parser mechanics are named
`synthetic_*` and contain obviously fake text.

## `uslm/` — Internal Revenue Code (26 U.S.C.)

- **Publisher:** Office of the Law Revision Counsel, U.S. House of Representatives.
- **Source file:** `xml_usc26@119-110.zip` → `usc26.xml`
- **URL:** <https://uscode.house.gov/download/releasepoints/us/pl/119/110/xml_usc26@119-110.zip>
- **Download page:** <https://uscode.house.gov/download/download.shtml>
- **Release point:** `119-110` (current through Pub. L. 119-110)
- **Retrieved:** 2026-09-21
- **Format:** USLM 1.0 (`http://xml.house.gov/schemas/uslm/1.0`)
- **Status:** public domain (17 U.S.C. § 105).

Each file wraps one `<section>` element from that document in a minimal `<uscDoc>`.
The statutory text is byte-for-byte as published. `<notes>` and `<sourceCredit>`
elements were removed to keep the fixtures small; they are editorial apparatus, never
statutory text, and TaxCite excludes them from provision text anyway.

| File | Provision | Why it is here |
|---|---|---|
| `s1.xml` | § 1 — Tax imposed | Subsection `(i)` proves the first pinpoint token is a subsection, not a roman clause |
| `s61.xml` | § 61 — Gross income defined | The canonical definition; `define "gross income"` must find § 61(a) |
| `s162.xml` | § 162 — Trade or business expenses | The "ordinary and necessary" quote; a chapeau/paragraphs/continuation structure |
| `s163.xml` | § 163 — Interest | Deep nesting, including § 163(j) |

| `s263.xml` | § 263 — Capital expenditures | The `263(a)` / `263A` confusion pair |
| `s263A.xml` | § 263A — Uniform capitalization | The other half of that pair |
| `s469.xml` | § 469 — Passive activity losses | Pairs with Temp. Treas. Reg. § 1.469-5T |
| `s1411.xml` | § 1411 — Net investment income tax | Cross-reference fixture |
| `s1400Z-2.xml` | § 1400Z-2 — Opportunity zones | Section number with a hyphen (published with an en dash) |
| `s7701.xml` | § 7701 — Definitions | Deep pinpoints such as `(a)(30)(A)` |
| `s1001.xml`, `s1012.xml` | §§ 1001, 1012 — Gain or loss; basis | A `§§ X and Y` list that resolves |
| `s1031.xml`, `s1032.xml`, `s1033.xml` | §§ 1031–1033 | Three consecutive sections, so a `§§ X–Z` range resolves end to end |
| `s4_repealed.xml` | § 4 — Repealed | `status="repealed"` |
| `s1000_reserved.xml` | § 1000 — Reserved | `status="reserved"` |
| `s2614_omitted.xml` | § 2614 — Omitted | `status="omitted"` |
| `s50A_50B_repealed.xml` | §§ 50A, 50B — Repealed | One element carrying two section identifiers |

## `uslm_notes/` — one section with its statutory notes

`s199A_with_notes.xml` is § 199A from the same release point with its `<notes>` intact.
Every other USLM fixture has notes stripped, which keeps them small; this one exists so
the tests can check two things at once — that notes never leak into provision text, and
that the effective dates buried in them are extracted. § 199A is the right section for
it: created by the 2017 Act, with an "applicable to taxable years beginning after
December 31, 2017" effective-date note and a further amendment that does not bite until
after 2025.

## `uslm_115-35/` — the same sections at an earlier release point

§§ 162, 163 and 199 as they stood at release point `115-35` (Public Law 115-35, enacted
2017-05-17), taken from
<https://uscode.house.gov/download/releasepoints/us/pl/115/35/xml_usc26@115-35.zip> on
2026-09-22. Notes stripped, as above. These drive the time-travel tests: § 163(j) was a
different rule before the 2017 Act, § 199A did not exist, and § 199 has since been
repealed.

## `ecfr/` — Treasury Regulations (26 C.F.R.)

See the table in that directory's section below; added in Milestone 8.

## `memos/`

Memoranda written for testing. Any statutory language quoted in them is copied from the
`uslm/` fixtures above; deliberately altered or fabricated quotations are marked as such
in the accompanying `expected.json`.

### Memo fixture quotations

Every passage inside quotation marks in `memos/*.md` is copied from the `uslm/`
fixtures above, except where the accompanying `*.expected.json` records a `planted`
alteration. The planted errors are: one changed word in `memo03`, a verbatim § 162(a)
passage attributed to § 162(b), a verbatim § 61(a) passage attributed to § 263(a), and
one wholly fabricated sentence, which is labelled as fabricated and is not presented
anywhere as real law.

## `snapshots/`

`sample_report.md` is the verification report for `examples/sample_memo.md` rendered
against the fixture index in this directory, checked byte for byte by
`tests/test_examples.py`. The copy in `examples/` is the same report rendered against a
full Title 26 index; the two differ only in the nearest-section suggestions, which
depend on how much of the Code is indexed.

## `uslm_notes/` — one section with its statutory notes

`s199A_with_notes.xml` is § 199A from the same release point with its `<notes>` intact.
Every other USLM fixture has notes stripped, which keeps them small; this one exists so
the tests can check two things at once — that notes never leak into provision text, and
that the effective dates buried in them are extracted. § 199A is the right section for
it: created by the 2017 Act, with an "applicable to taxable years beginning after
December 31, 2017" effective-date note and a further amendment that does not bite until
after 2025.

## `uslm_115-35/` — the same sections at an earlier release point

§§ 162, 163 and 199 as they stood at release point `115-35` (Public Law 115-35, enacted
2017-05-17), taken from
<https://uscode.house.gov/download/releasepoints/us/pl/115/35/xml_usc26@115-35.zip> on
2026-09-22. Notes stripped, as above. These drive the time-travel tests: § 163(j) was a
different rule before the 2017 Act, § 199A did not exist, and § 199 has since been
repealed.

## `ecfr/` — Treasury Regulations (26 C.F.R.)

- **Publisher:** Office of the Federal Register and the Government Publishing Office.
- **Endpoint:** `https://www.ecfr.gov/api/versioner/v1/full/2026-09-08/title-26.xml?part=<part>&section=<section>`
- **Docs:** <https://www.ecfr.gov/developers/documentation/api/v1>
- **eCFR date:** `2026-09-08` (Title 26 was current as of 2026-09-18 when these were
  taken; the `full` endpoint is addressed by issue date)
- **Retrieved:** 2026-09-21
- **Status:** public domain (17 U.S.C. § 105).

Each file is the endpoint's response for one section, unmodified.

| File | Provision | Why it is here |
|---|---|---|
| `1.61-1.xml` | § 1.61-1 — Gross income | A short section, and the regulation counterpart of IRC § 61 |
| `1.162-1.xml` | § 1.162-1 — Business expenses | A paragraph that opens with two markers: `(b) Cross references. (1) …` |
| `1.263a-4.xml` | § 1.263(a)-4 — Intangibles | A section number containing parentheses, and italic `(<I>1</I>)` levels six deep |
| `1.469-5T.xml` | § 1.469-5T — Material participation | A temporary regulation, and a `[Reserved]` paragraph |
| `301.7701-3.xml` | § 301.7701-3 — Entity classification | Deep nesting through `(b)(2)(i)(A)` |
| `synthetic_levels.xml` | — | **Synthetic.** Parser mechanics only; obviously fake "Lorem" text |

## `irb/` — the Internal Revenue Bulletin

- **Publisher:** Internal Revenue Service.
- **URL:** `https://www.irs.gov/irb/{year}-{week}_IRB`
- **Retrieved:** 2026-09-22
- **Status:** public domain (17 U.S.C. § 105).

Three bulletins, saved unmodified, chosen because the IRS markup is *not* stable
across years and the parser has to cope with all of it:

| File | Why it is here |
|---|---|
| `2019-45.html` | Anchors named `REV-RUL-2019-25`, plus non-document anchors (`HI1`) that must be skipped |
| `2024-30.html` | Whitespace inside the heading element, which an anchor-name parser misses entirely |
| `2026-01.html` | Current markup, and a Revenue Procedure that states it supersedes an earlier one |

The parser identifies a document by running TaxCite's own citation parser over the
article title rather than by matching anchor conventions, which is what lets one code
path read all three.

## Case law — no fixture files

Court decisions are not checked into this repository. They are reached live through
the CourtListener API (Free Law Project), and the tests that touch it are marked
`@pytest.mark.network`, so the default test run never makes a request.

- **Publisher:** Free Law Project, CourtListener.
- **URL:** `https://www.courtlistener.com/api/rest/v4/search/`
- **Retrieved:** 2026-09-22
- **Status:** U.S. federal judicial opinions are public domain (17 U.S.C. § 105 and
  *Banks v. Manchester*, 128 U.S. 244 (1888)).

One real passage is quoted in `tests/test_network.py`, in order to check a quotation
against the decision it came from:

| Passage | Source |
|---|---|
| "the standard set up by the statute is not a rule of law; it is rather a way of life" | *Welch v. Helvering*, 290 U.S. 111, 115 (1933) |

The opinion text used in `tests/test_case_quotes.py` is **synthetic** — invented,
obviously so, and about periscopes — because those tests exercise matching mechanics,
star pagination and pinpoint arithmetic rather than any real holding.

## `rates/` — published § 382 rates

- **Publisher:** Internal Revenue Service.
- **URL:** `https://www.irs.gov/irb/2026-37_IRB`
- **Retrieved:** 2026-09-23
- **Status:** public domain (17 U.S.C. § 105).

| File | Why it is here |
|---|---|
| `irb-2026-37-table3.html` | Table 3 of Rev. Rul. 2026-17, verbatim, with the sentence that states the month it governs |

The § 382(f) long-term tax-exempt rate is published monthly in Table 3 of the
applicable-federal-rate ruling. The excerpt is the table exactly as the IRS published
it, trimmed only of the surrounding bulletin — the rate (3.88% for September 2026),
the ruling number and the wording of the row labels are unaltered, because the parser
matches on that wording.
