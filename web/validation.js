const validationLabels = {passed:'Passed',failed:'Failed',warning:'Review',not_verified:'Not verified',not_applicable:'Not applicable'};
const validationIcons = {passed:'✓',failed:'×',warning:'!',not_verified:'?',not_applicable:'—'};
let validationBusy = false;
const validationSource = () => datasets.find(d => d.id === $('validation-source').value);
const validationTable = () => validationSource()?.tables.find(t => t.id === $('validation-table').value);
function validationBadge(status) {return `<span class="validation-badge ${status}">${validationIcons[status] || '?'} ${validationLabels[status] || escapeHTML(status)}</span>`;}

function renderValidation(resetRules = true) {
  const selectedSource = $('validation-source').value;
  $('validation-source').innerHTML = datasets.length ? datasets.map(d => `<option value="${escapeHTML(d.id)}">${escapeHTML(d.title || d.source_url)}${d.sample ? ' (sample)' : ''}</option>`).join('') : '<option value="">Extract a live source first</option>';
  if (datasets.some(d => d.id === selectedSource)) $('validation-source').value = selectedSource;
  else if (current()) $('validation-source').value = current().id;
  const d = validationSource();
  const selectedTable = $('validation-table').value;
  $('validation-table').innerHTML = d?.tables.length ? d.tables.map(t => `<option value="${escapeHTML(t.id)}">${escapeHTML(t.name)}</option>`).join('') : '<option value="">No table found</option>';
  if (d?.tables.some(t => t.id === selectedTable)) $('validation-table').value = selectedTable;
  const report = d?.validation_report;
  const available = Boolean(report) && !d?.sample;
  $('validation-empty').classList.toggle('hidden', available);
  $('validation-content').classList.toggle('hidden', !available);
  $('rerun-validation').disabled = !d || Boolean(d.sample) || validationBusy;
  $('download-validation').disabled = !available || validationBusy;
  if (!available) {
    $('validation-empty').querySelector('p').textContent = d?.sample ? 'Illustrative sample data cannot establish live-source checks. Extract a real source to generate its validation report.' : d ? 'This extraction has no validation report yet. Click Refresh checks to evaluate its saved JSON, or re-extract for complete HTML row accounting.' : 'Extract a live page, then see checks for JSON structure, records, context, duplicates, numeric conversion, totals, and freshness.';
    return;
  }
  if (resetRules) populateValidationRules();
  renderValidationChecks();
}

function populateValidationRules() {
  const d = validationSource(), t = validationTable();
  const applied = d?.validation_report.tables.find(item => item.table_id === t?.id)?.rules || {};
  $('rule-columns').value = (applied.expected_columns || []).join('\n');
  $('rule-numeric').value = (applied.numeric_columns || []).join('\n');
  $('rule-count').value = applied.expected_record_count ?? '';
  $('rule-year').value = applied.expected_financial_year || '';
  $('rule-keys').value = (applied.key_columns || []).join('\n');
  $('rule-age').value = d?.validation_report.rules.max_age_hours ?? 24;
  $('rule-reference').value = applied.reference_rows?.length ? JSON.stringify(applied.reference_rows,null,2) : '';
  $('validation-rule-error').classList.add('hidden');
}

