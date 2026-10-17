// Switch to false when the real extraction server is ready.
const USE_MOCK_DATA = true;
const MOCK_INVOICES = [
  {
    vendor: "Northwind Cloud Hosting", bill_number: "NW-48213", bill_date: "2026-10-03",
    amount: "1240.50", currency: "USD", expense_category: "Software",
    cost_center: "CC-210", business_purpose: "Annual hosting renewal, 3 seats"
  },
  {
    vendor: "Contoso Productivity", bill_number: "CT-73018", bill_date: "2026-10-07",
    amount: "289.00", currency: "USD", expense_category: "Software",
    cost_center: "CC-210", business_purpose: "Monthly team productivity subscription, 10 seats"
  },
  {
    vendor: "Fabrikam Design Tools", bill_number: "FB-20984", bill_date: "2026-10-09",
    amount: "799.00", currency: "USD", expense_category: "Software",
    cost_center: "CC-210", business_purpose: "Annual design software licenses, 2 seats"
  },
  {
    vendor: "Alpine Security", bill_number: "AS-10562", bill_date: "2026-10-12",
    amount: "560.00", currency: "USD", expense_category: "Software",
    cost_center: "CC-210", business_purpose: "Quarterly endpoint security subscription, 20 devices"
  },
  {
    vendor: "Tailspin Analytics", bill_number: "TA-66127", bill_date: "2026-10-15",
    amount: "2150.00", currency: "USD", expense_category: "Software",
    cost_center: "CC-210", business_purpose: "Annual analytics platform subscription, 5 seats"
  }
];
const FIELD_LABELS = {
  vendor: "Vendor", bill_number: "Invoice number", bill_date: "Invoice date",
  amount: "Total amount", currency: "Currency", expense_category: "Expense category",
  cost_center: "Cost center", business_purpose: "Business purpose",
  due_date: "Due date", terms: "Terms", po: "PO"
};
const extractButton = document.getElementById("extract");
const fillButton = document.getElementById("fill");
const invoiceSelect = document.getElementById("invoice");
const status = document.getElementById("status");
let lastExtraction = null;
let selectedMockInvoiceId = null;
document.getElementById("mode").textContent = USE_MOCK_DATA
  ? "Choose a demo invoice, open New expense, then click Prefill form."
  : "Select invoice text, or extract the whole page.";
extractButton.hidden = USE_MOCK_DATA;
document.getElementById("invoice-picker").hidden = !USE_MOCK_DATA;
if (USE_MOCK_DATA) {
  for (const invoice of MOCK_INVOICES) {
    const option = document.createElement("option");
    option.value = invoice.bill_number;
    option.textContent = `${invoice.bill_number} · ${invoice.vendor}`;
    invoiceSelect.append(option);
  }
}

function setStatus(message, error = false) {
  status.textContent = message;
  status.dataset.error = String(error);
}

function setBusy(busy) {
  extractButton.disabled = busy;
  invoiceSelect.disabled = busy;
  fillButton.disabled = busy || !lastExtraction;
}

async function selectMockInvoice(id) {
  const invoice = MOCK_INVOICES.find(invoice => invoice.bill_number === id) || MOCK_INVOICES[0];
  const fields = { ...invoice };
  await chrome.storage.local.set({ selectedMockInvoiceId: invoice.bill_number, lastExtraction: fields });
  selectedMockInvoiceId = invoice.bill_number;
  invoiceSelect.value = selectedMockInvoiceId;
  lastExtraction = fields;
  renderFields();
}

invoiceSelect.addEventListener("change", async () => {
  setBusy(true);
  try {
    await selectMockInvoice(invoiceSelect.value);
    setStatus("Invoice selected. Click Prefill form to use these values.");
  } catch (error) {
    invoiceSelect.value = selectedMockInvoiceId || MOCK_INVOICES[0].bill_number;
    setStatus(`Could not select invoice: ${error.message}`, true);
  } finally {
    setBusy(false);
  }
});

function renderFields() {
  const body = document.getElementById("fields");
  body.replaceChildren();
  for (const [key, label] of Object.entries(FIELD_LABELS)) {
    if (lastExtraction[key] == null) continue;
    const row = document.createElement("tr");
    const heading = document.createElement("th");
    heading.scope = "row";
    heading.textContent = label;
    const cell = document.createElement("td");
    cell.textContent = lastExtraction[key] ?? "—";
    row.append(heading, cell);
    body.append(row);
  }
  document.getElementById("result").hidden = false;
}

