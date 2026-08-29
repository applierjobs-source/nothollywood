// ═══════════════════════════════════════════════════════════════════════
// Pricing page — credit packs + cost calculator
// Stripe integration comes in the next phase.
// ═════════════════════════════════════════════════════════════════════════

const API = window.location.origin;
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

// ─── Pack config (must stay in sync with pricing.html) ─────────────
const PACKS = [
  { id: "starter",     name: "Starter",     price: 15,  credits: 75   },
  { id: "studio",      name: "Studio",      price: 75,  credits: 500  },
  { id: "feature",     name: "Feature",     price: 250, credits: 1800 },
  { id: "blockbuster", name: "Blockbuster", price: 500, credits: 3600 },
];

// ─── Auth widgets ──────────────────────────────────────────────────
let sb = null;
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
    sb = window.supabase.createClient(cfg.supabase_url, cfg.supabase_anon_key, {
      auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true },
    });
    const { data: sessData } = await sb.auth.getSession();
    if (sessData?.session) currentUser = sessData.session.user;
    sb.auth.onAuthStateChange((_e, session) => {
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
  if (sb) await sb.auth.signOut();
});

// ─── Cost calculator ───────────────────────────────────────────────
const calcDuration = $("#calcDuration");
const calcDurationDisplay = $("#calcDurationDisplay");
const calcCredits = $("#calcCredits");
const calcPack = $("#calcPack");
const calcTotal = $("#calcTotal");
let calcResolution = "768"; // "768" | "2k"

function formatDuration(seconds) {
  if (seconds < 60) return `${seconds} sec`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  if (m < 60) return s === 0 ? `${m} min` : `${m} min ${s} sec`;
  const h = Math.floor(m / 60);
  const rem = m % 60;
  if (rem === 0) return h === 1 ? `1 hour` : `${h} hours`;
  return `${h} hr ${rem} min`;
}

function updateCalculator() {
  const seconds = parseInt(calcDuration.value, 10);
  const creditsPerSec = calcResolution === "2k" ? 2 : 1;
  const creditsNeeded = seconds * creditsPerSec;

  calcDurationDisplay.textContent = formatDuration(seconds);
  calcCredits.textContent = creditsNeeded.toLocaleString();

  const recommended = recommendPack(creditsNeeded);
  calcPack.textContent = recommended.label;
  calcTotal.textContent = `$${recommended.totalCost.toLocaleString()}`;
}

function recommendPack(creditsNeeded) {
  // Prefer smallest single pack that covers the need
  for (const p of PACKS) {
    if (p.credits >= creditsNeeded) {
      return { label: p.name, totalCost: p.price };
    }
  }
  // Beyond all packs — need multiple Blockbusters
  const biggest = PACKS[PACKS.length - 1];
  const count = Math.ceil(creditsNeeded / biggest.credits);
  return {
    label: `${count}× ${biggest.name}`,
    totalCost: biggest.price * count,
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

// ─── Pack CTAs ─────────────────────────────────────────────────────
$$("[data-checkout]").forEach((btn) => {
  btn.addEventListener("click", async (e) => {
    e.preventDefault();
    const pack = btn.dataset.checkout;
    const originalLabel = btn.textContent;

    // Require sign-in if auth is enabled
    if (authRequired && !currentUser) {
      window.location.href = `/?auth=1&next=/pricing.html&pack=${pack}`;
      return;
    }

    btn.textContent = "Opening checkout…";
    btn.style.pointerEvents = "none";

    try {
      // Attach Supabase JWT so the backend can associate the purchase with the user
      const headers = { "Content-Type": "application/json" };
      if (sb) {
        const { data } = await sb.auth.getSession();
        if (data?.session?.access_token) {
          headers["Authorization"] = `Bearer ${data.session.access_token}`;
        }
      }

      const res = await fetch(`${API}/api/create-checkout-session`, {
        method: "POST",
        headers,
        body: JSON.stringify({
          pack,
          success_url: `${window.location.origin}/?checkout=success&pack=${pack}`,
          cancel_url: `${window.location.origin}/pricing.html?checkout=cancelled`,
        }),
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.error || `Checkout failed (${res.status})`);
      }

      const { url } = await res.json();
      if (!url) throw new Error("No checkout URL returned");
      window.location.href = url;
    } catch (err) {
      console.error("checkout error", err);
      alert(
        `Couldn't start checkout for the ${pack} pack.\n\n` +
        `${err.message || err}\n\n` +
        `Stripe may still be getting set up on this deployment. Try again in a few minutes.`
      );
      btn.textContent = originalLabel;
      btn.style.pointerEvents = "";
    }
  });
});

// ─── Init ──────────────────────────────────────────────────────────
(async () => {
  await initAuth();
  renderAuthUI();
})();
