"""Evidence-based validation. A pass applies only to the condition stated."""
import datetime as dt
import json
import math
import re
from collections import Counter
from decimal import Decimal, InvalidOperation, localcontext
from urllib.parse import parse_qs, urlsplit

STATUSES = ('passed', 'failed', 'warning', 'not_verified', 'not_applicable')
DEFAULT_RULES = {'max_age_hours': 24, 'expected_columns': [], 'expected_record_count': None,
                 'expected_financial_year': '', 'numeric_columns': [], 'key_columns': [], 'reference_rows': []}


def normalize_rules(value=None):
    if value is not None and not isinstance(value, dict):
        raise ValueError('Validation rules must be a JSON object.')
    value = value or {}
    unknown = set(value) - set(DEFAULT_RULES)
    if unknown:
        raise ValueError(f'Unknown validation rule: {sorted(unknown)[0]}')
    rules = {**DEFAULT_RULES, **value}
    for key in ('expected_columns', 'numeric_columns', 'key_columns'):
        if not isinstance(rules[key], list) or len(rules[key]) > 200 or any(not isinstance(c, str) or not c.strip() for c in rules[key]):
            raise ValueError(f'{key} must be a list of nonempty column names.')
        rules[key] = list(dict.fromkeys(c.strip() for c in rules[key]))
    age = rules['max_age_hours']
    if isinstance(age, bool) or not isinstance(age, (int, float)) or not 0 < age <= 87600:
        raise ValueError('Freshness threshold must be greater than 0 and at most 87,600 hours.')
    count = rules['expected_record_count']
    if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 0):
        raise ValueError('Expected record count must be a nonnegative integer or null.')
    if not isinstance(rules['expected_financial_year'], str) or len(rules['expected_financial_year']) > 30:
        raise ValueError('Expected financial year must be a short string.')
    references = rules['reference_rows']
    if not isinstance(references, list) or len(references) > 100 or any(not isinstance(row, dict) or not row or
            any(not isinstance(k, str) or not isinstance(v, (str, int, float)) or isinstance(v, bool) or
                (isinstance(v, float) and not math.isfinite(v)) for k, v in row.items()) for row in references):
        raise ValueError('Reference rows must be an array of up to 100 nonempty objects with string or numeric values.')
    return rules


def number(value):
    """Parse explicit plain/grouped decimal strings; do not guess currency or %."""
    text = str(value).strip()
    if len(text) > 120:
        return None
    plain = r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)'
    grouped = r'[+-]?(?:\d{1,3}(?:,\d{3})+|\d{1,2}(?:,\d{2})*,\d{3})(?:\.\d*)?'
    if not re.fullmatch(f'(?:{plain}|{grouped})', text):
        return None
    try:
        return Decimal(text.replace(',', ''))
    except InvalidOperation:
        return None


def check(key, title, status, detail, evidence=None):
    result = {'id': key, 'title': title, 'status': status, 'detail': detail}
    if evidence is not None:
        result['evidence'] = evidence
    return result


