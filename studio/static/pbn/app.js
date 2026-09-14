/* app.js — UI du pipeline PBN : upload, params, exécution étape par étape. */
const $ = id => document.getElementById(id);
const state = { file: null, jobId: null, running: false };

/* ---------------- helpers UI ---------------- */
function toast(msg) {
  const t = $("toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(t._h); t._h = setTimeout(() => t.hidden = true, 5000);
}
function bindRange(id, fmt = v => v) {
  const el = $(id), out = $(id + "Out");
  el.addEventListener("input", () => out.textContent = fmt(+el.value));
}
["widthCm","dpi","colors","seed","minZoneMm","strokeMm","palScale"].forEach(i => bindRange(i));
bindRange("heightCm", v => v === 0 ? "auto" : v);
bindRange("maxPx", v => v === 0 ? "∞" : v);
bindRange("maxZones", v => v === 0 ? "auto" : v);
bindRange("density", v => v === 0 ? "auto" : v.toFixed(2));
bindRange("minPaintMm", v => v.toFixed(1));

/* segmented buttons */
function segValue(id) { return document.querySelector(`#${id} button.on`).dataset.v; }
document.querySelectorAll(".segmented").forEach(seg => {
  seg.addEventListener("click", e => {
    const b = e.target.closest("button"); if (!b) return;
    seg.querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b));
  });
});

/* ---------------- collecte des paramètres ---------------- */
function collectParams() {
  const p = {
    width_cm: +$("widthCm").value,
    height_cm: +$("heightCm").value || null,
    dpi: +$("dpi").value,
    max_px: +$("maxPx").value || 0,
    colors: +$("colors").value,
    seed: +$("seed").value,
    smooth: segValue("smoothSeg"),
    detail: segValue("detailSeg"),
    cnn: segValue("cnnSeg"),
    hed_model_dir: "./hed",
    min_zone_mm: +$("minZoneMm").value,
    min_paint_mm: +$("minPaintMm").value,
    max_zones: +$("maxZones").value || 0,
    density: +$("density").value || 0,
    stroke_mm: +$("strokeMm").value,
    pal_scale: +$("palScale").value,
    brand: $("brand").value,
    discount_text: $("discountText").value,
    discount_url: $("discountUrl").value,
    no_qr: $("noQr").checked,
  };
  const logo = $("logoInput").files[0];
  if (logo) p.logo = null; // le logo uploadé requiert un champ dédié — simplifié ici
  return p;
}

/* ---------------- upload ---------------- */
const dz = $("dropzone");
["dragover","dragenter"].forEach(ev => dz.addEventListener(ev, e => {
  e.preventDefault(); dz.classList.add("over");
}));
["dragleave","drop"].forEach(ev => dz.addEventListener(ev, e => {
  e.preventDefault(); dz.classList.remove("over");
}));
dz.addEventListener("drop", e => { if (e.dataTransfer.files[0]) setFile(e.dataTransfer.files[0]); });
$("fileInput").addEventListener("change", e => setFile(e.target.files[0]));

function setFile(f) {
  if (!f.type.startsWith("image/")) return toast("Seules les images sont acceptées.");
  state.file = f;
  $("dropLabel").hidden = true;
  const img = $("thumb"); img.hidden = false;
  img.src = URL.createObjectURL(f);
  $("runBtn").disabled = false;
}

/* ---------------- exécution du pipeline ---------------- */
const MEDIA = ""; // les urls renvoyées sont relatives à /media/ géré par MEDIA_URL
const STEP_ORDER = ["resize","smooth","quantize","modefilter","clean","limit",
                    "template","preview","palette"];

$("runBtn").addEventListener("click", () => run(false));
$("rerunBtn").addEventListener("click", () => run(true));

async function run(reloadOnly = false) {
  if (state.running) return;
  state.running = true;
  $("runBtn").disabled = true;
  $("timeline").innerHTML = "";
  setProgress(0);

  try {
    if (!reloadOnly || !state.jobId) {
      const fd = new FormData();
      fd.append("image", state.file);
      fd.append("params", JSON.stringify(collectParams()));
      const r = await fetch("/pbn/api/upload/", { method: "POST", body: fd });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || "upload échoué");
      state.jobId = d.job_id;
      $("jobBadge").hidden = false;
      $("jobId").textContent = state.jobId;
      addStepCard(d.step, true);          // étape upload affichée
      $("rerunBtn").hidden = false;
    }
    for (let i = 0; i < STEP_ORDER.length; i++) {
      await runStep(STEP_ORDER[i], i);
      setProgress(((i + 1) / STEP_ORDER.length) * 100);
    }
  } catch (err) {
    toast("⚠ " + err.message);
  } finally {
    state.running = false;
    $("runBtn").disabled = false;
    $("runBtn").textContent = "\u25B6 Relancer (params actuels)";
  }
}

async function runStep(key, idx) {
  const card = addStepCard({ title: "Étape en cours…", key }, false, true);
  const fd = new FormData();
  fd.append("job_id", state.jobId);
  const r = await fetch(`/pbn/api/step/${key}/`, { method: "POST", body: fd });
  const d = await r.json();
  if (!r.ok) {
    card.classList.add("err");
    card.querySelector("h4").textContent = "Échec : " + (d.error || key);
    card.querySelector(".spinner")?.remove();
    throw new Error(d.error || "étape " + key);
  }
  fillCard(card, d.step);
  // petite pause pour l'effet "temps réel" visible
  await new Promise(res => setTimeout(res, 250));
}

/* ---------------- rendu des cartes ---------------- */
function addStepCard(step, done = false, pending = false) {
  $("placeholder")?.remove();
  const card = document.createElement("article");
  card.className = "step" + (done ? " done" : "");
  card.innerHTML = `
    <div class="step-head">
      <span class="dot"></span><h4>${step.title}</h4>
      ${pending ? '<span class="spinner"></span>' : ""}
    </div>
    <div class="step-body">
      <div class="img-wrap"></div>
      <div>
        <p class="desc">${step.desc || ""}</p>
        <div class="chips"></div>
        <div class="dl"></div>
      </div>
    </div>`;
  $("timeline").appendChild(card);
  if (done) fillCard(card, step);
  card.scrollIntoView({ behavior: "smooth", block: "nearest" });
  return card;
}

function fillCard(card, step) {
  card.querySelector(".spinner")?.remove();
  const prefix = step.media_prefix || "/media/";
  const img = document.createElement("img");
  img.src = prefix + step.file + "?t=" + Date.now();
  img.loading = "lazy";
  card.querySelector(".img-wrap").appendChild(img);
  card.querySelector(".desc").textContent = step.desc || "";
  card.querySelector(".chips").innerHTML =
    (step.stats || []).map(s => `<span class="chip">${s}</span>`).join("");
  const dl = card.querySelector(".dl");
  const link = document.createElement("a");
  link.href = prefix + step.file;
  link.download = step.file.split("/").pop();
  link.textContent = "⬇ Télécharger " + step.file.split("/").pop();
  dl.appendChild(link);
  (step.extra_files || []).forEach(xf => {
    const a = document.createElement("a");
    a.href = prefix + xf.url; a.download = xf.name;
    a.textContent = "⬇ Télécharger " + xf.name;
    dl.appendChild(a);
  });
}

function setProgress(p) { $("progressBar").style.width = p + "%"; }
