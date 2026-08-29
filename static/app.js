/* ═══════════════════════════════════════════════════════════════════════
   NOT HOLLYWOOD - Netflix for creators
   Hydrates hero + shelves from /api/jobs, opens composer modal.
   ══════════════════════════════════════════════════════════════════════ */

const API = "";                                    // same-origin
const POLL_MS = 3000;

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
  authModal: $("#authModal"),
  authForm: $("#authForm"),
  authEmail: $("#authEmail"),
  authPassword: $("#authPassword"),
  authError: $("#authError"),
  authSubmitBtn: $("#authSubmitBtn"),
  authSubmitLabel: $("#authSubmitLabel"),
  authTitle: $("#authTitle"),
  authSub: $("#authSub"),
  authSwitchLine: $("#authSwitchLine"),
  authPasswordHint: $("#authPasswordHint"),

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
let authMode = "signin";    // "signin" | "signup"

async function initAuth() {
  try {
    const r = await fetch(`${API}/api/config`);
    if (!r.ok) throw new Error("config failed");
    const cfg = await r.json();
    authRequired = !!cfg.auth_required;
    if (!authRequired) {
      // Legacy mode: no Supabase configured server-side. Keep composer open to all.
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
    sb.auth.onAuthStateChange((_event, session) => {
      currentUser = session ? session.user : null;
      renderAuthUI();
      refreshJobs();
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
  } else {
    el.navSignInBtn.hidden = false;
    el.navUser.hidden = true;
    // Hide user library + active shelf when signed out — showcase remains.
    el.shelfRenders.hidden = true;
    el.shelfActive.hidden = true;
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
  const waitMin = Math.max(2, scenes * 1.5);
  const waitMax = Math.max(3, scenes * 3);
  const stitchOverhead = scenes > 1 ? 0.5 : 0;
  el.estCost.textContent = `$${cost.toFixed(2)}`;
  const total = scenes === 1
    ? `${Math.round(waitMin)}, ${Math.round(waitMax)} min`
    : `${Math.round(waitMin + stitchOverhead)}, ${Math.round(waitMax + stitchOverhead)} min`;
  el.estWait.textContent = total;
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

function openComposer(prefillPrompt) {
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
    e.preventDefault();
    document.querySelectorAll(".nav-tabs a").forEach((x) => x.classList.remove("active"));
    a.classList.add("active");
    const tab = a.dataset.tab;
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

  const fd = new FormData();
  fd.append("prompt", prompt);
  fd.append("duration", String(duration));
  fd.append("resolution", document.querySelector('input[name="resolution"]:checked').value);
  const f = el.refFile.files?.[0];
  if (f) fd.append("reference", f);

  // Auth check: if the backend requires auth but the user isn't signed in,
  // close the composer and open the auth modal instead of submitting.
  if (authRequired && !currentUser) {
    closeModal(el.composerModal);
    setTimeout(() => openAuthModal("signin"), 200);
    return;
  }

  el.submitBtn.disabled = true;
  const origLabel = el.submitBtn.querySelector(".btn-label").textContent;
  el.submitBtn.querySelector(".btn-label").textContent = "Submitting…";

  try {
    const r = await authedFetch(`${API}/api/generate`, { method: "POST", body: fd });
    if (r.status === 401) {
      closeModal(el.composerModal);
      setTimeout(() => openAuthModal("signin"), 200);
      throw new Error("session expired — sign in again");
    }
    if (!r.ok) throw new Error(await r.text() || `HTTP ${r.status}`);
    const job = await r.json();
    // Close composer, jump user to the "Now Rendering" shelf
    closeModal(el.composerModal);
    el.prompt.value = "";
    el.prompt.dispatchEvent(new Event("input"));
    showDropEmpty();
    setTimeout(() => {
      refreshJobs().then(() => {
        el.shelfActive.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    }, 260);
  } catch (err) {
    alert("Show failed: " + (err.message || err));
  } finally {
    el.submitBtn.disabled = false;
    el.submitBtn.querySelector(".btn-label").textContent = origLabel;
  }
});

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
  el.heroPlayBtn.querySelector("span").textContent = "Create a Show";
  el.heroPlayBtn.onclick = () => openComposer();
}

// ─── Rendering: shelves ────────────────────────────────────────────
function renderTile(job) {
  const isActive = job.status !== "done" && job.status !== "failed";
  const isFailed = job.status === "failed";

  if (isActive) {
    const pct = job.scene_total > 1
      ? Math.round(((job.scene_index || 0) / job.scene_total) * 100)
      : 20;
    const sceneLine = job.scene_total > 1
      ? `Scene ${job.scene_index || 0}/${job.scene_total} · ${escapeHtml(job.scene_status || job.status)}`
      : escapeHtml(job.status);
    return `
      <div class="tile tile-active" data-id="${escapeHtml(job.id)}">
        <span class="tile-badge rendering">${escapeHtml(job.status.toUpperCase())}</span>
        <div class="tile-active-body">
          <div>
            <div class="tile-active-prompt">${escapeHtml(firstLine(job.prompt))}</div>
            <div class="tile-active-scene">${sceneLine}</div>
          </div>
          <div class="tile-active-progress">
            <div class="tile-active-progress-fill" style="width:${pct}%"></div>
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
  if (finished.length) {
    el.rendersEmpty.hidden = true;
    el.rendersRow.innerHTML = finished
      .sort((a, b) => (b.finished_at || b.created_at || 0) - (a.finished_at || a.created_at || 0))
      .map(renderTile).join("");
  } else {
    el.rendersEmpty.hidden = false;
    el.rendersRow.querySelectorAll(".tile").forEach((t) => t.remove());
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
  const job = jobs.find((j) => j.id === tile.dataset.id);
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
  { value: "10", unit: "min", desc: "60 scenes. A full sitcom episode. Big spend.", pre: 600 },
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
    jobs = Array.isArray(remote) ? remote : [];
    renderHero();
    renderShelves();
    schedulePoll();
  } catch (err) {
    if (!jobs) jobs = [];
    renderHero();
    renderShelves();
    console.warn("refresh failed", err);
  }
}
function schedulePoll() {
  if (pollTimer) clearTimeout(pollTimer);
  const hasActive = jobs.some((j) => j.status !== "done" && j.status !== "failed");
  if (hasActive) {
    pollTimer = setTimeout(refreshJobs, POLL_MS);
  }
}

// ─── Init ──────────────────────────────────────────────────────────
// Auth modal wiring
function openAuthModal(mode = "signin") {
  setAuthMode(mode);
  el.authError.hidden = true;
  el.authError.textContent = "";
  openModal(el.authModal);
  setTimeout(() => el.authEmail.focus(), 100);
}

function setAuthMode(mode) {
  authMode = mode;
  const tabs = el.authModal.querySelectorAll(".auth-tab");
  tabs.forEach((t) => t.classList.toggle("active", t.dataset.authMode === mode));
  if (mode === "signup") {
    el.authTitle.textContent = "Create your account.";
    el.authSub.textContent = "Your renders stay in your library. Nothing goes public.";
    el.authSubmitLabel.textContent = "Create account";
    el.authPassword.setAttribute("autocomplete", "new-password");
    el.authPasswordHint.textContent = "At least 6 characters. Pick something memorable.";
    el.authSwitchLine.innerHTML =
      'Already have an account? <button type="button" class="link-btn" data-auth-switch="signin">Sign in</button>';
  } else {
    el.authTitle.textContent = "Sign in to Not Hollywood.";
    el.authSub.textContent = "Welcome back. Pick up your library.";
    el.authSubmitLabel.textContent = "Sign in";
    el.authPassword.setAttribute("autocomplete", "current-password");
    el.authPasswordHint.textContent = "At least 6 characters.";
    el.authSwitchLine.innerHTML =
      'New here? <button type="button" class="link-btn" data-auth-switch="signup">Create an account</button>';
  }
}

el.navSignInBtn.addEventListener("click", () => openAuthModal("signin"));
el.navSignOutBtn.addEventListener("click", async () => {
  if (!sb) return;
  await sb.auth.signOut();
});

document.addEventListener("click", (e) => {
  const tab = e.target.closest("[data-auth-mode]");
  if (tab) {
    setAuthMode(tab.dataset.authMode);
    el.authError.hidden = true;
    return;
  }
  const sw = e.target.closest("[data-auth-switch]");
  if (sw) {
    setAuthMode(sw.dataset.authSwitch);
    el.authError.hidden = true;
  }
});

el.authForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!sb) return;
  el.authError.hidden = true;
  el.authError.textContent = "";
  el.authError.style.color = "";
  const email = el.authEmail.value.trim();
  const password = el.authPassword.value;
  if (!email || !password) return;

  el.authSubmitBtn.disabled = true;
  const origLabel = el.authSubmitLabel.textContent;
  el.authSubmitLabel.textContent = authMode === "signup" ? "Creating…" : "Signing in…";

  try {
    let resp;
    if (authMode === "signup") {
      // We disable email confirmation in Supabase — sign-up should return an
      // active session immediately. If for any reason no session comes back
      // (e.g. someone re-enabled Confirm email in the dashboard), fall through
      // to an immediate password sign-in rather than telling the user to check
      // their email.
      resp = await sb.auth.signUp({ email, password });
      if (resp.error) throw resp.error;
      if (resp.data && resp.data.user && !resp.data.session) {
        const signIn = await sb.auth.signInWithPassword({ email, password });
        if (signIn.error) throw signIn.error;
      }
    } else {
      resp = await sb.auth.signInWithPassword({ email, password });
      if (resp.error) throw resp.error;
    }
    closeModal(el.authModal);
    el.authForm.reset();
  } catch (err) {
    el.authError.hidden = false;
    el.authError.textContent = (err && err.message) || String(err);
  } finally {
    el.authSubmitBtn.disabled = false;
    el.authSubmitLabel.textContent = origLabel;
  }
});

renderIdeas();
renderTemplates();
wireTileHover(el.rendersRow);
wireTileHover(el.activeRow);
wireTileHover(el.featuredRow);
updateLengthDisplay();

(async () => {
  await initAuth();
  renderAuthUI();
  await refreshJobs();
})();
