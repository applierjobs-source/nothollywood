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
  noCreditsModal: $("#noCreditsModal"),
  noCreditsMessage: $("#noCreditsMessage"),
  noCreditsCta: $("#noCreditsCta"),
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
  previewRefFile: $("#previewRefFile"),
  previewRefUploadBtn: $("#previewRefUploadBtn"),
  previewRefUploadedName: $("#previewRefUploadedName"),
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
    const isUnlimited = !!data.unlimited;
    // Unlimited accounts (staff / owner) display "Unlimited" instead of a
    // number and treat the balance as effectively infinite so nothing gates.
    lastCreditsBalance = isUnlimited ? Infinity : bal;
    el.navCreditsValue.textContent = isUnlimited ? "Unlimited" : bal.toLocaleString();
    el.navCredits.hidden = false;
    el.navCredits.classList.toggle("low", !isUnlimited && bal < 10);
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
  // Per-scene MiniMax H3 render time from observed fal jobs:
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

function showNoCreditsModal(customMessage) {
  const msg = customMessage || "You need credits to render. Packs start at $15.";
  if (el.noCreditsMessage) el.noCreditsMessage.textContent = msg;
  if (el.noCreditsCta) {
    el.noCreditsCta.onclick = () => { window.location.href = "/pricing.html"; };
  }
  if (el.noCreditsModal) {
    openModal(el.noCreditsModal);
  } else if (window.confirm(msg + "\n\nOpen pricing to add credits?")) {
    window.location.href = "/pricing.html";
  }
}

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
  // Pre-flight credit check: if we already know the user has zero credits
  // (and they're not unlimited), don't let them fill out a prompt only to
  // hit a 402 on submit. Send them to pricing right away.
  if (currentUser && lastCreditsBalance !== null && lastCreditsBalance !== Infinity && lastCreditsBalance <= 0) {
    showNoCreditsModal();
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

// Preview modal has its own inline upload button that appears in the empty
// state when the ref grid comes back with zero candidates. It writes to the
// same pendingUpload slot as the composer, so Approve just works after the
// user picks a file. Without this, users are stuck: the picker is hidden,
// the copy tells them to "pick one of the show stills" that don't exist,
// and Approve keeps refusing to submit.
if (el.previewRefUploadBtn && el.previewRefFile) {
  el.previewRefUploadBtn.addEventListener("click", () => el.previewRefFile.click());
  el.previewRefFile.addEventListener("change", (e) => {
    const f = e.target.files?.[0];
    if (!f) return;
    pendingUpload = f;
    if (el.previewRefUploadedName) {
      el.previewRefUploadedName.textContent = `Selected: ${f.name}`;
      el.previewRefUploadedName.hidden = false;
    }
    // Also mirror the file into the composer's file input so if the user
    // ever backs out and re-approves, the same file stays attached.
    try {
      const dt = new DataTransfer();
      dt.items.add(f);
      el.refFile.files = dt.files;
      showDropPreview(f);
    } catch { /* older browsers may not support DataTransfer for input.files */ }
  });
}

// ─── Submit ────────────────────────────────────────────────────────
// ---- Two-stage render flow -----------------------------------------
// Stage 1: user submits prompt → we call /api/plan for candidates+storyboard
// Stage 2: user picks ref + edits scenes → we submit /api/generate
//
// pendingPlan holds the data between stages so the approval modal can render.
let pendingPlan = null;
let pendingUpload = null; // File object if user uploaded a reference

// Flash an inline error under the submit button so the user never thinks
// the click did nothing. Auto-clears after 4s or on next submit attempt.
function showSubmitError(msg) {
  console.warn("[submit]", msg);
  let box = document.getElementById("submitError");
  if (!box) {
    box = document.createElement("p");
    box.id = "submitError";
    box.style.cssText = "margin:10px 0 0;color:#ff6b6b;font-size:13px;font-weight:500;";
    el.submitBtn.parentNode.insertBefore(box, el.submitBtn.nextSibling);
  }
  box.textContent = msg;
  box.style.display = "block";
  clearTimeout(showSubmitError._t);
  showSubmitError._t = setTimeout(() => { box.style.display = "none"; }, 4000);
}

// Global in-flight guard: testers were spam-clicking 'Send to the
// machines' 3-15 times when the button felt unresponsive during the
// 10-30s /api/plan wait, resulting in duplicate rendered videos. Refuse
// re-entry until the current submit resolves or errors out.
let submitInFlight = false;

el.renderForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (submitInFlight) {
    showSubmitError("Already working on this \u2014 give it a moment.");
    return;
  }
  // Clear any prior error banner.
  const existingErr = document.getElementById("submitError");
  if (existingErr) existingErr.style.display = "none";

  const prompt = el.prompt.value.trim();
  if (!prompt) {
    showSubmitError("Add a prompt describing what you want to make.");
    el.prompt.focus();
    return;
  }
  submitInFlight = true;
  // Disable the button synchronously so a second click within the same
  // frame can't slip past the async guard above.
  el.submitBtn.disabled = true;
  const origLabel = el.submitBtn.querySelector(".btn-label").textContent;

  try {
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
    el.submitBtn.querySelector(".btn-label").textContent = "Generating storyboard…";
    // Ask (once, non-blocking) for browser-notification permission so we
    // can ping the user when the storyboard is ready if they tabbed away.
    if ("Notification" in window && Notification.permission === "default") {
      try { Notification.requestPermission(); } catch (_) { /* fine */ }
    }
    // Show a full-composer overlay so a user who tabs away and comes back
    // sees an unmistakable "still working" state instead of an idle-looking
    // form. Testers reported missing the storyboard popup after switching
    // windows.
    showStoryboardLoading();

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
      hideStoryboardLoading();
      closeModal(el.composerModal);
      openPreview(pendingPlan);
      // Ping user with a browser notification if the tab isn't focused --
      // testers reported switching windows during the ~10-30s storyboard
      // wait and missing the popup.
      if (typeof document !== "undefined" && document.hidden && "Notification" in window) {
        try {
          if (Notification.permission === "granted") {
            new Notification("Storyboard ready", {
              body: "Your Not Hollywood storyboard is ready for approval.",
              icon: "/favicon.png",
              tag: "nh-storyboard-ready",
            });
          }
        } catch (_) { /* Notification API is best-effort */ }
      }
    } catch (err) {
      hideStoryboardLoading();
      showSubmitError("Storyboard generation failed: " + (err.message || err));
    }
  } finally {
    // Always release the guard, no matter which return path we took.
    submitInFlight = false;
    el.submitBtn.disabled = false;
    el.submitBtn.querySelector(".btn-label").textContent = origLabel;
  }
});

