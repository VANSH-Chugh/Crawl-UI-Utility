const $ = id => document.getElementById(id);
let datasets = [], active = 0, page = 0, jsonMode = false, busy = false;
const PAGE_SIZE = 12;
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const current = () => datasets[active];
const table = () => current()?.tables[Number($('table-select').value) || 0];
const safeFilename = d => `civictable-${d.id}.json`;
const sourceCells = (cells, tag = 'th') => cells.map(cell => `<${tag} colspan="${cell.colspan}" rowspan="${cell.rowspan}"${tag === 'th' ? ' scope="col"' : ''}>${escapeHTML(cell.text)}</${tag}>`).join('');
const sourceTotals = t => (t.totals || []).map(summary => {
  const row = Array(t.columns.length).fill('');
  summary.cells.forEach(cell => {row[cell.column] = cell.text;});
  return row;
});
let toastTimer;
function toast(message) { $('toast').textContent = message; $('toast').classList.remove('hidden'); clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').classList.add('hidden'), 5000); }
function download(content, name, mime = 'application/json') {
  const url = URL.createObjectURL(content instanceof Blob ? content : new Blob([content], {type:mime}));
  const a = document.createElement('a'); a.href = url; a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function dataPayload(d) {
  const fields = ['schema_version','id','source_url','title','retrieved_at','published_dates'];
  const tableFields = ['id','name','page_url','columns','header_rows','context','context_rows','rows','totals'];
  return {...Object.fromEntries(fields.filter(key => key in d).map(key => [key,d[key]])),
    tables:d.tables.map(t => Object.fromEntries(tableFields.filter(key => key in t).map(key => [key,t[key]])))};
}
function exportJSON(d) { if (!d) return toast('Extract a source or open the sample first.'); download(JSON.stringify(dataPayload(d), null, 2), safeFilename(d)); }
function showView(view) {
  ['extract','json','validation','guide'].forEach(name => $(name + '-view').classList.toggle('hidden', name !== view));
  document.querySelectorAll('.nav').forEach(button => button.classList.toggle('active', button.dataset.view === view));
  $('breadcrumb').textContent = {extract:'Data extractor',json:'JSON & API',validation:'JSON validation',guide:'How it works'}[view];
  if (view === 'json') renderAPI();
  if (view === 'validation') renderValidation();
  window.scrollTo({top:0});
}
document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => showView(button.dataset.view)));
async function crawl() {
  if (busy) return;
  const urls = $('source-urls').value.split(/\r?\n/).map(x => x.trim()).filter(Boolean);
  $('error').classList.add('hidden');
  try {
    if (!urls.length || urls.length > 10) throw Error('Add between 1 and 10 source URLs, one per line.');
    for (const url of urls) { const parsed = new URL(url); if (!['http:', 'https:'].includes(parsed.protocol)) throw Error('Source URLs must start with http:// or https://.'); }
    busy = true; $('crawl-button').disabled = true; $('load-demo').disabled = true; $('empty-demo').disabled = true;
    $('crawl-button').textContent = 'Extracting…'; $('progress').classList.remove('hidden');
    const response = await fetch('/api/workspace/crawl', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({urls, follow_links:$('follow-links').checked})});
    const data = await response.json();
    if (!response.ok) throw Error(data.error || 'Extraction failed.');
    datasets = data.datasets; active = 0; page = 0;
    renderWorkspace();
    toast(`Extraction finished. ${datasets.reduce((n,d) => n + d.tables.length,0)} tables found.`);
  } catch (error) { $('error').textContent = error.message; $('error').classList.remove('hidden'); }
  finally { busy = false; $('crawl-button').disabled = false; $('load-demo').disabled = false; $('empty-demo').disabled = false; $('crawl-button').innerHTML = 'Extract tables <span>→</span>'; $('progress').classList.add('hidden'); }
}
$('crawl-button').addEventListener('click', crawl);
function renderWorkspace() {
  const count = datasets.reduce((n,d) => n + d.tables.length,0);
  $('result-count').textContent = `${datasets.length} source${datasets.length === 1 ? '' : 's'}`;
  $('results-description').textContent = datasets.some(d => d.sample) ? 'Illustrative sample · explore the workflow, then add your own source.' : 'Review your extracted data and choose how to use it.';
  $('stat-sources').textContent = datasets.length || '—'; $('stat-tables').textContent = count;
  $('stat-rows').textContent = datasets.reduce((n,d) => n + d.tables.reduce((m,t) => m + t.rows.length,0),0).toLocaleString();
  $('stat-time').textContent = datasets.some(d => d.sample) ? 'Sample data' : datasets.length ? new Date(datasets[0].retrieved_at).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}) : 'Not yet';
  $('empty').classList.toggle('hidden', Boolean(datasets.length)); $('results').classList.toggle('hidden', !datasets.length);
  $('dataset-tabs').innerHTML = datasets.map((d,i) => `<button class="dataset-tab ${i === active ? 'active' : ''} ${d.status === 'error' ? 'error-tab' : ''}" data-source="${i}" title="${escapeHTML(d.source_url)}">${d.status === 'error' ? '⚠ ' : '↗ '}${escapeHTML(d.title || new URL(d.source_url).hostname)} <span>· ${d.tables.length}</span></button>`).join('');
  document.querySelectorAll('[data-source]').forEach(button => button.addEventListener('click', () => {active = Number(button.dataset.source); page = 0; renderWorkspace();}));
  $('table-select').innerHTML = (current()?.tables || []).map((t,i) => `<option value="${i}">${escapeHTML(t.name)}</option>`).join('') || '<option>No tables found</option>';
  $('table-search').value = ''; renderTable(); renderAPI();
  if (typeof renderValidation === 'function') renderValidation();
}
function renderTable() {
  const d = current(), t = table(); if (!d) return;
  $('warnings').classList.toggle('hidden', !d.warnings.length); $('warnings').textContent = d.warnings.join(' ');
  $('sample-badge').classList.toggle('hidden', !d.sample);
  $('source-caption').innerHTML = d.sample ? 'Sample dataset · illustrative values, not official statistics' : `Source: <a href="${escapeHTML(t?.page_url || d.source_url)}" target="_blank" rel="noopener noreferrer">${escapeHTML(t?.page_url || d.source_url)} ↗</a> · Fetched ${escapeHTML(new Date(d.retrieved_at).toLocaleString())}`;
  $('table-meta').textContent = t ? `${t.columns.length} source columns · ${t.rows.length.toLocaleString()} records${t.totals?.length ? ` · ${t.totals.length} total footer` : ''}${t.context?.financial_year ? ` · FY ${t.context.financial_year}` : ''}` : d.status;
  ['export-csv','export-ppt'].forEach(id => $(id).disabled = !t);
  $('export-ppt').disabled = !t || Boolean(d.sample);
  $('export-ppt').title = d.sample ? 'Extract a live source to generate a presentation.' : 'Export all tables in this source to PowerPoint';
  const query = $('table-search').value.toLowerCase();
  const rows = t?.rows.filter(row => Object.values(row).some(value => String(value).toLowerCase().includes(query))) || [];
  const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE)); page = Math.min(page, pages - 1);
  $('row-count').textContent = `${rows.length.toLocaleString()} ${query ? 'matching ' : ''}rows`;
  const headers = t?.header_rows?.length ? t.header_rows.map(cells => `<tr>${sourceCells(cells)}</tr>`).join('') : `<tr>${(t?.columns || []).map(c => `<th scope="col">${escapeHTML(c)}</th>`).join('')}</tr>`;
  const footers = (t?.totals || []).map(summary => `<tr>${sourceCells(summary.cells,'td')}</tr>`).join('');
  const contextRows = (t?.context_rows || []).map(cells => `<tr>${sourceCells(cells,'td')}</tr>`).join('');
  $('table-container').innerHTML = t ? `<table class="source-table"><thead>${headers}</thead>${contextRows ? `<tbody class="table-context">${contextRows}</tbody>` : ''}<tbody>${rows.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE).map(row => `<tr>${t.columns.map(c => `<td title="${escapeHTML(row[c])}" class="${/^[\d, .%()+\-]+$/.test(String(row[c])) ? 'numeric' : ''}">${escapeHTML(row[c])}</td>`).join('')}</tr>`).join('') || `<tr><td colspan="${t.columns.length}">No matching rows.</td></tr>`}</tbody>${footers ? `<tfoot title="Original source totals, covering all ${t.rows.length} records">${footers}</tfoot>` : ''}</table>${footers && (query || pages > 1) ? `<div class="total-note">Source totals cover all ${t.rows.length} records, including records outside this view.</div>` : ''}` : '<div style="padding:40px;text-align:center;color:#8899a5">No tables available for this source. Review the extraction details above.</div>';
  $('inline-json').textContent = JSON.stringify(dataPayload(d), null, 2);
  $('table-container').classList.toggle('hidden', jsonMode); $('inline-json').classList.toggle('hidden', !jsonMode);
  $('page-info').textContent = `Page ${page + 1} of ${pages} · ${rows.length.toLocaleString()} rows`;
  $('prev-page').disabled = page === 0 || jsonMode; $('next-page').disabled = page >= pages - 1 || jsonMode;
}
$('table-select').addEventListener('change', () => {page = 0; $('table-search').value = ''; renderTable();});
$('table-search').addEventListener('input', () => {page = 0; renderTable();});
$('prev-page').addEventListener('click', () => {page--; renderTable();}); $('next-page').addEventListener('click', () => {page++; renderTable();});
function setTableView(json) {jsonMode = json; $('table-tab').classList.toggle('selected', !json); $('json-tab').classList.toggle('selected', json); renderTable();}
$('table-tab').addEventListener('click', () => setTableView(false)); $('json-tab').addEventListener('click', () => setTableView(true));
$('validation-tab').addEventListener('click', () => showView('validation'));
$('export-json').addEventListener('click', () => exportJSON(current()));
$('export-csv').addEventListener('click', () => {
  const t = table(); if (!t) return;
  // Neutralize formula-leading values when CSV is opened in a spreadsheet.
  const cell = value => {let s = String(value ?? ''); if (/^[=+@\-\t\r]/.test(s)) s = "'" + s; return '"' + s.replace(/"/g, '""') + '"';};
  const csv = [t.columns, ...t.rows.map(r => t.columns.map(c => r[c])), ...sourceTotals(t)].map(row => row.map(cell).join(',')).join('\r\n');
  download('\ufeff' + csv, `civictable-${t.id}.csv`, 'text/csv;charset=utf-8');
});
$('export-ppt').addEventListener('click', async () => {
  const button = $('export-ppt'); button.disabled = true; button.textContent = 'Generating…';
  try {const response = await fetch('/api/ppt', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:current().id})}); if (!response.ok) {const data = await response.json(); throw Error(data.error);} download(await response.blob(), `civictable-${current().id}.pptx`); toast('Presentation generated with source headings and a separate total footer.');}
  catch(error) {toast(error.message);} finally {button.disabled = false; button.textContent = '▧ Generate PPT';}
});
function renderAPI() {
  const selected = $('json-source').value;
  $('json-source').innerHTML = datasets.length ? datasets.map(d => `<option value="${d.id}">${escapeHTML(d.title || d.source_url)}${d.sample ? ' (sample)' : ''}</option>`).join('') : '<option value="">Extract a source to get started</option>';
  if (datasets.some(d => d.id === selected)) $('json-source').value = selected;
  const d = datasets.find(d => d.id === $('json-source').value);
  if (d) $('json-preview').textContent = JSON.stringify(dataPayload(d),null,2);
  const url = d && !d.sample ? d.source_url : 'https://government.gov/statistics';
  const endpoint = d && !d.sample ? `${location.origin}/api/json/${d.id}` : null;
  $('api-example').textContent = endpoint ? `GET ${endpoint}?refresh=true\n\n# Fetch fresh source data on each call.\n# Without ?refresh=true: last saved JSON.\n# rows = records; totals = source footer.` : `GET ${location.origin}/api/json?url=${encodeURIComponent(url)}\n\n# Extract a source to get its own stable API URL.`;
  $('cached-endpoint').textContent = endpoint || '/api/json/{source_id}';
  $('json-api-link').textContent = endpoint ? 'Open JSON API ↗' : 'Extract a live source to get its API link';
  $('json-api-link').href = endpoint || '#';
  $('json-api-link').classList.toggle('hidden', !endpoint);
  $('copy-json-api').disabled = !endpoint;
  $('download-source').disabled = !d; $('download-all').disabled = !datasets.length;
}
$('json-source').addEventListener('change', renderAPI);
$('download-source').addEventListener('click', () => exportJSON(datasets.find(d => d.id === $('json-source').value)));
$('download-all').addEventListener('click', () => datasets.forEach((d,i) => setTimeout(() => exportJSON(d), i * 350)));
async function copyEndpoint(fresh) {const d = datasets.find(d => d.id === $('json-source').value); const url = d && !d.sample ? `${location.origin}/api/json/${d.id}${fresh ? '?refresh=true' : ''}` : `${location.origin}/api/json?url=${encodeURIComponent('https://government.gov/statistics')}`; try {await navigator.clipboard.writeText(url); toast(`${fresh ? 'Fresh extraction' : 'Source JSON'} API URL copied.`);} catch {toast('Clipboard unavailable. Select and copy the API URL shown above.');}}
$('copy-api').addEventListener('click', () => copyEndpoint(true));
$('copy-json-api').addEventListener('click', () => copyEndpoint(false));
function loadDemo() {
  datasets = [{id:'sample-population',source_url:'https://example.gov.in/demographics',title:'Population indicators · sample',retrieved_at:null,status:'complete',sample:true,pages_crawled:0,published_dates:[],warnings:[],tables:[{id:'sample-table-1',name:'State population indicators',page_url:'https://example.gov.in/demographics',columns:['State / UT','Population (millions)','Urban share (%)','Literacy (%)','Reference year'],rows:[['Maharashtra','112.4','45.2','82.3','2011'],['Uttar Pradesh','199.8','22.3','67.7','2011'],['Tamil Nadu','72.1','48.4','80.1','2011'],['Karnataka','61.1','38.7','75.4','2011'],['Gujarat','60.4','42.6','78.0','2011'],['Rajasthan','68.5','24.9','66.1','2011'],['Kerala','33.4','47.7','94.0','2011'],['West Bengal','91.3','31.9','76.3','2011'],['Madhya Pradesh','72.6','27.6','69.3','2011'],['Bihar','104.1','11.3','61.8','2011'],['Punjab','27.7','37.5','75.8','2011'],['Odisha','42.0','16.7','72.9','2011'],['Assam','31.2','14.1','72.2','2011'],['Haryana','25.4','34.9','75.6','2011']].map(values => Object.fromEntries(['State / UT','Population (millions)','Urban share (%)','Literacy (%)','Reference year'].map((c,i) => [c,values[i]])))}]}];
  active = 0; page = 0; jsonMode = false; setTableView(false); renderWorkspace(); toast('Sample loaded. These illustrative values are not a live extraction.');
}
$('load-demo').addEventListener('click', loadDemo); $('empty-demo').addEventListener('click', loadDemo);
renderAPI();