function renderValidationChecks() {
  const d = validationSource(), t = validationTable(), report = d?.validation_report;
  if (!report) return;
  const count = report.summary;
  $('validation-summary').innerHTML = ['passed','failed','warning','not_verified'].map(status => `<div class="validation-metric ${status}"><strong>${count[status] || 0}</strong><span>${validationLabels[status]}</span></div>`).join('');
  const headline = report.automation_status === 'failed' ? 'Some automated checks failed — resolve them before handover.' : report.automation_status === 'warning' ? 'Automated checks found items to review.' : 'Automated checks passed. Independent review is still required.';
  $('validation-outcome').className = `validation-outcome ${report.automation_status}`;
  $('validation-outcome').textContent = headline;
  $('validation-checked-at').textContent = `Checked ${new Date(report.checked_at).toLocaleString()} · Summary covers this source and all its tables. Checklist below shows source checks and the selected table.`;
  const selectedReport = report.tables.find(item => item.table_id === t?.id);
  const groups = [{name:'Source & JSON checks',checks:report.dataset_checks}, {name:t?.name || 'Table checks',checks:selectedReport?.checks || []}];
  const filter = $('validation-filter').value;
  $('validation-checks').innerHTML = groups.map(group => {
    const checks = group.checks.filter(c => filter === 'all' || (filter === 'passed' ? c.status === 'passed' : ['failed','warning','not_verified'].includes(c.status)));
    return `<section class="validation-group"><h3>${escapeHTML(group.name)}</h3><div class="card validation-check-list">${checks.map(c => `<article class="validation-check"><div class="validation-check-top"><h4>${escapeHTML(c.title)}</h4>${validationBadge(c.status)}</div><p>${escapeHTML(c.detail)}</p>${c.evidence !== undefined ? `<details class="validation-evidence"><summary>View evidence</summary><pre>${escapeHTML(JSON.stringify(c.evidence,null,2))}</pre></details>` : ''}</article>`).join('') || '<p class="no-checks">No checks match this filter.</p>'}</div></section>`;
  }).join('');
  const url = `${location.origin}/api/validation/${d.id}`;
  $('validation-api-link').href = url;
  $('validation-api-link').textContent = url;
}

function validationWorking(value) {
  validationBusy = value;
  ['rerun-validation','save-validation-rules','reset-validation-rules','validation-source','validation-table','download-validation'].forEach(id => $(id).disabled = value);
  $('rerun-validation').textContent = value ? 'Checking…' : 'Refresh checks ↻';
}

async function requestValidation(rules) {
  const d = validationSource(), t = validationTable();
  if (!d || d.sample || validationBusy) return;
  validationWorking(true);
  $('validation-rule-error').classList.add('hidden');
  try {
    const response = rules ? await fetch('/api/validation', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:d.id,table_id:t?.id,rules,save_rules:true})}) : await fetch(`/api/validation/${d.id}`);
    const data = await response.json();
    if (!response.ok) throw Error(data.error || 'Validation could not be completed.');
    d.validation_report = data;
    renderAPI();
    renderValidation();
    toast(rules ? 'Validation rules saved and checks evaluated.' : 'Checks refreshed against saved data. To fetch new source data, re-extract the page.');
  } catch(error) {
    $('validation-rule-error').textContent = error.message;
    $('validation-rule-error').classList.remove('hidden');
    toast(error.message);
  } finally {validationWorking(false);}
}

$('validation-source').addEventListener('change', () => renderValidation());
$('validation-table').addEventListener('change', () => renderValidation());
$('validation-filter').addEventListener('change', renderValidationChecks);
$('rerun-validation').addEventListener('click', () => requestValidation());
$('download-validation').addEventListener('click', () => {const d=validationSource(); if(d?.validation_report) download(JSON.stringify(d.validation_report,null,2),`civictable-${d.id}-validation.json`);});
$('validation-rules-form').addEventListener('submit', event => {
  event.preventDefault();
  try {
    const list = id => $(id).value.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
    const reference = $('rule-reference').value.trim();
    requestValidation({expected_columns:list('rule-columns'),numeric_columns:list('rule-numeric'),
      expected_record_count:$('rule-count').value === '' ? null : Number($('rule-count').value),
      expected_financial_year:$('rule-year').value.trim(),key_columns:list('rule-keys'),
      max_age_hours:Number($('rule-age').value),reference_rows:reference ? JSON.parse(reference) : []});
  } catch(error) {$('validation-rule-error').textContent = `Check your reference JSON: ${error.message}`; $('validation-rule-error').classList.remove('hidden');}
});
$('reset-validation-rules').addEventListener('click', () => requestValidation({expected_columns:[],numeric_columns:[],expected_record_count:null,expected_financial_year:'',key_columns:[],max_age_hours:24,reference_rows:[]}));
renderValidation();
