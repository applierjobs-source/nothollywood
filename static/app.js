/* ═══════════════════════════════════════════════════════════════════════
   NOT HOLLYWOOD - Netflix for creators
   Hydrates hero + shelves from /api/jobs, opens composer modal.
   ══════════════════════════════════════════════════════════════════════ */

const API = "";                                    // same-origin
const POLL_MS = 3000;
// When no active renders, keep a slow heartbeat poll so newly-finished renders
// (email says done, but tab was throttled) show up when the user comes back.
const IDLE_POLL_MS = 15000;

// ─── Element cache ─────────────────────────────────────────────────
const $ = (sel) => document.querySelector(sel);
const el = {
  nav: $("#nav"),
  hero: $("#hero"),
  heroVideo: $("#heroVideo"),
  heroEyebrow: $("#heroEyebrow"),
  heroTitle: $("#heroTitle"),
  heroMeta: $("#heroMeta"),
  heroSynopsis: $("#heroSynopsis"),
  heroPlayBtn: $("#heroPlayBtn"),
  heroInfoBtn: $("#heroInfoBtn"),

  openComposerBtn: $("#openComposerBtn"),

  shelfActive: $("#shelfActive"),
  activeCount: $("#activeCount"),
  activeRow: $("#activeRow"),

  shelfFeatured: $("#shelfFeatured"),
  featuredRow: $("#featuredRow"),

  shelfRenders: $("#shelfRenders"),
  rendersRow: $("#rendersRow"),
  rendersEmpty: $("#rendersEmpty"),

  // Auth
  navSignInBtn: $("#navSignInBtn"),
  navUser: $("#navUser"),
  navUserEmail: $("#navUserEmail"),
  navSignOutBtn: $("#navSignOutBtn"),
  navCredits: $("#navCredits"),
  navCreditsValue: $("#navCreditsValue"),
  authModal: $("#authModal"),
  renderStartedModal: $("#renderStartedModal"),
  renderStartedEmail: $("#renderStartedEmail"),
  renderStartedEta: $("#renderStartedEta"),
  authError: $("#authError"),
  authTitle: $("#authTitle"),
  authSub: $("#authSub"),
  googleSignInBtn: $("#googleSignInBtn"),

  ideasRow: $("#ideasRow"),
  templatesRow: $("#templatesRow"),

  // Composer modal
  composerModal: $("#composerModal"),
  renderForm: $("#renderForm"),
  prompt: $("#prompt"),
  promptCount: $("#promptCount"),
  dropZone: $("#dropZone"),
  dropEmpty: $("#dropEmpty"),
  dropPreview: $("#dropPreview"),
  refPreview: $("#refPreview"),
  refFile: $("#refFile"),
  pickFileBtn: $("#pickFileBtn"),
  clearRefBtn: $("#clearRefBtn"),
  durationSlider: $("#durationSlider"),
  lengthValue: $("#lengthValue"),
  lengthUnit: $("#lengthUnit"),
  lengthHint: $("#lengthHint"),
  presetChips: $("#presetChips"),
  submitBtn: $("#submitBtn"),
  estCost: $("#estCost"),
  estWait: $("#estWait"),

  // Preview & approval modal
  previewModal: $("#previewModal"),
  previewLoading: $("#previewLoading"),
  previewBody: $("#previewBody"),
  previewSubtitle: $("#previewSubtitle"),
  previewRefs: $("#previewRefs"),
  previewRefSub: $("#previewRefSub"),
  previewRefEmpty: $("#previewRefEmpty"),
  previewScenes: $("#previewScenes"),
  previewRegenBtn: $("#previewRegenBtn"),
  previewBackBtn: $("#previewBackBtn"),
  previewApproveBtn: $("#previewApproveBtn"),

  // Detail modal
  detailModal: $("#detailModal"),
  detailVideo: $("#detailVideo"),
  detailEyebrow: $("#detailEyebrow"),
  detailTitle: $("#detailTitle"),
  detailMeta: $("#detailMeta"),
  detailPrompt: $("#detailPrompt"),
  detailDownload: $("#detailDownload"),
  detailRemix: $("#detailRemix"),
};

// ─── State ─────────────────────────────────────────────────────────
let jobs = [];              // user-owned jobs (or [] when signed out)
let heroJob = null;
let pollTimer = null;
let currentDetailJob = null;

// ─── Auth (Supabase) ───────────────────────────────────────────────
// The Supabase client is created after we fetch /api/config on init. Until then
// auth is disabled and the app behaves as an anonymous read-only browse.
// NOTE: renamed from `supabase` to `sb` — the CDN's @supabase/supabase-js UMD
// bundle sets `window.supabase` as the SDK factory, and declaring `let supabase`
// at the top level of a classic <script> collides with that global in some
// browsers, throwing 'Identifier supabase has already been declared' and
// killing the entire script.
let sb = null;
let currentUser = null;     // null when signed out; { id, email, ... } when signed in
let authRequired = false;   // true when backend reports auth is configured
let libraryRenders = [];    // durable renders fetched from /api/library (survive deploys)
// (Google SSO is the only auth path now.)

async function initAuth() {
  try {
    const r = await fetch(`${API}/api/config`);
    if (!r.ok) throw new Error("config failed");
    const cfg = await r.json();
    authRequired = !!cfg.auth_required;
    if (cfg.auth_disabled) {
      // Testing mode: server-side AUTH_DISABLED=1. Show a small banner so it's
      // obvious auth is off, and skip Supabase client setup entirely.
      const bar = document.createElement("div");
      bar.textContent = "Testing mode — sign-in and account creation are disabled";
      bar.style.cssText =
        "position:fixed;top:0;left:0;right:0;z-index:9999;padding:6px 12px;" +
        "background:#7a3a00;color:#fff;font:600 12px/1.4 system-ui,sans-serif;" +
        "text-align:center;letter-spacing:.02em;";
      document.body.prepend(bar);
      document.body.style.paddingTop = "28px";
    }
    if (!authRequired) {
      // No Supabase auth (either not configured or testing mode). Keep composer open to all.
      return;
    }
    // eslint-disable-next-line no-undef
    sb = window.supabase.createClient(cfg.supabase_url, cfg.supabase_anon_key, {
      auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true },
    });
    // Restore session from localStorage if present.
    const { data: sessData } = await sb.auth.getSession();
    if (sessData && sessData.session) {
      currentUser = sessData.session.user;
    }
    // Subscribe to changes (sign-in, sign-out, token refresh).
    // SIGNED_IN fires after detectSessionInUrl parses the Google callback
    // fragment, so this is where we resume the pending composer prompt.
    sb.auth.onAuthStateChange((event, session) => {
      currentUser = session ? session.user : null;
      renderAuthUI();
      refreshJobs();
      refreshLibrary();
      if (event === "SIGNED_IN" && currentUser) {
        closeModal(el.authModal);
        handleOAuthReturn();
      }
    });
  } catch (err) {
    console.warn("auth init failed", err);
  }
}

async function currentAccessToken() {
  if (!sb) return null;
  const { data } = await sb.auth.getSession();
  return data && data.session ? data.session.access_token : null;
}

async function authedFetch(url, opts = {}) {
  const token = await currentAccessToken();
  const headers = new Headers(opts.headers || {});
  if (token) headers.set("Authorization", `Bearer ${token}`);
  return fetch(url, { ...opts, headers });
}

function renderAuthUI() {
  if (!authRequired) {
    // Auth disabled server-side: hide auth widgets entirely.
    el.navSignInBtn.hidden = true;
    el.navUser.hidden = true;
    el.shelfRenders.hidden = false;
    return;
  }
  if (currentUser) {
    el.navSignInBtn.hidden = true;
    el.navUser.hidden = false;
    el.navUserEmail.textContent = currentUser.email || "signed in";
    el.shelfRenders.hidden = false;
    refreshCredits(); // fire and forget — fills the chip when it lands
  } else {
    el.navSignInBtn.hidden = false;
    el.navUser.hidden = true;
    if (el.navCredits) el.navCredits.hidden = true;
    // Hide user library + active shelf when signed out — showcase remains.
    el.shelfRenders.hidden = true;
    el.shelfActive.hidden = true;
  }
}

