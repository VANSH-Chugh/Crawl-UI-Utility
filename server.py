"""CivicTable local crawler and JSON handover API. Python 3.10+."""
import concurrent.futures
import datetime as dt
import hashlib
import http.client
import io
import ipaddress
import json
import re
import socket
import ssl
import threading
import time
import urllib.parse
import urllib.robotparser
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from validation import build_report, normalize_rules

ROOT = Path(__file__).parent
DATA_DIR = ROOT / 'data' / 'datasets'
AGENT = 'CivicTable/1.0'
MAX_BYTES = 4_000_000
CACHE = {}
LOCK = threading.Lock()


def load_rule_config(key):
    path = DATA_DIR.parent / 'validation-rules' / f'{key}.json'
    with LOCK:
        try:
            return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {'rules': {}, 'tables': {}}
        except (OSError, ValueError):
            return {'rules': {}, 'tables': {}}


def save_rule_config(key, config):
    with LOCK:
        path = DATA_DIR.parent / 'validation-rules' / f'{key}.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(path)


def validation_report(dataset):
    config = load_rule_config(dataset['id'])
    return build_report(dataset, config.get('rules'), table_rules=config.get('tables'))


def with_validation(dataset):
    # Re-evaluate freshness on every API response, without changing source retrieval time.
    clean = {k: v for k, v in dataset.items() if k != 'validation_report'}
    return {**clean, 'validation_report': validation_report(clean)}


def public_dataset(dataset):
    """Final data contract: allow source data/provenance, never internal checks."""
    fields = ('schema_version', 'id', 'source_url', 'title', 'retrieved_at', 'published_dates')
    table_fields = ('id', 'name', 'page_url', 'columns', 'header_rows', 'context', 'context_rows', 'rows', 'totals')
    return {**{key: dataset[key] for key in fields if key in dataset},
            'tables': [{key: table[key] for key in table_fields if key in table} for table in dataset.get('tables', [])]}


def save_dataset(dataset):
    with LOCK:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        destination = DATA_DIR / f"{dataset['id']}.json"
        temporary = destination.with_suffix('.tmp')
        temporary.write_text(json.dumps(dataset, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(destination)
        CACHE[dataset['id']] = dataset


def load_dataset(key):
    if not re.fullmatch(r'[0-9a-f]{16}', key):
        return None
    with LOCK:
        if key in CACHE:
            return CACHE[key]
        path = DATA_DIR / f'{key}.json'
        if path.is_file():
            try:
                dataset = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                return None
            CACHE[key] = dataset
            return dataset
    return None


def create_tls_context():
    """Use OS certificate-chain validation, without disabling hostname checks."""
    try:
        import truststore
    except ImportError:
        return ssl.create_default_context()
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def readable_error(exc):
    if isinstance(exc, ssl.SSLCertVerificationError):
        return ('HTTPS certificate verification failed. Install requirements.txt and restart the server '
                'to enable system certificate trust. If the problem persists, the source certificate '
                'chain needs attention; verification has not been bypassed.')
    if isinstance(exc, OSError) and getattr(exc, 'winerror', None) == 10013:
        return 'Network access is blocked by this environment. Run the server with network access to fetch public sources.'
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return 'The source timed out. Please try again or choose a more direct data page.'
    return str(exc)


def normalize_url(url):
    parts = urllib.parse.urlsplit(str(url).strip())
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('Enter a public http:// or https:// URL without credentials.')
    if parts.port and parts.port not in (80, 443):
        raise ValueError('Only standard web ports are supported.')
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or '/', parts.query, ''))