def table_report(table, rules, source_url):
    columns, rows = table.get('columns', []), table.get('rows', [])
    checks, types, reconciliation = [], [], []
    keys_ok = bool(columns) and len(set(columns)) == len(columns) and all(isinstance(c, str) and c for c in columns)
    shape_ok = keys_ok and all(isinstance(row, dict) and set(row) == set(columns) for row in rows)
    checks.append(check('record_shape', 'Record shape and field alignment', 'passed' if shape_ok else 'failed',
                        'Every record has exactly the declared field keys. This checks structure, not semantic meaning.',
                        {'columns': columns, 'records': len(rows)}))
    extraction = table.get('validation', {})
    headings_ok = bool(table.get('header_rows')) and extraction.get('status') == 'recognized'
    checks.append(check('source_headers', 'Source headings recognized', 'passed' if headings_ok else 'warning',
                        'Column keys were derived from recognized HTML header cells.' if headings_ok else
                        'Headings are missing, generic, or need review.', extraction.get('issues', [])))
    expected = rules['expected_columns']
    checks.append(check('expected_fields', 'Expected fields and order',
                        ('passed' if columns == expected else 'failed') if expected else 'not_verified',
                        'Compared with your configured column list.' if expected else 'Add expected column names to validate this application contract.',
                        {'expected': expected, 'actual': columns} if expected else None))
    expected_count = rules['expected_record_count']
    checks.append(check('expected_count', 'Expected record count',
                        ('passed' if len(rows) == expected_count else 'failed') if expected_count is not None else 'not_verified',
                        'Compared with your configured count. This does not prove site-wide completeness.' if expected_count is not None else
                        'The tool counts extracted records, but has no independent expected count.',
                        {'actual': len(rows), 'expected': expected_count}))
    audit = table.get('extraction_audit')
    accounting_ok = audit and audit['source_row_count'] == sum(audit[k] for k in ('header_row_count', 'context_row_count', 'record_count', 'summary_row_count'))
    checks.append(check('row_accounting', 'Parsed HTML row accounting', 'passed' if accounting_ok else 'not_verified',
                        'Every parsed HTML row is accounted for as a header, context, record, or summary. Hidden/API/paginated rows are outside this check.', audit))
    checks.append(check('source_column_coverage', 'Original row column coverage',
                        ('warning' if audit.get('ragged_record_rows') else 'passed') if audit else 'not_verified',
                        'Checks parsed HTML grid widths before records are padded. Merged cells are expanded; irregular source rows need review.',
                        {'ragged_record_rows': audit.get('ragged_record_rows', [])} if audit else None))
    missing = [{'record': i + 1, 'column': c} for i, row in enumerate(rows) for c in columns if row.get(c) is None or str(row.get(c, '')).strip() == '']
    checks.append(check('missing_values', 'Missing cell values', 'warning' if missing else 'passed',
                        f'{len(missing)} blank or null cells. Blanks may be legitimate; nothing was filled or dropped.',
                        {'count': len(missing), 'examples': missing[:10]}))
    signatures = Counter(tuple(str(row.get(c, '')) for c in columns) for row in rows)
    duplicates = sum(n - 1 for n in signatures.values() if n > 1)
    checks.append(check('duplicate_rows', 'Exact duplicate records', 'warning' if duplicates else 'passed',
                        f'{duplicates} repeated records across all fields. No records were removed.', {'duplicate_count': duplicates}))
    key_columns = rules['key_columns']
    if key_columns:
        unknown = [c for c in key_columns if c not in columns]
        keys = [tuple(str(row.get(c, '')).strip() for c in key_columns) for row in rows]
        repeated = len(keys) - len(set(keys))
        blanks = sum(any(not v for v in key) for key in keys)
        key_status = 'failed' if unknown or repeated or blanks else 'passed'
        checks.append(check('record_identifiers', 'Unique record identifiers', key_status, 'Checked your selected key fields for missing values and uniqueness.',
                            {'key_columns': key_columns, 'unknown_columns': unknown, 'duplicate_keys': repeated, 'blank_keys': blanks}))
    else:
        checks.append(check('record_identifiers', 'Unique record identifiers', 'not_verified', 'Configure key columns, such as State Name, to detect repeated entities even if serial numbers differ.'))
    numeric_issues = []
    for column in columns:
        values = [row.get(column) for row in rows if row.get(column) is not None and str(row.get(column)).strip()]
        parsed = [number(value) for value in values]
        valid = sum(value is not None for value in parsed)
        expected_numeric = column in rules['numeric_columns']
        inferred = bool(values) and valid / len(values) >= .8
        kind = 'string'
        if valid and (inferred or expected_numeric):
            kind = 'integer' if all(v is None or v == v.to_integral_value() for v in parsed) else 'decimal'
            if any('.' in str(v) for v in values if number(v) is not None):
                kind = 'decimal'
        if (expected_numeric or inferred) and (not values or valid != len(values)):
            numeric_issues.append({'column': column, 'invalid_values': [str(v) for v, p in zip(values, parsed) if p is None][:5], 'expected_numeric': expected_numeric})
        types.append({'column': column, 'storage_type': 'string', 'suggested_type': kind, 'basis': 'configured' if expected_numeric else 'inferred',
                      'nonempty_values': len(values), 'numeric_values': valid})
    unknown_numeric = [c for c in rules['numeric_columns'] if c not in columns]
    numeric_status = 'failed' if unknown_numeric or any(i['expected_numeric'] for i in numeric_issues) else 'warning' if numeric_issues else 'passed'
    checks.append(check('type_conversion', 'Numeric conversion and type consistency', numeric_status,
                        'Suggested types are inferred unless configured. Raw strings are preserved; use exact decimals for money. Units and semantic types are not inferred.',
                        {'columns': types, 'issues': numeric_issues, 'unknown_numeric_columns': unknown_numeric}))
    checks.append(check('configured_types', 'Application numeric type contract',
                        ('passed' if numeric_status == 'passed' else numeric_status) if rules['numeric_columns'] else 'not_verified',
                        'Configured numeric fields were checked for conversion.' if rules['numeric_columns'] else 'Configure numeric columns to assert which fields must be numbers.'))
    for summary in table.get('totals', []):
        whole_table = bool(re.fullmatch(r'(?:(?:grand|overall)\s*[- ]?\s*)?total\s*[:.\-–]*\s*', summary.get('label', ''), re.I))
        for column, total in summary.get('values', {}).items():
            reported = number(total)
            values = [number(row.get(column, '')) for row in rows]
            nonadditive = bool(re.search(r'%|rate|ratio|average|mean|median|percent|index', column, re.I))
            reason = None
            if not whole_table:
                reason = 'Summary is not labeled as a whole-table total.'
            elif nonadditive:
                reason = 'Field may be non-additive; no sum was assumed.'
            elif reported is None or not values or any(v is None for v in values):
                reason = 'The total or some records are not explicit numeric values.'
            entry = {'label': summary.get('label'), 'column': column, 'reported_total': str(total)}
            if reason:
                entry.update({'status': 'not_verified', 'reason': reason})
            else:
                with localcontext() as ctx:
                    ctx.prec = 160
                    calculated = sum(values, Decimal(0))
                    difference = calculated - reported
                entry.update({'status': 'passed' if difference == 0 else 'failed', 'record_sum': str(calculated), 'difference': str(difference)})
            reconciliation.append(entry)
    total_status = 'failed' if any(e['status'] == 'failed' for e in reconciliation) else 'passed' if reconciliation and all(e['status'] == 'passed' for e in reconciliation) else 'not_verified'
    checks.append(check('totals', 'Source total reconciliation', total_status,
                        'Exact decimal sums compared with reported whole-table totals. Agreement proves arithmetic consistency only; it does not establish that summing is semantically appropriate.' if reconciliation else
                        'No comparable source totals were found. No totals were invented.', reconciliation))
    context = table.get('context', {})
    query = parse_qs(urlsplit(source_url).query)
    requested_year = next((v[0] for k, v in query.items() if k.lower() in ('fy', 'financial_year') and v), '')
    expected_year = rules['expected_financial_year'] or requested_year
    actual_year = context.get('financial_year')
    year_status = ('passed' if actual_year == expected_year else 'failed') if expected_year and actual_year else 'not_verified'
    checks.append(check('financial_year', 'Financial year and requested filter', year_status,
                        'Compared an explicit table year with your rule or the FY URL parameter. Other source filters are not validated.',
                        {'actual': actual_year, 'expected': expected_year or None, 'expected_from': 'rule' if rules['expected_financial_year'] else 'URL' if requested_year else None,
                         'labels': context.get('labels', [])}))
    checks.append(check('units_currency', 'Units, currency, and scale', 'not_verified',
                        'Original headings and context are preserved, but their units/currency/scale have not been independently verified. Confirm these before calculations.'))
    references = rules['reference_rows']
    results = []
    for index, reference in enumerate(references):
        matches = [row for row in rows if all(k in row and str(row[k]) == str(v) for k, v in reference.items())]
        results.append({'reference': index + 1, 'status': 'passed' if len(matches) == 1 else 'failed', 'matching_records': len(matches), 'expected': reference})
    ref_status = ('passed' if all(r['status'] == 'passed' for r in results) else 'failed') if references else 'not_verified'
    checks.append(check('reference_samples', 'Trusted reference sample values', ref_status,
                        'Each supplied reference object must match exactly one extracted record, using exact field names and string values. Reference correctness remains your responsibility.' if references else
                        'Supply independently verified sample records to check field meaning and sample values.', results or None))
    checks.append(check('semantic_accuracy', 'Meaning and publisher accuracy', 'not_verified',
                        'Automated checks cannot establish that a value represents the intended real-world fact, or that the publisher is correct.'))
    return {'table_id': table.get('id'), 'name': table.get('name'), 'checks': checks,
            'column_types': types, 'reconciliation': reconciliation}


