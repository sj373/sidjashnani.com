/**
 * Enquiry endpoint for sidjashnani.com.
 * POST /api/enquiry — Turnstile, honeypot, minimum time on page, then email.
 *
 * Preferred delivery is the Cloudflare Email Service send_email binding
 * (verified destination). Set MAIL_PROVIDER=resend and RESEND_API_KEY to use Resend instead.
 * A zone rate-limiting rule is still required. See README.md.
 */

const MAX_BODY_BYTES = 32_000;
const MIN_ELAPSED_MS = 3_000;
const MAX_ELAPSED_MS = 24 * 60 * 60 * 1000;
const FUTURE_SKEW_MS = 60_000;

const TYPES = new Set(["keynote", "workshop", "advisory", "other"]);
const AUDIENCES = new Set(["", "Under 50", "50–200", "200–500", "500+"]);
const REVENUE_BANDS = new Set([
  "",
  "Under $50M",
  "$50M–$100M",
  "$100M–$250M",
  "$250M–$500M",
  "Over $500M",
]);

const LIMITS = {
  name: [1, 100],
  email: [6, 254],
  company: [1, 200],
  role: [0, 120],
  message: [10, 4000],
  event_location: [0, 200],
  budget: [0, 80],
  event_date: [0, 10],
  audience_size: [0, 20],
  revenue_band: [0, 40],
};

const TYPE_LABELS = {
  keynote: "Keynote",
  workshop: "Workshop",
  advisory: "Advisory",
  other: "Other",
};

const JSON_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "no-store",
  "x-content-type-options": "nosniff",
  "referrer-policy": "no-referrer",
};

export default {
  async fetch(request, env) {
    return handleRequest(request, env);
  },
};

export async function handleRequest(request, env, deps = {}) {
  const now = deps.now ? deps.now() : Date.now();
  const url = new URL(request.url);

  if (request.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: { "cache-control": "no-store" } });
  }

  if (request.method !== "POST" || !url.pathname.endsWith("/api/enquiry")) {
    return respond(request, 405, { ok: false, error: "method" });
  }

  if (!originAllowed(request)) {
    return respond(request, 403, { ok: false, error: "forbidden" });
  }

  const declared = Number(request.headers.get("content-length") || 0);
  if (declared > MAX_BODY_BYTES) {
    return respond(request, 413, { ok: false, error: "too_large" });
  }

  let raw;
  try {
    raw = await request.text();
  } catch {
    return respond(request, 400, { ok: false, error: "validation" });
  }
  if (raw.length > MAX_BODY_BYTES) {
    return respond(request, 413, { ok: false, error: "too_large" });
  }

  let input;
  try {
    input = parseBody(request, raw);
  } catch {
    return respond(request, 400, { ok: false, error: "validation" });
  }

  // Filled honeypot: pretend it worked so the bot does not adapt.
  if (cleanLine(input.fax).length > 0) {
    return respond(request, 200, { ok: true });
  }

  const timing = checkTiming(input.ts, now);
  if (!timing.ok) {
    return respond(request, 400, { ok: false, error: timing.error });
  }

  const validated = validateEnquiry(input);
  if (!validated.ok) {
    return respond(request, 400, { ok: false, error: "validation", fields: validated.fields });
  }

  const token = cleanLine(input.turnstileToken);
  if (!token || token.length > 2048) {
    return respond(request, 400, { ok: false, error: "turnstile" });
  }
  if (!env.TURNSTILE_SECRET_KEY) {
    return respond(request, 503, { ok: false, error: "unavailable" });
  }

  const verify = deps.verifyTurnstile || verifyTurnstile;
  let verdict;
  try {
    verdict = await verify(token, env.TURNSTILE_SECRET_KEY, clientIp(request));
  } catch {
    return respond(request, 400, { ok: false, error: "turnstile" });
  }
  if (!verdict || verdict.success !== true) {
    return respond(request, 400, { ok: false, error: "turnstile" });
  }

  const mail = buildEmail(validated.value, env, {
    ip: clientIp(request),
    submittedAt: new Date(now).toISOString(),
  });

  try {
    await deliver(env, mail, deps);
  } catch (error) {
    const code = error && error.code ? error.code : "delivery";
    console.error("enquiry delivery failed", code);
    const status = code === "not_configured" ? 503 : 502;
    return respond(request, status, { ok: false, error: "unavailable" });
  }

  return respond(request, 200, { ok: true });
}

