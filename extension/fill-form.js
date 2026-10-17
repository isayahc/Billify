// Self-contained because chrome.scripting.executeScript copies this function into the tab.
async function fillForm(fields) {
  const synonyms = {
    vendor: ["vendor", "vendor name", "supplier", "supplier name", "merchant"],
    bill_number: ["bill number", "bill no", "bill #", "invoice number", "invoice no", "invoice #"],
    bill_date: ["bill date", "invoice date", "issue date", "date issued"],
    due_date: ["due date", "payment due", "pay by"],
    terms: ["terms", "payment terms"],
    amount: ["amount", "total", "total amount", "invoice total", "amount due", "balance due"],
    currency: ["currency", "currency code"],
    po: ["po", "p o", "po number", "po no", "po #", "purchase order", "purchase order number"],
    expense_category: ["expense category", "category", "expense type"],
    cost_center: ["cost center", "cost centre", "cost center code", "cost centre code"],
    business_purpose: ["business purpose", "purpose", "expense purpose", "business reason"]
  };
  const normalize = value => String(value || "")
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2").toLowerCase()
    .replace(/#/g, " number ").replace(/[^a-z0-9]+/g, " ").trim();
  const keys = Object.keys(synonyms).filter(key => fields[key] != null && String(fields[key]).trim() !== "");
  const report = { filled: [], notFound: [], noValue: [], failed: [], matchedCount: 0 };
  const selector = "input, select, textarea";

  function eligible(el) {
    return !el.matches(":disabled") && !el.readOnly && el.getClientRects().length &&
      getComputedStyle(el).visibility !== "hidden" && !el.closest('[role="search"]') &&
      el.getAttribute("role") !== "searchbox" &&
      !["hidden", "button", "submit", "reset", "checkbox", "radio", "file", "image", "password", "search"].includes(el.type);
  }

  function hintsFor(el) {
    const hints = [...(el.labels || [])].map(label => label.textContent);
    hints.push(el.getAttribute("placeholder") || "", el.getAttribute("aria-label") || "");
    for (const id of (el.getAttribute("aria-labelledby") || "").split(/\s+/)) {
      if (id) hints.push(document.getElementById(id)?.textContent || "");
    }
    // React forms often render a label beside an input without linking their IDs.
    // Only inspect a small container containing this one control, never the whole form.
    let container = el.parentElement;
    for (let depth = 0; container && depth < 3; depth++, container = container.parentElement) {
      if (["FORM", "BODY", "HTML"].includes(container.tagName) || container.querySelectorAll(selector).length !== 1) break;
      for (const label of container.querySelectorAll("label")) hints.push(label.textContent);
    }
    return [...new Set(hints.map(normalize))].filter(hint => hint && !/\bsearch\b/.test(hint));
  }

  function findMatches() {
    const controls = [...document.querySelectorAll(selector)].filter(eligible);
    const candidates = [];
    for (const el of controls) {
      const identifiers = [el.id, el.name].filter(Boolean).map(normalize);
      const hints = hintsFor(el);
      for (const key of keys) {
        let score = el.id === key ? 10000 : el.name === key ? 9900 : 0;
        for (const alias of synonyms[key].map(normalize)) {
          for (const hint of identifiers) {
            if (hint === alias) score = Math.max(score, 8000 + alias.length);
            else if (` ${hint} `.includes(` ${alias} `)) score = Math.max(score, 2000 + alias.length);
          }
          for (const hint of hints) {
            if (hint === alias) score = Math.max(score, 6000 + alias.length);
            else if (` ${hint} `.includes(` ${alias} `)) score = Math.max(score, 1000 + alias.length);
          }
        }
        if (score) candidates.push({ key, el, score });
      }
    }
    // Resolve strong, specific matches first so a broad alias cannot steal another field.
    candidates.sort((a, b) => b.score - a.score);
    const matches = {};
    const used = new Set();
    for (const { key, el } of candidates) {
      if (!matches[key] && !used.has(el)) { matches[key] = el; used.add(el); }
    }
    return matches;
  }

  function dateValue(value) {
    const iso = value.match(/^(\d{4}-\d{2}-\d{2})(?:T.*)?$/);
    if (iso) {
      const date = new Date(iso[1] + "T00:00:00Z");
      if (!Number.isNaN(date.getTime()) && date.toISOString().slice(0, 10) === iso[1]) return iso[1];
    } else {
      const date = new Date(value);
      if (!Number.isNaN(date.getTime())) {
        return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
      }
    }
    throw new Error("invalid date");
  }

  let matches = findMatches();
  report.matchedCount = Object.keys(matches).length;
  const written = [];
  for (const key of keys) {
    if (matches[key] && !matches[key].isConnected) matches = findMatches();
    const el = matches[key];
    if (!el) { report.notFound.push(key); continue; }
    try {
      let value = String(fields[key]).trim();
      if (key === "amount") {
        const cleaned = value.replace(/\b[A-Z]{3}\b/gi, "").replace(/[$€£¥,\s]/g, "");
        if (!/^-?\d+(?:\.\d+)?$/.test(cleaned) || !Number.isFinite(Number(cleaned))) throw new Error("invalid amount");
        value = typeof fields[key] === "number" ? String(fields[key]) : cleaned;
      }
      if (el.type === "date") value = dateValue(value);
      if (el instanceof HTMLSelectElement) {
        const target = normalize(value);
        const option = [...el.options].find(option => !option.disabled && !option.parentElement.disabled && (
          normalize(option.value) === target || normalize(option.textContent) === target ||
          (key === "terms" && normalize(option.value) === target.replace(/^net /, "")) ||
          (["currency", "cost_center"].includes(key) && normalize(option.textContent).startsWith(target + " "))
        ));
        if (!option) throw new Error("no matching dropdown option");
        value = option.value;
      }
      const prototype = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype :
        el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(prototype, "value").set.call(el, value);
      el.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
      el.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
      written.push({ key, el, value });
    } catch (error) {
      report.failed.push(`${key} (${error.message})`);
    }
  }

  // Give controlled React inputs time to update, then report values that actually stuck.
  if (written.length) await new Promise(resolve => setTimeout(resolve, 100));
  const currentMatches = findMatches();
  for (const { key, el: original, value } of written) {
    const el = original.isConnected ? original : currentMatches[key];
    if (!el || el.value !== value) {
      report.failed.push(`${key} (page rejected value)`);
      continue;
    }
    el.animate?.([{ backgroundColor: "#fff19c" }, { backgroundColor: getComputedStyle(el).backgroundColor }], { duration: 1400 });
    report.filled.push(key);
  }
  return report;
}
