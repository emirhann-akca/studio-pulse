/* Reklam Merkezi (/reklam). A file, not inline code: the CSP allows no inline script and htmx runs with
   allowEval off. Every handler is delegated from the document, so content htmx swaps in keeps working.
   Names from data are only ever set with textContent / attributes, never as HTML. */
(function () {
  "use strict";
  var AUTO_KEY = "reklam-auto-refresh";
  var AUTO_MS = 15 * 60 * 1000;

  function store(get, value) {
    try {
      if (get) return localStorage.getItem(AUTO_KEY);
      if (value) localStorage.setItem(AUTO_KEY, "1"); else localStorage.removeItem(AUTO_KEY);
    } catch (e) { return null; }
    return null;
  }

  // the game switcher submits itself
  document.addEventListener("change", function (ev) {
    var el = ev.target;
    if (el && el.closest && el.closest("form[data-autosubmit]") && el.tagName === "SELECT") {
      el.form.submit();
    }
    if (el && el.matches && el.matches("[data-fill-brief]")) {
      var target = document.querySelector(el.getAttribute("data-fill-brief"));
      var opt = el.options[el.selectedIndex];
      if (target && opt && opt.getAttribute("data-brief")) target.value = opt.getAttribute("data-brief");
    }
    if (el && el.name === "mode") {
      var real = document.getElementById("rk-real");
      if (real) real.classList.toggle("on", el.value === "real" && el.checked);
    }
    if (el && el.matches && el.matches("[data-auto-refresh]")) {
      store(false, el.checked);
      schedule();
    }
  });

  // auto refresh: press "Şimdi yenile" every 15 minutes while the box is ticked (remembered per viewer)
  var timer = null;
  function schedule() {
    if (timer) { clearInterval(timer); timer = null; }
    var box = document.querySelector("[data-auto-refresh]");
    if (!box || !box.checked) return;
    timer = setInterval(function () {
      var btn = document.getElementById("rk-refresh");
      if (btn && window.htmx) window.htmx.trigger(btn, "click");
    }, AUTO_MS);
  }
  var box = document.querySelector("[data-auto-refresh]");
  if (box) { box.checked = store(true) === "1"; schedule(); }

  // the campaign tree: open / close a row's children
  function rows(tbody) { return Array.prototype.slice.call(tbody.querySelectorAll("tr[data-id]")); }
  function setHidden(tbody, id, hidden) {
    rows(tbody).forEach(function (r) {
      if (r.getAttribute("data-parent") !== id) return;
      r.hidden = hidden;
      var tog = r.querySelector(".rk-tog");
      var open = tog ? tog.getAttribute("aria-expanded") === "true" : false;
      setHidden(tbody, r.getAttribute("data-id"), hidden || !open);
    });
  }
  document.addEventListener("click", function (ev) {
    var tog = ev.target.closest && ev.target.closest(".rk-tog");
    if (tog) {
      var open = tog.getAttribute("aria-expanded") === "true";
      tog.setAttribute("aria-expanded", open ? "false" : "true");
      setHidden(tog.closest("tbody"), tog.getAttribute("data-toggle"), open);
      return;
    }
    var th = ev.target.closest && ev.target.closest("table[data-sortable] th[data-sort]");
    if (th) { sortBy(th); return; }
    if (ev.target.closest && ev.target.closest("[data-drawer-close]")) { closeDrawer(); }
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape") closeDrawer();
  });

  // sorting keeps the tree: siblings are ordered under their parent, children follow their parent
  function sortBy(th) {
    var table = th.closest("table");
    var key = th.getAttribute("data-sort");
    var dir = th.getAttribute("aria-sort") === "descending" ? "ascending" : "descending";
    table.querySelectorAll("th[data-sort]").forEach(function (h) { h.removeAttribute("aria-sort"); });
    th.setAttribute("aria-sort", dir);
    var tbody = table.tBodies[0];
    var all = rows(tbody);
    var kids = {};
    all.forEach(function (r) { var p = r.getAttribute("data-parent") || ""; (kids[p] = kids[p] || []).push(r); });
    function val(r) {
      var v = r.getAttribute("data-" + key) || "";
      var n = parseFloat(v);
      return key === "name" ? v.toLocaleLowerCase("tr") : (isNaN(n) ? 0 : n);
    }
    function cmp(a, b) {
      var x = val(a), y = val(b);
      var c = x < y ? -1 : x > y ? 1 : 0;
      return dir === "ascending" ? c : -c;
    }
    var order = [];
    function walk(parent) {
      (kids[parent] || []).slice().sort(cmp).forEach(function (r) { order.push(r); walk(r.getAttribute("data-id")); });
    }
    walk("");
    order.forEach(function (r) { tbody.appendChild(r); });
  }

  // the creative drawer: htmx fills it; this opens and closes it
  var lastFocus = null;
  function openDrawer() {
    var d = document.getElementById("rk-drawer");
    if (!d) return;
    lastFocus = document.activeElement;
    d.hidden = false;
    var btn = d.querySelector("[data-drawer-close].btn");
    if (btn) btn.focus();
  }
  function closeDrawer() {
    var d = document.getElementById("rk-drawer");
    if (!d || d.hidden) return;
    d.hidden = true;
    var v = d.querySelector("video");
    if (v) v.pause();
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }
  document.addEventListener("htmx:beforeRequest", function (ev) {
    var elt = ev.detail && ev.detail.elt;
    if (elt && elt.hasAttribute && elt.hasAttribute("data-drawer-open")) {
      var body = document.getElementById("rk-drawer-body");
      if (body) body.style.opacity = "0.5";
      openDrawer();
    }
  });
  document.addEventListener("htmx:afterSwap", function (ev) {
    if (ev.detail && ev.detail.target && ev.detail.target.id === "rk-drawer-body") {
      ev.detail.target.style.opacity = "";
    }
  });
})();
