# Enquiry Worker

`POST https://sidjashnani.com/api/enquiry`

The site itself stays on GitHub Pages. This Worker is attached only to the route `sidjashnani.com/api/enquiry*` on the Cloudflare zone, so the form never exposes a public calendar link.

It checks, in order:

1. Same-site origin (`https://sidjashnani.com` or `www`).
2. Body size (32 KB).
3. Honeypot field `fax`. A filled honeypot is discarded and the Worker replies `200 { "ok": true }` so a bot is not told how it was caught.
4. Time on page. `ts` must be at least 3 seconds old and no more than 24 hours old.
5. Field shape, allow-lists, and length limits.
6. Cloudflare Turnstile, verified server-side at `challenges.cloudflare.com/turnstile/v0/siteverify`. Tokens are single-use.
7. Email to **sj@momentumdojo.com**, with Reply-To set to the person who wrote in.

There is no fee, calendar URL, or inbox address in the Worker response.

## 1. Turnstile

The public site key for the Managed widget on `sidjashnani.com` is already in `index.html`:

```html
window.SID_TURNSTILE_SITE_KEY = "0x4AAAAAAFfqKdXejbHSDzYp";
```

That file is served by GitHub Pages. The site key is public. The secret is not, and it is already stored on the live Worker. Never commit the secret.

The form treats a key as ready when it is non-empty and does not start with `YOUR_`. With the key above, the page loads the Turnstile widget and does not show “This enquiry form is not accepting messages yet.”

Local dummy keys (always pass, not for production):

- Site key: `1x00000000000000000000AA`
- Secret: `1x0000000000000000000000000000000AA`

## 2. Where the email goes

Preferred path: Cloudflare Email Service `send_email` binding, locked to one verified destination. This works for a verified destination even when you only use it for outbound notification, and it does not require a public booking link.

Do **not** turn on inbound Email Routing for `sidjashnani.com` unless Sid wants Cloudflare to receive mail for that domain. Inbound routing replaces the domain's MX records. Creating a destination address does not.

From `worker/`:

```bash
npm ci
npx wrangler login
npx wrangler email routing addresses create sj@momentumdojo.com
```

Open the verification message sent to sj@momentumdojo.com and confirm the address. Check it with:

```bash
npx wrangler email routing addresses list
```

Then onboard **sending** for the From address `enquiry@sidjashnani.com`. Sending uses the `cf-bounce` subdomain and does not replace apex MX.

```bash
npx wrangler email sending enable sidjashnani.com
npx wrangler email sending dns get sidjashnani.com
```

Confirm SPF, DKIM, and DMARC in the dashboard under **Compute → Email Service → Email Sending**. If the domain already has an SPF record, merge it. Do not publish two SPF TXT records. Start DMARC at `p=none` if this domain has not sent mail before.

`FROM_EMAIL` must stay on `sidjashnani.com`. `TO_EMAIL` must stay `sj@momentumdojo.com`, matching `destination_address` in `wrangler.jsonc`.

### Resend instead

If Email Service sending is not available yet:

1. Verify `sidjashnani.com` in Resend.
2. In `wrangler.jsonc`, set `"MAIL_PROVIDER": "resend"`.
3. `npx wrangler secret put RESEND_API_KEY`

Leave the `send_email` binding in place. With `MAIL_PROVIDER=resend` the Worker does not call it. Set `MAIL_PROVIDER` back to `email` to return to the binding.

## 3. Deploy

The live Worker **sidjashnani-enquiry** is already deployed through the Cloudflare API, not through Wrangler. It answers `sidjashnani.com/api/enquiry*`. The Turnstile secret and the `send_email` binding are already configured. A POST with no Turnstile token returns `400 {"error":"turnstile"}`.

`wrangler.jsonc` keeps that same route pattern (`sidjashnani.com/api/enquiry*`) so the config matches what is live.

**A future `wrangler deploy` overwrites that API deployment** — the script, the route, the vars in `wrangler.jsonc`, and any binding the deploy does not resend. Run it only when you mean to replace the live Worker. Confirm `TURNSTILE_SECRET_KEY` is still set first, and do not change the route pattern.

```bash
cd worker
npm ci
npx wrangler secret put TURNSTILE_SECRET_KEY
npx wrangler deploy
```

`workers_dev` and preview URLs are off so the endpoint is not also published on `*.workers.dev`.

Secrets and vars:

| Name | Kind | Value |
| --- | --- | --- |
| `TURNSTILE_SECRET_KEY` | secret | Turnstile secret key |
| `RESEND_API_KEY` | secret, only for Resend | Resend API key |
| `MAIL_PROVIDER` | var | `email` (default) or `resend` |
| `TO_EMAIL` | var | `sj@momentumdojo.com` |
| `FROM_EMAIL` | var | `enquiry@sidjashnani.com` |
| `FROM_NAME` | var | `Sid Jashnani website` |

A later `wrangler deploy` replaces the Worker that was deployed via the API, and it writes the vars in `wrangler.jsonc` over dashboard edits of those same names. Keep the inbox, From address, and route `sidjashnani.com/api/enquiry*` in this file.

For a local check, copy nothing into git:

```bash
printf 'TURNSTILE_SECRET_KEY=1x0000000000000000000000000000000AA\n' > .dev.vars
npx wrangler dev
```

`.dev.vars` is gitignored.

## 4. Rate limit (do this in Cloudflare)

The Worker cannot count requests across isolates. Add a zone rate-limiting rule as well.

**Pro, Business, or Enterprise** (use this if the plan allows a 60 second window):

- Dashboard: **Security → Security rules → Rate limiting rules → Create**
- Expression: `(http.host eq "sidjashnani.com" and http.request.uri.path eq "/api/enquiry")`
- Characteristic: IP
- Period: 60 seconds
- Requests per period: 5
- Action: Block
- Duration: 10 minutes
- Also count `www.sidjashnani.com` if that host is proxied and not redirected

**Free plan** (one rule, 10 second period only):

- Field: URI Path equals `/api/enquiry`
- Period: 10 seconds
- Requests: 5
- Block for 10 seconds

Turnstile, the honeypot, and the 3 second floor still apply on every plan. The WAF rule is the extra brake on repeated posts.

## 5. Try it

The site key is in `index.html` and the Worker is already answering `https://sidjashnani.com/api/enquiry`. Submit the form on https://sidjashnani.com/#contact. A success ends with “Your enquiry has been sent.” A failure says that nothing was delivered. The page never shows an email address.

The page loads Turnstile with `0x4AAAAAAFfqKdXejbHSDzYp` and does not take the “not accepting messages yet” path. A POST with no token is rejected as `400 {"error":"turnstile"}`.

## Rocket Loader

Rocket Loader is on for this zone and can defer or rewrite the analytics and Turnstile scripts. The page marks those scripts `data-cfasync="false"`. The reliable fix is to turn Rocket Loader off for `sidjashnani.com` (**Speed → Optimization → Rocket Loader**). Email Address Obfuscation can stay off; the pages contain no email address to obfuscate.
