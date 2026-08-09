// Powens Finance — client interactions (vanilla JS, no build step).

// --- Mask amounts globally (persisted) -------------------------------------
// CSS blurs every `.amount`, including the ones inside the server-rendered SVG
// charts. Native SVG <title> tooltips cannot be styled, so their text is parked
// in a data attribute while masking is on — otherwise hovering a bar would leak
// the very figure the mask is meant to hide.
function setTooltips(hidden) {
  // Custom tooltip: data lives in data-tip on the parent element (circle, rect, path).
  document.querySelectorAll(".chart [data-tip], .donut [data-tip]").forEach((el) => {
    if (hidden) {
      if (el.dataset.tipReal === undefined) el.dataset.tipReal = el.dataset.tip;
      el.dataset.tip = "•••";
    } else if (el.dataset.tipReal !== undefined) {
      el.dataset.tip = el.dataset.tipReal;
    }
  });
  // Fallback: any remaining <title> nodes (shouldn't happen after initChartTips).
  document.querySelectorAll(".chart title, .donut title").forEach((node) => {
    if (hidden) {
      if (node.dataset.text === undefined) node.dataset.text = node.textContent;
      node.textContent = "•••";
    } else if (node.dataset.text !== undefined) {
      node.textContent = node.dataset.text;
    }
  });
  // A native `title` attribute cannot be blurred by CSS, so a tooltip quoting an
  // amount would hand back on hover exactly what the mask hides. Those live in
  // `data-amount-title` and only become real tooltips once amounts are revealed.
  document.querySelectorAll("[data-amount-title]").forEach((node) => {
    if (hidden) node.removeAttribute("title");
    else node.setAttribute("title", node.dataset.amountTitle);
  });
}

function initMask() {
  const btn = document.getElementById("mask-toggle");
  const root = document.documentElement;
  const sync = () => {
    const hidden = root.classList.contains("hide-amounts");
    if (btn) btn.textContent = hidden ? "🙈 Montants" : "👁 Montants";
    setTooltips(hidden);
  };
  sync();
  if (!btn) return;
  btn.addEventListener("click", () => {
    root.classList.toggle("hide-amounts");
    localStorage.setItem(
      "pf-hide",
      root.classList.contains("hide-amounts") ? "1" : "0"
    );
    sync();
  });
}

// --- Sortable tables (table.sortable) --------------------------------------
function cellValue(row, i) {
  const td = row.cells[i];
  if (!td) return "";
  if (td.dataset.sort !== undefined) {
    const n = parseFloat(td.dataset.sort);
    return isNaN(n) ? td.dataset.sort.toLowerCase() : n;
  }
  const txt = td.textContent.trim();
  const num = parseFloat(txt.replace(/\s/g, "").replace(/[^\d,.\-]/g, "").replace(",", "."));
  return txt !== "" && !isNaN(num) ? num : txt.toLowerCase();
}

function initSortable() {
  document.querySelectorAll("table.sortable").forEach((table) => {
    const head = table.tHead && table.tHead.rows[0];
    if (!head) return;
    Array.from(head.cells).forEach((th, i) => {
      if (th.classList.contains("no-sort")) return;
      th.classList.add("sortable");
      th.addEventListener("click", () => {
        const body = table.tBodies[0];
        const rows = Array.from(body.rows);
        const dir = th.dataset.dir === "asc" ? "desc" : "asc";
        Array.from(head.cells).forEach((c) => {
          delete c.dataset.dir;
          const a = c.querySelector(".arr");
          if (a) a.remove();
        });
        th.dataset.dir = dir;
        rows.sort((ra, rb) => {
          const a = cellValue(ra, i), b = cellValue(rb, i);
          if (a < b) return dir === "asc" ? -1 : 1;
          if (a > b) return dir === "asc" ? 1 : -1;
          return 0;
        });
        rows.forEach((r) => body.appendChild(r));
        const arr = document.createElement("span");
        arr.className = "arr";
        arr.textContent = dir === "asc" ? " ▲" : " ▼";
        th.appendChild(arr);
      });
    });
  });
}

// --- Search filter (input.search[data-target="#id"]) -----------------------
function initSearch() {
  document.querySelectorAll("input.search[data-target]").forEach((input) => {
    const table = document.querySelector(input.dataset.target);
    if (!table) return;
    input.addEventListener("input", () => {
      const q = input.value.toLowerCase();
      Array.from(table.tBodies[0].rows).forEach((tr) => {
        tr.style.display = tr.textContent.toLowerCase().includes(q) ? "" : "none";
      });
    });
  });
}

// --- Custom chart tooltips (replaces sluggish native SVG <title>) -----------
function initChartTips() {
  const tip = document.createElement("div");
  tip.className = "chart-tip";
  document.body.appendChild(tip);

  // Move <title> text into data-tip so the browser never shows the native
  // tooltip — and re-use it for our custom one.
  document.querySelectorAll(".chart [data-tip], .donut [data-tip]").forEach(() => {});
  document.querySelectorAll(".chart title, .donut title").forEach((t) => {
    const parent = t.parentElement;
    if (parent && !parent.dataset.tip) {
      parent.dataset.tip = t.textContent;
      t.remove();
    }
  });

  function show(e) {
    const el = e.target.closest("[data-tip]");
    if (!el) return hide();
    tip.textContent = el.dataset.tip;
    tip.classList.add("visible");
    position(e);
  }
  function position(e) {
    tip.style.left = e.clientX + 12 + "px";
    tip.style.top = e.clientY - 28 + "px";
  }
  function hide() {
    tip.classList.remove("visible");
  }

  document.querySelectorAll(".chart, .donut > svg").forEach((svg) => {
    svg.addEventListener("mousemove", show);
    svg.addEventListener("mouseleave", hide);
  });
}

initChartTips();   // must run before initMask so <title> text is captured before masking
initMask();
initSortable();
initSearch();