// Full-composer "Generating storyboard…" overlay. Rendered lazily so we
// only touch the DOM when the user actually hits Send. Auto-clears when
// the preview modal opens or an error banner shows.
function showStoryboardLoading() {
  let ov = document.getElementById("storyboardLoadingOverlay");
  if (!ov) {
    ov = document.createElement("div");
    ov.id = "storyboardLoadingOverlay";
    ov.style.cssText = [
      "position:absolute",
      "inset:0",
      "background:rgba(10,10,12,0.94)",
      "display:flex",
      "flex-direction:column",
      "align-items:center",
      "justify-content:center",
      "gap:20px",
      "z-index:5",
      "border-radius:inherit",
      "backdrop-filter:blur(6px)",
      "-webkit-backdrop-filter:blur(6px)",
    ].join(";");
    ov.innerHTML = `
      <div style="width:56px;height:56px;border:3px solid rgba(255,255,255,0.15);border-top-color:#ff4d4d;border-radius:50%;animation:sbSpin 0.9s linear infinite;"></div>
      <div style="text-align:center;max-width:360px;padding:0 24px;">
        <div style="font-size:18px;font-weight:600;color:#fff;margin-bottom:8px;">Generating storyboard…</div>
        <div style="font-size:13px;color:rgba(255,255,255,0.65);line-height:1.5;">
          We're planning scenes and casting characters. This usually takes 10 to 30 seconds. Stay on this tab and the storyboard will pop up for your approval.
        </div>
      </div>
      <style>@keyframes sbSpin{to{transform:rotate(360deg)}}</style>
    `;
    // Attach inside the composer card so it covers only the form, not the
    // whole page (keeps modal chrome intact and lets us layer above the form).
    const composerCard = el.composerModal.querySelector(".modal-card") || el.composerModal;
    if (getComputedStyle(composerCard).position === "static") {
      composerCard.style.position = "relative";
    }
    composerCard.appendChild(ov);
  }
  ov.style.display = "flex";
}

function hideStoryboardLoading() {
  const ov = document.getElementById("storyboardLoadingOverlay");
  if (ov) ov.style.display = "none";
}