// Fetches the current user's credit balance from the backend and paints the
// nav chip. Silent on failure (chip just stays hidden) so a broken creds
// endpoint never blocks the app.
let lastCreditsBalance = null;
async function refreshCredits() {
  if (!currentUser || !el.navCredits) return;
  try {
    const r = await authedFetch(`${API}/api/credits`);
    if (!r.ok) return;
    const data = await r.json();
    const bal = Number(data.balance) || 0;
    lastCreditsBalance = bal;
    el.navCreditsValue.textContent = bal.toLocaleString();
    el.navCredits.hidden = false;
    el.navCredits.classList.toggle("low", bal < 10);
  } catch (err) {
    // Silent: keep the chip hidden if the endpoint is unreachable.
    console.warn("credits fetch failed", err);
  }
}

// ─── Helpers ───────────────────────────────────────────────────────
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
function formatDuration(s) {
  s = Number(s);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s - m * 60;
  return rem ? `${m}m ${rem}s` : `${m}m`;
}
function planScenes(total) {
  if (total <= 10) return [total];
  const supported = [4, 6, 8, 10];
  const scenes = [];
  let remaining = total;
  while (remaining > 10) { scenes.push(10); remaining -= 10; }
  if (remaining > 0) {
    let final = 10;
    for (const s of supported) if (s >= remaining) { final = s; break; }
    scenes.push(final);
  }
  return scenes;
}
function sceneCount(total) { return planScenes(total).length; }
function currentDuration() { return Number(el.durationSlider.value); }
function timeAgo(ts) {
  if (!ts) return "";
  const seconds = Math.floor(Date.now() / 1000 - ts);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

// ─── Composer: length picker ───────────────────────────────────────
function updateLengthDisplay() {
  const d = currentDuration();
  if (d < 60) {
    el.lengthValue.textContent = d;
    el.lengthUnit.textContent = d === 1 ? "second" : "seconds";
  } else if (d < 3600) {
    const m = d / 60;
    el.lengthValue.textContent = Number.isInteger(m) ? m : m.toFixed(1);
    el.lengthUnit.textContent = m === 1 ? "minute" : "minutes";
  } else {
    const h = d / 3600;
    el.lengthValue.textContent = Number.isInteger(h) ? h : h.toFixed(1);
    el.lengthUnit.textContent = h === 1 ? "hour" : "hours";
  }
  const n = sceneCount(d);
  if (n === 1) {
    el.lengthHint.textContent = "Single H3 shot — fastest and cheapest.";
  } else if (d >= 1800) {
    el.lengthHint.textContent = `Long-form render: ${n} sequential 10-second scenes. Expect a long wait and higher credit cost.`;
  } else {
    el.lengthHint.textContent = `Rendered as ${n} sequential 10-second scenes and stitched together.`;
  }
  // Chip active state
  el.presetChips.querySelectorAll(".chip").forEach((c) => {
    c.classList.toggle("active", Number(c.dataset.preset) === d);
  });
  // Slider track fill (0-100%) for the linear-gradient styling
  const min = Number(el.durationSlider.min || 6);
  const max = Number(el.durationSlider.max || 3600);
  const pct = Math.round(((d - min) / (max - min)) * 100);
  el.durationSlider.style.setProperty("--slider-fill", `${pct}%`);
  updateEstimate();
}
function updateEstimate() {
  const d = currentDuration();
  const res = document.querySelector('input[name="resolution"]:checked')?.value || "768P";
  const perSec = res === "1080P" ? 0.13 : 0.08;
  const cost = d * perSec;
  const scenes = sceneCount(d);
  // Per-scene MiniMax H3 render time from observed reAPI jobs:
  //   768P: ~40s low / ~80s high per scene
  //   1080P: ~70s low / ~140s high per scene
  // Add ~4s per scene for download + a flat 8s ffmpeg concat when >1 scene.
  const perSceneLow = res === "1080P" ? 70 : 40;
  const perSceneHigh = res === "1080P" ? 140 : 80;
  const downloadPerScene = 4;
  const stitchOverhead = scenes > 1 ? 8 : 0;
  const totalLowSec = scenes * (perSceneLow + downloadPerScene) + stitchOverhead;
  const totalHighSec = scenes * (perSceneHigh + downloadPerScene) + stitchOverhead;
  el.estCost.textContent = `$${cost.toFixed(2)}`;
  el.estWait.textContent = formatWaitRange(totalLowSec, totalHighSec);
}
// Format a wait-time range into human-readable text. Picks the unit that
// keeps both numbers small and uses an en dash so it never reads as
// "203,406 minutes" (see: prior bug where a comma looked like a thousands
// separator on a 22-min movie).
function formatWaitRange(lowSec, highSec) {
  const pickUnit = (sec) => {
    if (sec < 90) return { v: Math.round(sec), u: "sec" };
    if (sec < 5400) return { v: Math.round(sec / 60), u: "min" };
    return { v: Math.round(sec / 360) / 10, u: "hr" };
  };
  // Use whichever unit works for the HIGH end so the range never straddles.
  const hi = pickUnit(highSec);
  const lo = hi.u === "sec" ? { v: Math.round(lowSec), u: "sec" }
          : hi.u === "min" ? { v: Math.max(1, Math.round(lowSec / 60)), u: "min" }
          : { v: Math.max(0.1, Math.round(lowSec / 360) / 10), u: "hr" };
  const label = hi.u === "hr" ? "hr" : hi.u === "min" ? "min" : "sec";
  return `${lo.v}\u2013${hi.v} ${label}`;
}

// ─── Composer: file drop ───────────────────────────────────────────
function showDropEmpty() {
  el.dropEmpty.hidden = false;
  el.dropPreview.hidden = true;
  el.refFile.value = "";
}
function showDropPreview(file) {
  const url = URL.createObjectURL(file);
  el.refPreview.src = url;
  el.dropEmpty.hidden = true;
  el.dropPreview.hidden = false;
}

// ─── Modal ─────────────────────────────────────────────────────────
function openModal(node) {
  node.hidden = false;
  requestAnimationFrame(() => node.setAttribute("aria-hidden", "false"));
  document.body.style.overflow = "hidden";
}
function closeModal(node) {
  node.setAttribute("aria-hidden", "true");
  setTimeout(() => {
    node.hidden = true;
    document.body.style.overflow = "";
    if (node === el.detailModal) {
      el.detailVideo.pause();
      el.detailVideo.removeAttribute("src");
      el.detailVideo.load();
    }
  }, 240);
}
document.querySelectorAll("[data-close]").forEach((n) => {
  n.addEventListener("click", (e) => {
    const modal = e.currentTarget.closest(".modal");
    if (modal) closeModal(modal);
  });
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    document.querySelectorAll('.modal[aria-hidden="false"]').forEach(closeModal);
  }
});

// Store any prompt the user tried to open the composer with (e.g. from a
// template tile) so we can restore it after they finish signing up.
let pendingComposerPrompt = null;

function openComposer(prefillPrompt) {
  // Auth gate: if auth is required and no user is signed in, route them to
  // the signup modal instead. The composer will auto-open after successful
  // signin/signup (see auth-form submit handler), so the user never has to
  // click Create a Show a second time.
  if (authRequired && !currentUser) {
    if (prefillPrompt) pendingComposerPrompt = prefillPrompt;
    openAuthModal("signup");
    return;
  }
  if (prefillPrompt) {
    el.prompt.value = prefillPrompt;
    el.prompt.dispatchEvent(new Event("input"));
  }
  openModal(el.composerModal);
  setTimeout(() => el.prompt.focus(), 260);
}

function openDetail(job) {
  currentDetailJob = job;
  el.detailEyebrow.textContent = job.status === "done" ? "Show" : job.status.toUpperCase();
  el.detailTitle.textContent = firstLine(job.prompt);
  el.detailMeta.innerHTML = renderMeta(job);
  el.detailPrompt.textContent = job.prompt;
  if (job.video) {
    el.detailVideo.src = job.video;
    el.detailVideo.load();
    el.detailVideo.play().catch(() => {});
    el.detailDownload.href = job.video;
    el.detailDownload.download = `nothollywood-${job.id}.mp4`;
    el.detailDownload.hidden = false;
  } else {
    el.detailDownload.hidden = true;
  }
  openModal(el.detailModal);
}