def fetch(url, redirects=4):
    """Pin connections to validated public IPs, including every redirect."""
    url = normalize_url(url)
    for _ in range(redirects + 1):
        p = urllib.parse.urlsplit(url)
        port = p.port or (443 if p.scheme == 'https' else 80)
        addresses = socket.getaddrinfo(p.hostname, port, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError('Private, loopback and reserved network addresses are blocked.')
        address = addresses[0]
        sock = socket.socket(address[0], socket.SOCK_STREAM)
        sock.settimeout(12)
        connection = None
        try:
            sock.connect(address[4])
            if p.scheme == 'https':
                sock = create_tls_context().wrap_socket(sock, server_hostname=p.hostname)
            connection = http.client.HTTPConnection(p.hostname, port, timeout=12)
            connection.sock = sock
            path = urllib.parse.urlunsplit(('', '', p.path or '/', p.query, ''))
            connection.request('GET', path, headers={'User-Agent': AGENT, 'Accept': 'text/html,text/plain', 'Cache-Control': 'no-cache'})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location')
                if not location:
                    raise ValueError('Redirect had no destination.')
                url = normalize_url(urllib.parse.urljoin(url, location))
                continue
            if response.status >= 400:
                raise ValueError(f'Source returned HTTP {response.status}.')
            content_type = response.getheader('Content-Type', '')
            if content_type and not any(t in content_type.lower() for t in ('text/', 'xhtml')):
                raise ValueError('This source is not an HTML page. Use a page containing tables.')
            body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise ValueError('Page exceeds the 4 MB extraction limit.')
            charset = re.search(r'charset=([\w-]+)', content_type)
            return body.decode(charset.group(1) if charset else 'utf-8', errors='replace'), url
        finally:
            if connection:
                connection.close()
            else:
                sock.close()
    raise ValueError('Too many redirects.')


class TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self.links, self.dates = [], [], []
        self.depth = 0
        self.rows = []
        self.row = None
        self.cell = None
        self.caption = None
        self.in_caption = False
        self.title = ''
        self.in_title = False
        self.skip = 0
        self.section = None
        self.row_section = None
        self.last_heading = ''
        self.heading_text = None
        self.table_heading = ''

    def finish_cell(self):
        if self.cell is not None:
            self.cell['text'] = ' '.join(self.cell['text'].split())
            self.row.append(self.cell)
            self.cell = None

    def finish_row(self):
        self.finish_cell()
        if self.row:
            self.rows.append({'cells': self.row, 'section': self.row_section})
        self.row = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ('script', 'style'):
            self.skip += 1
        if tag == 'title':
            self.in_title = True
        if re.fullmatch(r'h[1-6]', tag) and not self.depth:
            self.heading_text = ''
        if tag == 'a' and a.get('href'):
            self.links.append(a['href'])
        if tag == 'meta' and a.get('name', a.get('property', '')).lower() in ('last-modified', 'date', 'article:modified_time', 'article:published_time', 'dcterms.modified'):
            self.dates.append(a.get('content', ''))
        if tag == 'table':
            self.depth += 1
            if self.depth == 1:
                self.rows, self.caption = [], None
                self.row, self.cell, self.section = None, None, None
                self.table_heading = self.last_heading
        if self.depth != 1:
            return
        if tag == 'caption':
            self.caption = ''
            self.in_caption = True
        if tag in ('thead', 'tbody', 'tfoot'):
            self.finish_row()
            self.section = tag
        if tag == 'tr':
            self.finish_row()
            self.row = []
            self.row_section = self.section
        if tag in ('td', 'th'):
            # Browsers repair headers written directly inside <thead>, without <tr>.
            # Government dashboards commonly use this valid-in-browser structure.
            if self.row is None:
                self.row = []
                self.row_section = self.section
            self.finish_cell()
            def span(key):
                try:
                    return max(1, min(100, int(a.get(key, 1))))
                except ValueError:
                    return 1
            self.cell = {'text': '', 'header': tag == 'th', 'colspan': span('colspan'), 'rowspan': span('rowspan')}
        if tag == 'br' and self.cell is not None:
            self.cell['text'] += ' '

    def handle_data(self, data):
        if self.skip:
            return
        if self.in_title:
            self.title += data
        if self.heading_text is not None and not self.depth:
            self.heading_text += data
        if self.depth == 1:
            if self.cell is not None:
                self.cell['text'] += data
            elif self.in_caption:
                self.caption += data

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.skip = max(0, self.skip - 1)
        if tag == 'title':
            self.in_title = False
        if tag == 'caption':
            self.in_caption = False
        if re.fullmatch(r'h[1-6]', tag) and self.heading_text is not None:
            self.last_heading = ' '.join(self.heading_text.split())
            self.heading_text = None
        if self.depth == 1:
            if tag in ('td', 'th') and self.cell is not None:
                self.finish_cell()
            if tag == 'tr' and self.row is not None:
                self.finish_row()
            if tag in ('thead', 'tbody', 'tfoot'):
                self.finish_row()
                self.section = None
            if tag == 'table':
                self.finish_row()
                if self.rows:
                    self.tables.append(self.make_table(self.rows, self.caption or self.table_heading))
        if tag == 'table':
            self.depth = max(0, self.depth - 1)

    @staticmethod
    def make_table(rows, caption):
        grid, carried, source_rows = [], {}, []
        for r, record in enumerate(rows):
            row = record['cells']
            values = {}
            source_cells = []
            for col, (text, until) in list(carried.items()):
                if until > r:
                    values[col] = text
                else:
                    del carried[col]
            col = 0
            for cell in row:
                while any(c in values for c in range(col, col + cell['colspan'])):
                    col += 1
                source_cells.append({'text': cell['text'], 'column': col,
                                     'colspan': cell['colspan'], 'rowspan': cell['rowspan']})
                for c in range(col, col + cell['colspan']):
                    values[c] = cell['text']
                    if cell['rowspan'] > 1:
                        carried[c] = (cell['text'], r + cell['rowspan'])
                col += cell['colspan']
            grid.append(values)
            source_rows.append(source_cells)
        width = max((max(r, default=-1) + 1 for r in grid), default=0)
        # <thead> is a semantic header even if a source writes title/banner rows
        # before it. Browsers display the header first regardless of source order.
        header_indices = [r for r, record in enumerate(rows) if record['section'] == 'thead']
        if not header_indices:
            for r, record in enumerate(rows):
                if record['section'] != 'tfoot' and all(c['header'] for c in record['cells']):
                    header_indices.append(r)
                else:
                    break
        context_indices = []
        if header_indices:
            for r in range(max(header_indices) + 1):
                cells = source_rows[r]
                if len(cells) == 1 and cells[0]['colspan'] == width and cells[0]['rowspan'] == 1 and width > 1:
                    context_indices.append(r)
            header_indices = [r for r in header_indices if r not in context_indices]
        columns, used, missing_labels = [], set(), []
        for c in range(width):
            labels = list(dict.fromkeys(grid[r].get(c, '') for r in header_indices))
            if not any(labels):
                missing_labels.append(c + 1)
            label = ' / '.join(x for x in labels if x) or f'Column {c + 1}'
            original, n = label, 2
            while label in used:
                label = f'{original} ({n})'
                n += 1
            used.add(label)
            columns.append(label)
        data_rows, totals = [], []
        excluded = set(header_indices + context_indices)
        for r in range(len(rows)):
            if r in excluded:
                continue
            cells = source_rows[r]
            first = next((c['text'] for c in cells if c['text']), '')
            is_total = bool(re.fullmatch(r'(?:(?:grand|sub|overall)\s*[- ]?\s*)?total\s*[:.\-–]*\s*', first, re.I))
            if rows[r]['section'] == 'tfoot' or is_total:
                # Preserve the original merged label once; do not duplicate it across columns.
                values = {columns[cell['column']]: cell['text'] for cell in cells
                          if cell['text'] and cell['text'] != first}
                totals.append({'label': first, 'values': values, 'cells': cells})
            else:
                data_rows.append({columns[c]: grid[r].get(c, '') for c in range(width)})
        context_labels = [source_rows[r][0]['text'] for r in context_indices]
        context = {'labels': context_labels}
        year = re.search(r'\b(?:FY|financial year)\s*[-:]?\s*(\d{4}\s*[-–/]\s*(?:\d{4}|\d{2}))', ' '.join(context_labels), re.I)
        if year:
            context['financial_year'] = re.sub(r'\s+', '', year.group(1))
        issues = []
        if not header_indices:
            issues.append('Column headings were not recognized. Generic keys require review before downstream processing.')
        elif missing_labels:
            issues.append(f'Column headings are missing at positions {missing_labels}. Review generic keys before downstream processing.')
        record_indices = [r for r in range(len(rows)) if r not in excluded and rows[r]['section'] != 'tfoot'
                          and not re.fullmatch(r'(?:(?:grand|sub|overall)\s*[- ]?\s*)?total\s*[:.\-–]*\s*', next((c['text'] for c in source_rows[r] if c['text']), ''), re.I)]
        ragged = [i + 1 for i, r in enumerate(record_indices) if len(grid[r]) != width]
        if ragged:
            issues.append(f'Record rows with incomplete source column coverage need review: {ragged[:10]}')
        return {'name': (caption or '').strip() or 'Untitled table', 'columns': columns,
                'header_rows': [source_rows[r] for r in header_indices],
                'context': context, 'context_rows': [source_rows[r] for r in context_indices],
                'rows': data_rows, 'totals': totals,
                'extraction_audit': {'source_row_count': len(rows), 'header_row_count': len(header_indices),
                                     'context_row_count': len(context_indices), 'record_count': len(data_rows),
                                     'summary_row_count': len(totals), 'source_column_count': width,
                                     'ragged_record_rows': ragged},
                'validation': {'scope': 'table_structure', 'status': 'needs_review' if issues else 'recognized',
                               'issues': issues, 'record_count': len(data_rows)}}


def crawl_source(url, follow=False):
    url = normalize_url(url)
    result = {'schema_version': '1.2', 'id': hashlib.sha256(url.encode()).hexdigest()[:16], 'source_url': url,
              'retrieved_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'status': 'complete',
              'tables': [], 'pages_crawled': 0, 'warnings': [], 'published_dates': []}
    result['endpoints'] = {'json': f"/api/json/{result['id']}",
                           'refresh': f"/api/json/{result['id']}?refresh=true",
                           'download': f"/api/datasets/{result['id']}.json",
                           'validation': f"/api/validation/{result['id']}"}
    pending, seen, robots = [url], set(), {}
    try:
        while pending and len(seen) < (5 if follow else 1):
            page = pending.pop(0)
            if page in seen:
                continue
            seen.add(page)
            origin = urllib.parse.urlsplit(page)
            root = f'{origin.scheme}://{origin.netloc}'
            if root not in robots:
                policy = urllib.robotparser.RobotFileParser()
                try:
                    body, _ = fetch(root + '/robots.txt')
                    policy.parse(body.splitlines())
                except ValueError as exc:
                    if 'HTTP 404' in str(exc):
                        policy.parse([])
                    else:
                        raise ValueError(f'Unable to verify robots.txt: {readable_error(exc)}')
                robots[root] = policy
            if not robots[root].can_fetch(AGENT, page):
                result['warnings'].append(f'robots.txt disallows crawling {page}')
                continue
            if result['pages_crawled']:
                time.sleep(1)
            try:
                body, resolved = fetch(page)
                parser = TableParser()
                parser.feed(body)
                result['pages_crawled'] += 1
                result.setdefault('title', parser.title.strip() or origin.hostname)
                result['published_dates'].extend({'page_url': resolved, 'value': d} for d in parser.dates if d)
                for table in parser.tables:
                    table.update({'page_url': resolved, 'id': f"{result['id']}-{len(result['tables']) + 1}"})
                    if table['name'] == 'Untitled table':
                        table['name'] = f"Table {len(result['tables']) + 1}"
                    result['tables'].append(table)
                    result['warnings'].extend(f"{table['name']}: {issue}" for issue in table['validation']['issues'])
                if follow and page == url:
                    for link in parser.links:
                        try:
                            candidate = normalize_url(urllib.parse.urljoin(resolved, link))
                        except ValueError:
                            continue
                        if urllib.parse.urlsplit(candidate).netloc == origin.netloc and candidate not in seen and candidate not in pending:
                            pending.append(candidate)
                            if len(pending) >= 4:
                                break
            except Exception as exc:
                result['warnings'].append(f'{page}: {readable_error(exc)}')
        if not result['tables']:
            result['status'] = 'empty'
            result['warnings'].append('No HTML tables found. JavaScript-rendered tables, PDFs and sign-in pages need a source-specific adapter.')
        elif result['warnings']:
            result['status'] = 'partial'
    except Exception as exc:
        result['status'] = 'error'
        result['warnings'].append(readable_error(exc))
    result = with_validation(result)
    save_dataset(result)
    return result


def presentation(dataset):
    from pptx import Presentation
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
    from pptx.oxml.xmlchemy import OxmlElement
    deck = Presentation()
    deck.slide_width, deck.slide_height = Inches(13.333), Inches(7.5)

    def text_box(slide, text, x, y, width, height, size, color):
        frame = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(width), Inches(height)).text_frame
        frame.word_wrap = True
        frame.text = text
        for paragraph in frame.paragraphs:
            paragraph.font.name = 'Arial'
            paragraph.font.size = Pt(size)
            paragraph.font.color.rgb = RGBColor.from_string(color)

    def style_cell(cell, text, fill, white=False, align=PP_ALIGN.LEFT, bold=False, size=12):
        cell.text = str(text)
        cell.fill.solid()
        cell.fill.fore_color.rgb = RGBColor.from_string(fill)
        cell.margin_left = cell.margin_right = Inches(.075)
        cell.margin_top = cell.margin_bottom = Inches(.045)
        cell.vertical_anchor = MSO_ANCHOR.MIDDLE
        cell.text_frame.word_wrap = True
        for paragraph in cell.text_frame.paragraphs:
            paragraph.font.name = 'Arial'
            paragraph.font.size = Pt(size)
            paragraph.font.bold = bold
            paragraph.font.color.rgb = RGBColor.from_string('FFFFFF' if white else '111111')
            paragraph.alignment = align
            paragraph.space_before = paragraph.space_after = Pt(0)
        properties = cell._tc.get_or_add_tcPr()
        for edge in ('lnL', 'lnR', 'lnT', 'lnB'):
            border = OxmlElement(f'a:{edge}')
            border.set('w', '6350')
            fill_node = OxmlElement('a:solidFill')
            color = OxmlElement('a:srgbClr')
            color.set('val', 'FFFFFF' if white else '4E95F4')
            fill_node.append(color)
            border.append(fill_node)
            properties.append(border)

    for table in dataset['tables']:
        for col_start in range(0, len(table['columns']), 8):
            cols = table['columns'][col_start:col_start + 8]
            col_end = col_start + len(cols)
            header_rows = table.get('header_rows') or [[{'text': c, 'column': i, 'colspan': 1, 'rowspan': 1}
                                                        for i, c in enumerate(table['columns'])]]
            context_rows = table.get('context_rows', [])
            content_start = len(header_rows) + len(context_rows)
            weights = []
            for column in cols:
                if re.fullmatch(r'(s\.?\s*no\.?|sr\.?\s*no\.?|serial.*|#)', column, re.I):
                    weights.append(.65)
                elif any(re.search(r'[A-Za-z]', str(row.get(column, ''))) for row in table['rows'][:20]):
                    weights.append(3.4)
                else:
                    weights.append(max(1.2, min(2.5, len(column) / 8)))
            widths = [12.2 * w / sum(weights) for w in weights]
            for offset in range(0, max(1, len(table['rows'])), 12):
                rows = table['rows'][offset:offset + 12]
                final_page = offset + len(rows) >= len(table['rows'])
                totals = table.get('totals', []) if final_page else []
                s = deck.slides.add_slide(deck.slide_layouts[6])
                s.background.fill.solid()
                s.background.fill.fore_color.rgb = RGBColor.from_string('FFFFFF')
                text_box(s, table['name'], .55, .4, 12.2, .8, 25, '172C43')
                text_box(s, 'Source Data — ' + table['page_url'], .55, 1.25, 12.2, .65, 14, '0070C0')
                footer = f"Retrieved: {dataset['retrieved_at']}"
                if len(table['rows']) > 12 or len(table['columns']) > 8:
                    footer += f" | Records {offset + 1}–{offset + len(rows)} | Columns {col_start + 1}–{col_end}"
                text_box(s, footer, .55, 7.03, 12.2, .28, 8, '617480')
                s.notes_slide.notes_text_frame.text = f"Source: {table['page_url']}\nRetrieved: {dataset['retrieved_at']}\nTotals are supplied by the source, not recalculated."
                count = content_start + len(rows) + len(totals)
                shape = s.shapes.add_table(count, len(cols), Inches(.55), Inches(2.0), Inches(12.2), Inches(min(4.8, count * .43))).table
                for c, width in enumerate(widths):
                    shape.columns[c].width = Inches(width)

                def merged_cells(source_cells, target_row, kind='header'):
                    for source in source_cells:
                        left = max(col_start, source['column'])
                        right = min(col_end, source['column'] + source['colspan'])
                        if left >= right:
                            continue
                        top = shape.cell(target_row, left - col_start)
                        bottom_row = min(count - 1, target_row + source.get('rowspan', 1) - 1)
                        if right - left > 1 or bottom_row > target_row:
                            top.merge(shape.cell(bottom_row, right - col_start - 1))
                        style_cell(top, source['text'], '008B5E', True, PP_ALIGN.RIGHT if kind == 'total' else PP_ALIGN.CENTER, True, 11)

                for r, header in enumerate(header_rows):
                    for c in range(len(cols)):
                        style_cell(shape.cell(r, c), '', '008B5E', True)
                for r, header in enumerate(header_rows):
                    merged_cells(header, r)
                for r, context_row in enumerate(context_rows, start=len(header_rows)):
                    for c in range(len(cols)):
                        style_cell(shape.cell(r, c), '', '008B5E', True)
                    merged_cells(context_row, r, 'context')
                for r, row in enumerate(rows, start=content_start):
                    for c, column in enumerate(cols):
                        cell = shape.cell(r, c)
                        value = str(row.get(column, ''))
                        numeric = bool(re.fullmatch(r'[\d, .%()+\-]+', value))
                        serial = weights[c] == .65
                        style_cell(cell, value, 'FFFFFF' if (r - content_start) % 2 == 0 else 'DAE5F4',
                                   align=PP_ALIGN.CENTER if serial else PP_ALIGN.RIGHT if numeric else PP_ALIGN.LEFT)
                for r, summary in enumerate(totals, start=content_start + len(rows)):
                    for c in range(len(cols)):
                        style_cell(shape.cell(r, c), '', '008B5E', True)
                    merged_cells(summary['cells'], r, 'total')
    output = io.BytesIO()
    deck.save(output)
    return output.getvalue()


