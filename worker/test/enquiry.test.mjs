import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { buildEmail, handleRequest, validateEnquiry } from "../src/index.js";

const NOW = 1_700_000_000_000;

function enquiry(overrides = {}) {
  return {
    type: "keynote",
    name: "Ada Lovelace",
    email: "ada@example.com",
    company: "Analytical Engines",
    role: "Organiser",
    message: "We would like Sid to speak at our leadership offsite in London.",
    event_date: "2026-11-12",
    event_location: "London",
    audience_size: "50–200",
    budget: "",
    revenue_band: "",
    fax: "",
    ts: String(NOW - 10_000),
    turnstileToken: "token-ok",
    ...overrides,
  };
}

function env(overrides = {}) {
  return {
    TURNSTILE_SECRET_KEY: "secret",
    TO_EMAIL: "sj@momentumdojo.com",
    FROM_EMAIL: "enquiry@sidjashnani.com",
    FROM_NAME: "Sid Jashnani website",
    MAIL_PROVIDER: "email",
    EMAIL: {
      async send(message) {
        env.last = message;
        return { messageId: "msg_1" };
      },
    },
    ...overrides,
  };
}

function request(body, { headers = {}, method = "POST", url = "https://sidjashnani.com/api/enquiry" } = {}) {
  return new Request(url, {
    method,
    headers: {
      "content-type": "application/json",
      accept: "application/json",
      origin: "https://sidjashnani.com",
      ...headers,
    },
    body: method === "GET" || method === "OPTIONS" ? undefined : JSON.stringify(body),
  });
}

async function post(body, options = {}) {
  const sent = [];
  const target = env({
    EMAIL: {
      async send(message) {
        sent.push(message);
        return { messageId: "msg_1" };
      },
    },
    ...(options.env || {}),
  });
  const response = await handleRequest(request(body, options), target, {
    now: () => NOW,
    verifyTurnstile: options.verifyTurnstile || (async () => ({ success: true })),
    fetch: options.fetch,
  });
  const payload = await response.json();
  return { status: response.status, payload, sent };
}

test("accepts a valid enquiry and emails the fixed inbox", async () => {
  const { status, payload, sent } = await post(enquiry());
  assert.equal(status, 200);
  assert.equal(payload.ok, true);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].to, "sj@momentumdojo.com");
  assert.equal(sent[0].from.email, "enquiry@sidjashnani.com");
  assert.equal(sent[0].replyTo.email, "ada@example.com");
  assert.match(sent[0].subject, /^Website enquiry \(Keynote\): Ada Lovelace/);
  assert.match(sent[0].text, /We would like Sid to speak/);
  assert.match(sent[0].text, /London/);
  assert.doesNotMatch(sent[0].text, /fax/i);
  assert.equal(sent[0].html.includes("<script"), false);
});

test("drops a filled honeypot without sending", async () => {
  const { status, payload, sent } = await post(enquiry({ fax: "https://spam.example" }));
  assert.equal(status, 200);
  assert.equal(payload.ok, true);
  assert.equal(sent.length, 0);
});

test("rejects a submission faster than three seconds", async () => {
  const { status, payload, sent } = await post(enquiry({ ts: String(NOW - 500) }));
  assert.equal(status, 400);
  assert.equal(payload.error, "too_fast");
  assert.equal(sent.length, 0);
});

test("rejects a stale or future timestamp", async () => {
  const stale = await post(enquiry({ ts: String(NOW - 25 * 60 * 60 * 1000) }));
  assert.equal(stale.payload.error, "expired");
  const future = await post(enquiry({ ts: String(NOW + 5 * 60 * 1000) }));
  assert.equal(future.payload.error, "expired");
});

