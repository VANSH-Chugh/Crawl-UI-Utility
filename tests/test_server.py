import io
import json
import ssl
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from decimal import Decimal
from unittest.mock import patch

import server


def setUpModule():
    global data_directory, data_patch
    data_directory = tempfile.TemporaryDirectory()
    data_patch = patch('server.DATA_DIR', Path(data_directory.name) / 'datasets')
    data_patch.start()


def tearDownModule():
    data_patch.stop()
    data_directory.cleanup()


MSME_HTML = (Path(__file__).parent / 'fixtures' / 'msme_uam.html').read_text(encoding='utf-8')
PMEGP_HTML = (Path(__file__).parent / 'fixtures' / 'msme_pmegp.html').read_text(encoding='utf-8')

HTML = '''<title>Official statistics</title><meta name="last-modified" content="2026-10-20">
<table><caption>Regional figures</caption><tr><th rowspan="2">Region</th><th colspan="2">Totals</th></tr>
<tr><th>2025</th><th>2026</th></tr><tr><td>North</td><td>1,200</td><td>1,300</td></tr></table>
<a href="/other">Other page</a>'''


class ParserTests(unittest.TestCase):
    def test_spanning_headers_and_values(self):
        parser = server.TableParser()
        parser.feed(HTML)
        t = parser.tables[0]
        self.assertEqual(t['columns'], ['Region', 'Totals / 2025', 'Totals / 2026'])
        self.assertEqual(t['rows'], [{'Region': 'North', 'Totals / 2025': '1,200', 'Totals / 2026': '1,300'}])
        self.assertEqual(t['name'], 'Regional figures')
        self.assertEqual(parser.dates, ['2026-10-20'])

    def test_no_headers_and_duplicate_headers(self):
        parser = server.TableParser()
        parser.feed('<table><tr><td>A</td><td>B</td></tr></table><table><tr><th>X</th><th>X</th></tr><tr><td>1</td><td>2</td></tr></table>')
        self.assertEqual(parser.tables[0]['columns'], ['Column 1', 'Column 2'])
        self.assertEqual(parser.tables[0]['rows'][0]['Column 1'], 'A')
        self.assertEqual(parser.tables[1]['columns'], ['X', 'X (2)'])

    def test_body_rowspan(self):
        parser = server.TableParser()
        parser.feed('<table><tr><th>A</th><th>B</th></tr><tr><td rowspan="2">Group</td><td>1</td></tr><tr><td>2</td></tr></table>')
        self.assertEqual(parser.tables[0]['rows'][1], {'A': 'Group', 'B': '2'})

    def test_msme_original_headings_records_and_merged_total(self):
        parser = server.TableParser()
        parser.feed(MSME_HTML)
        self.assertEqual(len(parser.tables), 1)
        table = parser.tables[0]
        self.assertEqual(table['columns'], ['S. No.', 'District Name', 'Total Udyog Aadhaar', 'Micro', 'Small', 'Medium'])
        self.assertEqual(table['name'], 'District Wise Udyog Aadhaar Registration Details')
        self.assertEqual(len(table['rows']), 8)
        self.assertEqual(table['rows'][0]['S. No.'], '1')
        self.assertEqual(table['rows'][-1]['District Name'], 'WEST TRIPURA')
        self.assertEqual(table['totals'][0]['label'], 'Total:-')
        self.assertEqual(table['totals'][0]['values'], {'Total Udyog Aadhaar':'4695','Micro':'4136','Small':'536','Medium':'23'})
        self.assertEqual(table['totals'][0]['cells'][0]['colspan'], 2)
        self.assertEqual(sum(c['text'] == 'Total:-' for c in table['totals'][0]['cells']), 1)

    def test_thead_td_and_tfoot_preserve_structure(self):
        parser = server.TableParser()
        parser.feed('<table><thead><td>Name</td><td>Value</td></thead><tbody><tr><td>A</td><td>2</td></tr></tbody><tfoot><tr><td>Grand total</td><td>2</td></tr></tfoot></table>')
        t = parser.tables[0]
        self.assertEqual(t['columns'], ['Name', 'Value'])
        self.assertEqual(len(t['rows']), 1)
        self.assertEqual(t['totals'][0]['values'], {'Value': '2'})

    def test_pmegp_banner_is_context_not_a_record_or_column_key(self):
        parser = server.TableParser()
        parser.feed(PMEGP_HTML)
        t = parser.tables[0]
        self.assertEqual(t['columns'], ['S.No.', 'State Name', 'Number of Projects', 'Margin Money', 'Employement'])
        self.assertEqual(len(t['rows']), 35)
        self.assertEqual(t['context']['financial_year'], '2024-25')
        self.assertEqual(t['context']['labels'], ['Margin Money Disbursed to State in FY - 2024-25'])
        self.assertEqual(t['context_rows'][0][0]['colspan'], 5)
        self.assertEqual(len(t['header_rows']), 1)
        self.assertEqual(t['rows'][0], {'S.No.':'1','State Name':'ANDAMAN NICOBAR','Number of Projects':'14','Margin Money':'1734577.00','Employement':'112'})
        self.assertEqual([r['S.No.'] for r in t['rows']], [str(i) for i in range(1, 36)])
        self.assertEqual(t['rows'][-1]['State Name'], 'WEST BENGAL')
        self.assertEqual(t['validation']['status'], 'recognized')
        # Independently reconcile all numeric data columns against source totals.
        for column, reported in t['totals'][0]['values'].items():
            self.assertEqual(sum(Decimal(r[column]) for r in t['rows']), Decimal(reported), column)

    def test_unrecognized_headers_are_flagged_for_api_consumers(self):
        parser = server.TableParser()
        parser.feed('<table><tr><td>Unlabeled record</td><td>1</td></tr></table>')
        self.assertEqual(parser.tables[0]['validation']['status'], 'needs_review')
        self.assertTrue(parser.tables[0]['validation']['issues'])

    def test_full_width_thead_title_is_context(self):
        parser = server.TableParser()
        parser.feed('<table><thead><tr><th colspan="2">FY 2024-25</th></tr><tr><th>State</th><th>Value</th></tr></thead><tr><td>A</td><td>9</td></tr></table>')
        t = parser.tables[0]
        self.assertEqual(t['columns'], ['State', 'Value'])
        self.assertEqual(t['context']['financial_year'], '2024-25')
        self.assertEqual(len(t['rows']), 1)


