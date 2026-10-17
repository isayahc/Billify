/* Billify expense workflow: add invoices, review what Billify read, transfer them to the simulated Acme Expense portal.
   Reading, the AP checks and every submission run on the Billify server; this page shows and edits their results. */
(function () {
  'use strict';
  var FIELDS = ['vendor', 'invoice_number', 'invoice_date', 'amount', 'currency', 'category', 'cost_center', 'description'];
  var LABELS = {vendor: 'Vendor', invoice_number: 'Invoice number', invoice_date: 'Invoice date', amount: 'Amount',
    currency: 'Currency', category: 'Category', cost_center: 'Cost center', description: 'Description'};
  var RUNS = {read_invoice: 'Read the invoice', validate_invoice: 'AP checks', transfer_invoice: 'Transfer to Acme Expense'};
  var STATUS = {ok: ['ok', 'Done'], warn: ['warn', 'Needs a look'], error: ['bad', 'Failed']};
  var st = {items: [], uid: 0, busy: false, view: 'home', version: 'v2', defCC: 'CC-210', folder: [], inbox: [],
    categories: [], reportId: '', report: null, modalKind: null, onlyFlag: false, fast: false, stale: false};
  var $ = function (id) { return document.getElementById(id); };
  var sleep = function (ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c];
    });
  }
  function money(value) {
    return Number(value).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
  }
  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : (many || one + 's')); }

  /* ---- server ---- */
  function api(path, body) {
    var options = body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)};
    return fetch(path, options).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) throw new Error(data.error || 'The server answered ' + response.status + '.');
        return data;
      });
    });
  }
  function showError(message) { $('err').textContent = message; $('err').hidden = false; }
  function hideError() { $('err').hidden = true; }

  /* ---- items ---- */
  function kindOf(name) { var m = /\.([a-z0-9]+)$/i.exec(name); return m ? m[1].toUpperCase() : 'TXT'; }
  function addItem(o) {
    st.uid++;
    st.items.push({uid: st.uid, name: o.name, meta: o.meta || '', source: o.source, text: o.text == null ? null : o.text,
      pdf: o.pdf || null, status: 'queued', send: true, read: null, fields: null, issues: [], edited: {}, transfers: [],
      rejectedBefore: false, pseq: 0});
  }
  function byUid(uid) { return st.items.filter(function (i) { return i.uid === uid; })[0]; }
  function sendable() { return st.items.filter(function (i) { return i.fields && i.send && i.status !== 'created'; }); }
  function held(it) { return !!(it.read && it.read.hold); }
  function worthALook(it) {
    if (it.status === 'created') return false;
    return !it.fields || held(it) || it.issues.length > 0 || (it.read.warnings || []).length > 0;
  }

  function pdfText(file) {
    return new Promise(function (resolve, reject) {
      if (typeof pdfjsLib === 'undefined') { reject(new Error('PDF reader did not load')); return; }
      var timer = setTimeout(function () { reject(new Error('PDF reader timed out')); }, 12000);
      pdfjsLib.GlobalWorkerOptions.workerSrc = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';
      file.arrayBuffer().then(function (buffer) {
        return pdfjsLib.getDocument({data: new Uint8Array(buffer)}).promise;
      }).then(async function (doc) {
        var out = [];
        for (var p = 1; p <= Math.min(doc.numPages, 3); p++) {
          var content = await (await doc.getPage(p)).getTextContent(), rows = {};
          content.items.forEach(function (it) {
            if (!it.str.trim()) return;
            var y = Math.round(it.transform[5] / 3);
            (rows[y] = rows[y] || []).push({x: it.transform[4], s: it.str});
          });
          Object.keys(rows).map(Number).sort(function (a, b) { return b - a; }).forEach(function (y) {
            out.push(rows[y].sort(function (a, b) { return a.x - b.x; }).map(function (i) { return i.s; }).join(' ').replace(/\s+/g, ' ').trim());
          });
        }
        clearTimeout(timer);
        resolve(out.join('\n'));
      }).catch(function (e) { clearTimeout(timer); reject(e); });
    });
  }
  function base64Of(file) {
    return new Promise(function (resolve, reject) {
      var reader = new FileReader();
      reader.onload = function () { resolve(String(reader.result).split(',')[1] || ''); };
      reader.onerror = function () { reject(new Error('Could not open the file')); };
      reader.readAsDataURL(file);
    });
  }
  function addFiles(files) {
    var readable = /\.(txt|eml|csv|html?|md)$/i;
    var jobs = Array.prototype.slice.call(files).map(function (f) {
      var meta = f.size > 1024 ? Math.round(f.size / 1024) + ' KB' : f.size + ' B';
      if (readable.test(f.name)) {
        return f.text().then(function (text) { addItem({name: f.name, meta: meta, source: 'Upload', text: text}); },
          function () { addItem({name: f.name, meta: meta, source: 'Upload'}); });
      }
      if (/\.pdf$/i.test(f.name)) {
        // Text PDFs are read here; a PDF without a text layer goes to the server as a file.
        return pdfText(f).then(function (text) {
          if (text.replace(/\s/g, '').length >= 30) return addItem({name: f.name, meta: meta, source: 'Upload', text: text});
          return base64Of(f).then(function (pdf) { addItem({name: f.name, meta: meta + ' · no text layer', source: 'Upload', pdf: pdf}); });
        }).catch(function () {
          return base64Of(f).then(function (pdf) { addItem({name: f.name, meta: meta, source: 'Upload', pdf: pdf}); },
            function () { addItem({name: f.name, meta: meta, source: 'Upload'}); });
        });
      }
      addItem({name: f.name, meta: meta, source: 'Upload'});
      return Promise.resolve();
    });
    Promise.all(jobs).then(function () { go('staged'); });
  }

  /* ---- navigation ---- */
  function steps(n) {
    var names = ['Add invoices', 'Review', 'Transfer'];
    $('steps').innerHTML = names.map(function (x, i) {
      return '<span class="' + (i + 1 === n ? 'on' : i + 1 < n ? 'done' : '') + '">' + (i + 1) + ' ' + x + '</span>';
    }).join('<i>&rarr;</i>');
  }
  function go(view) {
    st.view = view;
    $('v-home').hidden = view !== 'home';
    $('v-work').hidden = view === 'home';
    ['staged', 'review', 'transfer'].forEach(function (k) { $('v-' + k).hidden = k !== view; });
    $('h-home').hidden = view === 'home';
    $('h-sp').hidden = view !== 'home';
    if (view === 'staged') { steps(1); renderStaged(); }
    if (view === 'review') { steps(2); renderReview(); }
    if (view === 'transfer') steps(3);
    window.scrollTo(0, 0);
  }
  function setVersion(version) {
    st.version = version;
    $('s-raw').checked = version === 'v1';
    $('h-ver').textContent = version === 'v1' ? 'Agent v1 · as written' : 'Agent v2';
    $('h-ver').className = 'chip ' + (version === 'v1' ? 'warn' : 'idle');
  }

  function renderStaged() {
    var n = st.items.length;
    $('st-h').textContent = n + (n === 1 ? ' invoice ready' : ' invoices ready');
    if (!n) { $('st-list').innerHTML = '<div class="empty">Nothing added yet.</div>'; return; }
    $('st-list').innerHTML = st.items.map(function (it) {
      return '<div class="item"><div class="ficon">' + esc(kindOf(it.name)) + '</div><div class="nm"><b>' + esc(it.name) +
        '</b><span>' + esc(it.meta) + '</span></div><span class="chip idle">' + esc(it.source) + '</span><button class="x" data-rm="' +
        it.uid + '" aria-label="Remove ' + esc(it.name) + '">&times;</button></div>';
    }).join('');
    document.querySelectorAll('[data-rm]').forEach(function (b) {
      b.addEventListener('click', function () {
        var uid = +b.getAttribute('data-rm');
        st.items = st.items.filter(function (i) { return i.uid !== uid; });
        if (!st.items.length) go('home'); else renderStaged();
      });
    });
  }

  /* ---- read ---- */
  function fileOf(it) {
    var file = {name: it.name, source: it.source};
    if (it.text != null) file.text = it.text;
    if (it.pdf) file.pdf = it.pdf;
    return file;
  }
  function applyRead(it, read) {
    it.read = read;
    it.status = read.status;
    it.edited = {};
    it.fields = read.status === 'read' ? Object.assign({}, read.fields) : null;
    it.issues = read.status === 'read' ? read.precheck : [];
    it.send = read.status === 'read' && !read.hold;
  }
  async function readItems(list) {
    list = list.filter(function (it) { return it.status !== 'created'; });
    if (!list.length) return true;
    hideError();
    try {
      var res = await api('/api/read', {version: st.version, default_cost_center: st.defCC, files: list.map(fileOf)});
      res.items.forEach(function (read, i) { applyRead(list[i], read); });
      return true;
    } catch (e) {
      showError('Billify could not read the invoices: ' + e.message);
      return false;
    }
  }
  async function readAll() {
    if (st.busy) return;
    st.busy = true;
    $('b-read').innerHTML = '<span class="spin"></span>Reading ' + plural(st.items.length, 'invoice') + '...';
    $('b-read').disabled = true;
    st.reportId = '';
    st.report = null;
    var ok = await readItems(st.items);
    if (ok) st.stale = false;
    st.busy = false;
    $('b-read').textContent = 'Yes, read with Billify';
    $('b-read').disabled = false;
    if (ok) go('review');
  }
  var rereadTimer = null;
  function rereadInReview() {
    // Settings apply at the next read; on the review page that read happens now.
    if (st.view !== 'review') { st.stale = st.items.some(function (it) { return it.read; }); return; }
    clearTimeout(rereadTimer);
    rereadTimer = setTimeout(async function () {
      if (st.view !== 'review' || st.busy) return;
      st.busy = true;
      $('rv-sub').innerHTML = '<span class="spin"></span>Reading again with the new settings...';
      if (await readItems(st.items)) st.stale = false;
      st.busy = false;
      renderReview();
    }, 400);
  }

  /* ---- review ---- */
  function fieldNote(it, f) {
    var written = (it.read.written || {})[f] || '', value = it.fields[f] || '';
    if (it.edited[f]) return 'edited · invoice: ' + (written || 'none');
    if (!written) return value ? 'not on the invoice · filled in' : 'not on the invoice';
    return written === value ? 'as written' : 'invoice: ' + written;
  }
  function fieldClass(it, f) {
    var bad = it.issues.some(function (e) { return e.field === f; });
    var cleaned = !it.edited[f] && (it.fields[f] || '') !== ((it.read.written || {})[f] || '');
    return (bad ? 'bad' : '') + (cleaned ? ' chg' : '');
  }
  function fieldHtml(it, f) {
    var value = it.fields[f] || '', id = 'r' + it.uid + f;
    var note = '<div class="cf" id="cf' + it.uid + f + '">' + esc(fieldNote(it, f)) + '</div>';
    if (f === 'category') {
      var options = '<option value="">Select</option>' + st.categories.map(function (c) {
        return '<option' + (c === value ? ' selected' : '') + '>' + esc(c) + '</option>';
      }).join('');
      if (value && st.categories.indexOf(value) < 0) options += '<option selected value="' + esc(value) + '">' + esc(value) + ' (not in list)</option>';
      return '<div><label for="' + id + '">' + LABELS[f] + '</label><select id="' + id + '" data-uid="' + it.uid + '" data-f="' + f +
        '" class="' + fieldClass(it, f) + '">' + options + '</select>' + note + '</div>';
    }
    var wide = f === 'vendor' || f === 'description' ? ' class="w2"' : '';
    return '<div' + wide + '><label for="' + id + '">' + LABELS[f] + '</label><input id="' + id + '" data-uid="' + it.uid + '" data-f="' + f +
      '" class="' + fieldClass(it, f) + '" value="' + esc(value) + '">' + note + '</div>';
  }
  function notesLine(it) {
    if (it.read.version !== 'v2' || !it.read.notes.length) return '';
    return '<p class="notes"><b>Billify cleaned up:</b> ' + it.read.notes.map(function (n) {
      return esc(n.label + ': ' + (n.written ? n.written + '  →  ' + (n.sent || '(left empty)') + (n.why ? ' (' + n.why + ')' : '') : n.why));
    }).join(' &middot; ') + '</p>';
  }
  function findingsBox(it) {
    var findings = (it.read.controls || []).concat(it.read.checks || []);
    if (!findings.length) return '<div class="okline">Passed the AP checks: approved vendor, purchase order, not billed before, amounts.</div>';
    return '<div class="issues hold"><b>Held for review. It starts unchecked; check it only to send it anyway.</b><ul>' + findings.map(function (c) {
      return '<li><code>' + esc(c.code) + '</code> ' + esc(c.message) + '</li>';
    }).join('') + '</ul></div>';
  }
  function warningsBox(it) {
    return (it.read.warnings || []).map(function (w) {
      return '<div class="issues hold"><b>Check the totals.</b> ' + esc(w) + '.</div>';
    }).join('');
  }
  function issuesBox(it) {
    if (!it.issues.length) return '<div class="okline">Ready for the portal.</div>';
    return '<div class="issues"><b>The portal would reject ' + plural(it.issues.length, 'field') + ':</b><ul>' + it.issues.map(function (e) {
      return '<li>' + esc(LABELS[e.field] || e.field) + ': ' + esc(e.message) + '</li>';
    }).join('') + '</ul></div>';
  }
  function cardHtml(it) {
    var head = '<div class="rh">';
    if (it.status === 'created') {
      var expense = it.transfers[it.transfers.length - 1].expense;
      return '<div class="card rcard"><div class="rh"><b>' + esc(it.name) + '</b><span class="chip ok grow">In the portal · ' + esc(expense.id) + '</span></div></div>';
    }
    if (!it.fields) {
      var chip = it.status === 'not_invoice' ? '<span class="chip idle grow">Not an invoice</span>' : '<span class="chip warn grow">Needs text</span>';
      var body = '<p class="sub" style="margin:0">' + esc(it.read.why) + '</p>';
      if (it.status === 'needs_text') {
        body = '<p class="sub" style="margin:0 0 8px">' + esc(it.read.why) + '</p><textarea data-paste="' + it.uid +
          '" placeholder="Vendor name&#10;INVOICE&#10;Invoice No: ...&#10;Date: ...&#10;Total Due: ..." aria-label="Invoice text for ' + esc(it.name) +
          '"></textarea><div class="row" style="margin-top:8px"><button class="btn pri" data-readtxt="' + it.uid + '">Read this text</button></div>';
      }
      return '<div class="card rcard">' + head + '<b>' + esc(it.name) + '</b>' + chip + '</div><div class="rb">' + body + '</div></div>';
    }
    head += '<label><input type="checkbox" data-send="' + it.uid + '"' + (it.send ? ' checked' : '') + '> ' + esc(it.name) + '</label>' +
      '<span class="chip idle">' + esc(it.source) + '</span>' + (held(it) ? '<span class="chip warn">Held for review</span>' : '') +
      (it.rejectedBefore ? '<span class="chip bad">Rejected earlier</span>' : '') +
      '<button class="btn sm grow" data-steps="' + it.uid + '">Agent steps</button></div>';
    return '<div class="card rcard" id="card' + it.uid + '">' + head + '<div class="rb"><div class="fg">' +
      FIELDS.map(function (f) { return fieldHtml(it, f); }).join('') + '</div>' + notesLine(it) + findingsBox(it) + warningsBox(it) +
      '<div id="iss' + it.uid + '">' + issuesBox(it) + '</div></div></div>';
  }
  function renderReview() {
    var read = st.items.filter(function (i) { return i.fields || i.status === 'created'; }).length;
    var nHeld = st.items.filter(function (i) { return i.status !== 'created' && held(i); }).length;
    var nLook = st.items.filter(worthALook).length;
    var v1 = st.items.some(function (i) { return i.read && i.read.version === 'v1'; });
    var sub = 'Edit anything that looks wrong. ' + (v1 ? 'Agent v1 fills the portal with every value exactly as written on the invoice'
      : 'Highlighted fields were cleaned up to match the portal') + '; invoices that fail an AP check are held and unchecked.';
    if (st.items.length > 3) {
      sub += '<br><b>' + read + ' read</b> &middot; ' + nHeld + ' held for review &middot; ' + nLook + ' worth a look &nbsp;<label style="white-space:nowrap"><input type="checkbox" id="only"' +
        (st.onlyFlag ? ' checked' : '') + '> Show only those worth a look</label>';
    }
    $('rv-sub').innerHTML = sub;
    if ($('only')) $('only').addEventListener('change', function () { st.onlyFlag = this.checked; renderReview(); });
    var shown = st.items.filter(function (it) { return !st.onlyFlag || worthALook(it); });
    $('rv-list').innerHTML = shown.length ? shown.map(cardHtml).join('') : '<div class="card empty">Nothing needs a look.</div>';
    wireReview();
    updateCount();
  }
  function wireReview() {
    document.querySelectorAll('[data-f]').forEach(function (el) {
      el.addEventListener(el.tagName === 'SELECT' ? 'change' : 'input', function () {
        var it = byUid(+el.getAttribute('data-uid')), f = el.getAttribute('data-f');
        it.fields[f] = el.value;
        it.edited[f] = true;
        el.classList.remove('chg');
        $('cf' + it.uid + f).textContent = fieldNote(it, f);
        precheck(it);
      });
    });
    document.querySelectorAll('[data-send]').forEach(function (el) {
      el.addEventListener('change', function () { byUid(+el.getAttribute('data-send')).send = el.checked; updateCount(); });
    });
    document.querySelectorAll('[data-readtxt]').forEach(function (b) {
      b.addEventListener('click', async function () {
        var it = byUid(+b.getAttribute('data-readtxt')), text = document.querySelector('[data-paste="' + it.uid + '"]').value;
        if (!text.trim() || st.busy) return;
        st.busy = true;
        b.innerHTML = '<span class="spin"></span>Reading...';
        it.text = text;
        it.pdf = null;
        await readItems([it]);
        st.busy = false;
        renderReview();
      });
    });
    document.querySelectorAll('[data-steps]').forEach(function (b) {
      b.addEventListener('click', function () { openSteps(byUid(+b.getAttribute('data-steps'))); });
    });
  }
  function precheck(it) {
    clearTimeout(it.ptimer);
    it.ptimer = setTimeout(function () {
      var seq = ++it.pseq;
      api('/api/precheck', {fields: it.fields}).then(function (res) {
        if (seq !== it.pseq) return;
        it.issues = res.issues;
        var box = $('iss' + it.uid);
        if (box) box.innerHTML = issuesBox(it);
        FIELDS.forEach(function (f) {
          var el = $('r' + it.uid + f);
          if (el) el.classList.toggle('bad', it.issues.some(function (e) { return e.field === f; }));
        });
        updateCount();
      }).catch(function (e) { showError('Could not check the fields: ' + e.message); });
    }, 250);
  }
  function updateCount() {
    var list = sendable(), bad = list.filter(function (i) { return i.issues.length; }).length;
    var nHeld = st.items.filter(function (i) { return i.status !== 'created' && held(i) && !i.send; }).length;
    $('rv-count').textContent = list.length + ' to send' + (bad ? ' · ' + bad + ' will be rejected as they are' : '') +
      (nHeld ? ' · ' + nHeld + ' held' : '');
    $('b-transfer').disabled = !list.length;
    $('b-transfer').textContent = 'Transfer ' + plural(list.length, 'invoice') + ' to Acme Expense';
  }

  /* ---- agent steps drawer ---- */
  function stepsList(steps) {
    return '<ol>' + steps.map(function (s) {
      var status = STATUS[s.status] || STATUS.ok;
      return '<li><div><code>' + esc(s.name) + '</code><span class="o">' + esc(s.output) + '</span></div><span class="chip ' + status[0] + '">' + status[1] + '</span></li>';
    }).join('') + '</ol>';
  }
  function openSteps(it) {
    var groups = [];
    (it.read ? it.read.steps : []).forEach(function (s) {
      var last = groups[groups.length - 1];
      if (!last || last.run !== s.run) groups.push(last = {run: s.run, title: RUNS[s.run] || s.run, steps: []});
      last.steps.push(s);
    });
    it.transfers.forEach(function (r, i) {
      groups.push({run: 'transfer_invoice', title: RUNS.transfer_invoice + (it.transfers.length > 1 ? ' (attempt ' + (i + 1) + ')' : ''), steps: r.steps});
    });
    $('d-title').textContent = 'Agent steps · ' + it.name;
    $('d-body').innerHTML = (it.read ? '<p class="sid">Session ' + esc(it.read.id) + ' · agent ' + esc(it.read.version) + '</p>' : '') +
      (groups.length ? groups.map(function (g) {
        return '<h3>' + esc(g.title) + ' <code>' + esc(g.run) + '</code></h3>' + stepsList(g.steps);
      }).join('') : '<p class="sub">Billify has not read this file yet.</p>');
    $('drawer').hidden = false;
    $('scrim').hidden = false;
    $('d-x').focus();
  }
  function closeOverlays() { $('drawer').hidden = true; $('modal').hidden = true; $('scrim').hidden = true; }

  /* ---- transfer ---- */
  function buildCategories(extra) {
    var select = $('p_category');
    select.innerHTML = '';
    var first = document.createElement('option');
    first.value = '';
    first.textContent = 'Select a category';
    select.appendChild(first);
    st.categories.concat(extra && st.categories.indexOf(extra) < 0 ? [extra] : []).forEach(function (c) {
      var option = document.createElement('option');
      option.value = c;
      option.textContent = st.categories.indexOf(c) < 0 ? c + ' (not in list)' : c;
      select.appendChild(option);
    });
  }
  function clearPortal() {
    buildCategories();
    FIELDS.forEach(function (f) { var e = $('p_' + f); e.value = ''; e.classList.remove('fill', 'err'); });
    document.querySelectorAll('.ferr').forEach(function (e) { e.textContent = ''; });
  }
  async function typeInto(f, value) {
    var e = $('p_' + f), s = String(value || '');
    e.classList.add('fill');
    if (f === 'category') buildCategories(s);
    if (st.fast || f === 'category') { e.value = s; if (!st.fast) await sleep(80); return; }
    e.value = '';
    for (var i = 0; i < s.length; i++) { e.value += s[i]; await sleep(s.length > 24 ? 5 : 10); }
  }
  function renderPortal() {
    var report = st.report;
    $('prep').textContent = report ? report.id : 'not opened yet';
    var totals = report ? report.totals : {}, currencies = Object.keys(totals);
    $('ptotal').textContent = currencies.length ? currencies.map(function (c) { return money(totals[c]) + ' ' + c; }).join(' + ') : '0.00';
    $('pcount').textContent = report ? report.expenses.length : 0;
    if (!report || !report.expenses.length) { $('ptbl').innerHTML = '<div class="empty">No expenses on this report yet.</div>'; return; }
    $('ptbl').innerHTML = '<table><thead><tr><th>Date</th><th>Vendor</th><th>Category</th><th class="num">Amount</th><th>Status</th></tr></thead><tbody>' +
      report.expenses.map(function (x) {
        var c = x.claim;
        return '<tr><td class="d">' + esc(c.invoice_date) + '</td><td>' + esc(c.vendor) + '<small>' + esc(c.invoice_number) + ' &middot; ' + esc(c.cost_center) +
          '</small></td><td>' + esc(c.category) + '</td><td class="num">' + money(c.amount) + ' ' + esc(c.currency) +
          '</td><td><span class="chip ac">Pending approval</span><small>' + esc(x.id) + '</small></td></tr>';
      }).join('') + '</tbody></table>';
  }
  function renderTL(rows) {
    $('tr-list').innerHTML = rows.map(function (row) {
      var chip = {wait: '<span class="chip idle">Waiting</span>', run: '<span class="chip bf"><span class="spin" style="width:10px;height:10px;margin-right:4px"></span>Filling</span>',
        ok: '<span class="chip ok">Created</span>', bad: '<span class="chip bad">Rejected</span>', stop: '<span class="chip idle">Not sent</span>'}[row.t];
      var stepsButton = row.it.transfers.length ? '<button class="btn sm" data-tsteps="' + row.it.uid + '">Steps</button>' : '';
      return '<div class="item"><div class="nm"><b>' + esc(row.it.name) + '</b><span>' + esc(row.msg || row.it.fields.vendor) + '</span></div>' + stepsButton + chip + '</div>';
    }).join('');
    document.querySelectorAll('[data-tsteps]').forEach(function (b) {
      b.addEventListener('click', function () { openSteps(byUid(+b.getAttribute('data-tsteps'))); });
    });
  }
  function errorText(errors) {
    return errors.map(function (e) { return (LABELS[e.field] || e.field) + ': ' + e.message; }).join(' · ');
  }
  async function transfer(list) {
    if (!list.length || st.busy) return;
    st.busy = true;
    st.fast = list.length > 6;
    go('transfer');
    hideError();
    $('tr-sum').innerHTML = '';
    $('tr-actions').hidden = true;
    var rows = list.map(function (it) { return {it: it, t: 'wait', msg: ''}; });
    renderTL(rows);
    clearPortal();
    renderPortal();
    for (var i = 0; i < list.length; i++) {
      var it = list[i];
      rows[i].t = 'run';
      rows[i].msg = 'Filling the portal form';
      renderTL(rows);
      $('pmsg').textContent = 'Billify is filling the form for ' + (it.fields.vendor || it.name) + '...';
      clearPortal();
      for (var j = 0; j < FIELDS.length; j++) await typeInto(FIELDS[j], it.fields[FIELDS[j]]);
      await sleep(st.fast ? 40 : 300);
      var res;
      try {
        res = await api('/api/transfer', {version: it.read.version, report_id: st.reportId,
          items: [{id: it.read.id, name: it.name, fields: it.fields}]});
      } catch (e) {
        showError('The transfer stopped: ' + e.message);
        for (var k = i; k < rows.length; k++) { rows[k].t = 'stop'; rows[k].msg = 'Not sent'; }
        break;
      }
      st.reportId = res.report.id;
      st.report = res.report;
      var result = res.results[0];
      it.transfers.push(result);
      FIELDS.forEach(function (f) { $('p_' + f).classList.remove('fill'); });
      if (result.status === 'created') {
        it.status = 'created';
        rows[i].t = 'ok';
        rows[i].msg = result.expense.id + ' · pending approval';
        $('pmsg').textContent = 'Saved as ' + result.expense.id + '.';
      } else {
        it.status = 'rejected';
        it.rejectedBefore = true;
        rows[i].t = 'bad';
        rows[i].msg = result.errors.map(function (e) { return e.code; }).join(', ');
        result.errors.forEach(function (e) {
          var slot = document.querySelector('.ferr[data-for="' + e.field + '"]');
          if (slot && !slot.textContent) slot.textContent = e.message;
          if ($('p_' + e.field)) $('p_' + e.field).classList.add('err');
        });
        $('pmsg').textContent = result.http === 409 ? 'Rejected: this invoice is already on the report.'
          : 'Rejected: ' + plural(result.errors.length, 'field') + ' failed portal validation.';
      }
      renderTL(rows);
      renderPortal();
      await sleep(st.fast ? 60 : (result.status === 'rejected' ? 1200 : 500));
    }
    summarize(rows);
    st.busy = false;
  }
  function summarize(rows) {
    var created = rows.filter(function (r) { return r.t === 'ok'; }).length;
    var rejected = rows.filter(function (r) { return r.t === 'bad'; });
    var sent = rows.map(function (r) { return r.it; });
    var nHeld = st.items.filter(function (i) { return sent.indexOf(i) < 0 && i.status !== 'created' && held(i); }).length;
    var heldLine = nHeld ? '<p>' + plural(nHeld, 'invoice') + ' held for review ' + (nHeld === 1 ? 'was' : 'were') + ' not sent.</p>' : '';
    var where = st.report ? ' on report ' + esc(st.report.id) : '';
    var html;
    if (!rejected.length) {
      html = '<div class="sum ok"><b>' + created + ' created.</b> ' + (created ? 'Every expense sent is' + where + ', pending approval.' : '') + heldLine + '</div>';
    } else {
      html = '<div class="sum bad"><b>' + created + ' created, ' + rejected.length + ' rejected.</b> The portal named the field and the reason for each rejection.' + heldLine +
        '<ul class="rejs">' + rejected.map(function (r) {
          return '<li><b>' + esc(r.it.name) + '</b> · ' + esc(errorText(r.it.transfers[r.it.transfers.length - 1].errors)) + '</li>';
        }).join('') + '</ul></div>';
    }
    $('tr-sum').innerHTML = html;
    $('b-fix').hidden = !rejected.length;
    $('b-replay').hidden = !sent.some(function (it) { return it.read.version === 'v1'; });
    $('tr-actions').hidden = false;
  }

  /* ---- sources modal ---- */
  function openModal(kind) {
    st.modalKind = kind;
    var src = kind === 'folder' ? st.folder : st.inbox;
    $('m-title').textContent = kind === 'folder' ? 'Online folder' : 'Email inbox';
    $('m-bn').textContent = kind === 'folder' ? 'Sample folder with made-up invoices, served by the Billify server.' :
      'Sample inbox with made-up messages. Connecting a real mailbox is not built yet.';
    $('m-path').textContent = kind === 'folder' ? 'data/sample_invoices · ' + plural(src.length, 'file') : 'Sample inbox · ' + plural(src.length, 'message');
    $('m-list').innerHTML = src.map(function (x, i) {
      var icon = kindOf(x.name === 'No attachment' ? 'MSG.eml' : x.name);
      var title = kind === 'mail' ? x.subject : x.name, meta = kind === 'mail' ? x.sender + ' · ' + x.name : x.meta;
      return '<label class="item' + (x.off ? ' off' : '') + '"><input type="checkbox" data-m="' + i + '"' + (x.off ? ' disabled' : ' checked') +
        '><div class="ficon">' + esc(icon) + '</div><div class="nm"><b>' + esc(title) + '</b><span>' + esc(meta) + '</span></div>' +
        (x.off ? '<span class="chip idle">' + esc(x.off) + '</span>' : '') + '</label>';
    }).join('');
    $('scrim').hidden = false;
    $('modal').hidden = false;
    document.querySelectorAll('[data-m]').forEach(function (c) { c.addEventListener('change', modalCount); });
    modalCount();
    $('m-add').focus();
  }
  function modalCount() {
    var n = document.querySelectorAll('[data-m]:checked').length;
    $('m-count').textContent = n + ' selected';
    $('m-add').disabled = !n;
  }

  /* ---- events ---- */
  function pickFiles() { $('file').click(); }
  ['b-up', 'b-up2', 'b-add-up'].forEach(function (id) { $(id).addEventListener('click', pickFiles); });
  $('file').addEventListener('change', function () { if (this.files.length) addFiles(this.files); this.value = ''; });
  ['b-folder', 'b-add-folder'].forEach(function (id) { $(id).addEventListener('click', function () { openModal('folder'); }); });
  ['b-mail', 'b-add-mail'].forEach(function (id) { $(id).addEventListener('click', function () { openModal('mail'); }); });
  $('m-all').addEventListener('click', function () {
    document.querySelectorAll('[data-m]:not(:disabled)').forEach(function (c) { c.checked = true; });
    modalCount();
  });
  $('m-none').addEventListener('click', function () {
    document.querySelectorAll('[data-m]').forEach(function (c) { c.checked = false; });
    modalCount();
  });
  $('m-x').addEventListener('click', closeOverlays);
  $('m-cancel').addEventListener('click', closeOverlays);
  $('d-x').addEventListener('click', closeOverlays);
  $('scrim').addEventListener('click', closeOverlays);
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { closeOverlays(); $('settings').hidden = true; $('h-set').setAttribute('aria-expanded', 'false'); }
  });
  $('m-add').addEventListener('click', function () {
    var src = st.modalKind === 'folder' ? st.folder : st.inbox, source = st.modalKind === 'folder' ? 'Folder' : 'Email';
    document.querySelectorAll('[data-m]:checked').forEach(function (c) {
      var x = src[+c.getAttribute('data-m')];
      if (st.items.some(function (i) { return i.name === x.name && i.source === source; })) return;
      addItem({name: x.name, meta: source === 'Email' ? x.subject : x.meta, source: source, text: x.text});
    });
    closeOverlays();
    go('staged');
  });
  $('b-try').addEventListener('click', function () {
    st.folder.filter(function (x) { return !x.off; }).forEach(function (x) {
      if (!st.items.some(function (i) { return i.name === x.name; })) addItem({name: x.name, meta: x.meta, source: 'Folder', text: x.text});
    });
    go('staged');
  });
  $('q').addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && this.value.trim()) {
      addItem({name: 'Pasted invoice text', meta: this.value.trim().length + ' characters', source: 'Pasted', text: this.value.replace(/\\n/g, '\n')});
      this.value = '';
      go('staged');
    }
  });
  $('q').addEventListener('paste', function (e) {
    var text = (e.clipboardData || window.clipboardData).getData('text');
    if (text && text.indexOf('\n') >= 0) {
      e.preventDefault();
      addItem({name: 'Pasted invoice text', meta: text.length + ' characters', source: 'Pasted', text: text});
      go('staged');
    }
  });
  ['dragenter', 'dragover'].forEach(function (ev) { document.addEventListener(ev, function (e) { e.preventDefault(); $('pill').classList.add('drag'); }); });
  ['dragleave', 'drop'].forEach(function (ev) { document.addEventListener(ev, function (e) { e.preventDefault(); $('pill').classList.remove('drag'); }); });
  document.addEventListener('drop', function (e) { if (e.dataTransfer && e.dataTransfer.files.length) addFiles(e.dataTransfer.files); });
  $('b-read').addEventListener('click', readAll);
  $('b-clear').addEventListener('click', function () { st.items = []; go('home'); });
  $('b-back').addEventListener('click', function () { go('staged'); });
  $('b-transfer').addEventListener('click', function () { transfer(sendable()); });
  $('b-fix').addEventListener('click', async function () {
    if (st.busy) return;
    if (st.stale) {
      st.busy = true;
      this.innerHTML = '<span class="spin"></span>Reading again with the new settings...';
      var ok = await readItems(st.items);
      st.busy = false;
      this.textContent = 'Fix rejected invoices';
      if (!ok) return;
    }
    st.stale = false;
    st.items.forEach(function (it) { if (it.fields) it.send = it.rejectedBefore && it.status !== 'created'; });
    go('review');
  });
  $('b-replay').addEventListener('click', async function () {
    if (st.busy) return;
    st.busy = true;
    this.innerHTML = '<span class="spin"></span>Reading again with v2...';
    setVersion('v2');
    st.items.forEach(function (it) { it.status = 'queued'; it.transfers = []; it.rejectedBefore = false; });
    st.reportId = '';
    st.report = null;
    var ok = await readItems(st.items);
    if (ok) st.stale = false;
    st.busy = false;
    this.textContent = 'Replay the same invoices with v2';
    if (ok) transfer(sendable());
  });
  $('b-new').addEventListener('click', function () { st.items = []; st.reportId = ''; st.report = null; go('home'); });
  $('h-home').addEventListener('click', function () { if (!st.busy) go(st.items.length ? 'staged' : 'home'); });
  $('h-set').addEventListener('click', function () {
    var s = $('settings');
    s.hidden = !s.hidden;
    this.setAttribute('aria-expanded', String(!s.hidden));
  });
  $('s-raw').addEventListener('change', function () {
    setVersion(this.checked ? 'v1' : 'v2');
    rereadInReview();
  });
  $('s-cc').addEventListener('input', function () {
    var value = this.value.trim(), valid = !value || /^CC-\d{3}$/.test(value);
    $('s-cc-help').textContent = valid ? 'Used when an invoice has no cost center. Clear it to leave the field empty. Demo value only.'
      : 'Use CC- and three digits, for example CC-210, or leave it empty.';
    if (!valid) return;
    st.defCC = value;
    rereadInReview();
  });

  setVersion('v2');
  buildCategories();
  renderPortal();
  go('home');
  api('/api/samples').then(function (data) {
    st.folder = data.folder;
    st.inbox = data.inbox;
    st.categories = data.categories;
    buildCategories();
  }).catch(function (e) { showError('Could not load the sample invoices: ' + e.message); });
})();