test("rejects missing and oversized fields", async () => {
  const missing = await post(enquiry({ name: "  ", message: "short" }));
  assert.equal(missing.status, 400);
  assert.equal(missing.payload.error, "validation");
  assert.ok(missing.payload.fields.includes("name"));
  assert.ok(missing.payload.fields.includes("message"));

  const huge = await post(enquiry({ message: "a".repeat(4001) }));
  assert.ok(huge.payload.fields.includes("message"));

  const badEmail = await post(enquiry({ email: "not-an-email" }));
  assert.ok(badEmail.payload.fields.includes("email"));

  const badType = await post(enquiry({ type: "discovery" }));
  assert.ok(badType.payload.fields.includes("type"));
});

test("rejects an audience or revenue value that is not on the form", () => {
  const result = validateEnquiry(enquiry({ audience_size: "everyone", revenue_band: "secret price" }));
  assert.equal(result.ok, false);
  assert.ok(result.fields.includes("audience_size"));
  assert.ok(result.fields.includes("revenue_band"));
});

test("strips newlines from the subject", () => {
  const mail = buildEmail(
    validateEnquiry(enquiry({ name: "Ada\r\nBcc: evil@example.com", company: "Engines" })).value,
    env(),
    { submittedAt: "2026-10-21T12:00:00.000Z", ip: "203.0.113.8" },
  );
  assert.equal(mail.subject.includes("\n"), false);
  assert.equal(mail.subject.includes("\r"), false);
  assert.match(mail.text, /203\.0\.113\.8/);
});

test("rejects a failed Turnstile check and a missing secret", async () => {
  const failed = await post(enquiry(), { verifyTurnstile: async () => ({ success: false, "error-codes": ["invalid-input-response"] }) });
  assert.equal(failed.status, 400);
  assert.equal(failed.payload.error, "turnstile");
  assert.equal(failed.sent.length, 0);

  const unconfigured = await post(enquiry(), { env: { TURNSTILE_SECRET_KEY: "" } });
  assert.equal(unconfigured.status, 503);
  assert.equal(unconfigured.payload.error, "unavailable");
});

test("returns a clear error when email is not configured", async () => {
  const { status, payload } = await post(enquiry(), { env: { EMAIL: undefined } });
  assert.equal(status, 503);
  assert.equal(payload.error, "unavailable");
});

test("uses Resend when MAIL_PROVIDER=resend", async () => {
  let captured;
  const { status, payload, sent } = await post(enquiry(), {
    env: { MAIL_PROVIDER: "resend", RESEND_API_KEY: "re_test", EMAIL: undefined },
    fetch: async (url, init) => {
      captured = { url, body: JSON.parse(init.body), authorization: init.headers.authorization };
      return new Response(JSON.stringify({ id: "email_1" }), { status: 200 });
    },
  });
  assert.equal(status, 200);
  assert.equal(payload.ok, true);
  assert.equal(sent.length, 0);
  assert.equal(captured.url, "https://api.resend.com/emails");
  assert.equal(captured.authorization, "Bearer re_test");
  assert.deepEqual(captured.body.to, ["sj@momentumdojo.com"]);
  assert.equal(captured.body.reply_to, "ada@example.com");
  assert.match(captured.body.from, /enquiry@sidjashnani\.com/);
});

test("rejects other origins, other methods, and oversized bodies", async () => {
  const evil = await post(enquiry(), { headers: { origin: "https://evil.example" } });
  assert.equal(evil.status, 403);

  const get = await handleRequest(request(null, { method: "GET" }), env(), { now: () => NOW });
  assert.equal(get.status, 405);

  const big = "x".repeat(33_000);
  const response = await handleRequest(
    new Request("https://sidjashnani.com/api/enquiry", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        accept: "application/json",
        origin: "https://sidjashnani.com",
      },
      body: big,
    }),
    env(),
    { now: () => NOW },
  );
  const payload = await response.json();
  assert.equal(response.status, 413);
  assert.equal(payload.error, "too_large");
});