// ─── Nav ───────────────────────────────────────────────────────────
window.addEventListener("scroll", () => {
  el.nav.classList.toggle("scrolled", window.scrollY > 60);
}, { passive: true });

el.openComposerBtn.addEventListener("click", () => openComposer());
const ctaBandBtn = document.getElementById("ctaBandBtn");
if (ctaBandBtn) ctaBandBtn.addEventListener("click", () => openComposer());

document.querySelectorAll(".nav-tabs a").forEach((a) => {
  a.addEventListener("click", (e) => {
    // External links (like /pricing.html) have no data-tab. Let the browser handle them.
    const tab = a.dataset.tab;
    if (!tab) return;
    e.preventDefault();
    document.querySelectorAll(".nav-tabs a").forEach((x) => x.classList.remove("active"));
    a.classList.add("active");
    const target = tab === "home" ? el.hero
      : tab === "renders" ? el.shelfRenders
      : tab === "ideas" ? document.querySelector("#shelfIdeas")
      : tab === "templates" ? document.querySelector("#shelfTemplates")
      : null;
    if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
  });
});

// ─── Composer wiring ───────────────────────────────────────────────
el.prompt.addEventListener("input", () => {
  el.promptCount.textContent = el.prompt.value.length;
});

el.durationSlider.addEventListener("input", updateLengthDisplay);
el.presetChips.addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
  if (!chip) return;
  el.durationSlider.value = chip.dataset.preset;
  updateLengthDisplay();
});
document.querySelectorAll('input[name="resolution"]').forEach((r) =>
  r.addEventListener("change", updateEstimate)
);

el.pickFileBtn.addEventListener("click", () => el.refFile.click());
el.refFile.addEventListener("change", (e) => {
  const f = e.target.files?.[0];
  if (f) showDropPreview(f);
});
el.dropZone.addEventListener("click", (e) => {
  if (e.target === el.dropEmpty || el.dropEmpty.contains(e.target)) {
    if (e.target.tagName !== "BUTTON") el.refFile.click();
  }
});
["dragenter", "dragover"].forEach((evt) =>
  el.dropZone.addEventListener(evt, (e) => {
    e.preventDefault();
    el.dropZone.classList.add("dragover");
  })
);
["dragleave", "drop"].forEach((evt) =>
  el.dropZone.addEventListener(evt, (e) => {
    e.preventDefault();
    el.dropZone.classList.remove("dragover");
  })
);
el.dropZone.addEventListener("drop", (e) => {
  const f = e.dataTransfer.files?.[0];
  if (f && f.type.startsWith("image/")) {
    const dt = new DataTransfer();
    dt.items.add(f);
    el.refFile.files = dt.files;
    showDropPreview(f);
  }
});
el.clearRefBtn.addEventListener("click", showDropEmpty);

// ─── Submit ────────────────────────────────────────────────────────
// ---- Two-stage render flow -----------------------------------------
// Stage 1: user submits prompt → we call /api/plan for candidates+storyboard
// Stage 2: user picks ref + edits scenes → we submit /api/generate
//
// pendingPlan holds the data between stages so the approval modal can render.
let pendingPlan = null;
let pendingUpload = null; // File object if user uploaded a reference

el.renderForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const prompt = el.prompt.value.trim();
  if (!prompt) return;

  const duration = currentDuration();
  const scenes = sceneCount(duration);
  if (scenes > 6) {
    const ok = window.confirm(
      `This will render ${scenes} sequential scenes (${formatDuration(duration)} of video).\n\n` +
      `Estimated cost: ${el.estCost.textContent}\n` +
      `Estimated wait: ${el.estWait.textContent}\n\n` +
      `Continue?`
    );
    if (!ok) return;
  }

  // Auth check: if the backend requires auth but the user isn't signed in,
  // close the composer and open the auth modal instead of submitting.
  if (authRequired && !currentUser) {
    closeModal(el.composerModal);
    setTimeout(() => openAuthModal("signin"), 200);
    return;
  }

  // Uploaded reference short-circuits the approval flow — user's own image
  // already answers the "which reference?" question, and they wouldn't upload
  // an image just to have us pick a different one for them.
  const uploadedFile = el.refFile.files?.[0] || null;
  if (uploadedFile) {
    pendingUpload = uploadedFile;
    pendingPlan = null;
    await submitGenerate({ prompt, duration });
    return;
  }

  // No upload → fetch preview data and show approval modal.
  pendingUpload = null;
  el.submitBtn.disabled = true;
  const origLabel = el.submitBtn.querySelector(".btn-label").textContent;
  el.submitBtn.querySelector(".btn-label").textContent = "Building preview…";

  try {
    const fd = new FormData();
    fd.append("prompt", prompt);
    fd.append("duration", String(duration));
    const r = await authedFetch(`${API}/api/plan`, { method: "POST", body: fd });
    if (r.status === 401) {
      closeModal(el.composerModal);
      setTimeout(() => openAuthModal("signin"), 200);
      throw new Error("session expired — sign in again");
    }
    if (!r.ok) throw new Error(await r.text() || `HTTP ${r.status}`);
    pendingPlan = await r.json();
    pendingPlan._prompt = prompt;
    pendingPlan._duration = duration;
    pendingPlan._resolution = document.querySelector('input[name="resolution"]:checked').value;
    closeModal(el.composerModal);
    openPreview(pendingPlan);
  } catch (err) {
    alert("Preview failed: " + (err.message || err));
  } finally {
    el.submitBtn.disabled = false;
    el.submitBtn.querySelector(".btn-label").textContent = origLabel;
  }
});

function openPreview(plan) {
  // Show modal in loading state briefly, then swap to body when populated.
  openModal(el.previewModal);
  el.previewLoading.hidden = true;
  el.previewBody.hidden = false;

  // Header: if we detected a show title, surface it in the subtitle.
  if (plan.title) {
    el.previewSubtitle.textContent = `We think this is a "${plan.title}" episode. Pick a cast reference or skip.`;
  } else {
    el.previewSubtitle.textContent = "Pick a reference frame (optional), then approve the storyboard.";
  }

  // Reference grid.
  // Show 'More options' button whenever we detected a show — lets users
  // request fresh candidates if the first batch doesn't include anything good.
  el.previewRegenBtn.hidden = !plan.title;
  const cands = plan.candidates || [];
  if (cands.length === 0) {
    el.previewRefEmpty.hidden = false;
    el.previewRefs.hidden = true;
  } else {
    el.previewRefEmpty.hidden = true;
    el.previewRefs.hidden = false;
    renderRefTiles(cands);
  }
  renderStoryboard(plan);
}

function renderRefTiles(cands) {
  el.previewRefs.innerHTML = "";
  cands.forEach((c, idx) => {
      const div = document.createElement("div");
      div.className = "preview-ref";
      div.dataset.url = c.url;
      div.dataset.idx = idx;
      const src = c.thumbnail || c.url;
      div.innerHTML = `
        <img src="${src}" alt="reference ${idx + 1}" loading="lazy" referrerpolicy="no-referrer"
             onerror="this.parentElement.style.display='none'" />
        ${c.source === "cache" ? '<span class="badge">Saved</span>' : ""}
        <span class="check">✓</span>
      `;
      div.addEventListener("click", () => {
        el.previewRefs.querySelectorAll(".preview-ref").forEach((n) => n.classList.remove("selected"));
        div.classList.add("selected");
      });
      el.previewRefs.appendChild(div);
    });
  // NB: storyboard rendered separately in renderStoryboard() so regenerate
  // can refresh tiles without touching the storyboard.
}

function renderStoryboard(plan) {
  el.previewScenes.innerHTML = "";
  (plan.scene_prompts || []).forEach((text, idx) => {
    const dur = (plan.scenes_plan || [])[idx] || 6;
    const div = document.createElement("div");
    div.className = "preview-scene";
    div.innerHTML = `
      <div class="preview-scene-head">
        <span class="preview-scene-label">Scene ${idx + 1}</span>
        <span class="preview-scene-dur">${dur}s</span>
      </div>
      <textarea data-idx="${idx}"></textarea>
    `;
    div.querySelector("textarea").value = text;
    el.previewScenes.appendChild(div);
  });
}

