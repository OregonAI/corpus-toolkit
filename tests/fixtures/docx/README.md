# Word fixtures for tests/test_docx_to_text.py (corpus-toolkit#219)

Real files, copied byte for byte, except the one marked derived. Sources, all fetched by their
corpora through the toolkit Fetcher and committed there as `_meta/snapshots/<name>`:

| file | corpus |
|---|---|
| `wa-ea-185-10`, `wv-csr-163-2`, `me-cmr-29-250-ch950`, `wa-data-01-04-g`, `ca-simm-18b` | states-government-it |
| `ut-dts-4000-0008` (a Title-style first paragraph), `or-eis-ai-attachment-a` (a title split across two Title paragraphs inside a text box, runs split inside a word) | states-government-it |
| `or-perkins-plan-app-h`, `or-perkins-plan-app-c` | cte-oregon |
| `or-perkins-plan-cover-and-text-boxes` (**derived**) | cte-oregon `or-perkins-plan.docx` (5 MB): body paragraphs 0-8 (the cover, where "202" + shape + "8" splits a word), 13, 27, 138 (text boxes pandoc reads) and the paragraphs around Figure 2 and the Clackamas photo credit (text boxes in group shapes, which pandoc drops), with media, headers, footers and custom XML parts removed. Every paragraph kept is unchanged. |