test("form posts without JavaScript receive an HTML error, not a silent JSON blob", async () => {
  const body = new URLSearchParams(enquiry({ ts: "" }));
  const response = await handleRequest(
    new Request("https://sidjashnani.com/api/enquiry", {
      method: "POST",
      headers: {
        "content-type": "application/x-www-form-urlencoded",
        accept: "text/html",
        origin: "https://sidjashnani.com",
      },
      body,
    }),
    env(),
    { now: () => NOW, verifyTurnstile: async () => ({ success: true }) },
  );
  const html = await response.text();
  assert.equal(response.status, 400);
  assert.match(response.headers.get("content-type"), /text\/html/);
  assert.match(html, /Nothing was sent/);
  assert.doesNotMatch(html, /@/);
});

test("public site files keep the content rules", () => {
  const root = join(dirname(fileURLToPath(import.meta.url)), "../..");
  const files = ["index.html", "404.html", "llms.txt", "llms-full.txt", "robots.txt", "sitemap.xml", "CNAME", "assets/favicon/site.webmanifest"];
  const combined = files.map((file) => readFileSync(join(root, file), "utf8")).join("\n");

  assert.equal(/\bEOS\b/i.test(combined), false, "EOS");
  assert.equal(/E\.O\.S/i.test(combined), false, "E.O.S");
  assert.equal(/rekruuto/i.test(combined), false);
  assert.equal(/superhuman/i.test(combined), false);
  assert.equal(/discovery call/i.test(combined), false);
  assert.equal(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/i.test(combined), false, "email address");
  assert.equal(/(?<!Outgrow )Certified Advisor/.test(combined), false, "Certified Advisor missing Outgrow");
  assert.equal(/Certified Outgrow Advisor/.test(combined), false);
  assert.match(combined, /Outgrow Certified Advisor/);
  assert.match(combined, /£27M\+/);
  assert.equal(/PHOTO:/.test(combined), false);

  for (const file of ["index.html", "404.html"]) {
    const visible = readFileSync(join(root, file), "utf8")
      .replace(/<!--[\s\S]*?-->/g, "")
      .replace(/<script[\s\S]*?<\/script>/gi, "")
      .replace(/<style[\s\S]*?<\/style>/gi, "");
    assert.equal(/PHOTO:/.test(visible), false, file);
    assert.equal(/\[[A-Za-z][^\]]{0,120}\]/.test(visible), false, `visible bracket note in ${file}`);
  }

  const page = readFileSync(join(root, "index.html"), "utf8");
  const faqHtml = [...page.matchAll(/<summary>(.*?)<\/summary><p>(.*?)<\/p>/g)].map((match) => ({
    q: textOf(match[1]),
    a: textOf(match[2]),
  }));
  const jsonld = JSON.parse(page.match(/<script type="application\/ld\+json">\n([\s\S]*?)\n  <\/script>/)[1]);
  const faqLd = jsonld["@graph"].find((node) => node["@type"] === "FAQPage").mainEntity;
  assert.equal(faqHtml.length, faqLd.length);
  faqHtml.forEach((item, index) => {
    assert.equal(item.q, faqLd[index].name);
    assert.equal(item.a, faqLd[index].acceptedAnswer.text);
  });
  assert.equal(jsonld["@graph"].some((node) => node.price || node.priceSpecification), false);
  assert.equal(JSON.stringify(jsonld).includes("offers"), false);

  assert.match(readFileSync(join(root, "index.html"), "utf8"), /G-YF4Y5T2LR7/);
  assert.match(readFileSync(join(root, "CNAME"), "utf8"), /^sidjashnani\.com\s*$/);

  const walked = walk(join(root, "assets"));
  assert.equal(walked.some((file) => file.endsWith("sid-jashnani.jpg")), false);
  assert.equal(walked.some((file) => file.includes("/src/")), false);
});

function textOf(value) {
  return value
    .replace(/<[^>]+>/g, "")
    .replace(/&#x27;/g, "'")
    .replace(/&amp;/g, "&")
    .replace(/\s+/g, " ")
    .trim();
}

function walk(dir) {
  const found = [];
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) found.push(...walk(path));
    else found.push(path);
  }
  return found;
}
