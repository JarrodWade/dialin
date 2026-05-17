# Clerk setup (dialin)

This app can authenticate with **Clerk** in the browser (**`Authorization: Bearer`**) while **Lambda** verifies the JWT using Clerk’s **JWKS** (same Frontend API URL as token **`iss`**). Routes stay **authorization NONE** at API Gateway on purpose — Clerk session tokens usually omit **`aud`**, which blocks API Gateway JWT authorizers. Legacy mode stays available when `clerk_jwt_issuer` is empty (`ALLOW_CLIENT_USER_ID=true`).

### If Clerk’s docs or agent skills show “Add Clerk to Next.js”

That path (`npx create-next-app`, `npm install @clerk/nextjs`, `proxy.ts` + `clerkMiddleware()`, `<ClerkProvider>` in `app/layout.tsx`, `<Show>` / `<UserButton>`) is **only for Next.js App Router**. **Skip all of it for dialin** unless you migrate the UI to Next.

Dialin uses **static HTML + `web/shared.js`**, which loads **`@clerk/clerk-js`** from a CDN and mounts sign-in / user UI into `#clerk-mount`. The API URL you paste hits **API Gateway** then **Lambda**; identity is **`sub`** after JWT verification (`clerk_jwt.py`). For official docs, prefer Clerk’s **JavaScript / vanilla** (Clerk JS) guides, not the Next.js quickstart.

---

## 1. Create a Clerk application

1. Sign up at [Clerk Dashboard](https://dashboard.clerk.com/).
2. Create an application (or use an existing one).
3. Under **Configure → User & authentication**, enable the sign-in methods you want (e.g. Google, Apple, email).

---

## 2. “Where do I put localhost?” (often: nowhere)

Clerk’s dashboard **does not always show a dedicated “localhost URL” field**, and for a **Development** instance it often **allows `http://localhost` (any port) by default** when you use a **`pk_test_…`** key. So you can try **skipping URL configuration** first: set the publishable key, run `make ui`, sign in. If it works, you are done.

Dialin mounts sign-in with **hash routing** (`routing: "hash"`), which keeps more of the flow on the same origin and avoids needing separate `/sign-in` routes like Next apps.

**If sign-in or OAuth fails** (redirect errors, “redirect_uri mismatch”, or “not allowed”):

1. Open **[Paths](https://dashboard.clerk.com/~/paths)** for your app (Clerk’s sidebar: **Configure** → **Paths**, or that direct link). Set **Application paths** / home / sign-in URLs to match how you host the UI (e.g. app root `http://localhost:8000/` if that is where you load the HTML).
2. Open **[Domains](https://dashboard.clerk.com/~/domains)** when you move to **Production** (`pk_live_…`): production keys expect a **real domain** and DNS per Clerk; localhost is for development keys only.
3. For **Google / GitHub / etc.**, you may also need the provider’s console to allow Clerk’s callback URLs — Clerk usually documents this under that provider’s **Social connections** setup in the dashboard.

Labels move between releases; if you cannot find “Paths”, use the dashboard search or Clerk’s docs for **“Paths”** or **“redirect URLs”**.

---

## 3. Publishable key → `web/dialin-config.js` (local, gitignored)

1. **Configure → API Keys** (or **Developers → API keys**).
2. Copy the **Publishable key** (`pk_test_…` or `pk_live_…`).
3. `cp web/dialin-config.example.js web/dialin-config.js` and set the key:

```javascript
window.DIALIN_CONFIG = window.DIALIN_CONFIG || {
  clerkPublishableKey: "pk_test_xxxxxxxx",
};
```

Alternatively leave that file empty and set the key in the browser once:

```js
localStorage.setItem("dialin.clerkPk", "pk_test_xxxxxxxx");
```

The UI loads `@clerk/clerk-js` and sends `Authorization: Bearer <session JWT>` on API calls when a key is configured.

---

## 4. JWT issuer → Terraform

Lambda verifies the Bearer token against **Clerk’s JWKS**. The token’s **`iss`** must match **`clerk_jwt_issuer`** exactly (your **Frontend API URL**).

1. In **Configure → API Keys** (or **Domains**), copy the **Frontend API URL** (e.g. `https://<something>.clerk.accounts.dev`).

2. In `terraform/terraform.tfvars`:

```hcl
clerk_jwt_issuer = "https://your-instance.clerk.accounts.dev"
```

3. **`clerk_jwt_audience`** — ignore it; Terraform keeps the variable unused. Session JWTs often have **no `aud`** — that’s normal.

4. Apply (**requires Python 3 + pip + internet**: Terraform installs `lambda/requirements.txt` into `lambda/build/` before zipping):

```bash
cd terraform && terraform init
terraform apply
```

When `clerk_jwt_issuer` is non-empty, **`ALLOW_CLIENT_USER_ID=false`**; identity comes from verified JWT **`sub`** only.

---

## 5. Smoke test

1. Paste the **API URL** into the UI and sign in.

2. In DevTools → **Network**, confirm requests carry **`Authorization: Bearer …`**.

3. Still **401**? Check **`iss`** equals `clerk_jwt_issuer`, Lambda egress can reach JWKS (no VPC egress issues), and you deployed a bundle **with deps** (**`terraform apply`** or **`make deploy-lambda`**, not a raw `zip` of only `.py` files).

---

## 6. Turning Clerk off (legacy / local)

- Remove or blank out `clerk_jwt_issuer` in `terraform.tfvars`, then **`terraform apply`**.

- Clear `clerkPublishableKey` and `localStorage.dialin.clerkPk`.

- The manual **User id** returns; **`ALLOW_CLIENT_USER_ID=true`**.

---

## References

- [Clerk session tokens](https://clerk.com/docs/backend-requests/resources/session-tokens)
- [Manual JWT verification (issuer + JWKS)](https://clerk.com/docs/request-authentication/validate-session-tokens) — analogous to Lambda’s JWKS verification in `clerk_jwt.py`
