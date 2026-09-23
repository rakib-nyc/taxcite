# What TaxCite sends, stores, and never touches

Short version: **your document does not leave your machine**, with one exception that
is spelled out below and can be switched off: checking a quotation attributed to a
court decision sends that quoted passage to CourtListener. Everything else is a local
operation against a local index. Nothing you check is logged anywhere or used to train
anything.

This matters beyond preference. I.R.C. § 7216 restricts a return preparer's disclosure
of taxpayer information, and pasting a client's draft into a cloud service is a
disclosure. So is uploading it to a research platform. The ordinary duty of
confidentiality points the same way. A tool that checks citations without transmitting
the text being checked is in a materially different position from one that does not.

> **This document describes what the software does. It is not legal advice about
> § 7216, Circular 230, or your confidentiality obligations, and it is not a
> representation that using TaxCite satisfies any of them.** Whether a particular use
> is permissible is a question for you and your own counsel. TaxCite is an independent
> open-source project, not affiliated with or endorsed by any government agency.

## What is transmitted, and when

| When | To | What |
|---|---|---|
| `taxcite build-index` | uscode.house.gov | A request for the published Title 26 archive. Nothing of yours. |
| `taxcite build-index --regs` | ecfr.gov | A request for the published CFR parts. Nothing of yours. |
| `taxcite versions` | uscode.house.gov | A request for the list of release points. Nothing of yours. |
| A regulation not in the index | ecfr.gov | The **section number** you cited, e.g. `1.162-1`. Not the document. |
| A case citation not in the index | courtlistener.com | The **reporter citation** you cited, e.g. `290 U.S. 111`, and one follow-up query for its citation history. Not the document. |
| **A quotation attributed to a case** | courtlistener.com | **The quoted passage itself**, as a search phrase. See below. |
| Everything else | nobody | — |

Two of those rows carry something derived from your document, and one of them carries
text from it.

The section-number and reporter-citation rows are narrow: the request contains the
citation and nothing else — not the sentence around it, not the file, not the
quotation.

### Checking a quotation from a case sends the quotation

This is the exception, and it deserves to be understood rather than buried. TaxCite
holds the Code and the regulations locally, so a quotation from either is checked
without a request. It does **not** hold court opinions — they are not published as a
bulk download the way the Code is. To check a quotation against a decision without the
opinion text, TaxCite asks CourtListener whether that run of words occurs in that
case, which means sending the words. A failing quotation is probed a few more times
with progressively longer prefixes, to find where it stops matching.

What is sent is the quoted passage, normalised, and the reporter citation. What is not
sent is your surrounding text, your file, your client's name, or anything else in the
document. A quotation is a passage from a published judicial opinion, so in the
ordinary case the transmitted text is public-domain law rather than anything of yours —
but the *selection* of it is yours, and on a bad quotation the words sent are the words
your draft got wrong. Both facts are worth knowing before this runs against a
privileged draft.

CourtListener is operated by the Free Law Project, a non-profit. TaxCite sends no
credential with these requests unless you have set one, and it does not send an
unbounded number of them: one verification run has a fixed budget of search requests,
and when it is spent the remaining case quotations are reported as unverifiable rather
than checked. An accurate quotation costs a single request.

**To prevent it:** `--offline` blocks it outright, as it blocks everything else.
Nothing about the rest of verification depends on it — statutes, regulations and
guidance are still checked locally and in full — you simply get `unverifiable` on case
quotations rather than an answer.

### If you set `COURTLISTENER_TOKEN`

An optional API token makes TaxCite download the opinion text instead. That is
*better* for confidentiality, not worse: the opinion is fetched by its own identifier,
cached on disk, and every quotation is then checked locally, so no passage from your
draft is transmitted at all. The token is sent to courtlistener.com in an
`Authorization` header on those fetches and is never written to the index, the cache,
or a diligence record.

## `--offline` is enforced, not requested

`taxcite verify --offline` does not merely set a flag that the network code is asked to
respect. It **seals the process**: every outbound request raises for the remainder of
the run, irreversibly, whatever code path attempts it — including one added in a later
version by someone who forgot. An uncached regulation is then reported as
`source_unavailable`, which is the honest answer.

The test suite does the same thing to itself. Every test that is not explicitly marked
`network` runs with outbound requests blocked, so a change that starts making requests
where it should not fails in CI rather than in a firm's hands.

## What is stored, and where

Everything lives under `~/.taxcite/` (or `$TAXCITE_DATA_DIR`):

```
~/.taxcite/
  taxcite.db          the index — published law only
  versions/           historical release points, one file each
  raw/                the downloaded government archives
  cache/              HTTP response cache, published law only
```

None of it contains anything of yours. Delete the directory and TaxCite forgets
everything except how to download the law again.

TaxCite writes no telemetry, opens no sockets except the ones in the table above, and
has no account, licence check, or phone-home.

## Diligence records

`taxcite verify --record <file>` appends a line to a log you nominate. That log
identifies each document **by SHA-256 hash rather than by content**, so an engagement
file can evidence that a document was checked without the record itself becoming
another copy of privileged text. It stays where you put it.

## The one thing to watch

If you run TaxCite as an MCP server inside an AI assistant, then the *assistant* sees
whatever you give it, under whatever terms that assistant operates. TaxCite's own
behaviour is unchanged — it still transmits nothing — but the tool cannot make promises
about the program calling it. If the draft is sensitive, check it with the CLI.