class Handler(BaseHTTPRequestHandler):
    def send(self, data, status=200, kind='application/json', filename=None):
        body = json.dumps(data, ensure_ascii=False).encode() if kind == 'application/json' else data
        self.send_response(status)
        self.send_header('Content-Type', kind + ('; charset=utf-8' if kind.startswith('text/') or kind == 'application/json' else ''))
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if filename:
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = urllib.parse.urlsplit(self.path)
        if p.path == '/api/health':
            return self.send({'status': 'ok'})
        if p.path.startswith('/api/validation/'):
            key = p.path.removeprefix('/api/validation/').removesuffix('.json')
            data = load_dataset(key)
            if data is None:
                return self.send({'error': 'Dataset not found. Extract a source first.'}, 404)
            return self.send(validation_report(data))
        if p.path in ('/api/extract', '/api/json'):
            q = urllib.parse.parse_qs(p.query)
            try:
                result = crawl_source(q.get('url', [''])[0], q.get('follow', ['false'])[0] == 'true')
                return self.send(public_dataset(result), 502 if result['status'] == 'error' else 200)
            except ValueError as exc:
                return self.send({'error': str(exc)}, 400)
        if p.path.startswith('/api/json/'):
            key = p.path.removeprefix('/api/json/').removesuffix('.json')
            data = load_dataset(key)
            if data is None:
                return self.send({'error': 'Dataset not found. Extract its source first.'}, 404)
            query = urllib.parse.parse_qs(p.query)
            if query.get('refresh', ['false'])[0].lower() == 'true':
                data = crawl_source(data['source_url'], query.get('follow', ['false'])[0] == 'true')
            return self.send(public_dataset(data), 502 if data['status'] == 'error' else 200)
        if p.path.startswith('/api/datasets/'):
            key = p.path.rsplit('/', 1)[-1].removesuffix('.json')
            data = load_dataset(key)
            return self.send(public_dataset(data), filename=f'{key}.json') if data else self.send({'error': 'Dataset not found. Run an extraction first.'}, 404)
        files = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'),
                 '/validation.js': ('validation.js', 'text/javascript'), '/styles.css': ('styles.css', 'text/css')}
        if p.path in files:
            name, kind = files[p.path]
            return self.send((ROOT / 'web' / name).read_bytes(), kind=kind)
        self.send({'error': 'Not found'}, 404)

    def do_POST(self):
        # Only this local UI may trigger stateful operations through a browser.
        origin = self.headers.get('Origin')
        if origin and origin not in ('http://127.0.0.1:8000', 'http://localhost:8000'):
            return self.send({'error': 'Origin not allowed'}, 403)
        try:
            length = int(self.headers.get('Content-Length', 0))
            if not 0 < length <= 100_000:
                return self.send({'error': 'Request must be between 1 and 100,000 bytes'}, 413)
            body = json.loads(self.rfile.read(length))
            if self.path == '/api/validation':
                key = str(body.get('id', ''))
                dataset = load_dataset(key)
                if dataset is None:
                    return self.send({'error': 'Dataset not found. Extract a live source first.'}, 404)
                table_id = body.get('table_id')
                if not any(t.get('id') == table_id for t in dataset['tables']):
                    raise ValueError('Choose an extracted table for validation rules.')
                rules = normalize_rules(body.get('rules'))
                config = load_rule_config(key)
                config.setdefault('tables', {})[table_id] = rules
                config.setdefault('rules', {})['max_age_hours'] = rules['max_age_hours']
                if body.get('save_rules', False):
                    save_rule_config(key, config)
                return self.send(build_report(dataset, config['rules'], table_rules=config['tables']))
            if self.path in ('/api/crawl', '/api/workspace/crawl'):
                urls = body.get('urls', [])
                if not isinstance(urls, list) or not 1 <= len(urls) <= 10:
                    raise ValueError('Supply between 1 and 10 source URLs.')
                urls = list(dict.fromkeys(normalize_url(u) for u in urls))
                with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                    data = list(pool.map(lambda u: crawl_source(u, bool(body.get('follow_links'))), urls))
                return self.send({'datasets': data if self.path == '/api/workspace/crawl' else [public_dataset(d) for d in data]})
            if self.path == '/api/ppt':
                dataset = load_dataset(str(body.get('id', '')))
                if not dataset or not dataset['tables']:
                    raise ValueError('Extract a source with tables before generating a presentation.')
                return self.send(presentation(dataset), kind='application/vnd.openxmlformats-officedocument.presentationml.presentation', filename=f"civictable-{dataset['id']}.pptx")
            self.send({'error': 'Not found'}, 404)
        except (ValueError, TypeError, AttributeError) as exc:
            self.send({'error': str(exc)}, 400)
        except ImportError:
            self.send({'error': 'PPT export requires python-pptx. Install requirements.txt.'}, 503)
        except Exception:
            self.send({'error': 'The request could not be completed.'}, 500)


if __name__ == '__main__':
    print('CivicTable running at http://127.0.0.1:8000', flush=True)
    ThreadingHTTPServer(('127.0.0.1', 8000), Handler).serve_forever()
