import { test, expect } from "./e2e-helpers";
import { setEnglish } from "./test-helpers";

test("[e2e][states] authentication failure displays a safe visible error", async ({
  page,
  diagnostics,
}) => {
  await page.route("**/api/v1/auth/local/login", async (route) => {
    diagnostics.allowResponseFailure(route.request().url());
    diagnostics.allowConsoleError(/503 \(Service Unavailable\)/);
    await route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ code: "REQUEST_FAILED" }),
    });
  });
  await page.goto("/");
  await setEnglish(page);
  await page.getByLabel("Username", { exact: true }).fill("offline-test");
  await page.getByLabel("Password", { exact: true }).fill("offline-test");
  await page.getByRole("button", { name: "Local", exact: true }).click();
  await expect(page.getByRole("alert")).toHaveText(
    "The request could not be completed.",
  );
});

test("[e2e][states] empty search stays disabled until a query is entered", async ({
  page,
  knowledgeBase,
}) => {
  await page.getByTestId("tab-search").click();
  const panel = page.getByTestId("panel-search");
  await expect(panel).toBeVisible();
  const search = panel.getByRole("button", { name: "Search", exact: true });
  await expect(search).toBeDisabled();
  await panel
    .getByLabel("Search documents", { exact: true })
    .fill("no-match-marker");
  await expect(search).toBeEnabled();
  await expect(
    page.getByLabel(knowledgeBase.name, { exact: true }),
  ).toBeVisible();
});
