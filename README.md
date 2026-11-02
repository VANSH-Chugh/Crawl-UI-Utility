# CivicTable

A local UI utility for extracting public HTML tables from government websites, exploring them, and handing them over as separate JSON datasets or PowerPoint presentations.

## Run

On this Windows machine, double-click `start.cmd`, then open http://127.0.0.1:8000. It prefers the project's `.venv`, then the bundled Python runtime when available. Keep the terminal window open while using the app.

Requires Python 3.10 or later. Install `truststore` for OS-backed HTTPS certificate validation and `python-pptx` for PPT export.

```powershell
python -m pip install -r requirements.txt
python server.py
```

If using an existing project virtual environment, install with `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`. HTTPS uses the operating system certificate store through `truststore`, including native certificate-chain building on Windows. Certificate and hostname checks remain enabled. If the dependency is absent, Python's standard verified TLS context is used; sites with incomplete chains may fail until requirements are installed.

Open http://127.0.0.1:8000. On this machine, if Python is not on PATH, use:

```powershell
& 'C:\Users\vansh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' server.py
```

## Workflow

1. Enter up to 10 source page links, one per line.
2. Optionally check up to 4 additional links on each source's website. This is a bounded, one-level crawl, not a full site index or a newest-report detector.
3. Review individual sources and their tables, search rows, and use pagination.
4. Download a table as CSV, all tables from one source as JSON, or generate a PPT for one source.
5. Use **JSON & API** to download each source separately and copy integration URLs.

The sample is explicitly illustrative, not fetched government data. PPT export is enabled for real extracted data. Original headings and serial-number columns are preserved; the UI does not add an index column. Header cells directly inside `<thead>` without a `<tr>` are supported, matching browser repair of the MSME dashboard's markup. The nearest preceding heading supplies a table title when no caption exists.

All extracted values remain strings. JSON `rows` contains records only. Source totals and `<tfoot>` summaries are separate in `totals`, with original labels, values, and merged-cell metadata. `header_rows` preserves grouped header spans, while `columns` supplies flattened, unique JSON keys. Without actual headings, generic column names are still necessary. The UI and PPT preserve header/footer merges; CSV uses one label per summary and blank cells across the rest of its span.

Full-width leading title/year bands are preserved in `context_rows` and `context.labels`, not repeated into column keys or included as records. An explicitly labeled financial year is available as `context.financial_year`. Explicit `<thead>` headers are recognized even when a banner precedes them in raw HTML, matching the browser's header placement. Source spellings are retained (for example PMEGP's `Employement`). JSON values remain strings: downstream applications should convert counts to integers and money to a decimal type before arithmetic.

Every table has `validation` with scope `table_structure`, a `record_count`, and status `recognized` or `needs_review`. Unrecognized column headings produce a review issue and a source warning. This is structural validation, not a guarantee that the publisher's data or every semantic interpretation is correct. Consumers should check source status/warnings, table validation, context/year, required columns, and expected record counts. The PMEGP fixture checks all three numerical column sums against the reported grand totals using exact decimal arithmetic.

Current extractions have top-level `schema_version: "1.2"`. Re-extract old saved datasets to obtain complete extraction accounting and the current structure; copying a previously downloaded JSON file does not upgrade it.

## On-page JSON validation

Open **JSON validation** in the sidebar, or **Validation** above the extracted table. The report distinguishes `passed`, `failed`, `warning`, `not_verified`, and `not_applicable`. Each check includes an explanation and evidence. There is no correctness score or automatic claim that a dataset is guaranteed accurate. `review_required` remains true while failed, warning, or unverified checks remain.

Automatic checks cover strict JSON serialization, supported schema, source fetch warnings, provenance, retrieval age, record field shape, recognized headings, accounting of parsed HTML rows, source row widths, missing cells, exact duplicate rows, inferred numeric conversion, financial-year agreement with an explicit FY URL filter, and exact decimal reconciliation of source totals. Context and totals are separated from data records. Rates/percentages and subtotals are not automatically treated as additive whole-table totals.

The rules form can add expected columns and order, expected record count, explicit numeric fields, unique key fields, expected financial year, maximum fetch age, and independently verified reference records. Rules apply to the selected source/table; freshness is source-wide. Rules are saved locally under `data/validation-rules`. A reference object must match exactly one record using the provided field names and exact values. Prefer strings for precise decimal sample values. Tests cannot establish that your reference data is itself correct.

Units/currency/scale, publisher correctness, full site/API pagination coverage, and latest-release status remain **Not verified**. A passed expected count does not prove coverage. Suggested numeric types are inference, not semantic type guarantees. Numeric values in the original JSON remain strings.

Final data JSON downloads, JSON previews, and public data API responses contain source data and provenance only: source identity/URL, retrieval time, tables, headings, context, records, and totals. They exclude validation reports, tests, extraction audits, warnings/status diagnostics, and endpoint metadata. Validation remains in the page, the separate report download, and the `/api/validation/{source_id}` endpoint. Internal workspace state retains the information needed for the UI; it is not the final data export.

```text
GET /api/validation/{source_id}
```

Returns only the current validation report for saved data; this does not crawl the source again. Use the source's `?refresh=true` API for fresh extraction.

```http
POST /api/validation
Content-Type: application/json

{"id":"source_id","table_id":"source_id-1","save_rules":true,"rules":{"expected_record_count":35,"key_columns":["State Name"],"numeric_columns":["Number of Projects","Margin Money"],"max_age_hours":24}}
```