function openPreview(plan) {
  // Show modal in loading state briefly, then swap to body when populated.
  openModal(el.previewModal);
  el.previewLoading.hidden = true;
  el.previewBody.hidden = false;

  // Header: if we detected a show title, surface it in the subtitle.
  if (plan.title) {
    el.previewSubtitle.textContent = `We think this is a "${plan.title}" episode. Pick a cast reference before approving.`;
  } else {
    el.previewSubtitle.textContent = "Pick a reference frame, then approve the storyboard.";
  }

  // Reset any per-open empty-state UI so a fresh open doesn't show a stale
  // “Selected: <filename>” line from a previous preview session.
  if (el.previewRefUploadedName) {
    el.previewRefUploadedName.hidden = true;
    el.previewRefUploadedName.textContent = "";
  }
  if (el.previewRefFile) el.previewRefFile.value = "";

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
  // Long-form renders (>=60s) return an outline instead of scene prompts.
  // Show the outline approval card; short renders keep the storyboard editor.
  if (plan.mode === "outline" && plan.outline) {
    renderOutline(plan);
  } else {
    renderStoryboard(plan);
  }
}

function renderRefTiles(cands) {
  el.previewRefs.innerHTML = "";
  let loaded = 0;
  let failed = 0;

  const checkAllFailed = () => {
    // If every tile failed, surface it — the empty-state upload button in
    // the composer is the escape hatch, but we shouldn't leave a silent
    // empty grid.
    if (failed === cands.length && loaded === 0) {
      const note = document.createElement("div");
      note.className = "preview-refs-note";
      note.textContent = "Reference images failed to load. Try More options, or upload your own below.";
      el.previewRefs.appendChild(note);
    }
  };

  cands.forEach((c, idx) => {
      const div = document.createElement("div");
      div.className = "preview-ref";
      div.dataset.url = c.url;
      div.dataset.idx = idx;
      const src = c.thumbnail || c.url;
      const img = document.createElement("img");
      img.alt = `reference ${idx + 1}`;
      img.loading = "lazy";
      // Same-origin proxied URLs don't need no-referrer; leaving it doesn't
      // hurt for the third-party fallback path.
      img.referrerPolicy = "no-referrer";
      img.src = src;
      img.addEventListener("load", () => { loaded += 1; });
      img.addEventListener("error", () => {
        // Fallback: try the raw URL if the proxied one failed for any reason.
        if (c._raw && img.src !== c._raw) {
          img.src = c._raw;
          return;
        }
        failed += 1;
        div.classList.add("preview-ref-broken");
        // Replace img with a subtle broken-image marker instead of vanishing
        // silently. User can still click other tiles, or use the upload path.
        div.innerHTML = '<div class="preview-ref-fallback">image unavailable</div>';
        checkAllFailed();
      });
      div.appendChild(img);
      if (c.source === "cache") {
        const badge = document.createElement("span");
        badge.className = "badge";
        badge.textContent = "Saved";
        div.appendChild(badge);
      }
      const check = document.createElement("span");
      check.className = "check";
      check.textContent = "✓";
      div.appendChild(check);

      div.addEventListener("click", () => {
        // Don't allow selecting broken tiles — they'd fail at generate time.
        if (div.classList.contains("preview-ref-broken")) return;
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

// Long-form (>=60s) two-pass flow: render an editable story-outline card
// instead of the flat per-scene list. Each section (logline, cold open,
// A-story beats, B-story beats, tag) becomes a textarea the user can edit,
// then we JSON.stringify the edited outline back into chosen_outline when
// they hit Approve. The backend hands it to expand_prompt so scene prompts
// follow the approved A/B story structure.
//
// Note: for outline mode we hide the per-scene editor entirely — users approve
// the story structure, not 120 individual scenes. Scene prompts get expanded
// after Approve in the worker, using the approved outline as the blueprint.
function renderOutline(plan) {
  el.previewScenes.innerHTML = "";
  const outline = plan.outline;
  const totalScenes = (plan.scenes_plan || []).length;
  // Thread mode comes from the backend. Fall back to detecting b_story so old
  // API responses still work. Single-thread = no B-story card, cleaner label.
  const threadMode = plan.outline_thread_mode || (outline.b_story ? "dual" : "single");
  const isSingle = threadMode === "single";
  const wrap = document.createElement("div");
  wrap.className = "outline-card" + (isSingle ? " outline-single" : " outline-dual");

  const errBanner = plan.outline_ok
    ? ""
    : `<div class="outline-warn">Outline generation used fallback (${escapeHtml(plan.outline_error || "unknown")}). Edit below to fix.</div>`;

  const beatList = (beats, prefix) =>
    (beats || [])
      .map((b, i) => `<div class="outline-beat"><span class="outline-beat-label">${prefix} · Beat ${i + 1}</span><textarea data-key="${prefix.toLowerCase().replace(/[^a-z]/g, "")}-beat" data-idx="${i}">${escapeHtml(b)}</textarea></div>`)
      .join("");

  // For single-thread mode, don't call the A-story an "A-Story" — it's just
  // "the story" — and skip the B-story block entirely.
  const storyLabel = isSingle ? "Story" : "A-Story";
  const bStoryBlock = isSingle ? "" : `
    <div class="outline-section outline-b">
      <label class="outline-label">B-Story — <input class="outline-inline" data-key="b_story-title" value="${escapeHtml(outline.b_story?.title || "")}" /></label>
      <textarea data-key="b_story-premise" placeholder="Premise">${escapeHtml(outline.b_story?.premise || "")}</textarea>
      ${beatList(outline.b_story?.beats, "B-STORY")}
      <div class="outline-cast-line">Cast: ${escapeHtml((outline.b_story?.characters || []).join(", ") || "—")}</div>
    </div>`;

  wrap.innerHTML = `
    <div class="outline-hero">
      <div class="outline-hero-eyebrow">Story Outline${isSingle ? "" : " · A / B"}</div>
      <div class="outline-hero-title">Approve the story before we render ${totalScenes} scenes</div>
      <div class="outline-hero-sub">Edit any section. We’ll write the scene prompts from this after you approve.</div>
    </div>
    ${errBanner}
    <div class="outline-section">
      <label class="outline-label">Logline</label>
      <textarea data-key="logline">${escapeHtml(outline.logline || "")}</textarea>
    </div>
    <div class="outline-section">
      <label class="outline-label">Cold Open</label>
      <textarea data-key="cold_open-beat">${escapeHtml(outline.cold_open?.beat || "")}</textarea>
      <div class="outline-cast-line">Cast: ${escapeHtml((outline.cold_open?.characters || []).join(", ") || "—")}</div>
    </div>
    <div class="outline-section outline-a">
      <label class="outline-label">${storyLabel} — <input class="outline-inline" data-key="a_story-title" value="${escapeHtml(outline.a_story?.title || "")}" /></label>
      <textarea data-key="a_story-premise" placeholder="Premise">${escapeHtml(outline.a_story?.premise || "")}</textarea>
      ${beatList(outline.a_story?.beats, "A-STORY")}
      <div class="outline-cast-line">Cast: ${escapeHtml((outline.a_story?.characters || []).join(", ") || "—")}</div>
    </div>
    ${bStoryBlock}
    <div class="outline-section">
      <label class="outline-label">Tag</label>
      <textarea data-key="tag-beat">${escapeHtml(outline.tag?.beat || "")}</textarea>
      <div class="outline-cast-line">Cast: ${escapeHtml((outline.tag?.characters || []).join(", ") || "—")}</div>
    </div>
    ${outline.notes ? `<div class="outline-notes">Writer’s notes: ${escapeHtml(outline.notes)}</div>` : ""}
  `;
  el.previewScenes.appendChild(wrap);
}

// Read the edited outline back out of the DOM. Returns the same shape the
// backend sent minus any keys the user emptied. Never throws.
function collectOutlineFromDom() {
  const card = el.previewScenes.querySelector(".outline-card");
  if (!card) return null;
  const isSingle = card.classList.contains("outline-single");
  const get = (sel) => (card.querySelector(sel)?.value || "").trim();
  const getList = (prefix) =>
    Array.from(card.querySelectorAll(`textarea[data-key="${prefix}-beat"]`))
      .sort((a, b) => Number(a.dataset.idx) - Number(b.dataset.idx))
      .map((t) => t.value.trim())
      .filter(Boolean);
  const orig = pendingPlan?.outline || {};
  const result = {
    logline: get('textarea[data-key="logline"]'),
    cold_open: {
      beat: get('textarea[data-key="cold_open-beat"]'),
      characters: orig.cold_open?.characters || [],
    },
    a_story: {
      title: get('input[data-key="a_story-title"]'),
      premise: get('textarea[data-key="a_story-premise"]'),
      beats: getList("astory"),
      characters: orig.a_story?.characters || [],
    },
    tag: {
      beat: get('textarea[data-key="tag-beat"]'),
      characters: orig.tag?.characters || [],
    },
    notes: orig.notes || "",
  };
  // Only include b_story in dual-thread mode. In single-thread mode we omit
  // it entirely so the backend expander takes the single-thread path.
  if (!isSingle) {
    result.b_story = {
      title: get('input[data-key="b_story-title"]'),
      premise: get('textarea[data-key="b_story-premise"]'),
      beats: getList("bstory"),
      characters: orig.b_story?.characters || [],
    };
  }
  return result;
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
      showPreviewError("No new options for that variant \u2014 tap again for another set.");
    } else {
      renderRefTiles(cands);
      el.previewRefEmpty.hidden = true;
      el.previewRefs.hidden = false;
    }
  } catch (err) {
    showPreviewError("Could not fetch new options: " + (err.message || err));
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

// Same in-flight guard as the composer: testers were double-tapping
// the approve button while /api/generate ran, producing duplicate
// renders. Refuse re-entry until the current submit resolves.
let approveInFlight = false;

// Inline banner inside the preview modal so we never fall back to a
// native alert() on iOS/Safari when validation or a fetch fails.
function showPreviewError(msg) {
  console.warn("[preview]", msg);
  let box = document.getElementById("previewError");
  if (!box) {
    box = document.createElement("p");
    box.id = "previewError";
    box.style.cssText = "margin:10px 16px 0;color:#ff6b6b;font-size:13px;font-weight:500;";
    // Insert just above the approve button so it's the last thing the
    // user sees before their finger moves.
    const anchor = el.previewApproveBtn && el.previewApproveBtn.parentNode;
    if (anchor) anchor.insertBefore(box, el.previewApproveBtn);
    else el.previewModal.appendChild(box);
  }
  box.textContent = msg;
  box.style.display = "block";
  clearTimeout(showPreviewError._t);
  showPreviewError._t = setTimeout(() => { box.style.display = "none"; }, 5000);
}

el.previewApproveBtn.addEventListener("click", async () => {
  if (!pendingPlan) return;
  if (approveInFlight) {
    showPreviewError("Already submitting \u2014 hang tight.");
    return;
  }
  const selected = el.previewRefs.querySelector(".preview-ref.selected");
  // A reference image is required. If the user didn't have an upload
  // already in the composer AND hasn't picked a candidate, stop them
  // here with a friendly nudge rather than letting the backend 400.
  //
  // Empty-state path: when the ref grid came back with zero candidates we
  // hide the grid and show an inline upload button. Highlight whichever of
  // (grid, empty-state) is currently visible so the user knows where to go.
  if (!selected && !pendingUpload) {
    const emptyShown = el.previewRefEmpty && !el.previewRefEmpty.hidden;
    if (emptyShown) {
      showPreviewError("No cast frames came back \u2014 tap \u201cMore options\u201d or upload your own reference frame below.");
      el.previewRefEmpty.scrollIntoView({ behavior: "smooth", block: "center" });
      el.previewRefEmpty.classList.add("needs-pick");
      setTimeout(() => el.previewRefEmpty.classList.remove("needs-pick"), 1600);
    } else {
      showPreviewError("Pick a reference image before continuing \u2014 tap one of the stills, or go back and upload your own.");
      if (el.previewRefs) {
        el.previewRefs.scrollIntoView({ behavior: "smooth", block: "center" });
        el.previewRefs.classList.add("needs-pick");
        setTimeout(() => el.previewRefs.classList.remove("needs-pick"), 1600);
      }
    }
    return;
  }
  const chosenRefUrl = selected ? selected.dataset.url : "";

  // Two-mode collection: outline card (long form) vs storyboard textareas
  // (short form). Only one is present in the DOM at a time.
  let chosenScenes = null;
  let chosenOutline = null;
  if (pendingPlan.mode === "outline") {
    chosenOutline = collectOutlineFromDom();
  } else {
    const editedScenes = Array.from(el.previewScenes.querySelectorAll("textarea"))
      .map((t) => t.value.trim())
      .filter(Boolean);
    if (editedScenes.length === pendingPlan.scenes_plan.length) {
      chosenScenes = editedScenes;
    }
  }

  approveInFlight = true;
  el.previewApproveBtn.disabled = true;
  const origLabel = el.previewApproveBtn.querySelector(".btn-label").textContent;
  el.previewApproveBtn.querySelector(".btn-label").textContent = "Submitting…";

  try {
    await submitGenerate({
      prompt: pendingPlan._prompt,
      duration: pendingPlan._duration,
      resolution: pendingPlan._resolution,
      chosenRefUrl,
      chosenScenes,
      chosenOutline,
    });
    closeModal(el.previewModal);
    pendingPlan = null;
  } catch (err) {
    showPreviewError("Submission failed: " + (err.message || err));
  } finally {
    approveInFlight = false;
    el.previewApproveBtn.disabled = false;
    el.previewApproveBtn.querySelector(".btn-label").textContent = origLabel;
  }
});

async function submitGenerate({ prompt, duration, resolution, chosenRefUrl, chosenScenes, chosenOutline }) {
  const fd = new FormData();
  fd.append("prompt", prompt);
  fd.append("duration", String(duration));
  fd.append("resolution", resolution || document.querySelector('input[name="resolution"]:checked').value);
  if (pendingUpload) fd.append("reference", pendingUpload);
  if (chosenRefUrl) fd.append("chosen_ref_url", chosenRefUrl);
  if (chosenScenes) fd.append("chosen_scenes", JSON.stringify(chosenScenes));
  if (chosenOutline) fd.append("chosen_outline", JSON.stringify(chosenOutline));

  const r = await authedFetch(`${API}/api/generate`, { method: "POST", body: fd });
  if (r.status === 401) {
    closeModal(el.composerModal);
    closeModal(el.previewModal);
    setTimeout(() => openAuthModal("signin"), 200);
    throw new Error("session expired — sign in again");
  }
  if (r.status === 400) {
    // Missing reference image (or other structured 400). Surface a
    // human-readable message rather than a raw payload.
    try {
      const body = await r.json();
      if (body && body.detail && body.detail.error === "reference_required") {
        showSubmitError(body.detail.message || "A reference image is required.");
        throw new Error("reference required");
      }
      if (body && typeof body.detail === "string") {
        throw new Error(body.detail);
      }
    } catch (parseErr) {
      if (parseErr && parseErr.message === "reference required") throw parseErr;
    }
  }
  if (r.status === 402) {
    // Out of credits — surface a themed modal that deep-links to pricing
    // rather than a native confirm() dialog.
    let msg = "You need credits to render. Packs start at $15.";
    try {
      const body = await r.json();
      if (body && body.detail && body.detail.message) msg = body.detail.message;
    } catch (_) { /* body wasn't JSON */ }
    // Also close any composer/preview modals so the credits modal is on top
    // and the user isn't confused about which dialog they're in.
    closeModal(el.composerModal);
    closeModal(el.previewModal);
    setTimeout(() => showNoCreditsModal(msg), 240);
    // Refresh nav balance since the server just told us we're out.
    refreshCredits();
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
  // Show + cast chips: only present when Grok casting ran (franchise renders).
  // The cast chip is marked so users can see who Grok picked and tell us if
  // it got the wrong character — the whole reason this UI exists.
  if (job.show_title) {
    bits.push(`<span class="chip" title="Detected show">${escapeHtml(job.show_title)}</span>`);
  }
  if (Array.isArray(job.cast) && job.cast.length) {
    const castLabel = job.cast.map(escapeHtml).join(", ");
    bits.push(`<span class="chip chip-cast" title="Characters Grok cast for scene 1. Tell us if this is wrong.">Cast: ${castLabel}</span>`);
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
  // Real Fal signals when the backend has them:
  //   s.queue_position: int   — "you're 3rd in line at Fal" while IN_QUEUE
  //   s.progress:       0..1  — parsed from model logs while IN_PROGRESS
  // Fall back to wall-clock elapsed only when neither is present.
  const hasProgress = typeof s.progress === "number" && s.progress >= 0;
  const progPct = hasProgress ? Math.round(s.progress * 100) : 0;
  const label = (() => {
    if (status === "queued") {
      if (typeof s.queue_position === "number") {
        if (s.queue_position === 0) return "Next up at Fal";
        return `Queued · #${s.queue_position + 1} in line`;
      }
      return "Queued";
    }
    if (status === "submitting") return "Submitting…";
    if (status === "running") {
      const elapsed = s.started_at
        ? Math.max(0, Math.round((Date.now() / 1000) - s.started_at))
        : 0;
      if (hasProgress) {
        return elapsed ? `Rendering · ${progPct}% · ${elapsed}s` : `Rendering · ${progPct}%`;
      }
      return s.started_at ? `Rendering · ${elapsed}s` : "Rendering…";
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
  // Per-row mini progress bar for running scenes with a real percent. Purely
  // visual — the label already says "42%" — but gives a scannable stripe
  // that makes long scene lists easier to skim.
  const miniBar = status === "running" && hasProgress
    ? `<span class="scene-row-bar" aria-hidden="true"><span class="scene-row-bar-fill" style="width:${progPct}%"></span></span>`
    : "";
  return `
    <div class="scene-row scene-row-${status}" data-scene-idx="${s.index}" data-scene-started="${s.started_at || 0}" data-scene-progress="${hasProgress ? s.progress : ""}">
      ${dot}
      <span class="scene-row-idx">Scene ${s.index + 1}</span>
      <span class="scene-row-dur">${durLabel}</span>
      <span class="scene-row-label">${escapeHtml(label)}</span>
      ${miniBar}
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
    // Overall progress model:
    //   Sum every scene's contribution to the whole:
    //     succeeded  → 1.0
    //     running    → real Fal fraction if we have one, else 0.15 shimmer
    //     queued     → 0
    //     failed     → 1.0 (counts as done for bar purposes so we don't stall)
    //   Divide by total. This makes the bar climb in real time as Fal reports
    //   model progress, instead of only jumping when a whole scene finishes.
    const scenesForBar = Array.isArray(job.scenes_state) ? job.scenes_state : [];
    let contribution = 0;
    if (scenesForBar.length) {
      for (const s of scenesForBar) {
        const st = (s.status || "queued").toLowerCase();
        if (st === "succeeded" || st === "failed") { contribution += 1; continue; }
        if (st === "running") {
          const p = typeof s.progress === "number" ? s.progress : null;
          contribution += (p !== null ? Math.max(0.02, Math.min(0.98, p)) : 0.15);
          continue;
        }
        // queued → 0
      }
    } else {
      // Fallback: no scenes_state yet, use coarse counters.
      contribution = done + Math.min(running, total) * 0.15;
    }
    const overallFrac = Math.min(0.98, contribution / total);
    const pct = Math.max(2, Math.round(overallFrac * 100));
    const isStitching = job.status === "stitching";
    const finalPct = isStitching ? 96 : pct;

    // Aggregate ETA: use median per-scene ETA * (remaining scenes / concurrency).
    // fal concurrency is 4; slowest-scene wall-clock model gives an honest bound.
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
  // Finished.
  //
  // Thumbnail strategy: browsers with preload="metadata" often paint nothing
  // until enough of the video is buffered to render frame 0.8 — that's why
  // tiles were going black and then "popping in." Two fixes here:
  //   1) poster=<jpg> when we have a real thumb (library rows have one from
  //      Supabase; showcase clips have static/showcase/*.jpg alongside the
  //      mp4). Poster paints instantly, no video decode needed.
  //   2) preload="none" for anything without a poster — lets the black
  //      fallback show immediately and defer the mp4 fetch until hover.
  const poster = job.thumb || job.poster || "";
  const videoPreload = poster ? "none" : "metadata";
  return `
    <div class="tile" data-id="${escapeHtml(job.id)}" data-open="1">
      <video class="tile-video" src="${escapeHtml(job.video)}#t=0.8" ${poster ? `poster="${escapeHtml(poster)}"` : ""} muted preload="${videoPreload}" playsinline></video>
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

// Hover preview: play the tile video muted while hovered.
// If the video was set to preload="none" for the poster-first path,
// bump preload to "auto" on first hover so play() has data to work with.
function wireTileHover(container) {
  container.addEventListener("mouseover", (e) => {
    const v = e.target.closest(".tile")?.querySelector(".tile-video");
    if (v) {
      if (v.preload === "none") v.preload = "auto";
      v.currentTime = 0.8;
      v.play().catch(() => {});
    }
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
    poster: "static/showcase/showcase_sitcom.jpg",
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
    poster: "static/showcase/showcase_animated.jpg",
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
    poster: "static/showcase/showcase_truecrime.jpg",
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
    poster: "static/showcase/showcase_scifi.jpg",
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
