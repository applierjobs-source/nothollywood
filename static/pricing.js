// ═══════════════════════════════════════════════════════════════════════
// Pricing page — credit packs + cost calculator
// Stripe integration comes in the next phase.
// ═════════════════════════════════════════════════════════════════════════

const API = window.location.origin;
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

// ─── Pack config (must stay in sync with pricing.html) ─────────────
const PACKS = [
  { id: "taster",  name: "Taster",  price: 5,  credits: 20 },
  { id: "starter", name: "Starter", price: 15, credits: 75 },
  { id: "studio",  name: "Studio",  price: 75, credits: 500 },
];

// ─── Auth widgets ──────────────────────────────────────────────────
let supabase = null;
let currentUser = null;
let authRequired = false;

async function initAuth() {
  try {
    const r = await fetch(`${API}/api/config`);
    if (!r.ok) return;
    const cfg = await r.json();
    authRequired = !!cfg.auth_required;
    if (!authRequired || !cfg.supabase_url || !cfg.supabase_anon_key) return;
    // eslint-disable-next-line no-undef
    supabase = window.supabase.createClient(cfg.supabase_url, cfg.supabase_anon_key, {
      auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true },
    });
    const { data: sessData } = await supabase.auth.getSession();
    if (sessData?.session) currentUser = sessData.session.user;
    supabase.auth.onAuthStateChange((_e, session) => {
      currentUser = session ? session.user : null;
      renderAuthUI();
    });
  } catch (err) {
    console.warn("auth init failed", err);
  }
}

function renderAuthUI() {
  const btn = $("#navSignInBtn");
  const chip = $("#navUser");
  const email = $("#navUserEmail");
  if (!authRequired) {
    btn.hidden = true;
    chip.hidden = true;
    return;
  }
  if (currentUser) {
    btn.hidden = true;
    chip.hidden = false;
    email.textContent = currentUser.email || "signed in";
  } else {
    btn.hidden = false;
    chip.hidden = true;
  }
}

$("#navSignInBtn").addEventListener("click", () => {
  window.location.href = "/?auth=1";
});
$("#navSignOutBtn").addEventListener("click", async () => {
  if (supabase) await supabase.auth.signOut();
});

// ─── Cost calculator ───────────────────────────────────────────────
const calcDuration = $("#calcDuration");
const calcDurationDisplay = $("#calcDurationDisplay");
const calcCredits = $("#calcCredits");
const calcPack = $("#calcPack");
const calcTotal = $("#calcTotal");
let calcResolution = "768"; // "768" | "2k"

function updateCalculator() {
  const seconds = parseInt(calcDuration.value, 10);
  const creditsPerSec = calcResolution === "2k" ? 2 : 1;
  const creditsNeeded = seconds * creditsPerSec;

  // Format duration display
  if (seconds < 60) {
    calcDurationDisplay.textContent = `${seconds}`;
  } else {
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    calcDurationDisplay.textContent = s === 0 ? `${m * 60}` : `${seconds}`;
  }

  calcCredits.textContent = creditsNeeded.toLocaleString();

  // Recommend the smallest pack that covers the need
  const recommended = recommendPack(creditsNeeded);
  calcPack.textContent = recommended.pack.name;
  calcTotal.textContent = `$${recommended.totalCost}`;
}

function recommendPack(creditsNeeded) {
  // Try single pack first
  for (const p of PACKS) {
    if (p.credits >= creditsNeeded) {
      return { pack: p, totalCost: p.price };
    }
  }
  // Need multiple Studio packs
  const studio = PACKS[PACKS.length - 1];
  const count = Math.ceil(creditsNeeded / studio.credits);
  return {
    pack: { name: `${count}× Studio` },
    totalCost: studio.price * count,
  };
}

calcDuration.addEventListener("input", updateCalculator);
$$(".calc-opt").forEach((opt) => {
  opt.addEventListener("click", () => {
    $$(".calc-opt").forEach((o) => o.classList.toggle("active", o === opt));
    calcResolution = opt.dataset.res;
    updateCalculator();
  });
});
updateCalculator();

// ─── Pack checkout CTAs (stub — real Stripe wiring next phase) ─────
$$("[data-checkout]").forEach((btn) => {
  btn.addEventListener("click", async (e) => {
    e.preventDefault();
    const pack = btn.dataset.checkout;
    const price = btn.dataset.price;
    if (authRequired && !currentUser) {
      window.location.href = `/?auth=1&next=/pricing.html&pack=${pack}`;
      return;
    }
    alert(
      `Stripe checkout for the "${pack}" pack ($${price}) is being wired up. ` +
      "You'll be redirected to a Stripe-hosted payment page here in the next update."
    );
  });
});

// ─── Init ──────────────────────────────────────────────────────────
(async () => {
  await initAuth();
  renderAuthUI();
})();
