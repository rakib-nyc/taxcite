# TaxCite citation grammar

Every form TaxCite recognises, with the canonical identifier it produces. The patterns
themselves live in `src/taxcite/citations/patterns.py`; the extraction pipeline is in
`parser.py`. Each row below has a matching case in `tests/test_parser.py`.

Canonical identifiers are the USLM `identifier` attribute for the Internal Revenue Code
(`/us/usc/t26/s162/a/1/A`) and the same shape under `/us/cfr/t26` for Treasury
Regulations (`/us/cfr/t26/s1.263(a)-4/b/1`).

## 1. Internal Revenue Code

### 1.1 Section symbol

| Input | Canonical id |
|---|---|
| `§ 162` | `/us/usc/t26/s162` |
| `§162` | `/us/usc/t26/s162` |
| `§ 162(a)` | `/us/usc/t26/s162/a` |
| `§ 162(a)(1)(A)(i)(I)` | `/us/usc/t26/s162/a/1/A/i/I` |

### 1.2 The word "section"

| Input | Canonical id |
|---|---|
| `Section 162(a)` | `/us/usc/t26/s162/a` |
| `section 162` | `/us/usc/t26/s162` |
| `Sec. 162` / `sec. 162` | `/us/usc/t26/s162` |

### 1.3 Named authority

| Input | Canonical id |
|---|---|
| `IRC § 162`, `I.R.C. § 162` | `/us/usc/t26/s162` |
| `IRC Section 162` | `/us/usc/t26/s162` |
| `Code § 162`, `Code section 162` | `/us/usc/t26/s162` |
| `26 U.S.C. § 162`, `26 USC 162`, `26 U.S.C.A. § 61` | `/us/usc/t26/s162`, `/us/usc/t26/s61` |

A bare authority with no section marker (`Code 162`) is **not** matched: the signal is
too weak. `26 U.S.C. 162` is matched, because the title reference is unambiguous.

### 1.4 Section numbers with letters and hyphens

| Input | Canonical id |
|---|---|
| `§ 45Q` | `/us/usc/t26/s45Q` |
| `§ 199A` | `/us/usc/t26/s199A` |
| `§ 280G` | `/us/usc/t26/s280G` |
| `§ 1400Z-2` | `/us/usc/t26/s1400Z-2` |

A hyphenated suffix is only accepted after letters, so `§§ 1401-1403` reads as a range
of sections rather than one impossible section number.

### 1.5 Lists

| Input | Canonical ids |
|---|---|
| `§§ 162 and 263` | `…/s162`, `…/s263` |
| `§§ 162(a), 263(a), and 263A` | `…/s162/a`, `…/s263/a`, `…/s263A` |
| `sections 1001 and 1012` | `…/s1001`, `…/s1012` |
| `§ 162(a) and (b)` | `…/s162/a`, `…/s162/b` |

### 1.6 Ranges

| Input | Canonical ids |
|---|---|
| `§§ 1401–1403` | `…/s1401`, `…/s1402`, `…/s1403` |
| `sections 704 through 707` | `…/s704` … `…/s707` |
| `§ 162(a)–(c)` | `…/s162/a`, `…/s162/b`, `…/s162/c` |

Ranges of more than 26 items are not expanded: the two endpoints are returned with the
warning `range endpoints not expanded`.

Every citation produced by expanding a list or range keeps the span and `raw` text of
the whole phrase, and carries its own `display` and `canonical_id`.

## 2. Treasury Regulations

