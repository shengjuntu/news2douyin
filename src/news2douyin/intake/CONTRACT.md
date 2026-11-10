# Intake document and evidence boundary

`IntakeDocument` is the connector envelope for an industry website or another
application. It supplies `provider`, a stable provider-local `record_id`, optional
HTTP(S) `uri`, original `title`/`content`, timezone-qualified `published_at` and
`observed_at`, optional `language`/publisher `country`, JSON `metadata`, and an
optional SHA-256 of the exact UTF-8 body. The importing service does not fetch
URLs, search, translate, summarize, filter importance, or ask a model.

Canonical HTTP URI identifies the article across providers. Without a URI,
`provider + record_id` identifies it. Unchanged normalized observations are
idempotent; changed source metadata retains a separate receipt while unchanged
article text keeps its content revision. `envelope_json` retains external
observation metadata; immutable `ArticleVersion` retains the submitted body.
Public import receipts omit the potentially large envelope. To retain separately
identified repeated observations of identical text, connectors can include their
stable observation identifier in metadata.

GKG projection never overwrites an existing full article with empty discovery
content. An observation is retained through its immutable `(feed, batch,
row_index)` lake pointer and a SQLite projection receipt. Later correction or
new content creates another article version; a delayed older batch cannot roll
a newer discovery headline backwards. Duplicate URLs retain separate raw rows
and receipts but share article identity. Unsupported/citation URLs and invalid
records remain in the raw lake with an explicit skipped projection receipt.

GKG does not supply article body text. Missing titles are retained as empty
titles, not synthesized events. Such articles have an auditable version whose
reader state is `metadata_only`, not an extraction/model queue. A genuine source
headline or body can enter the normal document-reading pipeline. Publication,
GKG batch time, observation time, and event occurrence time are distinct.

The downstream layers continue to be evidence-grounded: document versions
contain passages; extracted event mentions bind their time, role arguments and
relations to passage offsets; graph candidates and model assessments refer to
those assertions. A source assertion is not a guarantee of world truth. GKG
place/person co-occurrence is not a directed country action or causal relation.

`project_step()` reads a bounded ordered batch outside a SQLite write lock,
then atomically writes articles, versions, receipts and `projected_offset`.
Every source row advances exactly one receipt, including rows that cannot be
projected. A row gap aborts instead of skipping. Original archive ingestion
and model/graph work run separately, so analysis lag does not discard acquired
material. `projection_status()` distinguishes archived-row projection from
external imported versions and reports pending rows and errors.
