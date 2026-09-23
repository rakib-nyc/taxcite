-- TaxCite index schema (SPEC 7).
--
-- One SQLite file holds every provision, the cross-reference graph, and the defined
-- terms. The full-text index covers each provision's OWN text rather than its
-- full_text, so a phrase matches at the deepest provision that actually contains it
-- instead of at every ancestor as well.

PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- The catalogue of published U.S. Code release points. A release point is the state
-- of the Code just after one public law was folded in, so `enacted` makes it
-- addressable by date: the law in force on a day is the latest release point on or
-- before it. Each indexed release point lives in its own database file under
-- versions/, because one row per provision per release point would be tens of
-- millions of rows for a corpus almost all of which is identical between versions.
CREATE TABLE IF NOT EXISTS release_points (
  release  TEXT PRIMARY KEY,         -- e.g. '118-42'
  enacted  TEXT,                     -- ISO date of the public law, NULL for current
  titles   TEXT NOT NULL DEFAULT '', -- JSON array of affected title numbers
  url      TEXT NOT NULL,
  indexed  INTEGER NOT NULL DEFAULT 0,
  is_current INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_rp_enacted ON release_points(enacted);

CREATE TABLE IF NOT EXISTS provisions (
  id             TEXT PRIMARY KEY,         -- canonical id, e.g. /us/usc/t26/s162/a/1
  source         TEXT NOT NULL,            -- 'irc' | 'reg'
  section        TEXT NOT NULL,
  path           TEXT NOT NULL,            -- JSON array of pinpoint tokens
  level          TEXT NOT NULL,
  num            TEXT,
  heading        TEXT,
  text           TEXT NOT NULL,            -- own text only
  full_text      TEXT NOT NULL,            -- own text plus descendants, document order
  parent_id      TEXT REFERENCES provisions(id),
  ordinal        INTEGER NOT NULL,         -- document order within the parent
  status         TEXT NOT NULL DEFAULT 'active',
  source_version TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_prov_section ON provisions(source, section);
CREATE INDEX IF NOT EXISTS idx_prov_parent  ON provisions(parent_id, ordinal);

CREATE VIRTUAL TABLE IF NOT EXISTS provisions_fts USING fts5(
  id UNINDEXED,
  heading,
  text,
  content='provisions',
  content_rowid='rowid',
  tokenize='porter unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS provisions_vocab
  USING fts5vocab(provisions_fts, 'row');

-- Statutory notes. These are not statutory text and must never be quoted as such,
-- which is why they live in their own table and are excluded from provisions.text.
-- They are also where the effective dates live: tax legislation routinely leaves
-- "applies to taxable years beginning after December 31, 2017" out of the codified
-- section and puts it in an uncodified provision of the public law, which reaches the
-- Code only as a note. A practitioner cannot tell whether a provision governed a
-- given tax year without them.
CREATE TABLE IF NOT EXISTS notes (
  provision_id  TEXT NOT NULL,
  ordinal       INTEGER NOT NULL,
  topic         TEXT NOT NULL,      -- effectiveDate, effectiveDateOfAmendment, ...
  heading       TEXT,
  text          TEXT NOT NULL,
  dates         TEXT NOT NULL DEFAULT '[]',  -- JSON array of ISO dates in the note
  applies_after TEXT,               -- derived: taxable years beginning after this date
  PRIMARY KEY (provision_id, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_notes_topic ON notes(topic);
CREATE INDEX IF NOT EXISTS idx_notes_applies ON notes(applies_after);

CREATE TABLE IF NOT EXISTS refs (
  from_id  TEXT NOT NULL,
  to_id    TEXT NOT NULL,
  kind     TEXT NOT NULL,                  -- 'explicit' | 'implicit' | 'relative'
  external INTEGER NOT NULL DEFAULT 0,
  raw      TEXT,
  PRIMARY KEY (from_id, to_id, kind)
);

CREATE INDEX IF NOT EXISTS idx_refs_to ON refs(to_id);

-- IRS guidance published in the Internal Revenue Bulletin. Kept apart from
-- provisions because it is a different kind of thing with a different identifier
-- scheme, and because a Revenue Ruling has no subdivision hierarchy to speak of.
CREATE TABLE IF NOT EXISTS guidance (
  id        TEXT PRIMARY KEY,     -- /us/irs/rev-rul/2019-24
  kind      TEXT NOT NULL,        -- 'Rev. Rul.', 'Notice', ...
  number    TEXT NOT NULL,        -- '2019-24'
  title     TEXT NOT NULL,
  text      TEXT NOT NULL,
  bulletin  TEXT NOT NULL,        -- '2019-45'
  url       TEXT NOT NULL,
  published TEXT,
  status    TEXT NOT NULL DEFAULT 'active'
);

CREATE INDEX IF NOT EXISTS idx_guidance_kind ON guidance(kind, number);

CREATE VIRTUAL TABLE IF NOT EXISTS guidance_fts USING fts5(
  id UNINDEXED,
  title,
  text,
  content='guidance',
  content_rowid='rowid',
  tokenize='porter unicode61'
);

-- What a document does to an earlier one: supersedes, obsoletes, revokes, modifies.
-- The Bulletin states these in prose and they are what turn a pile of rulings into
-- a citator.
CREATE TABLE IF NOT EXISTS guidance_relations (
  from_id TEXT NOT NULL,
  to_id   TEXT NOT NULL,
  kind    TEXT NOT NULL,
  PRIMARY KEY (from_id, to_id, kind)
);

CREATE INDEX IF NOT EXISTS idx_guidance_rel_to ON guidance_relations(to_id);

-- Court decisions, cached from CourtListener. Metadata only: the keyless API does
-- not serve opinion text, so a pincite or a quotation cannot be checked and TaxCite
-- must not imply otherwise.
-- Rates the government publishes monthly rather than legislating. The § 382(f)
-- long-term tax-exempt rate is the one that matters in deal work: it is fixed by the
-- month of the ownership change, so this is keyed by month and looked up exactly. A
-- neighbouring month is not a substitute and must never be silently substituted.
CREATE TABLE IF NOT EXISTS rates (
  month                      TEXT PRIMARY KEY,  -- first day of the governed month
  long_term_tax_exempt       REAL NOT NULL,     -- § 382(f), as a percentage
  adjusted_federal_long_term REAL,              -- § 1274(d), same month
  ruling                     TEXT NOT NULL,     -- "Rev. Rul. 2026-17"
  bulletin                   TEXT NOT NULL,     -- "2026-37"
  url                        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cases (
  id            TEXT PRIMARY KEY,   -- /us/case/290-u-s-111
  reporter_cite TEXT NOT NULL,
  case_name     TEXT NOT NULL,
  court         TEXT NOT NULL,
  date_filed    TEXT,
  url           TEXT NOT NULL,
  citations     TEXT NOT NULL DEFAULT '[]',
  cite_count    INTEGER NOT NULL DEFAULT 0,
  cluster_id    INTEGER,
  opinion_id    INTEGER,
  -- Opinion text, present only when an API token was configured. Without one the
  -- keyless search serves metadata and a short excerpt, and quotations are checked
  -- by asking whether a phrase occurs in this case rather than by comparing text.
  text          TEXT,
  excerpt       TEXT,
  -- How often later decisions cite this one, and when they last did. This is
  -- citation *history*, not treatment: no free source publishes whether a case was
  -- overruled, so TaxCite reports how alive a case looks and says plainly that it
  -- cannot tell you whether it is still good law.
  citing_count  INTEGER,
  last_cited    TEXT,
  last_citing   TEXT
);

CREATE INDEX IF NOT EXISTS idx_cases_cite ON cases(reporter_cite);

CREATE TABLE IF NOT EXISTS definitions (
  term_norm       TEXT NOT NULL,
  term_display    TEXT NOT NULL,
  provision_id    TEXT NOT NULL,
  scope           TEXT,
  definition_text TEXT NOT NULL,
  by_reference    TEXT,
  PRIMARY KEY (term_norm, provision_id)
);

CREATE INDEX IF NOT EXISTS idx_defs_term ON definitions(term_norm);

CREATE VIRTUAL TABLE IF NOT EXISTS definitions_fts USING fts5(
  term_norm,
  term_display,
  tokenize='porter unicode61'
);
