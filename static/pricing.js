// ═══════════════════════════════════════════════════════════════════════
// Pricing page — credit packs + cost calculator
// Stripe integration comes in the next phase.
// ═════════════════════════════════════════════════════════════════════════

const API = window.location.origin;
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

// ─── Pack config (must stay in sync with pricing.html) ─────────────
const PACKS = [
  { id: "starter",     name: "Starter",     price: 15,  credits: 75,   available: true  },
  { id: "studio",      name: "Studio",      price: 75,  credits: 500,  available: true  },
  { id: "feature",     name: "Feature",     price: 250, credits: 1800, available: false },
  { id: "blockbuster", name: "Blockbuster", price: 500, credits: 3600, available: false },
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
  // Prefer smallest single available pack that covers the need
  for (const p of PACKS) {
    if (p.available && p.credits >= creditsNeeded) {
      return { label: p.name, totalCost: p.price };
    }
  }
  // Coming-soon single pack that covers the need — show with hint
  for (const p of PACKS) {
    if (!p.available && p.credits >= creditsNeeded) {
      return { label: `${p.name} (coming soon)`, totalCost: p.price };
    }
  }
  // Beyond all packs — need multiple Blockbusters
  const biggest = PACKS[PACKS.length - 1];
  const count = Math.ceil(creditsNeeded / biggest.credits);
  return {
    label: `${count}× ${biggest.name} (coming soon)`,
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
    const price = btn.dataset.price;
    const comingSoon = btn.dataset.comingSoon === "1";

    if (comingSoon) {
      const email = prompt(
        `The ${pack.charAt(0).toUpperCase() + pack.slice(1)} pack ($${price}) launches soon. ` +
        `Drop your email and we'll notify you the day it opens:`
      );
      if (email && /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email.trim())) {
        try {
          await fetch(`${API}/api/notify-waitlist`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ pack, email: email.trim() }),
          }).catch(() => {});
        } catch (_) {}
        alert(`Got it. We'll email ${email.trim()} when the ${pack} pack launches.`);
      } else if (email !== null) {
        alert("That didn't look like a valid email — try again from the button.");
      }
      return;
    }

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
