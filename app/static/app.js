// Powens Finance — client interactions (vanilla JS, no build step).

// --- Theme (light <-> dark) ------------------------------------------------
function initTheme() {
  const btn = document.getElementById("theme-toggle");
  const root = document.documentElement;
  const icon = () => {
    const dark = root.getAttribute("data-theme") === "dark";
    if (btn) btn.textContent = dark ? "☀️" : "🌙";
  };
  icon();
  if (!btn) return;
  btn.addEventListener("click", () => {
    const next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
    root.setAttribute("data-theme", next);
    localStorage.setItem("pf-theme", next);
    icon();
  });
}

// --- Mask amounts globally (persisted) -------------------------------------
function initMask() {
  const btn = document.getElementById("mask-toggle");
  const root = document.documentElement;
  const sync = () => {
    const hidden = root.classList.contains("hide-amounts");
    if (btn) btn.textContent = hidden ? "🙈 Montants" : "👁 Montants";
  };
  sync();
  if (!btn) return;
  btn.addEventListener("click", () => {
    const hidden = root.classList.toggle("hide-amounts");
    localStorage.setItem("pf-hide", hidden ? "1" : "0");
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

initTheme();
initMask();
initSortable();
initSearch();