| Input | Canonical id |
|---|---|
| `Treas. Reg. § 1.162-1(a)` | `/us/cfr/t26/s1.162-1/a` |
| `Treas. Reg. §1.263(a)-4(b)(1)` | `/us/cfr/t26/s1.263(a)-4/b/1` |
| `Reg. § 1.61-1` | `/us/cfr/t26/s1.61-1` |
| `Regs. § 1.199A-1` | `/us/cfr/t26/s1.199A-1` |
| `Treasury Regulation section 1.1502-13` | `/us/cfr/t26/s1.1502-13` |
| `Treas. Regs. §§ 1.162-1 and 1.162-2` | `/us/cfr/t26/s1.162-1`, `/us/cfr/t26/s1.162-2` |
| `26 C.F.R. § 1.162-1`, `26 CFR 1.162-1` | `/us/cfr/t26/s1.162-1` |
| `Temp. Treas. Reg. § 1.469-5T` | `/us/cfr/t26/s1.469-5T` |
| `Prop. Treas. Reg. § 1.199A-1` | `/us/cfr/t26/s1.199A-1` |
| `§ 1.162-1` (bare) | `/us/cfr/t26/s1.162-1` |
| `§ 301.7701-3(b)(1)(ii)` | `/us/cfr/t26/s301.7701-3/b/1/ii` |
| `§ 1.1400Z2(a)-1` | `/us/cfr/t26/s1.1400Z2(a)-1` |
| `§ 31.3121(a)-1` | `/us/cfr/t26/s31.3121(a)-1` |

Note that the `(a)` in `1.263(a)-4` belongs to the *section number*, not the pinpoint
path: the canonical id is `/us/cfr/t26/s1.263(a)-4`, and `(b)(1)` is the path.

**Kind.** `Temp.` or a `-nT` suffix marks a citation `temporary`; `Prop.` marks it
`proposed`; everything else is `final`. Proposed citations resolve against the final
regulations; see SPEC 6.1.

## 3. IRS sub-regulatory guidance (recognised, not verifiable in v1)

`Rev. Rul. 2019-24`, `Revenue Ruling 2019-24`, `Rev. Proc. 2023-34`, `Notice 2024-7`,
`Ann. 2023-1`, `T.D. 9959`, `2019-45 I.R.B. 1234`, `PLR 202301001`, `TAM`, `CCA`, `GCM`,
`FSA`, `AOD`.

These become citations with `source = irs_guidance` (or `other` for taxpayer-specific
items such as PLRs and TAMs) and no canonical id. They are always reported, never
silently dropped, and always as `UNVERIFIABLE`.

## 4. Court decisions (recognised, not verifiable in v1)

`Commissioner v. Groetzinger, 480 U.S. 23 (1987)`, `Welch v. Helvering, 290 U.S. 111
(1933)`, `T.C. Memo. 2020-12`, `123 T.C. 456`, and the `F.2d` / `F.3d` / `F.4th` /
`F. Supp.` / `S. Ct.` / `B.T.A.` / `Fed. Cl.` / `A.F.T.R.2d` reporters.

Recognising these keeps reporter volume numbers from being read as Code sections.

## 5. What is deliberately **not** matched

| Input | Why |
|---|---|
| `Section 3 of the Agreement` | "of the *instrument*" guard |
| `section 3 of the Tax Cuts and Jobs Act` | same guard, title-case act name |
| `Pub. L. 115-97, section 13101` | preceded by a public-law reference |
| `https://uscode.house.gov/…?section=162` | URLs are masked before matching |
| `` `section 162` `` and fenced code blocks | code spans are masked |
| `The 162 rule` | no section marker |
| `§ 162(a) (emphasis added)` → only `§ 162(a)` | pinpoint tokens are ≤ 6 characters with no spaces |
| `See § 162(a).` → only `§ 162(a)` | trailing sentence punctuation is never consumed |

Masking replaces the excluded region with spaces of equal length, so every reported
span is a valid offset into the original text.

## 6. Level inference and warnings

Position in the path determines the level, not the shape of the token:

| Depth | IRC level | Expected shape |
|---|---|---|
| 0 | subsection | `[a-z]{1,2}` |
| 1 | paragraph | `\d+[A-Z]?` |
| 2 | subparagraph | `[A-Z]{1,2}` |
| 3 | clause | lowercase roman |
| 4 | subclause | uppercase roman |
| 5 | item | `[a-z]{2}` |
| 6 | subitem | `[A-Z]{2}` |

So `§ 1(i)` is subsection "i", not clause "i". A token that does not fit the level its
position implies still gets looked up, but the citation carries a warning such as
`unexpected token '(3)' at subsection level`.

Treasury Regulation paths use the same table without the `subsection` row, because CFR
paragraphs start at `(a)`.
