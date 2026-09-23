# TaxCite verification report

**Some citations or quotations are wrong.**

17 citations · 7 quotations · 5 error · 2 warning · 1 info

Sources — irb: 2024-2026, irc: 119-110. TaxCite 0.1.0.

| # | Citation | Line | Status | Note |
|---:|---|---:|---|---|
| 1 | I.R.C. § 162(a) | 15 | ok: verified, quote_exact | I.R.C. § 162(a) exists |
| 2 | I.R.C. § 162(a)(1) | 18 | ok: verified, quote_exact | I.R.C. § 162(a)(1) exists |
| 3 | I.R.C. § 61(a) | 21 | ok: verified, quote_exact | I.R.C. § 61(a) exists |
| 4 | I.R.C. § 263 | 26 | ok: verified | I.R.C. § 263 exists |
| 5 | I.R.C. § 263A | 26 | ok: verified | I.R.C. § 263A exists |
| 6 | I.R.C. § 263(a)(9) | 27 | ERROR: pinpoint_not_found | I.R.C. § 263(a)(9) does not exist; I.R.C. § 263 does — I.R.C. § 263(a) has paragraphs (1), (2); no (9); did you mean I.R.C. § 263A? |
| 7 | I.R.C. § 162(b) | 31 | warning: verified, quote_wrong_pinpoint | I.R.C. § 162(b) exists |
| 8 | I.R.C. § 469(h)(1) | 37 | ok: verified, quote_exact | I.R.C. § 469(h)(1) exists |
| 9 | Temp. Treas. Reg. § 1.469-5T(a) | 42 | ok: verified | Temp. Treas. Reg. § 1.469-5T(a) exists |
| 10 | I.R.C. § 162A | 46 | ERROR: not_found | I.R.C. § 162A does not exist — did you mean I.R.C. § 162, I.R.C. § 161, I.R.C. § 163? |
| 11 | I.R.C. § 162(z) | 46 | ERROR: pinpoint_not_found | I.R.C. § 162(z) does not exist; I.R.C. § 162 does — I.R.C. § 162 has subsections (a)–(s); no (z) |
| 12 | I.R.C. § 4 | 47 | warning: repealed | I.R.C. § 4 has been repealed (Repealed. Pub. L. 94–455, title V, § 501(b)(1), Oct. 4, 1976, 90 Stat. 1558) |
| 13 | I.R.C. § 263(a) | 49 | ERROR: verified, quote_misattributed | I.R.C. § 263(a) exists |
| 14 | I.R.C. § 61(a) | 52 | ERROR: verified, quote_not_found | I.R.C. § 61(a) exists |
| 15 | Rev. Rul. 2019-24 | 55 | info: unverifiable | recognised but not verified: IRS sub-regulatory guidance is outside TaxCite's sources |
| 16 | Notice 2024-7 | 55 | ok: verified | Notice 2024-7 — SECTION I. PURPOSE This notice provides relief for certain taxpayers from additions to tax for the failure to pay income tax with respect… |
| 17 | Commissioner v. Groetzinger, 480 U.S. 23 (1987) | 55 | ok: verified | Commissioner v. Groetzinger, 480 U.S. 23 (Supreme Court of the United States) — the opinion text is not held locally, so any quotation was matched on words alone, without punctuation; cited in 125 later decisions, most recently 2021-07-07 |

## Findings

### 6. I.R.C. § 263(a)(9) (line 27)

- **Status:** `pinpoint_not_found` (error)
- **Detail:** I.R.C. § 263(a)(9) does not exist; I.R.C. § 263 does
- **Heading:** Capital expenditures
- **Suggestion:** I.R.C. § 263(a) has paragraphs (1), (2); no (9); did you mean I.R.C. § 263A?
- **Source:** https://uscode.house.gov/view.xhtml?req=granuleid:USC-prelim-title26-section263&num=0&edition=prelim

### 7. I.R.C. § 162(b) (line 31)