// Rotate through DDG query variants each time user clicks 'More options'.
const REGEN_VARIANTS = ["group", "promo", "still", "poster", "scene"];
let regenCursor = 0;

el.previewRegenBtn.addEventListener("click", async () => {
  if (!pendingPlan || !pendingPlan.title) return;
  const variant = REGEN_VARIANTS[regenCursor % REGEN_VARIANTS.length];
  regenCursor += 1;
  el.previewRegenBtn.disabled = true;
  el.previewRegenBtn.classList.add("spinning");
  try {
    const fd = new FormData();
    fd.append("title", pendingPlan.title);
    fd.append("variant", variant);
    const r = await authedFetch(`${API}/api/plan/regenerate_refs`, { method: "POST", body: fd });
    if (!r.ok) throw new Error(await r.text() || `HTTP ${r.status}`);
    const data = await r.json();
    const cands = data.candidates || [];
    if (cands.length === 0) {
      alert("No new options found for that variant. Try again for a different set.");
    } else {
      renderRefTiles(cands);
      el.previewRefEmpty.hidden = true;
      el.previewRefs.hidden = false;
    }
  } catch (err) {
    alert("Could not fetch new options: " + (err.message || err));
  } finally {
    el.previewRegenBtn.disabled = false;
    el.previewRegenBtn.classList.remove("spinning");
  }
});

el.previewBackBtn.addEventListener("click", () => {
  closeModal(el.previewModal);
  // Restore the composer so the user can tweak their prompt.
  setTimeout(() => openModal(el.composerModal), 200);
});

el.previewApproveBtn.addEventListener("click", async () => {
  if (!pendingPlan) return;
  const selected = el.previewRefs.querySelector(".preview-ref.selected");
  const chosenRefUrl = selected ? selected.dataset.url : "";
  const editedScenes = Array.from(el.previewScenes.querySelectorAll("textarea"))
    .map((t) => t.value.trim())
    .filter(Boolean);

  el.previewApproveBtn.disabled = true;
  const origLabel = el.previewApproveBtn.querySelector(".btn-label").textContent;
  el.previewApproveBtn.querySelector(".btn-label").textContent = "Submitting…";

  try {
    await submitGenerate({
      prompt: pendingPlan._prompt,
      duration: pendingPlan._duration,
      resolution: pendingPlan._resolution,
      chosenRefUrl,
      chosenScenes: editedScenes.length === pendingPlan.scenes_plan.length ? editedScenes : null,
    });
    closeModal(el.previewModal);
    pendingPlan = null;
  } catch (err) {
    alert("Show failed: " + (err.message || err));
  } finally {
    el.previewApproveBtn.disabled = false;
    el.previewApproveBtn.querySelector(".btn-label").textContent = origLabel;
  }
});

