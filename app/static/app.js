// Powens Finance — client interactions (no build step, vanilla JS).

// --- Theme toggle (light "corporate" <-> dark "business") -------------------
function initTheme() {
  const btn = document.getElementById("theme-toggle");
  if (!btn) return;
  btn.addEventListener("click", () => {
    const cur = document.documentElement.getAttribute("data-theme");
    const next = cur === "business" ? "corporate" : "business";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("pf-theme", next);
  });
}

// --- Reveal sensitive amounts ----------------------------------------------
function initReveal() {
  const btn = document.getElementById("reveal-btn");
  if (!btn) return;
  const sync = () => {
    const on = document.body.classList.contains("reveal");
    btn.textContent = on ? "🙈 Masquer les montants" : "👁 Afficher les montants";
  };
  btn.addEventListener("click", () => {
    document.body.classList.toggle("reveal");
    sync();
  });
  sync();
}

// --- Sortable tables (class="sortable") ------------------------------------
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

// --- Search filter (input.table-search[data-target="#id"]) -----------------
function initSearch() {
  document.querySelectorAll("input.table-search").forEach((input) => {
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
initReveal();
initSortable();
initSearch();