- **Status:** `verified` (ok)
- **Detail:** I.R.C. § 162(b) exists
- **Heading:** Charitable contributions and gifts excepted
- **Source:** https://uscode.house.gov/view.xhtml?req=granuleid:USC-prelim-title26-section162&num=0&edition=prelim

- **Quote:** “[t]here shall be allowed as a deduction all the ordinary and necessary expenses paid or incurred during the taxable year in carrying on any trade or business,”
- **Quote status:** `quote_wrong_pinpoint` (score 1.00)
- **Matched:** `/us/usc/t26/s162/a`
- **Source text:** “There shall be allowed as a deduction all the ordinary and necessary expenses paid or incurred during the taxable year in carrying on any trade or business,”
- **Difference:** the quoted language appears at I.R.C. § 162(a) — In general

### 10. I.R.C. § 162A (line 46)

- **Status:** `not_found` (error)
- **Detail:** I.R.C. § 162A does not exist
- **Suggestion:** did you mean I.R.C. § 162, I.R.C. § 161, I.R.C. § 163?
- **Source:** https://uscode.house.gov/view.xhtml?req=granuleid:USC-prelim-title26-section162A&num=0&edition=prelim

### 11. I.R.C. § 162(z) (line 46)

- **Status:** `pinpoint_not_found` (error)
- **Detail:** I.R.C. § 162(z) does not exist; I.R.C. § 162 does
- **Heading:** Trade or business expenses
- **Suggestion:** I.R.C. § 162 has subsections (a)–(s); no (z)
- **Source:** https://uscode.house.gov/view.xhtml?req=granuleid:USC-prelim-title26-section162&num=0&edition=prelim

### 12. I.R.C. § 4 (line 47)

- **Status:** `repealed` (warning)
- **Detail:** I.R.C. § 4 has been repealed (Repealed. Pub. L. 94–455, title V, § 501(b)(1), Oct. 4, 1976, 90 Stat. 1558)
- **Heading:** Repealed. Pub. L. 94–455, title V, § 501(b)(1), Oct. 4, 1976, 90 Stat. 1558
- **Source:** https://uscode.house.gov/view.xhtml?req=granuleid:USC-prelim-title26-section4&num=0&edition=prelim

### 13. I.R.C. § 263(a) (line 49)

- **Status:** `verified` (ok)
- **Detail:** I.R.C. § 263(a) exists
- **Heading:** General rule
- **Source:** https://uscode.house.gov/view.xhtml?req=granuleid:USC-prelim-title26-section263&num=0&edition=prelim

- **Quote:** “gross income means all income from whatever source derived, including (but not limited to) the following items.”
- **Quote status:** `quote_misattributed` (score 0.99)
- **Matched:** `/us/usc/t26/s61/a`
- **Source text:** “gross income means all income from whatever source derived, including (but not limited to) the following items”
- **Difference:** the quoted language appears at I.R.C. § 61(a) — General definition

### 14. I.R.C. § 61(a) (line 52)

- **Status:** `verified` (ok)
- **Detail:** I.R.C. § 61(a) exists
- **Heading:** General definition
- **Source:** https://uscode.house.gov/view.xhtml?req=granuleid:USC-prelim-title26-section61&num=0&edition=prelim

- **Quote:** “all receipts of a commercial character shall be included in the computation of taxable profit for the year of receipt.”
- **Quote status:** `quote_not_found` (score 0.00)

### 15. Rev. Rul. 2019-24 (line 55)

- **Status:** `unverifiable` (info)
- **Detail:** recognised but not verified: IRS sub-regulatory guidance is outside TaxCite's sources

> Court decisions are checked for existence, case name and quoted language. TaxCite does **not** check whether a decision has been reversed, vacated, or overruled: no free source publishes treatment signals, so that remains a job for a citator.

> TaxCite verifies that cited provisions exist and that quoted language matches the official source. It does not judge whether a legal conclusion is correct, and it is not tax advice.

