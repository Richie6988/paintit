/* PaintIt · boites de dialogue a la marque (remplacent confirm / alert / prompt du navigateur).
 *
 *  PitDialog.confirm("Supprimer ce design ?", {danger:true}).then(function(ok){ ... })
 *  PitDialog.alert("Enregistre !")
 *  PitDialog.copy("https://...", "Lien de la pub")        // champ + bouton Copier
 *
 *  Sans JS supplementaire : <button data-confirm="Supprimer ?" data-danger> dans un formulaire,
 *  <a data-confirm="..."> ou <form data-confirm="..."> : la dialogue s'ouvre avant l'action.
 */
(function () {
  if (window.PitDialog) return;
  var lang = ((document.documentElement.getAttribute("lang") || "fr").slice(0, 2)).toLowerCase();
  var T = {
    fr: {ok: "Confirmer", cancel: "Annuler", close: "OK", copy: "Copier", copied: "Copié ✓", confirm: "Confirmation", info: "Information", danger: "Supprimer"},
    en: {ok: "Confirm", cancel: "Cancel", close: "OK", copy: "Copy", copied: "Copied ✓", confirm: "Please confirm", info: "Information", danger: "Delete"},
    de: {ok: "Bestätigen", cancel: "Abbrechen", close: "OK", copy: "Kopieren", copied: "Kopiert ✓", confirm: "Bestätigung", info: "Information", danger: "Löschen"},
    es: {ok: "Confirmar", cancel: "Cancelar", close: "OK", copy: "Copiar", copied: "Copiado ✓", confirm: "Confirmación", info: "Información", danger: "Eliminar"},
    it: {ok: "Conferma", cancel: "Annulla", close: "OK", copy: "Copia", copied: "Copiato ✓", confirm: "Conferma", info: "Informazione", danger: "Elimina"}
  }[lang] || null;
  if (!T) T = {ok: "Confirmer", cancel: "Annuler", close: "OK", copy: "Copier", copied: "Copié ✓", confirm: "Confirmation", info: "Information", danger: "Supprimer"};

  // logo : a cote de ce script (…/studio/js/dialog.js -> …/studio/img/icon.svg)
  var me = document.currentScript && document.currentScript.src || "";
  var ICON = me ? me.replace(/js\/dialog[^\/]*$/, "img/icon.svg") : "/static/studio/img/icon.svg";

  var css = ""
    + ".pitd-ov{position:fixed;inset:0;z-index:2147483000;display:flex;align-items:center;justify-content:center;padding:16px;"
    + "background:rgba(12,20,45,.55);-webkit-backdrop-filter:blur(3px);backdrop-filter:blur(3px);opacity:0;transition:opacity .16s ease}"
    + ".pitd-ov.on{opacity:1}"
    + ".pitd{width:min(420px,100%);background:#fff;border-radius:18px;box-shadow:0 24px 70px rgba(10,18,40,.35);overflow:hidden;"
    + "font-family:Inter,system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;color:#12224f;transform:translateY(10px) scale(.97);transition:transform .18s cubic-bezier(.2,1.2,.3,1)}"
    + ".pitd-ov.on .pitd{transform:none}"
    + ".pitd-bar{height:5px;background:linear-gradient(90deg,#2f6bf2,#7a3ff2,#e85d75,#f6b93b)}"
    + ".pitd-hd{display:flex;align-items:center;gap:12px;padding:20px 22px 6px}"
    + ".pitd-ic{width:42px;height:42px;border-radius:12px;display:flex;align-items:center;justify-content:center;flex:0 0 auto;background:#eef2fb}"
    + ".pitd-ic img{width:28px;height:28px}.pitd.danger .pitd-ic{background:#fdecef}"
    + ".pitd-ic svg{width:22px;height:22px}"
    + ".pitd-tt{font-weight:800;font-size:1.05rem;line-height:1.25}"
    + ".pitd-br{font-size:.72rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#8a97b4}"
    + ".pitd-msg{padding:6px 22px 4px 76px;font-size:.95rem;line-height:1.5;color:#33415c;white-space:pre-line}"
    + ".pitd-in{margin:10px 22px 0 76px;display:flex;gap:8px}"
    + ".pitd-in input{flex:1;min-width:0;border:1px solid #cdd8ea;border-radius:10px;padding:9px 11px;font-size:.86rem;color:#12224f;background:#f7f9fd}"
    + ".pitd-ft{display:flex;justify-content:flex-end;gap:10px;padding:18px 22px 20px}"
    + ".pitd-b{border:0;border-radius:11px;padding:10px 18px;font-weight:800;font-size:.9rem;cursor:pointer;font-family:inherit;transition:filter .12s,transform .12s}"
    + ".pitd-b:active{transform:scale(.97)}"
    + ".pitd-b.sec{background:#f1f4fa;color:#33415c}.pitd-b.sec:hover{background:#e6ebf5}"
    + ".pitd-b.pri{background:linear-gradient(90deg,#2f6bf2,#7a3ff2);color:#fff;box-shadow:0 6px 16px rgba(47,107,242,.3)}"
    + ".pitd-b.pri:hover{filter:brightness(1.07)}"
    + ".pitd.danger .pitd-b.pri{background:#d92d20;box-shadow:0 6px 16px rgba(217,45,32,.28)}"
    + ".pitd-b:focus-visible{outline:3px solid rgba(47,107,242,.35);outline-offset:2px}"
    + "@media(max-width:480px){.pitd-msg,.pitd-in{margin-left:0;padding-left:22px}.pitd-in{margin:10px 22px 0}.pitd-ft{flex-direction:column-reverse}.pitd-b{width:100%}}";
  var st = document.createElement("style");
  st.textContent = css;
  (document.head || document.documentElement).appendChild(st);

  var WARN = '<svg viewBox="0 0 24 24" fill="none" stroke="#d92d20" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14M10 11v6M14 11v6"/></svg>';

  function esc(s) { var d = document.createElement("div"); d.textContent = s == null ? "" : String(s); return d.innerHTML; }

  function open(o) {
    return new Promise(function (resolve) {
      var prev = document.activeElement;
      var ov = document.createElement("div");
      ov.className = "pitd-ov";
      ov.innerHTML = '<div class="pitd' + (o.danger ? " danger" : "") + '" role="dialog" aria-modal="true" aria-labelledby="pitd-t">'
        + '<div class="pitd-bar"></div>'
        + '<div class="pitd-hd"><div class="pitd-ic">' + (o.danger ? WARN : '<img src="' + ICON + '" alt="">') + '</div>'
        + '<div><div class="pitd-br">PaintIt</div><div class="pitd-tt" id="pitd-t">' + esc(o.title) + '</div></div></div>'
        + (o.message ? '<div class="pitd-msg">' + esc(o.message) + '</div>' : "")
        + (o.value != null ? '<div class="pitd-in"><input type="text" readonly value="' + esc(o.value) + '"><button class="pitd-b sec" data-a="copy">' + T.copy + '</button></div>' : "")
        + '<div class="pitd-ft">' + (o.cancel ? '<button class="pitd-b sec" data-a="no">' + esc(o.cancel) + '</button>' : "")
        + '<button class="pitd-b pri" data-a="yes">' + esc(o.ok) + '</button></div></div>';
      document.body.appendChild(ov);
      requestAnimationFrame(function () { ov.classList.add("on"); });
      var yes = ov.querySelector('[data-a="yes"]'), no = ov.querySelector('[data-a="no"]');
      (o.danger && no ? no : yes).focus();
      var inp = ov.querySelector(".pitd-in input");
      if (inp) { inp.addEventListener("focus", function () { inp.select(); }); }

      function done(v) {
        document.removeEventListener("keydown", key, true);
        ov.classList.remove("on");
        setTimeout(function () { ov.remove(); if (prev && prev.focus) try { prev.focus(); } catch (e) {} }, 170);
        resolve(v);
      }
      function key(e) {
        if (e.key === "Escape") { e.preventDefault(); done(false); }
        else if (e.key === "Enter" && document.activeElement && document.activeElement.tagName !== "BUTTON") { e.preventDefault(); done(true); }
        else if (e.key === "Tab") {   // focus piege dans la dialogue
          var f = [].slice.call(ov.querySelectorAll("button,input"));
          var i = f.indexOf(document.activeElement);
          if (e.shiftKey && i <= 0) { e.preventDefault(); f[f.length - 1].focus(); }
          else if (!e.shiftKey && i === f.length - 1) { e.preventDefault(); f[0].focus(); }
        }
      }
      document.addEventListener("keydown", key, true);
      ov.addEventListener("click", function (e) {
        var a = e.target.closest("[data-a]");
        if (e.target === ov) return done(false);
        if (!a) return;
        if (a.dataset.a === "copy") {
          var v = inp.value;
          var ok = function () { a.textContent = T.copied; setTimeout(function () { a.textContent = T.copy; }, 1400); };
          if (navigator.clipboard) navigator.clipboard.writeText(v).then(ok, function () { inp.select(); document.execCommand("copy"); ok(); });
          else { inp.select(); document.execCommand("copy"); ok(); }
          return;
        }
        done(a.dataset.a === "yes");
      });
    });
  }

  window.PitDialog = {
    confirm: function (message, opts) {
      opts = opts || {};
      // la question sert de titre ; opts.text = precision facultative sous le titre
      return open({title: opts.title || message, message: opts.text || (opts.title ? message : ""),
                   ok: opts.ok || (opts.danger ? T.danger : T.ok), cancel: opts.cancel || T.cancel, danger: !!opts.danger});
    },
    alert: function (message, opts) {
      opts = opts || {};
      return open({title: opts.title || T.info, message: message, ok: opts.ok || T.close, cancel: null, danger: false});
    },
    copy: function (value, title) {
      return open({title: title || T.copy, message: "", value: value, ok: T.close, cancel: null});
    }
  };

  // ---- data-confirm : confirmation declarative (boutons, liens, formulaires) ----
  function ask(el) {
    return window.PitDialog.confirm(el.getAttribute("data-confirm"), {
      danger: el.hasAttribute("data-danger"), ok: el.getAttribute("data-ok") || undefined,
      text: el.getAttribute("data-confirm-text") || undefined});
  }
  document.addEventListener("click", function (e) {
    var el = e.target.closest && e.target.closest("[data-confirm]");
    if (!el || el.tagName === "FORM") return;
    if (el.dataset.pitdOk === "1") { delete el.dataset.pitdOk; return; }
    e.preventDefault(); e.stopImmediatePropagation();
    ask(el).then(function (ok) {
      if (!ok) return;
      var form = el.form || (el.closest && el.closest("form"));
      if (form && (el.type === "submit" || el.tagName === "BUTTON")) {
        if (form.hasAttribute("data-confirm")) form.dataset.pitdOk = "1";
        if (form.requestSubmit) form.requestSubmit(el); else { el.dataset.pitdOk = "1"; el.click(); }
      } else if (el.tagName === "A" && el.href) {
        if (el.target === "_blank") window.open(el.href, "_blank"); else location.href = el.href;
      } else { el.dataset.pitdOk = "1"; el.click(); }
    });
  }, true);
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form.hasAttribute || !form.hasAttribute("data-confirm")) return;
    if (form.dataset.pitdOk === "1") { delete form.dataset.pitdOk; return; }
    e.preventDefault(); e.stopImmediatePropagation();
    var sub = e.submitter;
    ask(form).then(function (ok) {
      if (!ok) return;
      form.dataset.pitdOk = "1";
      if (form.requestSubmit) form.requestSubmit(sub || undefined); else form.submit();
    });
  }, true);
})();