export function validateEnquiry(input) {
  const fields = [];
  const value = {};

  value.type = cleanLine(input.type).toLowerCase();
  if (!TYPES.has(value.type)) fields.push("type");

  value.name = cleanLine(input.name);
  value.email = cleanLine(input.email);
  value.company = cleanLine(input.company);
  value.role = cleanLine(input.role);
  value.message = cleanMessage(input.message);
  value.event_date = cleanLine(input.event_date);
  value.event_location = cleanLine(input.event_location);
  value.audience_size = cleanLine(input.audience_size);
  value.budget = cleanLine(input.budget);
  value.revenue_band = cleanLine(input.revenue_band);

  for (const key of ["name", "email", "company", "role", "message", "event_location", "budget", "event_date", "audience_size", "revenue_band"]) {
    const [min, max] = LIMITS[key];
    const len = value[key].length;
    if (len < min || len > max) fields.push(key);
  }

  if (value.email && !isEmail(value.email)) fields.push("email");
  if (value.event_date && !isDate(value.event_date)) fields.push("event_date");
  if (!AUDIENCES.has(value.audience_size)) fields.push("audience_size");
  if (!REVENUE_BANDS.has(value.revenue_band)) fields.push("revenue_band");

  const unique = [...new Set(fields)];
  if (unique.length) return { ok: false, fields: unique };
  return { ok: true, value };
}

export function buildEmail(value, env, meta) {
  const to = env.TO_EMAIL || "sj@momentumdojo.com";
  const fromEmail = env.FROM_EMAIL || "enquiry@sidjashnani.com";
  const fromName = cleanLine(env.FROM_NAME || "Sid Jashnani website");
  const label = TYPE_LABELS[value.type] || "Enquiry";
  const subject = oneLine(`Website enquiry (${label}): ${value.name} — ${value.company}`).slice(0, 180);

  const lines = [
    "New enquiry from sidjashnani.com",
    "",
    `Type: ${label}`,
    `Name: ${value.name}`,
    `Work email: ${value.email}`,
    `Company: ${value.company}`,
    value.role ? `Role: ${value.role}` : "",
    "",
    "Message:",
    value.message,
  ];

  const eventBits = [
    value.event_date ? `Event date: ${value.event_date}` : "",
    value.event_location ? `Location: ${value.event_location}` : "",
    value.audience_size ? `Audience size: ${value.audience_size}` : "",
    value.budget ? `Budget noted by the sender: ${value.budget}` : "",
  ].filter(Boolean);
  if (eventBits.length) lines.push("", ...eventBits);
  if (value.revenue_band) lines.push("", `Annual revenue band: ${value.revenue_band}`);

  lines.push("", `Sent: ${meta.submittedAt}`);
  if (meta.ip) lines.push(`IP: ${meta.ip}`);
  lines.push("", "Reply to this message to respond to the sender.");

  const text = lines.filter((line, index, all) => !(line === "" && all[index - 1] === "")).join("\n");
  const html = textToHtml(text);

  return {
    to,
    from: { email: fromEmail, name: fromName },
    replyTo: { email: value.email, name: value.name },
    subject,
    text,
    html,
  };
}

async function deliver(env, mail, deps) {
  const provider = String(env.MAIL_PROVIDER || "email").toLowerCase();
  if (provider === "resend") {
    if (!env.RESEND_API_KEY) {
      const error = new Error("Resend is not configured");
      error.code = "not_configured";
      throw error;
    }
    const fetchImpl = deps.fetch || fetch;
    const response = await fetchImpl("https://api.resend.com/emails", {
      method: "POST",
      headers: {
        authorization: `Bearer ${env.RESEND_API_KEY}`,
        "content-type": "application/json",
      },
      body: JSON.stringify({
        from: `${mail.from.name} <${mail.from.email}>`,
        to: [mail.to],
        reply_to: mail.replyTo.email,
        subject: mail.subject,
        text: mail.text,
        html: mail.html,
      }),
    });
    if (!response.ok) {
      const error = new Error("Resend rejected the message");
      error.code = `resend_${response.status}`;
      throw error;
    }
    return;
  }

  if (!env.EMAIL || typeof env.EMAIL.send !== "function") {
    const error = new Error("Email binding is not configured");
    error.code = "not_configured";
    throw error;
  }
  await env.EMAIL.send({
    to: mail.to,
    from: mail.from,
    replyTo: mail.replyTo,
    subject: mail.subject,
    text: mail.text,
    html: mail.html,
  });
}

async function verifyTurnstile(token, secret, remoteip) {
  const body = { secret, response: token };
  if (remoteip) body.remoteip = remoteip;
  const response = await fetch("https://challenges.cloudflare.com/turnstile/v0/siteverify", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(8000),
  });
  if (!response.ok) return { success: false };
  return response.json();
}

