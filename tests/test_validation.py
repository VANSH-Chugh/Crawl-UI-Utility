import copy
import datetime as dt
import json
import unittest
from pathlib import Path

from server import TableParser
from validation import build_report, normalize_rules, number


NOW = dt.datetime(2026, 10, 28, 8, 0, tzinfo=dt.timezone.utc)


def dataset():
    parser = TableParser()
    parser.feed((Path(__file__).parent / 'fixtures' / 'msme_pmegp.html').read_text(encoding='utf-8'))
    table = parser.tables[0]
    url = 'https://dashboard.msme.gov.in/Pmegp_StateWise.aspx?FY=2024-25'
    table.update({'id':'pmegp-1','page_url':url})
    return {'schema_version':'1.2','id':'pmegp','source_url':url,'retrieved_at':NOW.isoformat(),
            'status':'complete','warnings':[],'pages_crawled':1,'tables':[table]}


def checks(report):
    return {c['id']: c for c in report['dataset_checks'] + report['tables'][0]['checks']}


class ValidationTests(unittest.TestCase):
    def test_good_source_has_proven_checks_and_explicit_limits(self):
        report = build_report(dataset(), now=NOW)
        result = checks(report)
        for name in ('json_syntax','schema','source_fetch','provenance','freshness','record_shape','source_headers','row_accounting','source_column_coverage','missing_values','duplicate_rows','type_conversion','totals','financial_year'):
            self.assertEqual(result[name]['status'], 'passed', name)
        for name in ('source_coverage','publisher_freshness','expected_count','expected_fields','configured_types','record_identifiers','reference_samples','units_currency','semantic_accuracy'):
            self.assertEqual(result[name]['status'], 'not_verified', name)
        self.assertFalse(report['guaranteed_accurate'])
        self.assertTrue(report['review_required'])
        self.assertEqual(len(report['tables'][0]['reconciliation']), 3)
        self.assertEqual(report['tables'][0]['reconciliation'][1]['difference'], '0.00')

    def test_contract_and_trusted_samples_are_evaluated(self):
        data = dataset()
        rules = {'expected_columns': data['tables'][0]['columns'], 'expected_record_count':35,
                 'expected_financial_year':'2024-25','numeric_columns':['Number of Projects','Margin Money','Employement'],
                 'key_columns':['State Name'],'reference_rows':[{'State Name':'ANDAMAN NICOBAR','Number of Projects':'14','Margin Money':'1734577.00'}]}
        report = build_report(data, rules, now=NOW)
        result = checks(report)
        for name in ('expected_fields','expected_count','configured_types','record_identifiers','reference_samples'):
            self.assertEqual(result[name]['status'], 'passed')
        self.assertFalse(report['guaranteed_accurate'])
        self.assertEqual(result['semantic_accuracy']['status'], 'not_verified')

    def test_total_mismatch_is_failed_with_exact_difference(self):
        data = dataset()
        data['tables'][0]['rows'][0]['Margin Money'] = '1734577.01'
        report = build_report(data, now=NOW)
        self.assertEqual(checks(report)['totals']['status'], 'failed')
        self.assertEqual(report['automation_status'], 'failed')
        entry = next(e for e in report['tables'][0]['reconciliation'] if e['column'] == 'Margin Money')
        self.assertEqual(entry['difference'], '0.01')

    def test_freshness_is_recomputed_and_future_dates_do_not_pass(self):
        data = dataset()
        stale = build_report(data, now=NOW + dt.timedelta(hours=25))
        self.assertEqual(checks(stale)['freshness']['status'], 'warning')
        self.assertEqual(checks(build_report(data, now=NOW - dt.timedelta(hours=1)))['freshness']['status'], 'warning')
        self.assertEqual(checks(build_report(data, {'max_age_hours':48}, now=NOW + dt.timedelta(hours=25)))['freshness']['status'], 'passed')

    def test_mismatched_contract_and_reference_fail(self):
        report = build_report(dataset(), {'expected_columns':['Wrong'], 'expected_record_count':37,
                                         'expected_financial_year':'2025-26', 'reference_rows':[{'State Name':'ANDAMAN NICOBAR','Number of Projects':'999'}]}, now=NOW)
        for name in ('expected_fields','expected_count','financial_year','reference_samples'):
            self.assertEqual(checks(report)[name]['status'], 'failed', name)

    def test_missing_duplicate_and_invalid_numeric_values(self):
        data = dataset()
        data['tables'][0]['rows'][1] = copy.deepcopy(data['tables'][0]['rows'][0])
        data['tables'][0]['rows'][2]['Margin Money'] = 'invalid'
        data['tables'][0]['rows'][3]['State Name'] = ''
        report = build_report(data, {'numeric_columns':['Margin Money'],'key_columns':['State Name']}, now=NOW)
        result = checks(report)
        self.assertEqual(result['duplicate_rows']['status'], 'warning')
        self.assertEqual(result['missing_values']['status'], 'warning')
        self.assertEqual(result['record_identifiers']['status'], 'failed')
        self.assertEqual(result['type_conversion']['status'], 'failed')
        self.assertEqual(result['totals']['status'], 'failed')
        self.assertEqual(next(e for e in report['tables'][0]['reconciliation'] if e['column'] == 'Margin Money')['status'], 'not_verified')

    def test_no_totals_are_not_passed_and_percentages_not_added(self):
        data = dataset()
        data['tables'][0]['totals'] = []
        self.assertEqual(checks(build_report(data, now=NOW))['totals']['status'], 'not_verified')
        data['tables'][0]['totals'] = [{'label':'Total','values':{'Rate (%)':'50'},'cells':[]}]
        self.assertEqual(checks(build_report(data, now=NOW))['totals']['status'], 'not_verified')

    def test_rules_are_applied_per_table(self):
        data = dataset()
        second = copy.deepcopy(data['tables'][0])
        second['id'] = 'other'
        data['tables'].append(second)
        report = build_report(data, now=NOW, table_rules={'pmegp-1':{'expected_record_count':99}})
        first = next(c for c in report['tables'][0]['checks'] if c['id'] == 'expected_count')
        other = next(c for c in report['tables'][1]['checks'] if c['id'] == 'expected_count')
        self.assertEqual(first['status'], 'failed')
        self.assertEqual(other['status'], 'not_verified')

    def test_rules_validation_and_exact_decimal_parsing(self):
        for invalid in [{'expected_record_count':True},{'max_age_hours':0},{'max_age_hours':float('nan')},{'unknown':1},{'reference_rows':[{'a':float('inf')}]},{'key_columns':'State Name'}]:
            with self.assertRaises(ValueError):
                normalize_rules(invalid)
        self.assertEqual(str(number('1,23,456.00')), '123456.00')
        self.assertEqual(str(number('1,234,567.01')), '1234567.01')
        for value in ['1,23','NaN','Infinity','50%','₹100']:
            self.assertIsNone(number(value))

    def test_invalid_json_values_do_not_pass_serialization(self):
        data = dataset()
        data['extra'] = float('nan')
        self.assertEqual(checks(build_report(data, now=NOW))['json_syntax']['status'], 'failed')
        json.dumps(build_report(dataset(), now=NOW), allow_nan=False)


if __name__ == '__main__':
    unittest.main()
