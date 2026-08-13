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
    if (btn) {
      btn.title = hidden ? "Afficher les montants" : "Masquer les montants";
      btn.setAttribute("aria-pressed", hidden ? "true" : "false");
    }
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
      // Un en-tête triable est un contrôle : il doit être atteignable au
      // clavier et annoncer son état de tri (aria-sort).
      th.tabIndex = 0;
      th.setAttribute("role", "columnheader");
      th.setAttribute("aria-sort", "none");
      const sort = () => {
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
        Array.from(head.cells).forEach((c) => c.setAttribute("aria-sort", "none"));
        th.setAttribute("aria-sort", dir === "asc" ? "ascending" : "descending");
        const arr = document.createElement("span");
        arr.className = "arr";
        arr.setAttribute("aria-hidden", "true");
        arr.textContent = dir === "asc" ? " ▲" : " ▼";
        th.appendChild(arr);
      };
      th.addEventListener("click", sort);
      th.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          sort();
        }
      });
    });
  });
}

// --- Lignes cliquables : clavier + rôle ------------------------------------
// `onclick` sur un <tr> n'est atteignable qu'à la souris. Chaque ligne
// cliquable devient un lien focusable, activable par Entrée.
function initClickableRows() {
  document.querySelectorAll("tr.clickable").forEach((tr) => {
    tr.tabIndex = 0;
    tr.setAttribute("role", "link");
    const target = tr.querySelector("a[href]");
    if (target && !tr.getAttribute("aria-label")) {
      tr.setAttribute("aria-label", target.textContent.trim());
    }
    tr.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        tr.click();
      }
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
      const rows = Array.from(table.tBodies[0].rows);
      rows.forEach((tr) => {
        if (tr.classList.contains("row-total")) return;  // traité juste après
        tr.style.display = tr.textContent.toLowerCase().includes(q) ? "" : "none";
      });
      // Une ligne de groupe (jour, famille) dont toutes les opérations sont
      // filtrées laissait une date orpheline : la masquer avec son groupe.
      rows.forEach((tr, i) => {
        if (!tr.classList.contains("row-total")) return;
        let visible = false;
        for (let j = i + 1; j < rows.length; j++) {
          if (rows[j].classList.contains("row-total")) break;
          if (rows[j].style.display !== "none") { visible = true; break; }
        }
        tr.style.display = visible ? "" : "none";
      });
    });
  });
}

// --- Custom chart tooltips (replaces sluggish native SVG <title>) -----------
function initChartTips() {
  const tip = document.createElement("div");
  tip.className = "chart-tip";
  document.body.appendChild(tip);

  // Copier le texte des <title> dans data-tip pour l'infobulle maison — SANS
  // les supprimer : un <title> est le nom accessible de la forme SVG, et le
  // retirer rendait les graphiques muets aux lecteurs d'écran. L'infobulle
  // native est neutralisée par `pointer-events: none` en CSS sur les <title>,
  // qui n'affecte pas l'arbre d'accessibilité.
  document.querySelectorAll(".chart title, .donut title").forEach((t) => {
    const parent = t.parentElement;
    if (parent && !parent.dataset.tip) parent.dataset.tip = t.textContent;
  });

  function show(e) {
    const el = e.target.closest("[data-tip]");
    if (!el) return hide();
    tip.textContent = el.dataset.tip;
    tip.classList.add("visible");
    position(e);
  }
  function position(e) {
    const tw = tip.offsetWidth || 120;
    const x = e.clientX + 12 + tw > window.innerWidth
      ? e.clientX - tw - 12
      : e.clientX + 12;
    tip.style.left = x + "px";
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
initClickableRows();
