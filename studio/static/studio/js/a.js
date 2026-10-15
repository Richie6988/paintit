/* Mesure d'audience PaintIt : sans cookie ni stockage (vue de page, temps passe, clics sur les appels a l'action). */
(function () {
  if (navigator.webdriver) return;
  var U = "/a/", hit = null, t0 = Date.now(), shown = 0, vis = document.visibilityState === "visible" ? Date.now() : 0;
  function send(o, beacon) {
    var b = JSON.stringify(o);
    if (beacon && navigator.sendBeacon) return navigator.sendBeacon(U, new Blob([b], {type: "application/json"}));
    return fetch(U, {method: "POST", body: b, headers: {"Content-Type": "application/json"}, keepalive: true, credentials: "same-origin"})
      .then(function (r) { return r.json(); }).catch(function () { return {}; });
  }
  var p = location.pathname;
  send({t: "pv", p: p, r: document.referrer, q: location.search, w: screen.width}).then(function (d) { hit = d && d.h; });
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") { vis = Date.now(); }
    else if (vis) { shown += Date.now() - vis; vis = 0; }
  });
  addEventListener("pagehide", function () {
    if (vis) { shown += Date.now() - vis; vis = 0; }
    if (hit) send({t: "end", h: hit, p: p, d: Math.round(shown / 1000)}, true);
  });
  document.addEventListener("click", function (e) {
    var el = e.target.closest && e.target.closest("[data-cta], .cta, .nav-cta, .btn-cta");
    if (!el) return;
    var n = el.getAttribute("data-cta") || (el.textContent || "").replace(/\s+/g, " ").trim().slice(0, 60);
    if (n) send({t: "c", p: p, n: n}, true);
  }, true);
})();
