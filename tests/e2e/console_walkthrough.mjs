// Offline first-run walkthrough. Run only against the throwaway console described in README.
// Usage: node tests/e2e/console_walkthrough.mjs "<console URL>" <screenshot directory>
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { chromium } from "playwright";
const [url, out] = process.argv.slice(2);
if (!url || !out) throw new Error("Pass the throwaway console URL and screenshot directory.");
await mkdir(out, { recursive: true });
const errors = [];
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1200, height: 900 }, reducedMotion: "reduce" });
page.on("pageerror", (error) => errors.push(String(error)));
try {
  await page.goto(url);
  await page.getByRole("button", { name: "Get started", exact: true }).waitFor();
  assert.equal(await page.getByRole("button", { name: /Try sample/ }).count(), 0);
  await page.screenshot({ path: `${out}/01-welcome.png`, fullPage: true });
  await page.getByRole("button", { name: "Get started", exact: true }).click();
  await page.getByRole("heading", { name: "Choose a model", exact: true }).waitFor();
  assert.equal(await page.getByRole("button", { name: "Try the sample first", exact: true }).count(), 0);
  // A fresh throwaway project has no key. This must fail before any provider request.
  await page.getByRole("button", { name: "Test and continue", exact: true }).click();
  await page.locator("#model-status").getByText(/is not set/).waitFor();
  assert.match(page.url(), /step=model/);
  assert.equal(await page.getByRole("button", { name: "Continue", exact: true }).count(), 0);
  await page.screenshot({ path: `${out}/04-missing-key.png`, fullPage: true });
  // Reach the Run step with fixture data; no real keys are sent and the agent is never started.
  const accounts = new URL(url);
  accounts.hash = "view=wizard&step=pipeboard";
  await page.goto(accounts.href);
  assert.equal(await page.getByRole("group", { name: "Ad platform integrations" }).getByRole("button").count(), 3);
  assert.deepEqual(await page.getByRole("list", { name: "Platforms provided by Pipeboard" }).getByRole("listitem").allTextContents(), ["Google Ads", "Meta Ads", "TikTok Ads", "Pinterest Ads", "Snap Ads", "Reddit Ads", "LinkedIn Ads", "Google Analytics"]);
  assert.equal(await page.getByRole("group", { name: "Direct connections", exact: true }).getByRole("button").count(), 2);
  // A direct platform returns to Accounts even when it is already the saved wizard step.
  await page.getByRole("button", { name: "Set up OpenAI Ads", exact: true }).click();
  await page.locator("#head-direct_openai_ads[aria-expanded='true']").waitFor();
  await page.getByRole("button", { name: "Back to accounts", exact: true }).click();
  await page.getByRole("heading", { name: "Connect ad accounts", exact: true }).waitFor();
  assert.match(page.url(), /view=wizard&step=pipeboard/);
  assert.equal(await page.getByRole("group", { name: "Ad platform integrations" }).getByRole("button").count(), 3);
  await page.getByRole("button", { name: "Use sample data", exact: true }).click();
  await page.getByRole("heading", { name: "Run your agent", exact: true }).waitFor();
  assert.match(page.url(), /step=run/);
  const runOptions = page.getByRole("group", { name: "Run options", exact: true });
  assert.equal(await runOptions.getByRole("button").count(), 2);
  assert.equal(await page.getByRole("button", { name: /^Run locally/ }).getAttribute("aria-expanded"), "true");
  assert.equal(await page.getByRole("button", { name: "Start agent", exact: true }).isVisible(), true);
  assert.equal(await page.getByText("docker compose up -d --build", { exact: true }).isVisible(), false);
  assert.equal(await page.getByText(/LangSmith|Managed Deep Agents|Postgres/).count(), 0);
  await page.getByRole("button", { name: /^Docker/ }).click();
  await page.getByText("docker compose up -d --build", { exact: true }).waitFor();
  assert.equal(await page.getByRole("button", { name: "Start agent", exact: true }).isVisible(), false);
  await page.getByRole("button", { name: /^Run locally/ }).click();
  // The state check opens the throwaway project's DuckDB file; it never starts the agent.
  await page.getByRole("button", { name: "Check state", exact: true }).click();
  await page.locator("#run-local").getByText(/state ready at/).waitFor();
  await page.getByText("Connect Slack (optional)", { exact: true }).click();
  await page.getByLabel("Bot token", { exact: true }).waitFor();
  await page.screenshot({ path: `${out}/06-run.png`, fullPage: true });
  // Polling must preserve focus and output disclosure while updating the process controls.
  let processState = { name: "serve", running: false, state: "stopped", command: "uv run paid-media-agent serve", log_tail: "Old agent output" };
  const processRequests = [];
  await page.route("**/api/status", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    data.processes = [processState];
    await route.fulfill({ json: data });
  });
  await page.route("**/api/processes/**", async (route) => {
    processRequests.push(new URL(route.request().url()).pathname);
    if (route.request().url().endsWith("/start")) {
      await route.fulfill({ status: 409, json: { detail: "Fixture start rejected" } });
      return;
    }
    processState = { ...processState, running: false, state: "completed", returncode: 0, log_tail: "Fixture agent stopped" };
    await route.fulfill({ json: processState });
  });
  await page.reload();
  await page.getByRole("button", { name: "Start agent", exact: true }).waitFor();
  assert.equal(await page.getByText("Process output", { exact: true }).isVisible(), false);
  processState = { ...processState, running: true, state: "active", log_tail: "Starting fixture agent" };
  await page.reload();
  const controls = page.locator(".process-controls").filter({ has: page.getByRole("button", { name: "Stop agent", exact: true }) });
  await controls.locator("summary").click();
  await page.getByRole("button", { name: "Stop agent", exact: true }).focus();
  processState = { ...processState, log_tail: "Fixture agent ready" };
  await controls.getByText("Fixture agent ready").waitFor();
  assert.equal(await page.locator(":focus").textContent(), "Stop agent");
  assert.equal(await controls.locator("details").getAttribute("open"), "");
  await page.getByRole("button", { name: "Stop agent", exact: true }).click();
  await page.getByRole("button", { name: "Start agent", exact: true }).waitFor();
  assert.equal(await page.locator(":focus").textContent(), "Start agent");
  await Promise.all([
    page.waitForResponse((response) => response.url().endsWith("/api/processes/serve/start")),
    page.getByRole("button", { name: "Start agent", exact: true }).click(),
  ]);
  await page.locator(".process-controls").getByText("Fixture start rejected").waitFor();
  assert.deepEqual(processRequests, ["/api/processes/serve/stop", "/api/processes/serve/start"]);
  await page.unroute("**/api/status");
  await page.unroute("**/api/processes/**");

  // Equal provider IDs on different platforms must remain independent rows.
  const discovered = [
    { platform: "google_ads", provider_account_id: "123", name: "Google fixture" },
    { platform: "meta_ads", provider_account_id: "123", name: "Meta fixture" },
  ];
  const mapped = [];
  await page.route("**/api/actions/pipeboard_test", (route) => route.fulfill({ json: { ok: true, status: "ok", summary: "Fixture connection" } }));
  await page.route("**/api/actions/accounts_discover", (route) => route.fulfill({ json: { ok: true, detail: { accounts: discovered } } }));
  await page.route("**/api/accounts", async (route) => { mapped.push(route.request().postDataJSON()); await route.fulfill({ json: { ok: true } }); });
  await page.goto(accounts.href);
  await page.getByRole("button", { name: "Set up Pipeboard", exact: true }).click();
  await page.getByRole("button", { name: "Connect", exact: true }).click();
  await page.getByRole("checkbox", { name: /Google fixture/ }).click();
  const meta = page.getByRole("checkbox", { name: /Meta fixture/ });
  assert.equal(await meta.getAttribute("aria-checked"), "true");
  await page.getByLabel("Name for Meta fixture", { exact: true }).press("End");
  await page.getByLabel("Name for Meta fixture", { exact: true }).press("Space");
  assert.equal(await meta.getAttribute("aria-checked"), "true");
  await Promise.all([
    page.waitForResponse((response) => response.url().endsWith("/api/accounts")),
    page.getByRole("button", { name: "Save selected accounts", exact: true }).click(),
  ]);
  assert.equal(mapped.length, 1);
  assert.equal(mapped[0].platform, "meta_ads");
  assert.equal(mapped[0].alias, "meta-meta-fixture");
  await page.locator("#theme-toggle").click();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: `${out}/05-mobile.png`, fullPage: true });
  assert.deepEqual(errors, []);
  console.log("Guided setup, missing key, run controls, independent accounts, and mobile layout passed.");
} finally {
  await browser.close();
}