Omitted rules use defaults. `save_rules: false` evaluates a temporary rule configuration. Clear custom rules using the UI's clear button. Downstream apps should inspect specific required check IDs; `automation_status: "passed"` does not mean all requirements are independently verified, and `guaranteed_accurate` is always `false`.

## API

Every extraction fetches the current content served by the supplied URL; retrieval time does not prove that source data was recently published. Available publication metadata is preserved without inference.

### Fetch one source as fresh JSON

```text
GET /api/json?url=https%3A%2F%2Fyour-source.gov%2Fdata
GET /api/extract?url=https%3A%2F%2Fyour-source.gov%2Fdata
GET /api/extract?url=https%3A%2F%2Fyour-source.gov%2Fdata&follow=true
```

### Dedicated per-source JSON API

```text
GET /api/json/{source_id}
GET /api/json/{source_id}?refresh=true
```

The first URL returns the saved source as `application/json` with no attachment header, suitable for API clients. The second re-crawls the source and returns fresh JSON. Optional `&follow=true` also crawls bounded linked pages. `GET /api/json?url=...` creates an extraction and its stable source ID. Build endpoints using the returned `id`, or use the UI's **JSON & API** copy and open buttons. Data responses contain no validation results or endpoint metadata.

Internal workspace data is saved under `data/datasets/{source_id}.json` (Git-ignored). These files and source endpoints survive a server restart. Public data responses are projected to the clean export format and include retrieval timestamps. The `/api/json` endpoints return HTTP 502 for extraction errors; use the separate validation endpoint for diagnostic checks and partial/empty extraction evidence. This remains a loopback-only local API, not a publicly hosted service.

### Extract several sources

```http
POST /api/crawl
Content-Type: application/json

{"urls":["https://your-source.gov/data"],"follow_links":false}
```

Returns `{"datasets":[...]}` with one clean dataset per normalized input URL. A source with no extracted tables has an empty `tables` array; use `/api/validation/{source_id}` for extraction outcomes and diagnostic details. The app uses `/api/workspace/crawl` internally to display its warnings and validation panel; workspace responses are not final data exports.

### Retrieve a cached extraction / download JSON

```text
GET /api/datasets/{source_id}.json
```

IDs are stable SHA-256 prefixes of normalized source URLs. Re-extraction replaces the saved result. The in-memory cache clears on restart and is repopulated from saved local JSON files.

### Generate a PPT

```http
POST /api/ppt
Content-Type: application/json

{"id":"source_id_from_extraction"}
```

Returns a `.pptx` starting directly with data, with original headings, a visible source URL, editable tables, green header/footer bands, alternating blue rows, and the original merged total footer. There is no automatic cover slide or generated serial-number column. Tables split across slides in groups of 12 records and 8 columns. Source totals appear only on the final record page for each column group, without recalculation. Full cell values are retained; unusually long content can need manual slide layout adjustment.

### Dataset schema

```json
{
  "id": "stable_source_id",
  "source_url": "https://your-source.gov/data",
  "retrieved_at": "2026-10-28T12:00:00+00:00",
  "status": "complete",
  "title": "Page title",
  "pages_crawled": 1,
  "published_dates": [],
  "warnings": [],
  "tables": [{
    "id": "stable_source_id-1",
    "name": "Table caption",
    "page_url": "https://your-source.gov/data",
    "columns": ["State", "Value"],
    "header_rows": [[
      {"text":"State","column":0,"colspan":1,"rowspan":1},
      {"text":"Value","column":1,"colspan":1,"rowspan":1}
    ]],
    "rows": [{"State": "Example", "Value": "1,234"}],
    "totals": [{
      "label":"Total",
      "values":{"Value":"1,234"},
      "cells":[
        {"text":"Total","column":0,"colspan":1,"rowspan":1},
        {"text":"1,234","column":1,"colspan":1,"rowspan":1}
      ]
    }]
  }]
}
```

## Scope and limits

- Fetches server-rendered HTML tables. JavaScript-only grids, PDFs, CAPTCHA, authentication, and advanced pagination require adapters.
- Checks robots.txt; blocks disallowed pages and reports unavailable policies (except missing robots.txt). Uses a one-second delay between additional pages of a source.
- Public HTTP/S addresses on standard ports only, with validated IP-pinned connections and redirect checks. Maximum HTML response size is 4 MB; socket timeout is 12 seconds. No user-supplied credentials.
- Behind a reverse proxy (for example Nginx on a cloud server), set `CIVICTABLE_ORIGINS` to the comma-separated public origins users open, such as `https://civictable.example.com,http://203.0.113.10`. Otherwise browser POST requests are rejected with "Origin not allowed".
- The server binds to loopback only. It has no authentication, scheduler, or cross-origin API access. Use it from local scripts or a same-origin app; production API hosting needs these additions. Saved JSON files persist locally; delete unneeded files from `data/datasets` when the server is stopped.
- Government domains are not hardcoded: public statistical sources may live on other domains. Users choose and verify their sources.
- CSV downloads neutralize formula-leading cell values for safe spreadsheet opening.

## Verify

```powershell
python -m unittest discover -s tests -v
```

Tests cover header/cell spans, URL restrictions, failure isolation, JSON endpoints, and generated PPT structure. Network fixtures are mocked so checks don't depend on a government website's availability.