async function submitGenerate({ prompt, duration, resolution, chosenRefUrl, chosenScenes }) {
  const fd = new FormData();
  fd.append("prompt", prompt);
  fd.append("duration", String(duration));
  fd.append("resolution", resolution || document.querySelector('input[name="resolution"]:checked').value);
  if (pendingUpload) fd.append("reference", pendingUpload);
  if (chosenRefUrl) fd.append("chosen_ref_url", chosenRefUrl);
  if (chosenScenes) fd.append("chosen_scenes", JSON.stringify(chosenScenes));

  const r = await authedFetch(`${API}/api/generate`, { method: "POST", body: fd });
  if (r.status === 401) {
    closeModal(el.composerModal);
    closeModal(el.previewModal);
    setTimeout(() => openAuthModal("signin"), 200);
    throw new Error("session expired — sign in again");
  }
  if (r.status === 402) {
    // Out of credits — send them to pricing rather than a generic error toast.
    let msg = "You don’t have enough credits for this render.";
    try {
      const body = await r.json();
      if (body && body.detail && body.detail.message) msg = body.detail.message;
    } catch (_) { /* body wasn't JSON */ }
    if (window.confirm(msg + "\n\nOpen pricing to add credits?")) {
      window.location.href = "/pricing.html";
    }
    throw new Error(msg);
  }
  if (!r.ok) throw new Error(await r.text() || `HTTP ${r.status}`);
  await r.json();

  // Reset composer state and jump to "Now Rendering" shelf.
  closeModal(el.composerModal);
  el.prompt.value = "";
  el.prompt.dispatchEvent(new Event("input"));
  showDropEmpty();
  pendingUpload = null;

  // Prominent post-submit notice: reassure the user they can leave and
  // an email will land when the render is ready. Personalize with their
  // Google-linked address and a duration-aware ETA.
  try {
    if (el.renderStartedEmail) {
      el.renderStartedEmail.textContent = (currentUser && currentUser.email) || "your inbox";
    }
    if (el.renderStartedEta) {
      const d = currentDuration();
      let eta = "3–6 minutes";
      if (d > 60) eta = "10–30 minutes";
      if (d > 300) eta = "30–60 minutes";
      if (d > 900) eta = "1–2 hours";
      el.renderStartedEta.textContent = eta;
    }
    openModal(el.renderStartedModal);
  } catch (e) { /* non-fatal: modal is optional decoration */ }

  setTimeout(() => {
    refreshJobs().then(() => {
      el.shelfActive.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }, 260);
}

// ─── Detail modal actions ──────────────────────────────────────────
el.detailRemix.addEventListener("click", () => {
  if (!currentDetailJob) return;
  closeModal(el.detailModal);
  setTimeout(() => openComposer(currentDetailJob.prompt), 260);
});

// ─── Rendering: hero ───────────────────────────────────────────────
function firstLine(prompt) {
  if (!prompt) return "Untitled render";
  const cleaned = prompt.replace(/\s+/g, " ").trim();
  return cleaned.length > 90 ? cleaned.slice(0, 87) + "…" : cleaned;
}
// Hero titles want to read like a show name - take the first sentence,
// or the first clause (up to the first comma), whichever is shorter and
// non-trivial. Cap at ~60 chars so the marquee never eats the fold.
function heroTitleFrom(prompt) {
  if (!prompt) return "Untitled render";
  const cleaned = prompt.replace(/\s+/g, " ").trim();
  const stop = cleaned.search(/[.!?]/);
  const comma = cleaned.indexOf(",");
  let candidate = cleaned;
  if (stop > 8) candidate = cleaned.slice(0, stop);
  if (comma > 12 && comma < candidate.length) candidate = candidate.slice(0, comma);
  candidate = candidate.trim();
  if (candidate.length < 6) candidate = cleaned;
  if (candidate.length > 60) candidate = candidate.slice(0, 57).replace(/\s+\S*$/, "") + "…";
  return candidate;
}
function renderMeta(job) {
  const bits = [];
  const isActive = job.status !== "done" && job.status !== "failed";
  if (isActive) {
    bits.push(`<span class="chip live">● ${escapeHtml(job.status.toUpperCase())}</span>`);
    if (job.scene_total > 1) {
      bits.push(`<span class="chip">Scene ${job.scene_index || 0} / ${job.scene_total}</span>`);
    }
  } else if (job.status === "failed") {
    bits.push(`<span class="chip" style="color:var(--danger);border-color:rgba(255,85,99,.35);">FAILED</span>`);
  } else {
    bits.push(`<span class="chip">HD</span>`);
  }
  bits.push(`<span class="chip">${formatDuration(job.duration)}</span>`);
  bits.push(`<span class="chip">${escapeHtml(job.resolution || "768P")}</span>`);
  if (job.finished_at) {
    bits.push(`<span class="chip">${timeAgo(job.finished_at)}</span>`);
  }
  return bits.join(" ");
}

function pickHeroJob(list) {
  // Prefer an active job (drama). Else the newest "rich" finished render
  // (long prompt AND longer duration) so the marquee never lands on a short
  // one-line test clip. Fall back to newest finished if nothing qualifies.
  const active = list.find((j) => j.status !== "done" && j.status !== "failed");
  if (active) return active;
  const finished = list.filter((j) => j.status === "done" && j.video);
  finished.sort((a, b) => (b.finished_at || 0) - (a.finished_at || 0));
  const rich = finished.find((j) => (j.prompt || "").length > 40 || (j.duration || 0) >= 10);
  return rich || finished[0] || null;
}
function renderHero() {
  // Hero is permanently pinned to the first showcase clip.
  // User renders never take over the hero; they appear on the shelves below.
  heroJob = null;
  el.hero.classList.remove("rendering");
  el.heroEyebrow.textContent = "The Studio";
  el.heroTitle.innerHTML = "Turn Anything<br/>You can Imagine<br/>Into a Show";
  el.heroInfoBtn.style.display = "none";
  const heroClip = (typeof SHOWCASE_JOBS !== "undefined" && SHOWCASE_JOBS[0] && SHOWCASE_JOBS[0].video)
    ? SHOWCASE_JOBS[0].video
    : "static/showcase/showcase_sitcom.mp4";
  if (el.heroVideo.getAttribute("src") !== heroClip) {
    el.heroVideo.src = heroClip;
    el.heroVideo.load();
  }
  // iOS Safari won't autoplay unless we explicitly call play() after load,
  // and the element must be muted + playsinline (both set in the markup).
  // Wrap in a try/catch: some browsers reject the promise on gesture policy
  // violations, and we don't want an unhandled rejection.
  el.heroVideo.muted = true;
  el.heroVideo.playsInline = true;
  el.heroVideo.setAttribute("playsinline", "");
  el.heroVideo.setAttribute("webkit-playsinline", "");
  const tryPlay = () => {
    const p = el.heroVideo.play();
    if (p && typeof p.catch === "function") p.catch(() => {});
  };
  tryPlay();
  // If the browser refused (common on iOS when the video isn't loaded yet),
  // retry once the browser signals it can play.
  el.heroVideo.addEventListener("canplay", tryPlay, { once: true });
  // And retry on the first user gesture anywhere on the page — tap, scroll,
  // or touch — which unlocks autoplay for the rest of the session.
  const gestureUnlock = () => {
    tryPlay();
    ["touchstart", "click", "scroll"].forEach(ev =>
      document.removeEventListener(ev, gestureUnlock, { capture: true }));
  };
  ["touchstart", "click", "scroll"].forEach(ev =>
    document.addEventListener(ev, gestureUnlock, { capture: true, once: true, passive: true }));
  el.heroPlayBtn.querySelector("span").textContent = "Create a Show";
  el.heroPlayBtn.onclick = () => openComposer();
}

// ─── Rendering: shelves ────────────────────────────────────────────
// Per-scene row inside the rendering tile. Shows an honest per-scene status
// with real elapsed time on running scenes. Data comes from the backend's
// scenes_state array — no per-scene fabrication on the frontend.
function sceneRowHTML(s, job) {
  const status = (s.status || "queued").toLowerCase();
  const label = (() => {
    if (status === "queued") return "Queued";
    if (status === "submitting") return "Submitting…";
    if (status === "running") {
      if (s.started_at) {
        const elapsed = Math.max(0, Math.round((Date.now() / 1000) - s.started_at));
        return `Rendering · ${elapsed}s`;
      }
      return "Rendering…";
    }
    if (status === "succeeded") {
      if (s.started_at && s.finished_at && s.finished_at > s.started_at) {
        const dur = Math.round(s.finished_at - s.started_at);
        return `Done · ${dur}s`;
      }
      return "Done";
    }
    if (status === "failed") return "Failed";
    return status;
  })();
  const dot = `<span class="scene-row-dot scene-row-dot-${status}"></span>`;
  const durLabel = `${s.duration}s`;
  return `
    <div class="scene-row scene-row-${status}" data-scene-idx="${s.index}" data-scene-started="${s.started_at || 0}">
      ${dot}
      <span class="scene-row-idx">Scene ${s.index + 1}</span>
      <span class="scene-row-dur">${durLabel}</span>
      <span class="scene-row-label">${escapeHtml(label)}</span>
    </div>
  `;
}

function renderTile(job) {
  const isActive = job.status !== "done" && job.status !== "failed";
  const isFailed = job.status === "failed";

  if (isActive) {
    // Progress model:
    //   - Base fraction from completed scenes (0/N … (N-1)/N).
    //   - Add the current scene's fraction, computed from wall-clock time
    //     against the backend's per-scene ETA. Caps at 95% until we get
    //     a real done/failed status so users never see 100% while waiting.
    //   - Falls back to 8% shimmer before the first scene reports running.
    const total = job.scene_total || 1;
    const idx = Math.max(0, (job.scene_index || 1) - 1); // 0-based
    // ==== HONEST PROGRESS ====
    // Overall bar = scenes done / total. NEVER let per-scene fraction inflate
    // the top-line bar — that's what made it read "almost done" during scene 1
    // of 7. Individual scene progress lives in the scene list below.
    const done = Number(job.scene_done || 0);
    const failed = Number(job.scene_failed || 0);
    const running = Number(job.scene_running || 0);
    // We fill the bar based on completed work. Running scenes push it a small
    // amount so it's not literally stuck at 0% while 4 scenes render in parallel.
    const runningBoost = Math.min(running, total) * 0.15; // half a scene, capped
    const overallFrac = Math.min(0.98, (done + runningBoost) / total);
    const pct = Math.max(2, Math.round(overallFrac * 100));
    const isStitching = job.status === "stitching";
    const finalPct = isStitching ? 96 : pct;

    // Aggregate ETA: use median per-scene ETA * (remaining scenes / concurrency).
    // reAPI concurrency is 4; slowest-scene wall-clock model gives an honest bound.
    const scenesState = Array.isArray(job.scenes_state) ? job.scenes_state : [];
    const perSceneEta = scenesState.length
      ? Math.max(30, ...scenesState.map((s) => s.eta_seconds || 60))
      : 60;
    const remainingScenes = Math.max(0, total - done);
    // Concurrency bound = 4 (matches SCENE_CONCURRENCY backend default).
    // While parallel, wall clock ≈ ceil(remaining / 4) * per-scene ETA.
    const parallelBatches = Math.max(1, Math.ceil(remainingScenes / 4));
    const totalEtaSec = isStitching ? 15 : parallelBatches * perSceneEta;
    // Best signal for time remaining: max(finished_at across done scenes) is
    // when the last thing finished; but we don't need this precision for the
    // header — the per-scene rows tell the detailed story. Use a simple
    // "~N min remaining" header.
    const etaHeader = (() => {
      if (isStitching) return "Finalizing your video…";
      if (done === total && total > 0) return "Stitching scenes together…";
      const mins = totalEtaSec / 60;
      if (mins < 1) return `~${Math.max(15, Math.round(totalEtaSec))}s remaining`;
      if (mins < 2) return `~1 min remaining`;
      return `~${Math.round(mins)} min remaining`;
    })();

    // Header counter
    const isExpanding = job.expansion_status === "expanding";
    const headerCounter = isExpanding
      ? `Casting characters and writing scene descriptions…`
      : (total > 1
        ? `${done} of ${total} scenes complete · ${running} rendering${failed ? ` · ${failed} failed` : ""}`
        : (job.scene_status === "running" ? "Rendering…" : "Preparing…"));

    // Small chip surfacing the character bible if we have one — answers
    // the question 'is the model actually going to know what Cartman looks like'
    const charChip = job.expansion_characters
      ? `<div class="tile-active-charbible" title="${escapeHtml(job.expansion_characters)}">🎭 ${escapeHtml(job.expansion_characters.slice(0, 90))}${job.expansion_characters.length > 90 ? "…" : ""}</div>`
      : "";

    // Per-scene rows. For single-scene renders we skip the list — no value.
    const sceneRows = (total > 1 && scenesState.length)
      ? scenesState.map((s) => sceneRowHTML(s, job)).join("")
      : "";

    return `
      <div class="tile tile-active" data-id="${escapeHtml(job.id)}">
        <span class="tile-badge rendering">${escapeHtml(job.status.toUpperCase())}</span>
        <div class="tile-active-body">
          <div class="tile-active-header">
            <div class="tile-active-prompt">${escapeHtml(firstLine(job.prompt))}</div>
            <div class="tile-active-scene">${escapeHtml(headerCounter)}${isExpanding ? "" : ` · <span class="tile-active-eta">${escapeHtml(etaHeader)}</span>`}</div>
          </div>
          ${charChip}
          ${sceneRows ? `<div class="tile-active-scenes">${sceneRows}</div>` : ""}
          <div class="tile-active-progress">
            <div class="tile-active-progress-fill" style="width:${finalPct}%"></div>
          </div>
        </div>
      </div>
    `;
  }
  if (isFailed) {
    return `
      <div class="tile tile-active" data-id="${escapeHtml(job.id)}" data-open="1">
        <span class="tile-badge failed">FAILED</span>
        <div class="tile-active-body">
          <div class="tile-active-prompt">${escapeHtml(firstLine(job.prompt))}</div>
          <div class="tile-active-scene" style="color:var(--danger);">Tap to see error</div>
        </div>
      </div>
    `;
  }
  // Finished
  return `
    <div class="tile" data-id="${escapeHtml(job.id)}" data-open="1">
      <video class="tile-video" src="${escapeHtml(job.video)}#t=0.8" muted preload="metadata" playsinline></video>
      <div class="tile-gradient"></div>
      <span class="tile-badge">${escapeHtml(formatDuration(job.duration))}</span>
      <button class="tile-download" data-download="1" data-url="${escapeHtml(job.video)}" data-id="${escapeHtml(job.id)}" title="Download" aria-label="Download video">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v12"/><path d="M6 12l6 6 6-6"/><path d="M5 21h14"/></svg>
      </button>
      <div class="tile-play">
        <svg viewBox="0 0 24 24" fill="white"><path d="M8 5v14l11-7z"/></svg>
      </div>
      <div class="tile-overlay">
        <div class="tile-title">${escapeHtml(firstLine(job.prompt))}</div>
        <div class="tile-meta">
          <span>${escapeHtml(job.resolution || "768P")}</span>
          <span class="dot"></span>
          <span>${timeAgo(job.finished_at)}</span>
        </div>
      </div>
    </div>
  `;
}

// Hover preview: play the tile video muted while hovered
function wireTileHover(container) {
  container.addEventListener("mouseover", (e) => {
    const v = e.target.closest(".tile")?.querySelector(".tile-video");
    if (v) { v.currentTime = 0.8; v.play().catch(() => {}); }
  });
  container.addEventListener("mouseout", (e) => {
    const t = e.target.closest(".tile");
    if (t && !t.contains(e.relatedTarget)) {
      const v = t.querySelector(".tile-video");
      if (v) v.pause();
    }
  });
}

function renderShelves() {
  const active = jobs.filter((j) => j.status !== "done" && j.status !== "failed");
  const finished = jobs.filter((j) => j.status === "done" || j.status === "failed");

  // Featured (always visible, always the curated set)
  el.featuredRow.innerHTML = SHOWCASE_JOBS.map(renderTile).join("");

  // Active: shown when signed in AND has active jobs
  if (active.length && (currentUser || !authRequired)) {
    el.shelfActive.hidden = false;
    el.activeCount.textContent = active.length;
    el.activeRow.innerHTML = active.map(renderTile).join("");
  } else {
    el.shelfActive.hidden = true;
  }

  // My Library: hidden when auth required + signed out
  if (authRequired && !currentUser) {
    el.shelfRenders.hidden = true;
    return;
  }
  el.shelfRenders.hidden = false;

  // Merge in-memory 'finished' jobs (fresh from this session) with durable
  // library renders (persist across deploys). Library rows come from
  // /api/library and are keyed by job_id, so we dedupe by id and prefer the
  // in-memory job when both exist (in-memory has richer status info during
  // the brief post-render window). Library-only rows are shown as static
  // done tiles using their signed video URL.
  const inMemoryIds = new Set(finished.map((j) => j.id));
  const libraryOnly = (libraryRenders || [])
    .filter((r) => !inMemoryIds.has(r.id))
    .map((r) => ({
      id: r.id,
      status: "done",
      video: r.video_url,
      thumb: r.thumb_url,
      prompt: r.prompt || "",
      duration: r.duration || 0,
      scenes: Array.from({ length: r.scene_count || 1 }, () => Math.max(1, Math.floor((r.duration || 0) / (r.scene_count || 1)))),
      finished_at: r.created_at ? new Date(r.created_at).getTime() / 1000 : 0,
      created_at: r.created_at ? new Date(r.created_at).getTime() / 1000 : 0,
      saved_to_library: true,
    }));
  const combined = [...finished, ...libraryOnly];
  if (combined.length) {
    el.rendersEmpty.hidden = true;
    el.rendersRow.innerHTML = combined
      .sort((a, b) => (b.finished_at || b.created_at || 0) - (a.finished_at || a.created_at || 0))
      .map(renderTile).join("");
  } else {
    el.rendersEmpty.hidden = false;
    el.rendersRow.querySelectorAll(".tile").forEach((t) => t.remove());
  }
}

async function refreshLibrary() {
  // Fetch the caller's durable renders. No-op when signed out. Silent on
  // failure so a bad response never blocks the rest of the UI — the user
  // still sees in-memory finished jobs and the Featured shelf.
  if (authRequired && !currentUser) {
    libraryRenders = [];
    return;
  }
  try {
    const r = await authedFetch(`${API}/api/library`);
    if (!r.ok) {
      // 401 during sign-in bounce is expected; suppress the log noise.
      if (r.status !== 401) console.warn("library fetch failed", r.status);
      return;
    }
    const data = await r.json();
    libraryRenders = Array.isArray(data.renders) ? data.renders : [];
    renderShelves();
  } catch (err) {
    console.warn("library fetch exception", err);
  }
}

// ─── Tile click wiring (delegated) ─────────────────────────────────
document.addEventListener("click", (e) => {
  // Download button — handle first so it wins over tile-open
  const dl = e.target.closest("[data-download]");
  if (dl) {
    e.stopPropagation();
    e.preventDefault();
    const url = dl.dataset.url;
    const id = dl.dataset.id || "clip";
    if (!url) return;
    downloadVideo(url, `nothollywood-${id}.mp4`);
    return;
  }
  const tile = e.target.closest(".tile[data-id]");
  if (!tile) return;
  if (tile.dataset.open !== "1") return;
  const id = tile.dataset.id;
  // Look up in every possible source: active jobs, showcase clips, and
  // durable library rows. Without this, the featured shelf tiles — which
  // aren't in the user's jobs[] array — silently do nothing on click.
  let job = jobs.find((j) => j.id === id);
  if (!job && typeof SHOWCASE_JOBS !== "undefined") {
    job = SHOWCASE_JOBS.find((j) => j.id === id);
  }
  if (!job) {
    const libRow = (libraryRenders || []).find((r) => r.id === id);
    if (libRow) {
      job = {
        id: libRow.id,
        status: "done",
        video: libRow.video_url,
        prompt: libRow.prompt || "",
        duration: libRow.duration || 0,
        resolution: libRow.resolution || "768P",
        finished_at: libRow.created_at ? new Date(libRow.created_at).getTime() / 1000 : 0,
      };
    }
  }
  if (job) openDetail(job);
});

// Force a real download (fetch as blob so browsers don't just play the mp4)
async function downloadVideo(url, filename) {
  try {
    const res = await fetch(url, { credentials: "same-origin" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const blob = await res.blob();
    const objUrl = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = objUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(objUrl), 1500);
  } catch (err) {
    // Fallback: open in a new tab (user can right-click → save)
    console.warn("Blob download failed, falling back to link", err);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.target = "_blank";
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    a.remove();
  }
}

// ─── Ideas shelf (curated prompts) ─────────────────────────────────
const IDEAS = [
  {
    cat: "COMEDY",
    title: "The Zoom That Ruins Everything",
    hint: "A CEO forgets to mute during a very human moment. Full body language, no dialogue needed.",
    prompt: "A stern CEO in a home office is on a Zoom call, then loudly slurps coffee thinking he's muted, then panics as everyone reacts. Handheld, comedic timing, 4pm lighting.",
    a: "#3a1a5a", b: "#0a0a2a",
  },
  {
    cat: "NOIR",
    title: "Detective Under the Fluorescents",
    hint: "1970s NYC. A rain-slicked detective interrogates a suspect. Chiaroscuro, cigarette smoke.",
    prompt: "A weary 1970s NYC detective leans across a metal table in a fluorescent-lit interrogation room, rain streaking the frosted glass, cigarette smoke curling up. Tight two-shot, film grain, low angle. He says, 'You were there. I know you were there.'",
    a: "#1a1a2a", b: "#0a0a0a",
  },
  {
    cat: "NATURE",
    title: "Golden Hour Cheetah Sprint",
    hint: "Slow-mo cheetah at full stride across the Serengeti. Every muscle visible.",
    prompt: "A cheetah at full sprint across the Serengeti at golden hour, slow motion, low tracking shot with the dust kicking up, every muscle rippling. Cinematic wildlife documentary style, David Attenborough color grade.",
    a: "#3a2a0a", b: "#5a1a0a",
  },
  {
    cat: "SCI-FI",
    title: "The Signal from Europa",
    hint: "First contact from Jupiter's moon. Mission control watches the screen turn on.",
    prompt: "Mission control, dim blue light. A young scientist stares at a monitor as pixel by pixel an image resolves ,  the surface of Europa, then something moving under the ice. The room goes silent. Wide slow push in, sound design of one heartbeat.",
    a: "#0a1a3a", b: "#0a0a1a",
  },
  {
    cat: "DRAMA",
    title: "A Father's First Apology",
    hint: "A gruff father knocks on his adult son's door with a shoebox of old letters.",
    prompt: "A gruff older man in a work jacket stands on a suburban porch at dusk, holding a shoebox of old letters. His grown son opens the door. The father says, quietly, 'I should have said this a long time ago.' Warm porch light, shallow depth of field, real emotion.",
    a: "#2a1a0a", b: "#1a0a0a",
  },
  {
    cat: "ACTION",
    title: "Rooftop Chase Over Marrakech",
    hint: "A parkour rooftop chase over the medina at sunset. Doves scattering.",
    prompt: "Sunset over the Marrakech medina. A parkour chase across terracotta rooftops, doves scattering, a leap over an alley. Handheld with a wide lens, dust and warm haze, colorful laundry lines flapping. High energy.",
    a: "#5a2a0a", b: "#3a1a0a",
  },
  {
    cat: "MUSICAL",
    title: "Diner Waltz at 3AM",
    hint: "A lone waitress and a jukebox. She starts to dance to a song only she can hear.",
    prompt: "A 1950s diner at 3am, empty except for a young waitress in a mint uniform. A jukebox glows. She starts to slow dance with the coffee pot as the neon buzzes. Warm cinematic key light, dolly-in through the window.",
    a: "#3a0a3a", b: "#2a0a1a",
  },
  {
    cat: "MYSTERY",
    title: "The Postcard That Wasn't Sent",
    hint: "A woman finds a stack of postcards addressed to her ,  but never mailed.",
    prompt: "A young woman opens a drawer in her late grandmother's attic and finds a rubber-banded stack of vintage postcards, all addressed to her, none of them stamped. She reads the first one. Dust motes in the sunlight, close on her face as her expression shifts.",
    a: "#1a2a2a", b: "#0a1a1a",
  },
];
function renderIdeas() {
  el.ideasRow.innerHTML = IDEAS.map((i) => `
    <div class="tile tile-idea" data-idea="1" data-prompt="${escapeHtml(i.prompt)}" style="--idea-a:${i.a};--idea-b:${i.b};">
      <div class="tile-idea-body">
        <div class="tile-idea-cat">${escapeHtml(i.cat)}</div>
        <div>
          <div class="tile-idea-title">${escapeHtml(i.title)}</div>
        </div>
        <div class="tile-idea-hint">${escapeHtml(i.hint)}</div>
      </div>
    </div>
  `).join("");
}
document.addEventListener("click", (e) => {
  const t = e.target.closest(".tile-idea");
  if (!t) return;
  openComposer(t.dataset.prompt);
});

// ─── Templates shelf ───────────────────────────────────────────────
const TEMPLATES = [
  { value: "6", unit: "sec", desc: "Quick single-shot test. Cheapest run.", pre: 6 },
  { value: "10", unit: "sec", desc: "One full-length H3 shot with room to breathe.", pre: 10 },
  { value: "30", unit: "sec", desc: "Three linked scenes. Perfect for a spot or teaser.", pre: 30 },
  { value: "1", unit: "min", desc: "Six scenes stitched. A cold open or short skit.", pre: 60 },
  { value: "3", unit: "min", desc: "18 scenes. A full short film beat.", pre: 180 },
  { value: "5", unit: "min", desc: "30 scenes. TV pilot cold open territory.", pre: 300 },
  { value: "10", unit: "min", desc: "60 scenes. A full sitcom episode.", pre: 600 },
  { value: "20", unit: "min", desc: "120 scenes. Half-hour sitcom without ads.", pre: 1200 },
  { value: "30", unit: "min", desc: "180 scenes. A full network-length episode.", pre: 1800 },
  { value: "1", unit: "hr", desc: "360 scenes. A drama-length episode. Max runtime.", pre: 3600 },
];
function renderTemplates() {
  el.templatesRow.innerHTML = TEMPLATES.map((t) => `
    <div class="tile tile-template" data-template="1" data-pre="${t.pre}">
      <div class="tile-template-body">
        <div>
          <div class="tile-template-value">${escapeHtml(t.value)}<span style="font-size:20px; margin-left:4px; color:var(--text-dim);">${escapeHtml(t.unit)}</span></div>
          <div class="tile-template-unit">${t.pre >= 60 ? Math.ceil(t.pre / 10) + " scenes" : "single shot"}</div>
        </div>
        <div class="tile-template-desc">${escapeHtml(t.desc)}</div>
      </div>
    </div>
  `).join("");
}
document.addEventListener("click", (e) => {
  const t = e.target.closest(".tile-template");
  if (!t) return;
  const d = Number(t.dataset.pre);
  el.durationSlider.value = d;
  updateLengthDisplay();
  openComposer();
});

// ─── Data ──────────────────────────────────────────────────────────
// Showcase/filler renders shown until the real backend responds. These
// reference the sample videos bundled in /videos/ so the hero and library
// feel populated even in a static preview.
const SHOWCASE_JOBS = [
  {
    id: "showcase-sitcom",
    status: "done",
    prompt: "Classic '90s three-camera sitcom. Two friends in a small NYC apartment kitchen argue about proper housewarming etiquette. Studio audience laughter.",
    duration: 8,
    resolution: "1080P",
    video: "static/showcase/showcase_sitcom.mp4",
    created_at: Math.floor(Date.now() / 1000) - 3600 * 2,
    finished_at: Math.floor(Date.now() / 1000) - 3600 * 2,
  },
  {
    id: "showcase-animated",
    status: "done",
    prompt: "Prime-time animated family sitcom. Dad gets ambushed on the couch by an excited puppy while mom laughs. Flat colors, thick outlines, bouncy motion.",
    duration: 8,
    resolution: "1080P",
    video: "static/showcase/showcase_animated.mp4",
    created_at: Math.floor(Date.now() / 1000) - 3600 * 6,
    finished_at: Math.floor(Date.now() / 1000) - 3600 * 6,
  },
  {
    id: "showcase-truecrime",
    status: "done",
    prompt: "True-crime documentary cold open. Rain-slicked suburban street at 3am, yellow police tape across a porch, somber narrator sets up the case. Filmic grain, cool desaturated grade.",
    duration: 8,
    resolution: "1080P",
    video: "static/showcase/showcase_truecrime.mp4",
    created_at: Math.floor(Date.now() / 1000) - 3600 * 18,
    finished_at: Math.floor(Date.now() / 1000) - 3600 * 18,
  },
  {
    id: "showcase-scifi",
    status: "done",
    prompt: "Sci-fi starship bridge. Captain in red stands center-frame as the science officer reports an unidentified vessel with an unknown power signature. Orchestral score swells. Cinematic sci-fi lighting.",
    duration: 8,
    resolution: "1080P",
    video: "static/showcase/showcase_scifi.mp4",
    created_at: Math.floor(Date.now() / 1000) - 3600 * 26,
    finished_at: Math.floor(Date.now() / 1000) - 3600 * 26,
  },
];

async function refreshJobs() {
  // Anonymous users don't fetch /api/jobs at all — only see the curated Featured shelf.
  if (authRequired && !currentUser) {
    jobs = [];
    renderHero();
    renderShelves();
    return;
  }
  try {
    const r = await authedFetch(`${API}/api/jobs`);
    if (!r.ok) throw new Error("failed");
    const remote = await r.json();
    const priorActiveIds = new Set(
      (jobs || []).filter((j) => j.status !== "done" && j.status !== "failed").map((j) => j.id)
    );
    jobs = Array.isArray(remote) ? remote : [];
    // If any job just transitioned out of the active set, credits may have
    // changed (charged on render, refunded on failure). Repaint the chip.
    const stillActiveIds = new Set(
      jobs.filter((j) => j.status !== "done" && j.status !== "failed").map((j) => j.id)
    );
    let flipped = false;
    for (const id of priorActiveIds) {
      if (!stillActiveIds.has(id)) { flipped = true; break; }
    }
    if (flipped) {
      refreshCredits();
      // A job just finished — pull the durable library so the tile keeps
      // pointing at the persisted MP4 even after the in-memory job ages out.
      refreshLibrary();
    }
    renderHero();
    renderShelves();
    schedulePoll();
    startProgressTick();
  } catch (err) {
    if (!jobs) jobs = [];
    renderHero();
    renderShelves();
    console.warn("refresh failed", err);
  }
}
function schedulePoll() {
  if (pollTimer) clearTimeout(pollTimer);
  // Always keep a heartbeat — fast when a render is active, slow when idle.
  // Previously we killed the poll when no active jobs remained; if the tab
  // was throttled at the moment a render finished, the tile stayed showing
  // 'Scene 1 rendering' forever until the user navigated. Now we always poll.
  const hasActive = jobs.some((j) => j.status !== "done" && j.status !== "failed");
  pollTimer = setTimeout(refreshJobs, hasActive ? POLL_MS : IDLE_POLL_MS);
}

// Tab focus / visibility handler: browsers throttle background timers,
// so when the tab comes back to the foreground we force an immediate
// refresh instead of waiting up to POLL_MS for the next scheduled tick.
// This fixes: 'came back from another tab and the tile still says rendering
// even though the email arrived'.
function installVisibilityRefresh() {
  const forceRefresh = () => {
    if (document.visibilityState === "visible") {
      refreshJobs();
    }
  };
  document.addEventListener("visibilitychange", forceRefresh);
  window.addEventListener("focus", forceRefresh);
  window.addEventListener("pageshow", forceRefresh);
  // Deep-link handoff: when the user clicks the email button while the app
  // is already open in another tab, the browser reuses that tab and just
  // updates the hash. We listen for the hash change so we can scroll to and
  // highlight the tile without a full reload.
  window.addEventListener("hashchange", () => {
    refreshJobs().then(() => handleDeepLink());
  });
}

// Parse '#job-<id>' from the URL and scroll that job's tile into view.
// If the tile isn't in the DOM yet (still rendering, or the user just
// arrived), we wait one poll cycle. Idempotent — safe to call multiple times.
function handleDeepLink() {
  const hash = window.location.hash || "";
  const match = hash.match(/^#job-([A-Za-z0-9\-_]+)/);
  if (!match) return;
  const jobId = match[1];
  // Try immediately; if not found, try once more after the next refresh.
  const tryScroll = () => {
    const tile = document.querySelector(`.tile[data-id="${jobId}"]`);
    if (!tile) return false;
    tile.scrollIntoView({ behavior: "smooth", block: "center" });
    tile.classList.add("deep-link-highlight");
    setTimeout(() => tile.classList.remove("deep-link-highlight"), 2400);
    // Try to auto-open the video player if the tile has a click handler.
    // The tile itself is clickable in this app; a synthetic click keeps
    // behavior consistent with a user tap.
    tile.click();
    return true;
  };
  if (!tryScroll()) {
    setTimeout(tryScroll, 1500);
  }
}

// Between full poll cycles, tick the progress bar and ETA text every 250ms
// so the bar visibly advances instead of jumping every 3 seconds. Only touches
// the visible progress-fill width and the ETA suffix — the tile itself isn't
// re-rendered, so users can still interact with links/buttons on it.
let tickTimer = null;
function startProgressTick() {
  if (tickTimer) return;
  tickTimer = setInterval(() => {
    const active = jobs.filter((j) => j.status !== "done" && j.status !== "failed");
    if (!active.length) {
      clearInterval(tickTimer);
      tickTimer = null;
      return;
    }
    for (const job of active) {
      const tile = document.querySelector(`.tile-active[data-id="${job.id}"]`);
      if (!tile) continue;
      // Update the elapsed counter on each running scene row without
      // re-rendering the tile — that lets the top-line poll happen every 3s
      // while these tick smoothly at 4Hz.
      const runningRows = tile.querySelectorAll(".scene-row-running");
      const nowSec = Date.now() / 1000;
      runningRows.forEach((row) => {
        const startedAt = Number(row.dataset.sceneStarted || 0);
        if (!startedAt) return;
        const elapsed = Math.max(0, Math.round(nowSec - startedAt));
        const labelEl = row.querySelector(".scene-row-label");
        if (labelEl) labelEl.textContent = `Rendering · ${elapsed}s`;
      });
    }
  }, 250);
}

// ─── Init ───────────────────────────────────────────────────────────
// Auth modal wiring — Google SSO only.
function openAuthModal() {
  el.authError.hidden = true;
  el.authError.textContent = "";
  openModal(el.authModal);
}

el.navSignInBtn.addEventListener("click", () => openAuthModal());
el.navSignOutBtn.addEventListener("click", async () => {
  if (!sb) return;
  await sb.auth.signOut();
});

// After the OAuth round-trip Supabase drops a `#access_token=...` fragment on
// the URL and detectSessionInUrl parses it. We stash the pending composer
// prompt in sessionStorage so a template/idea click that triggered the sign-in
// survives the full-page redirect back from Google.
async function signInWithGoogle() {
  if (!sb) {
    el.authError.hidden = false;
    el.authError.textContent = "Auth is still initializing — give it a second and try again.";
    return;
  }
  try {
    if (pendingComposerPrompt) {
      sessionStorage.setItem("pendingComposerPrompt", pendingComposerPrompt);
    } else {
      sessionStorage.removeItem("pendingComposerPrompt");
    }
  } catch (_) { /* private mode / storage disabled — continue */ }

  el.googleSignInBtn.disabled = true;
  el.googleSignInBtn.querySelector(".btn-google-label").textContent = "Redirecting to Google…";
  el.authError.hidden = true;

  try {
    const { error } = await sb.auth.signInWithOAuth({
      provider: "google",
      options: {
        redirectTo: window.location.origin + "/",
        queryParams: { access_type: "offline", prompt: "select_account" },
      },
    });
    if (error) throw error;
    // signInWithOAuth returns after kicking off the redirect. Browser will
    // navigate to accounts.google.com next tick — nothing more to do here.
  } catch (err) {
    el.googleSignInBtn.disabled = false;
    el.googleSignInBtn.querySelector(".btn-google-label").textContent = "Continue with Google";
    el.authError.hidden = false;
    el.authError.textContent = (err && err.message) || String(err);
  }
}

el.googleSignInBtn.addEventListener("click", signInWithGoogle);

// If we came back from a Google redirect, Supabase parsed the session into
// currentUser via onAuthStateChange. Check for a stashed composer prompt and
// pop the composer once so returning users land on the tile they clicked.
function handleOAuthReturn() {
  try {
    const stashed = sessionStorage.getItem("pendingComposerPrompt");
    if (stashed && currentUser) {
      sessionStorage.removeItem("pendingComposerPrompt");
      pendingComposerPrompt = null;
      setTimeout(() => {
        if (typeof openComposer === "function") openComposer(stashed);
      }, 200);
    }
  } catch (_) { /* storage disabled — no-op */ }
}

renderIdeas();
renderTemplates();
wireTileHover(el.rendersRow);
wireTileHover(el.activeRow);
wireTileHover(el.featuredRow);
updateLengthDisplay();

(async () => {
  await initAuth();
  renderAuthUI();
  installVisibilityRefresh();
  await refreshJobs();
  refreshLibrary(); // durable renders — non-blocking
  // Handle deep-links from the completion email (e.g. /#job-abc123).
  // If the URL has a #job-<id> fragment on load, scroll that tile into
  // view and open it. If the render is still active we fall through to
  // the normal polling loop.
  handleDeepLink();
})();
