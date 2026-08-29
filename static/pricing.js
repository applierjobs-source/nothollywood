// ═══════════════════════════════════════════════════════════════════════
// Pricing page — auth chip + billing toggle + checkout stubs
// Stripe integration comes in the next phase.
// ═════════════════════════════════════════════════════════════════════════

const API = window.location.origin;
const $ = (sel) => document.querySelector(sel);

// ─── Auth widgets (reuse the same pattern as app.js) ───────────────
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
  // Bounce to homepage where the auth modal lives
  window.location.href = "/?auth=1";
});
$("#navSignOutBtn").addEventListener("click", async () => {
  if (supabase) await supabase.auth.signOut();
});

// ─── Billing toggle ────────────────────────────────────────────────
const btOpts = document.querySelectorAll(".bt-opt");
btOpts.forEach((opt) => {
  opt.addEventListener("click", () => {
    btOpts.forEach((o) => {
      o.classList.toggle("active", o === opt);
      o.setAttribute("aria-checked", o === opt ? "true" : "false");
    });
    const period = opt.dataset.billing; // "monthly" | "annual"
    document.querySelectorAll(".price-amount[data-monthly]").forEach((el) => {
      el.textContent = period === "annual" ? el.dataset.annual : el.dataset.monthly;
    });
    document.querySelectorAll(".price-period").forEach((el) => {
      el.textContent = period === "annual" ? "/month, billed yearly" : "/month";
    });
  });
});

// ─── Checkout CTAs (stub — real Stripe wiring in next phase) ───────
document.querySelectorAll("[data-checkout]").forEach((btn) => {
  btn.addEventListener("click", async (e) => {
    e.preventDefault();
    const tier = btn.dataset.checkout;
    if (authRequired && !currentUser) {
      // Bounce to homepage for sign-in, then come back to pricing
      window.location.href = `/?auth=1&next=/pricing.html&tier=${tier}`;
      return;
    }
    // TODO: POST /api/checkout/create-session with { tier, billing }
    alert(
      `Stripe checkout for the "${tier}" tier is being wired up. ` +
      "You'll be redirected to a Stripe-hosted payment page here in the next update."
    );
  });
});

// ─── Init ──────────────────────────────────────────────────────────
(async () => {
  await initAuth();
  renderAuthUI();
})();
