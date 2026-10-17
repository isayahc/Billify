/* Render stored invoice evidence without inserting untrusted HTML. */
const state = { documents: [], selected: null, status: 'all', visible: [] };
const $ = selector => document.querySelector(selector);
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function chip(doc) { return element('span', doc.status === 'passed' ? 'Passed' : `${doc.issues.length} finding${doc.issues.length === 1 ? '' : 's'}`, `chip ${doc.status === 'passed' ? 'pass' : 'error'}`); }
function toBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(',')[1]);
    reader.onerror = () => reject(Error(`Could not read ${file.name}.`));
    reader.readAsDataURL(file);
  });
}
function source(doc) { return doc.pdf_read ? doc.filename : `${doc.filename} · line ${doc.row_number}`; }
function download(text, type, filename) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const link = element('a'); link.href = url; link.download = filename; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function showDetail(doc) {
  const target = $('#detail'); target.replaceChildren();
  if (!doc) { target.append(element('div', state.documents.length ? 'No invoices match these filters.' : 'No imported invoices yet. Import a CSV or invoice PDFs to get started.', 'card empty')); return; }
  const header = element('article', undefined, 'card');
  header.append(chip(doc), element('h2', doc.invoice_number || '(Missing invoice number)'), element('p', doc.normalized.vendor_name || '(Missing vendor)', 'muted'), element('p', `${doc.pdf_read ? `${doc.filename} · read from PDF` : `${doc.filename} · CSV line ${doc.row_number}`} · ${new Date(doc.created_at).toLocaleString()}`, 'muted'));
  target.append(header);
  const findings = element('article', undefined, `card ${doc.issues.length ? 'findings' : ''}`);
  findings.append(element('h3', doc.issues.length ? 'Needs review' : 'All configured checks passed'));
  if (!doc.issues.length) findings.append(element('p', 'A draft reimbursement form is available. No payment has been authorized.'));
  for (const issue of doc.issues) {
    const item = element('div', undefined, 'finding');
    item.append(element('strong', issue.code), element('p', issue.message), element('small', `Field: ${issue.field}`, 'muted')); findings.append(item);
  }
  target.append(findings);
  if (doc.pdf_read) {
    const read = element('article', undefined, 'card'); read.append(element('h3', 'Read from PDF'));
    for (const note of doc.pdf_read.ambiguities) read.append(element('p', note));
    const how = { name: 'approved name or alias', purchase_order: 'the purchase order', none: 'not matched' }[doc.pdf_read.vendor_matched_by];
    const written = element('dl');
    for (const [key, value] of Object.entries(doc.pdf_read.as_written)) if (value) written.append(element('dt', `${key.replaceAll('_', ' ')} as written`), element('dd', value));
    written.append(element('dt', 'Vendor ID from'), element('dd', how || '—'), element('dt', 'Read by'), element('dd', doc.pdf_read.model || '—'));
    read.append(written); target.append(read);
  }
  const split = element('div', undefined, 'split');
  const fields = element('article', undefined, 'card'); fields.append(element('h3', 'Imported invoice fields'));
  const dl = element('dl');
  for (const [key, value] of Object.entries(doc.normalized)) dl.append(element('dt', key.replaceAll('_', ' ')), element('dd', value || '—'));
  fields.append(dl); split.append(fields);
  const provenance = element('article', undefined, 'card'); provenance.append(element('h3', 'Source & decision'));
  const meta = element('dl');
  for (const [key, value] of [...(doc.pdf_read ? [['Source PDF', doc.filename]] : [['Source CSV', doc.filename], ['CSV line', doc.row_number]]), ['Import ID', doc.import_id], ['Session ID', doc.session_id], ['Decision', doc.status], ['Form state', doc.reimbursement ? doc.reimbursement.status : 'Not created']]) meta.append(element('dt', key), element('dd', value));
  provenance.append(meta);
  if (doc.reimbursement) {
    const btn = element('button', 'Download draft form', 'export');
    btn.addEventListener('click', () => download(JSON.stringify(doc.reimbursement, null, 2), 'application/json', 'billify-draft.json')); provenance.append(btn);
  }
  split.append(provenance); target.append(split);
}
function render() {
  const query = $('#search').value.trim().toLowerCase(), batch = $('#batch').value;
  const docs = filterDocuments(state.documents, {status: state.status, query, batch, sort: $('#sort').value});
  state.visible = docs;
  if (!docs.some(doc => doc.id === state.selected)) state.selected = docs[0]?.id || null;
  const list = $('#list'); list.replaceChildren();
  $('#visible-count').textContent = `${docs.length} of ${state.documents.length} invoices`;
  $('#export').disabled = docs.length === 0;
  for (const doc of docs) {
    const button = element('button', undefined, 'doc'); button.dataset.id = doc.id; button.dataset.status = doc.status;
    button.setAttribute('aria-pressed', String(doc.id === state.selected));
    const top = element('div', undefined, 'top'); top.append(element('span', doc.normalized.vendor_name || '(Missing vendor)'), element('span', `${doc.normalized.total || '—'} ${doc.normalized.currency || ''}`));
    button.append(top, element('strong', doc.invoice_number || '(Missing invoice number)'), chip(doc), element('small', source(doc)));
    button.addEventListener('click', () => { state.selected = doc.id; render(); }); list.append(button);
  }
  if (!docs.length) list.append(element('p', state.documents.length ? 'No matching invoices.' : 'No imports yet.', 'empty'));
  showDetail(docs.find(doc => doc.id === state.selected));
}
async function load() {
  const response = await fetch('/api/documents', { cache: 'no-store' });
  const data = await response.json(); if (!response.ok) throw Error(data.error || 'Could not load invoices.');
  state.documents = data.documents;
  $('#total').textContent = state.documents.length;
  $('#passed').textContent = state.documents.filter(d => d.status === 'passed').length;
  $('#errors').textContent = state.documents.filter(d => d.status === 'needs_review').length;
  const batches = new Map(state.documents.map(d => [d.import_id, d])); $('#batches').textContent = batches.size;
  const prior = $('#batch').value; $('#batch').replaceChildren(new Option('All imports', ''));
  for (const [id, doc] of batches) $('#batch').append(new Option(`${doc.filename} · ${new Date(doc.created_at).toLocaleString()} · ${id.slice(0, 8)}`, id));
  if (batches.has(prior)) $('#batch').value = prior;
  render();
}
for (const button of document.querySelectorAll('[data-status]')) button.addEventListener('click', () => {
  state.status = button.dataset.status;
  for (const tab of document.querySelectorAll('.tabs button')) tab.setAttribute('aria-pressed', String(tab === button));
  render();
});
$('#search').addEventListener('input', render); $('#batch').addEventListener('change', render); $('#sort').addEventListener('change', render);
$('#refresh').addEventListener('click', () => load().then(() => { $('#feedback').textContent = 'Invoice history refreshed.'; }).catch(error => { $('#feedback').textContent = error.message; }));
$('#upload').addEventListener('click', () => $('#files').click());
$('#files').addEventListener('change', async () => {
  const files = [...$('#files').files]; if (!files.length) return;
  $('#upload').disabled = true; let completed = 0; const failures = [];
  const pdfs = files.filter(file => /\.pdf$/i.test(file.name) || file.type === 'application/pdf');
  const csvs = files.filter(file => !pdfs.includes(file));
  for (const file of csvs) {
    $('#feedback').textContent = `Importing ${file.name} (${completed + failures.length + 1}/${csvs.length})…`;
    try {
      if (file.size > 1900000) throw Error('CSV exceeds 1.9 MB.');
      const csv = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer());
      const response = await fetch('/api/import', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ csv, filename: file.name }) });
      const data = await response.json(); if (!response.ok) throw Error(data.error || 'Import failed.');
      completed++;
    } catch (error) { failures.push(`${file.name}: ${error.message}`); }
  }
  if (pdfs.length) {
    $('#feedback').textContent = `Reading ${pdfs.length} PDF${pdfs.length === 1 ? '' : 's'} with Claude, then validating…`;
    try {
      if (pdfs.length > 20 || pdfs.some(file => file.size > 8000000)) throw Error('Choose up to 20 PDFs, each under 8 MB.');
      const encoded = await Promise.all(pdfs.map(async file => ({ name: file.name, data: await toBase64(file) })));
      const response = await fetch('/api/import-pdf', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ files: encoded }) });
      const data = await response.json(); if (!response.ok) throw Error(data.error || 'PDF import failed.');
      completed += data.imports.length;
      for (const skip of data.skipped) failures.push(`${skip.file}: ${skip.reason}`);
    } catch (error) { failures.push(`PDFs: ${error.message}`); }
  }
  try { await load(); } catch (error) { failures.push(error.message); }
  $('#feedback').textContent = `${completed} import${completed === 1 ? '' : 's'} saved.${failures.length ? ' Errors: ' + failures.join(' | ') : ''}`;
  $('#files').value = ''; $('#upload').disabled = false;
});
$('#export').addEventListener('click', () => {
  const safe = value => { let text = String(value ?? ''); if (/^[\s]*[=+@-]/.test(text)) text = "'" + text; return '"' + text.replaceAll('"', '""') + '"'; };
  const rows = [['filename', 'import_id', 'row_number', 'invoice_number', 'status', 'issue_codes', 'explanation', 'session_id'], ...state.visible.map(d => [d.filename, d.import_id, d.row_number, d.invoice_number, d.status, d.issues.map(i => i.code).join('; '), d.issues.map(i => i.message).join('; '), d.session_id])];
  download(rows.map(row => row.map(safe).join(',')).join('\r\n'), 'text/csv;charset=utf-8', 'billify-filtered-findings.csv');
});
load().catch(error => { $('#feedback').textContent = error.message; });