async function runInTab(func, args = []) {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) throw new Error("No active tab found.");
  try {
    return await chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, func, args });
  } catch (error) {
    throw new Error("Cannot access the form. Reload Billify at chrome://extensions and refresh this page to enable embedded form access. For local HTML, enable “Allow access to file URLs”. " + error.message);
  }
}

// Injected functions must be self-contained: Chrome copies them into the tab.
function readPageText() {
  let selected = window.getSelection()?.toString().trim() || "";
  const active = document.activeElement;
  if (!selected && active && typeof active.selectionStart === "number" && active.selectionEnd > active.selectionStart) {
    selected = active.value.slice(active.selectionStart, active.selectionEnd).trim();
  }
  return { selected, text: document.body?.innerText.trim() || "" };
}

async function extractInvoice(text) {
  if (USE_MOCK_DATA) return { ...lastExtraction };
  let response;
  try {
    response = await fetch("http://localhost:5050/extract", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }), signal: AbortSignal.timeout(60000)
    });
  } catch (error) {
    throw new Error(error.name === "TimeoutError" ? "Extraction timed out. Try again." : "Cannot reach the extractor. Start python3 server.py on localhost:5050.");
  }
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Extraction failed (HTTP ${response.status}).`);
  return data;
}

extractButton.addEventListener("click", async () => {
  setBusy(true);
  setStatus("Reading invoice… Keep this popup open until extraction finishes.");
  try {
    const frames = await runInTab(readPageText);
    const text = frames.find(frame => frame.result?.selected)?.result.selected ||
      frames.map(frame => frame.result?.text || "").filter(Boolean).join("\n\n");
    if (!text) throw new Error("No visible text found. Select invoice text and try again.");
    setStatus("Extracting… Keep this popup open until extraction finishes.");
    const data = await extractInvoice(text);
    if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("Extractor returned invalid invoice data.");
    const extracted = {};
    for (const key of Object.keys(FIELD_LABELS)) {
      if (data[key] != null && !["string", "number"].includes(typeof data[key])) throw new Error(`Invalid ${key} in extraction.`);
      extracted[key] = data[key] ?? null;
    }
    if (!Object.values(extracted).some(value => value != null && String(value).trim())) throw new Error("No invoice fields were returned.");
    await chrome.storage.local.set({ lastExtraction: extracted });
    lastExtraction = extracted;
    renderFields();
    setStatus("Saved. Open your form tab, reopen this popup, and click Prefill form.");
  } catch (error) {
    setStatus(error.message + (lastExtraction ? "\nThe previous saved invoice is still shown." : ""), true);
  } finally {
    setBusy(false);
  }
});

fillButton.addEventListener("click", async () => {
  if (!lastExtraction) return;
  setBusy(true);
  setStatus("Filling form…");
  try {
    const frames = await runInTab(fillForm, [lastExtraction]);
    const reports = frames.map(frame => frame.result).filter(report => report?.filled);
    const unique = values => [...new Set(values)];
    const filled = unique(reports.flatMap(report => report.filled));
    const failed = unique(reports.flatMap(report => report.failed || []))
      .filter(message => !filled.some(key => message.startsWith(`${key} (`)));
    const notFound = Object.keys(lastExtraction).filter(key =>
      lastExtraction[key] != null && String(lastExtraction[key]).trim() &&
      !filled.includes(key) && !failed.some(message => message.startsWith(`${key} (`))
    );
    const labels = keys => keys.map(key => FIELD_LABELS[key] || key).join(", ") || "None";
    const lines = [`Filled: ${labels(filled)}`, `Not found: ${labels(notFound)}`];
    if (failed.length) lines.push(`Could not fill: ${failed.join(", ")}`);
    lines.push(filled.length ? "Review the values before saving." : "Open the New expense form, then try again.");
    setStatus(lines.join("\n"), failed.length > 0 || !filled.length);
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    setBusy(false);
  }
});

(async () => {
  try {
    if (USE_MOCK_DATA) {
      const saved = await chrome.storage.local.get("selectedMockInvoiceId");
      await selectMockInvoice(saved.selectedMockInvoiceId);
    } else {
      ({ lastExtraction = null } = await chrome.storage.local.get("lastExtraction"));
    }
    if (lastExtraction) renderFields();
    setStatus(lastExtraction ? "Ready. Click Prefill form to fill the open form." : "Ready to extract an invoice.");
  } catch (error) {
    setStatus(`Could not load saved invoice: ${error.message}`, true);
  } finally {
    setBusy(false);
  }
})();