class CrawlerTests(unittest.TestCase):
    def setUp(self):
        server.CACHE.clear()

    def test_url_validation(self):
        for url in ['file:///etc/passwd', 'ftp://example.gov', 'https://user:pass@example.gov', 'https://example.gov:8080/']:
            with self.assertRaises(ValueError):
                server.normalize_url(url)

    def test_tls_keeps_certificate_and_hostname_checks(self):
        context = server.create_tls_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_certificate_errors_remain_actionable(self):
        message = server.readable_error(ssl.SSLCertVerificationError(1, 'unable to get local issuer certificate'))
        self.assertIn('certificate verification failed', message)
        self.assertIn('not been bypassed', message)

    def test_private_network_blocked_before_connection(self):
        for url in ['http://127.0.0.1/', 'http://[::1]/', 'http://192.168.1.1/']:
            with self.assertRaisesRegex(ValueError, 'blocked'):
                server.fetch(url)

    @staticmethod
    def fake_fetch(url):
        return ('User-agent: *\nAllow: /' if url.endswith('robots.txt') else HTML), url

    def test_extract_provenance_and_cache(self):
        with patch('server.fetch', side_effect=self.fake_fetch):
            d = server.crawl_source('https://example.gov/data')
        self.assertEqual(d['status'], 'complete')
        self.assertEqual(len(d['tables']), 1)
        self.assertEqual(d['published_dates'][0]['value'], '2026-10-20')
        self.assertIn('+00:00', d['retrieved_at'])
        self.assertEqual(server.CACHE[d['id']], d)

    def test_robots_denial(self):
        with patch('server.fetch', return_value=('User-agent: *\nDisallow: /', 'https://example.gov/robots.txt')) as fetch:
            d = server.crawl_source('https://example.gov/data')
        self.assertEqual(d['status'], 'empty')
        self.assertEqual(fetch.call_count, 1)
        self.assertIn('disallows', d['warnings'][0])

    def test_failure_is_reported(self):
        with patch('server.fetch', side_effect=ValueError('Source returned HTTP 503.')):
            d = server.crawl_source('https://example.gov/data')
        self.assertEqual(d['status'], 'error')
        self.assertTrue(d['warnings'])

    def test_linked_crawl_is_same_host_and_bounded(self):
        html = HTML + ''.join(f'<a href="/page{i}">Link</a>' for i in range(20)) + '<a href="https://elsewhere.gov/">External</a>'
        def fake(url):
            return ('User-agent: *\nAllow: /' if url.endswith('robots.txt') else html), url
        with patch('server.fetch', side_effect=fake) as fetch, patch('server.time.sleep'):
            d = server.crawl_source('https://example.gov/data', True)
        self.assertEqual(d['pages_crawled'], 5)
        self.assertTrue(all(call.args[0].startswith('https://example.gov/') for call in fetch.call_args_list))

    def test_ppt_contains_provenance_and_table(self):
        from pptx import Presentation
        with patch('server.fetch', side_effect=self.fake_fetch):
            d = server.crawl_source('https://example.gov/data')
        d['tables'][0]['rows'] *= 25
        result = Presentation(io.BytesIO(server.presentation(d)))
        self.assertEqual(len(result.slides), 3)
        tables = [shape.table for s in result.slides for shape in s.shapes if shape.has_table]
        self.assertEqual(sum(len(t.rows) - 2 for t in tables), 25)
        self.assertEqual(tables[0].cell(2, 0).text, 'North')
        self.assertTrue(tables[0].cell(0, 0).is_merge_origin)

    def test_msme_ppt_matches_source_structure(self):
        from pptx import Presentation
        parser = server.TableParser()
        parser.feed(MSME_HTML)
        dataset = {'source_url':'https://dashboard.msme.gov.in/uam_dist_wise.aspx?stid=16',
                   'retrieved_at':'2026-10-28T06:00:00+00:00', 'tables':parser.tables}
        dataset['tables'][0]['page_url'] = dataset['source_url']
        deck = Presentation(io.BytesIO(server.presentation(dataset)))
        self.assertEqual(len(deck.slides), 1)
        table = next(s.table for s in deck.slides[0].shapes if s.has_table)
        self.assertEqual(len(table.columns), 6)
        self.assertEqual(len(table.rows), 10)
        self.assertEqual(table.cell(0, 0).text, 'S. No.')
        self.assertEqual(table.cell(0, 2).text, 'Total Udyog Aadhaar')
        self.assertTrue(table.cell(9, 0).is_merge_origin)
        self.assertTrue(table.cell(9, 1).is_spanned)
        self.assertEqual(table.cell(9, 0).text, 'Total:-')
        self.assertEqual(table.cell(9, 2).text, '4695')

    def test_pmegp_ppt_preserves_context_band_separately(self):
        from pptx import Presentation
        parser = server.TableParser()
        parser.feed(PMEGP_HTML)
        dataset = {'retrieved_at':'2026-10-28T07:00:00+00:00', 'tables':parser.tables}
        dataset['tables'][0]['page_url'] = 'https://dashboard.msme.gov.in/Pmegp_StateWise.aspx?FY=2024-25'
        deck = Presentation(io.BytesIO(server.presentation(dataset)))
        self.assertEqual(len(deck.slides), 3)
        tables = [shape.table for s in deck.slides for shape in s.shapes if shape.has_table]
        self.assertEqual(tables[0].cell(0, 1).text, 'State Name')
        self.assertTrue(tables[0].cell(1, 0).is_merge_origin)
        self.assertEqual(tables[0].cell(1, 0).span_width, 5)
        self.assertEqual(tables[0].cell(2, 1).text, 'ANDAMAN NICOBAR')
        self.assertEqual(tables[-1].cell(len(tables[-1].rows) - 1, 0).text, 'Grand Total')


class APITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.httpd.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join()

    def test_ui_and_health(self):
        with urllib.request.urlopen(self.base + '/') as r:
            self.assertIn(b'CivicTable', r.read())
        with urllib.request.urlopen(self.base + '/api/health') as r:
            self.assertEqual(json.load(r)['status'], 'ok')

    def test_batch_and_separate_json(self):
        request = urllib.request.Request(self.base + '/api/crawl', data=json.dumps({'urls':['https://example.gov/data','https://second.gov/data']}).encode(), headers={'Content-Type':'application/json'})
        with patch('server.fetch', side_effect=CrawlerTests.fake_fetch):
            with urllib.request.urlopen(request) as r:
                datasets = json.load(r)['datasets']
        self.assertEqual(len(datasets), 2)
        self.assertNotEqual(datasets[0]['id'], datasets[1]['id'])
        with urllib.request.urlopen(self.base + f"/api/datasets/{datasets[0]['id']}.json") as r:
            self.assertIn('.json', r.headers['Content-Disposition'])
            self.assertEqual(json.load(r)['source_url'], datasets[0]['source_url'])

    def test_invalid_api_url(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(self.base + '/api/extract?url=file:///etc/passwd')
        self.assertEqual(error.exception.code, 400)

    def test_dedicated_json_api_persists_and_refreshes(self):
        url = self.base + '/api/json?url=https%3A%2F%2Fexample.gov%2Fmsme'
        def fake(url):
            return ('User-agent: *\nAllow: /' if url.endswith('robots.txt') else MSME_HTML), url
        with patch('server.fetch', side_effect=fake):
            with urllib.request.urlopen(url) as response:
                data = json.load(response)
        endpoint = self.base + f"/api/json/{data['id']}"
        server.CACHE.clear()  # Simulate process restart: read saved JSON from disk.
        with patch('server.fetch', side_effect=AssertionError('Cached API must not recrawl')):
            with urllib.request.urlopen(endpoint) as response:
                self.assertIsNone(response.headers.get('Content-Disposition'))
                self.assertEqual(response.headers.get_content_type(), 'application/json')
                saved = json.load(response)
        self.assertEqual(len(saved['tables'][0]['rows']), 8)
        self.assertEqual(saved['tables'][0]['totals'][0]['values']['Micro'], '4136')
        with patch('server.fetch', side_effect=fake) as fetch:
            with urllib.request.urlopen(endpoint + '?refresh=true') as response:
                fresh = json.load(response)
        self.assertGreater(fetch.call_count, 0)
        self.assertEqual(fresh['id'], data['id'])
        self.assertNotEqual(fresh['retrieved_at'], data['retrieved_at'])

    def test_json_api_missing_source(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(self.base + '/api/json/0000000000000000')
        self.assertEqual(error.exception.code, 404)

    def test_validation_api_and_persisted_table_rules(self):
        def fake(url):
            return ('User-agent: *\nAllow: /' if url.endswith('robots.txt') else PMEGP_HTML), url
        with patch('server.fetch', side_effect=fake):
            with urllib.request.urlopen(self.base + '/api/json?url=https%3A%2F%2Fexample.gov%2Fvalidation%3FFY%3D2024-25') as response:
                dataset = json.load(response)
        report_url = self.base + f"/api/validation/{dataset['id']}"
        with urllib.request.urlopen(report_url) as response:
            original = json.load(response)
        self.assertFalse(original['guaranteed_accurate'])
        self.assertEqual(original['automation_status'], 'passed')
        request = urllib.request.Request(self.base + '/api/validation', data=json.dumps({'id':dataset['id'],
            'table_id':dataset['tables'][0]['id'], 'rules':{'expected_record_count':37},'save_rules':True}).encode(), headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(request) as response:
            failed = json.load(response)
        self.assertEqual(failed['automation_status'], 'failed')
        server.CACHE.clear()
        with urllib.request.urlopen(report_url) as response:
            persisted = json.load(response)
        self.assertEqual(persisted['tables'][0]['rules']['expected_record_count'], 37)
        with urllib.request.urlopen(self.base + f"/api/json/{dataset['id']}") as response:
            exported = json.load(response)
        self.assertNotIn('validation_report', exported)
        self.assertNotIn('validation', exported['tables'][0])
        self.assertNotIn('extraction_audit', exported['tables'][0])
        self.assertEqual(len(exported['tables'][0]['rows']), 35)

    def test_final_data_endpoints_exclude_all_diagnostics(self):
        def fake(url):
            return ('User-agent: *\nAllow: /' if url.endswith('robots.txt') else PMEGP_HTML), url
        with patch('server.fetch', side_effect=fake):
            source = server.crawl_source('https://example.gov/clean-json?FY=2024-25')
        expected = server.public_dataset(source)
        self.assertEqual(set(expected), {'schema_version','id','source_url','title','retrieved_at','published_dates','tables'})
        self.assertEqual(expected['tables'][0]['rows'], source['tables'][0]['rows'])
        self.assertEqual(expected['tables'][0]['totals'], source['tables'][0]['totals'])
        self.assertEqual(expected['tables'][0]['context']['financial_year'], '2024-25')
        for path in [f"/api/json/{source['id']}", f"/api/datasets/{source['id']}.json"]:
            with urllib.request.urlopen(self.base + path) as response:
                data = json.load(response)
            self.assertEqual(data, expected)
            self.assertFalse(set(data) & {'validation_report','warnings','status','endpoints','pages_crawled'})
            self.assertFalse(set(data['tables'][0]) & {'validation','extraction_audit','validation_report'})
        # Projection does not strip an actual source column named "validation".
        original = {'tables':[{'columns':['validation'], 'rows':[{'validation':'source value'}], 'validation':{'status':'recognized'}}]}
        self.assertEqual(server.public_dataset(original)['tables'][0]['rows'], [{'validation':'source value'}])

    def test_invalid_rules_are_rejected_without_persisting(self):
        with patch('server.fetch', side_effect=CrawlerTests.fake_fetch):
            data = server.crawl_source('https://example.gov/bad-rules')
        request = urllib.request.Request(self.base + '/api/validation', data=json.dumps({'id':data['id'],
            'table_id':data['tables'][0]['id'], 'rules':{'expected_record_count':-1},'save_rules':True}).encode(), headers={'Content-Type':'application/json'})
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(server.load_rule_config(data['id']), {'rules':{},'tables':{}})


if __name__ == '__main__':
    unittest.main()