def build_report(dataset, rules=None, now=None, table_rules=None):
    rules = normalize_rules(rules)
    now = now or dt.datetime.now(dt.timezone.utc)
    checks = []
    try:
        roundtrip = json.loads(json.dumps(dataset, allow_nan=False))
        parseable = isinstance(roundtrip, dict)
    except (ValueError, TypeError):
        parseable = False
    checks.append(check('json_syntax', 'JSON serialization', 'passed' if parseable else 'failed', 'Dataset serializes as strict JSON with no NaN or Infinity values.'))
    schema_ok = dataset.get('schema_version') in ('1.1', '1.2') and isinstance(dataset.get('tables'), list) and bool(dataset['tables'])
    checks.append(check('schema', 'Supported dataset schema', 'passed' if schema_ok else 'failed',
                        'Requires schema 1.1 or 1.2 and at least one extracted table.', {'schema_version': dataset.get('schema_version')}))
    complete = dataset.get('status') == 'complete' and not dataset.get('warnings')
    checks.append(check('source_fetch', 'Extraction completed without warnings', 'passed' if complete and not dataset.get('sample') else 'failed',
                        'The bounded crawl completed without reported fetch/parser warnings. This is not a completeness claim.',
                        {'status': dataset.get('status'), 'warnings': dataset.get('warnings', [])}))
    provenance = bool(dataset.get('source_url') and dataset.get('retrieved_at')) and all(t.get('page_url') for t in dataset.get('tables', []))
    checks.append(check('provenance', 'Source and retrieval provenance', 'passed' if provenance else 'failed',
                        'Source URL, per-table page URL, and retrieval timestamp are present. This does not authenticate the publisher.',
                        {'source_url': dataset.get('source_url'), 'retrieved_at': dataset.get('retrieved_at')}))
    age_hours = None
    try:
        stamp = dt.datetime.fromisoformat(dataset['retrieved_at'])
        if stamp.tzinfo is None:
            raise ValueError('Timezone is missing')
        age_hours = (now - stamp).total_seconds() / 3600
        fresh_status = 'passed' if 0 <= age_hours <= rules['max_age_hours'] else 'warning'
    except (ValueError, TypeError, KeyError):
        fresh_status = 'failed'
    checks.append(check('freshness', 'Retrieval freshness', fresh_status,
                        'Measures age of the fetch at report time, not age of the published data.',
                        {'age_hours': round(age_hours, 4) if age_hours is not None else None, 'max_age_hours': rules['max_age_hours']}))
    checks.append(check('source_coverage', 'Complete source and pagination coverage', 'not_verified',
                        'The crawler cannot prove that all website/API pages, hidden rows, or server-side pagination were included.',
                        {'pages_crawled': dataset.get('pages_crawled', 0)}))
    checks.append(check('publisher_freshness', 'Publisher data is the latest release', 'not_verified',
                        'A fresh fetch does not prove the publisher has updated the data or that this is the newest report.', dataset.get('published_dates', [])))
    table_rules = table_rules or {}
    tables = []
    for table in dataset.get('tables', []):
        applied = normalize_rules({**rules, **table_rules.get(table.get('id'), {})})
        report = table_report(table, applied, dataset.get('source_url', ''))
        report['rules'] = applied
        tables.append(report)
    all_checks = checks + [c for t in tables for c in t['checks']]
    summary = {status: sum(c['status'] == status for c in all_checks) for status in STATUSES}
    automation = 'failed' if summary['failed'] else 'warning' if summary['warning'] else 'passed'
    return {'report_version': '1.0', 'checked_at': now.isoformat(), 'source_id': dataset.get('id'),
            'source_url': dataset.get('source_url'), 'automation_status': automation,
            'review_required': bool(summary['failed'] or summary['warning'] or summary['not_verified']),
            'guaranteed_accurate': False, 'summary': summary, 'rules': rules,
            'scope': 'Passes establish only the stated tested conditions at checked_at. Source truth, semantic accuracy, units, and site-wide completeness are not guaranteed.',
            'dataset_checks': checks, 'tables': tables}
