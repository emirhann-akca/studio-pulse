/* studio-pulse page behaviour that is not part of the shared studio-ui kit.
   A file, not inline handlers: the Content-Security-Policy allows no inline script
   and htmx runs with allowEval off, so hx-on attributes cannot be used. */
(function () {
  "use strict";
  // The Studio's ask box: an empty question is not sent; a sent one is shown at once.
  document.addEventListener("htmx:beforeRequest", function (ev) {
    var form = ev.detail && ev.detail.elt;
    if (!form || !form.classList || !form.classList.contains("ai-form")) return;
    var q = form.q ? form.q.value : "";
    if (!q.trim()) { ev.preventDefault(); return; }
    var chat = document.getElementById("chat");
    if (!chat) return;
    var d = document.createElement("div");
    d.className = "q";
    d.textContent = q;
    chat.appendChild(d);
  });
  document.addEventListener("htmx:afterRequest", function (ev) {
    var form = ev.detail && ev.detail.elt;
    if (form && form.classList && form.classList.contains("ai-form")) form.reset();
  });
})();
