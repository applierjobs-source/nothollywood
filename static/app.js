/* ═══════════════════════════════════════════════════════════════════════
   NOT HOLLYWOOD — Netflix for creators
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

  shelfRenders: $("#shelfRenders"),
  rendersRow: $("#rendersRow"),
  rendersEmpty: $("#rendersEmpty"),

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
let jobs = [];
let heroJob = null;
let pollTimer = null;
let currentDetailJob = null;

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
  } else {
    const m = d / 60;
    el.lengthValue.textContent = Number.isInteger(m) ? m : m.toFixed(1);
    el.lengthUnit.textContent = m === 1 ? "minute" : "minutes";
  }
  const n = sceneCount(d);
  el.lengthHint.textContent = n === 1
    ? "Single H3 shot — fastest and cheapest."
    : `Rendered as ${n} sequential 10-second scenes and stitched together.`;
  // Chip active state
  el.presetChips.querySelectorAll(".chip").forEach((c) => {
    c.classList.toggle("active", Number(c.dataset.preset) === d);
  });
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
    ? `${Math.round(waitMin)}–${Math.round(waitMax)} min`
    : `${Math.round(waitMin + stitchOverhead)}–${Math.round(waitMax + stitchOverhead)} min`;
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
  el.detailEyebrow.textContent = job.status === "done" ? "Render" : job.status.toUpperCase();
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

  el.submitBtn.disabled = true;
  const origLabel = el.submitBtn.querySelector(".btn-label").textContent;
  el.submitBtn.querySelector(".btn-label").textContent = "Submitting…";

  try {
    const pwd = localStorage.getItem("nh_pwd") || "";
    let r = await fetch(`${API}/api/generate`, {
      method: "POST",
      body: fd,
      headers: pwd ? { "X-Site-Password": pwd } : {},
    });
    if (r.status === 401) {
      localStorage.removeItem("nh_pwd");
      const entered = window.prompt("This site is password-protected. Enter the password to render:");
      if (!entered) throw new Error("password required");
      localStorage.setItem("nh_pwd", entered);
      r = await fetch(`${API}/api/generate`, {
        method: "POST", body: fd,
        headers: { "X-Site-Password": entered },
      });
      if (r.status === 401) {
        localStorage.removeItem("nh_pwd");
        throw new Error("Incorrect password");
      }
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
    alert("Render failed: " + (err.message || err));
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
// Hero titles want to read like a show name — take the first sentence,
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
    bits.push(`<span class="badge">${escapeHtml(job.status.toUpperCase())}</span>`);
    if (job.scene_total > 1) {
      bits.push(`<span>Scene ${job.scene_index || 0} / ${job.scene_total}</span>`);
    }
  } else if (job.status === "failed") {
    bits.push(`<span class="badge" style="background:var(--danger);">FAILED</span>`);
  } else {
    bits.push(`<span class="badge">HD</span>`);
  }
  bits.push(`<span>${formatDuration(job.duration)}</span>`);
  bits.push(`<span class="dot"></span>`);
  bits.push(`<span>${escapeHtml(job.resolution || "768P")}</span>`);
  if (job.finished_at) {
    bits.push(`<span class="dot"></span>`);
    bits.push(`<span>${timeAgo(job.finished_at)}</span>`);
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
  const job = pickHeroJob(jobs);
  heroJob = job;
  if (!job) {
    el.hero.classList.remove("rendering");
    el.heroEyebrow.textContent = "The Studio";
    el.heroTitle.textContent = "Direct your first scene.";
    el.heroMeta.innerHTML = "";
    el.heroSynopsis.textContent = "Not Hollywood is a studio, not a stream. Write a prompt, pick a length from six seconds to ten minutes, and the render lands in your library. Cinematic AI video, on your terms.";
    el.heroVideo.removeAttribute("src");
    el.heroPlayBtn.querySelector("span").textContent = "New Render";
    el.heroPlayBtn.onclick = () => openComposer();
    el.heroInfoBtn.style.display = "none";
    return;
  }
  const isActive = job.status !== "done" && job.status !== "failed";
  el.hero.classList.toggle("rendering", isActive);
  el.heroEyebrow.textContent = isActive ? "Rendering Now" : "Latest Render";
  el.heroTitle.textContent = heroTitleFrom(job.prompt);
  el.heroMeta.innerHTML = renderMeta(job);
  el.heroSynopsis.textContent = job.prompt;
  el.heroInfoBtn.style.display = "";
  if (job.video) {
    if (el.heroVideo.getAttribute("src") !== job.video) {
      el.heroVideo.src = job.video;
      el.heroVideo.load();
    }
    el.heroPlayBtn.querySelector("span").textContent = "Watch";
    el.heroPlayBtn.onclick = () => openDetail(job);
  } else {
    el.heroVideo.removeAttribute("src");
    el.heroPlayBtn.querySelector("span").textContent = "New Render";
    el.heroPlayBtn.onclick = () => openComposer();
  }
  el.heroInfoBtn.onclick = () => openDetail(job);
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

  // Active
  if (active.length) {
    el.shelfActive.hidden = false;
    el.activeCount.textContent = active.length;
    el.activeRow.innerHTML = active.map(renderTile).join("");
  } else {
    el.shelfActive.hidden = true;
  }

  // Renders
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
  const tile = e.target.closest(".tile[data-id]");
  if (!tile) return;
  if (tile.dataset.open !== "1") return;
  const job = jobs.find((j) => j.id === tile.dataset.id);
  if (job) openDetail(job);
});

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
    prompt: "Mission control, dim blue light. A young scientist stares at a monitor as pixel by pixel an image resolves — the surface of Europa, then something moving under the ice. The room goes silent. Wide slow push in, sound design of one heartbeat.",
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
    hint: "A woman finds a stack of postcards addressed to her — but never mailed.",
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
async function refreshJobs() {
  try {
    const r = await fetch(`${API}/api/jobs`);
    if (!r.ok) throw new Error("failed");
    jobs = await r.json();
    renderHero();
    renderShelves();
    schedulePoll();
  } catch (err) {
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
renderIdeas();
renderTemplates();
wireTileHover(el.rendersRow);
wireTileHover(el.activeRow);
updateLengthDisplay();
refreshJobs();
