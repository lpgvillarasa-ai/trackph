# Deploying TrackPH to Vercel

TrackPH now runs in two modes:

- **Local / LAN** (same as before): `python server.py` — uses a SQLite file, no setup needed.
- **Vercel (cloud)**: serverless Flask + a hosted Postgres database via the
  `DATABASE_URL` environment variable.

## 1. Database (required, free)

Vercel's serverless platform can't keep a SQLite file, so the cloud deployment
needs Postgres. The easiest free option:

1. Vercel dashboard → your **trackph** project → **Storage** tab
2. **Create Database** → **Neon (Serverless Postgres)** → free plan → **Connect**
3. This automatically adds `DATABASE_URL` to the project's environment variables.

Any other Postgres works too (e.g. a Supabase project) — just set `DATABASE_URL`
to its connection string in **Settings → Environment Variables**.

Tables are created automatically on first request — no migrations to run.

## 2. Other environment variables

| Variable | Required | Purpose |
|---|---|---|
| `DATABASE_URL` | yes | Postgres connection string (auto-set by Neon storage) |
| `SECRET_KEY` | yes | Session cookie signing — set to a long random string |
| `GOOGLE_CLIENT_ID` | for Google login | From Google Cloud Console |
| `GOOGLE_CLIENT_SECRET` | for Google login | From Google Cloud Console |
| `ALLOWED_IPS` | optional | Extra comma-separated always-allowed IPs (in addition to the ones managed in Admin → Settings) |
| `ADMIN_EMAILS` | optional | Comma-separated Google emails that are always admins (default: lpg.villarasa@gmail.com). Signing in with Google as one of these goes straight to the admin dashboard — no PIN. |

Generate a secret key with: `python -c "import secrets; print(secrets.token_hex(32))"`

After adding/changing env vars: **Deployments → ⋯ → Redeploy**.

## 3. Google sign-in setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/) → create a
   project (or reuse one) → **APIs & Services → OAuth consent screen**:
   - User type: **External**, fill in app name + your email, add test users
     (or publish the app so any Google account can be used).
2. **APIs & Services → Credentials → Create Credentials → OAuth client ID**:
   - Application type: **Web application**
   - Authorized redirect URI: `https://YOUR-DOMAIN.vercel.app/auth/google/callback`
3. Copy the **Client ID** and **Client Secret** into the Vercel env vars above
   and redeploy.

How it's used in the app:

- **Admin**: log in with your PIN once → Settings → *Google Sign-In (Admin)* →
  **Connect my Google account**. From then on you can enter the admin dashboard
  with the "Sign in with Google" button on the PIN screen.
- **Subcontractors**: they log in with their password once → tap
  **🔗 Connect Google** in the top bar. From then on they can use
  **Continue with Google** on the login screen.

## 4. IP restriction (lock the site to your network)

1. Log in as admin → **Settings → IP Access Restriction**.
2. Press **➕ Allow my current IP** while you're on the connection you want to
   allow (e.g. your office internet). Everyone on that same connection —
   you and your team — can use the site; everyone else gets a locked page.
3. Add more IPs the same way (e.g. from home), remove them with ✕, or press
   **Disable restriction** to open the site up again.

**If your IP changes and you're locked out:** the locked page has an admin PIN
field — enter your PIN, tick *"Also allow this IP from now on"*, and you're in.

## 5. First things to do after deploying

1. Open the site → Admin → default PIN is **1234** → **change it immediately**
   (Settings → Admin PIN), since the site starts publicly reachable.
2. Connect your Google account (Settings).
3. Lock the site to your IP (Settings → IP Access Restriction).
4. Add your subcontractors (Team tab).
