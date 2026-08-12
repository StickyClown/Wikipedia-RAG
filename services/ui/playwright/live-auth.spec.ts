import {
  test,
  expect,
  createKnowledgeBase,
  deleteKnowledgeBase,
} from "./e2e-helpers";
import {
  API_BASE_URL,
  assertNoProviderLeakage,
  loginAsConfiguredAdmin,
  setEnglish,
} from "./test-helpers";
import { liveRuntimeBlockers } from "./live-runtime";

const SESSION_COOKIE_NAME =
  process.env.WIKIPEDIARAG_UI_TEST_SESSION_COOKIE_NAME ??
  "wikipediarag_session";

test.beforeEach(async ({ request }, testInfo) => {
  void request;
  const blockers = await liveRuntimeBlockers();
  testInfo.skip(blockers.length > 0, `BLOCKED: ${blockers.join("; ")}`);
});

test("[live][auth] local login persists an HttpOnly session across reload", async ({
  page,
  context,
}) => {
  await page.goto("/");
  await loginAsConfiguredAdmin(page);
  await expect(page.getByTestId("workspace-tabs")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Logout", exact: true }),
  ).toBeVisible();

  const cookie = (await context.cookies(API_BASE_URL)).find(
    (item) => item.name === SESSION_COOKIE_NAME,
  );
  expect(cookie, "application session cookie").toMatchObject({
    httpOnly: true,
    sameSite: "Lax",
  });

  await page.reload();
  await expect(page.getByTestId("workspace-tabs")).toBeVisible();
});

test("[live][auth] logout revokes the visible browser session", async ({
  page,
}) => {
  await page.goto("/");
  await loginAsConfiguredAdmin(page);
  await page.getByRole("button", { name: "Logout", exact: true }).click();
  await expect(page.getByTestId("auth-panel")).toBeVisible();

  const session = await page.evaluate(async (apiBaseUrl) => {
    const response = await fetch(`${apiBaseUrl}/api/v1/auth/session`, {
      credentials: "include",
    });
    return (await response.json()) as { authenticated?: boolean };
  }, API_BASE_URL);
  expect(session.authenticated).toBe(false);
});

test("[live][auth] invalid local credentials show a safe error", async ({
  page,
  diagnostics,
}) => {
  const loginUrl = `${API_BASE_URL}/api/v1/auth/local/login`;
  diagnostics.allowResponseFailure(loginUrl);
  diagnostics.allowConsoleError(/401 \(Unauthorized\)/);
  await page.goto("/");
  await setEnglish(page);
  await page
    .getByLabel("Username", { exact: true })
    .fill(`invalid-${crypto.randomUUID()}`);
  await page.getByLabel("Password", { exact: true }).fill(crypto.randomUUID());
  await page.getByRole("button", { name: "Local", exact: true }).click();
  const alert = page.getByRole("alert");
  await expect(alert).toBeVisible();
  await assertNoProviderLeakage(await alert.innerText());
});

test("[live][auth] UI mutations send CSRF and missing CSRF is rejected", async ({
  page,
  diagnostics,
}) => {
  await page.goto("/");
  await loginAsConfiguredAdmin(page);
  const name = `live-auth-csrf-${crypto.randomUUID()}`;
  const creation = page.waitForRequest(
    (request) =>
      request.method() === "POST" &&
      request.url() === `${API_BASE_URL}/api/v1/knowledge-bases`,
  );
  const knowledgeBase = await createKnowledgeBase(page, name);
  try {
    const request = await creation;
    expect(request.headers()["x-csrf-token"], "UI CSRF header").toBeTruthy();

    const url = `${API_BASE_URL}/api/v1/knowledge-bases`;
    diagnostics.allowResponseFailure(url);
    diagnostics.allowConsoleError(/403 \(Forbidden\)/);
    const status = await page.evaluate(async (apiBaseUrl) => {
      const response = await fetch(`${apiBaseUrl}/api/v1/knowledge-bases`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: "csrf-must-be-rejected" }),
      });
      return response.status;
    }, API_BASE_URL);
    expect(status).toBe(403);
  } finally {
    await deleteKnowledgeBase(page, knowledgeBase.id);
  }
});