function parseBody(request, raw) {
  const type = request.headers.get("content-type") || "";
  if (type.includes("application/json")) {
    const data = JSON.parse(raw);
    if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("invalid json");
    return data;
  }
  if (type.includes("application/x-www-form-urlencoded") || type === "") {
    const params = new URLSearchParams(raw);
    return Object.fromEntries(params.entries());
  }
  throw new Error("unsupported type");
}

function checkTiming(ts, now) {
  const started = Number(ts);
  if (!Number.isFinite(started)) return { ok: false, error: "too_fast" };
  const elapsed = now - started;
  if (elapsed < -FUTURE_SKEW_MS || elapsed > MAX_ELAPSED_MS) return { ok: false, error: "expired" };
  if (elapsed < MIN_ELAPSED_MS) return { ok: false, error: "too_fast" };
  return { ok: true };
}

function originAllowed(request) {
  const origin = request.headers.get("origin");
  if (!origin) return true;
  let parsed;
  try {
    parsed = new URL(origin);
  } catch {
    return false;
  }
  const host = parsed.hostname;
  if (host === "sidjashnani.com" || host === "www.sidjashnani.com") return parsed.protocol === "https:";
  if (host === "localhost" || host === "127.0.0.1") return parsed.protocol === "http:" || parsed.protocol === "https:";
  return false;
}

function clientIp(request) {
  return request.headers.get("cf-connecting-ip") || "";
}

function cleanLine(value) {
  return oneLine(value).replace(/[\u0000-\u001F\u007F]/g, "").trim();
}

function oneLine(value) {
  return String(value ?? "").replace(/[\r\n]+/g, " ").replace(/\s+/g, " ").trim();
}

function cleanMessage(value) {
  let text = String(value ?? "").replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  text = text.replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g, "");
  text = text.replace(/\n{3,}/g, "\n\n").trim();
  return text;
}

function isEmail(value) {
  if (value.length > 254 || /\s/.test(value)) return false;
  return /^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$/.test(value);
}

function isDate(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const [year, month, day] = value.split("-").map(Number);
  if (year < 2024 || year > 2032) return false;
  const date = new Date(Date.UTC(year, month - 1, day));
  return date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day;
}

function textToHtml(text) {
  const escaped = text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  return `<!DOCTYPE html><html><body><pre style="font-family:sans-serif;white-space:pre-wrap">${escaped}</pre></body></html>`;
}

function wantsHtml(request) {
  const accept = request.headers.get("accept") || "";
  const type = request.headers.get("content-type") || "";
  if (type.includes("application/json")) return false;
  if (accept.includes("application/json")) return false;
  return true;
}

function respond(request, status, payload) {
  if (!wantsHtml(request)) {
    return new Response(JSON.stringify(payload), { status, headers: JSON_HEADERS });
  }
  const ok = payload.ok === true;
  const title = ok ? "Enquiry sent" : "Enquiry not sent";
  const message = ok
    ? "Thank you. Your enquiry has been sent. Sid will reply with next steps."
    : htmlErrorCopy(payload.error);
  const body = `<!DOCTYPE html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>${title} | Sid Jashnani</title>
<style>
  body{margin:0;min-height:100vh;display:grid;place-items:center;background:#012A6A;color:#fff;font-family:system-ui,sans-serif;padding:32px}
  main{max-width:38rem}
  h1{font-size:2rem;line-height:1.15;margin:0 0 12px}
  p{font-size:1.05rem;line-height:1.5;color:rgba(255,255,255,.88)}
  a{color:#012A6A;background:#6DBF5B;text-decoration:none;font-weight:700;display:inline-block;margin-top:18px;padding:12px 18px;border-radius:8px}
</style>
</head>
<body>
<main>
  <h1>${title}</h1>
  <p>${escapeHtml(message)}</p>
  <a href="/#contact">Back to the form</a>
</main>
</body>
</html>`;
  return new Response(body, {
    status,
    headers: {
      "content-type": "text/html; charset=utf-8",
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
      "referrer-policy": "no-referrer",
    },
  });
}

function htmlErrorCopy(code) {
  const copy = {
    too_fast: "Please wait a few seconds and try again. Nothing was sent.",
    expired: "This page has been open for a while. Refresh it and send the enquiry again. Nothing was sent.",
    validation: "Please check the form and try again. Nothing was sent.",
    turnstile: "The spam check failed. Please use the form on the site and try again. Nothing was sent.",
    unavailable: "We could not send your enquiry. Nothing was delivered. Please try again later.",
    too_large: "That message is too long. Please shorten it and try again. Nothing was sent.",
    forbidden: "We could not send your enquiry. Nothing was delivered.",
    method: "This address only accepts enquiry form submissions.",
  };
  return copy[code] || "We could not send your enquiry. Nothing was delivered.";
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[char]);
}
