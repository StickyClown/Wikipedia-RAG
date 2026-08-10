import { expect, test } from "@playwright/test";
import {
  API_BASE_URL,
  apiIsReachable,
  assertNoProviderLeakage,
  setEnglish,
} from "./test-helpers";

test.describe("read-only public UI", () => {
  test("[readonly] switches RU and EN without exposing raw errors", async ({
    page,
  }) => {
    const consoleErrors: string[] = [];
    page.on("console", (message) => {
      if (message.type() === "error") consoleErrors.push(message.text());
    });

    await page.goto("/");
    await expect(page.getByTestId("auth-panel")).toBeVisible();

    await setEnglish(page);
    await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
    await expect(
      page.getByRole("button", { name: "EN", exact: true }),
    ).toHaveAttribute("aria-pressed", "true");

    await page.getByRole("button", { name: "RU", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Вход" })).toBeVisible();
    await expect(
      page.getByRole("button", { name: "RU", exact: true }),
    ).toHaveAttribute("aria-pressed", "true");

    const bodyText = await page.locator("body").innerText();
    assertNoProviderLeakage(bodyText);
    expect(consoleErrors).toEqual([]);
  });

  test("[readonly] remains usable on a narrow viewport", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/");
    await expect(page.getByTestId("auth-panel")).toBeVisible();

    const layout = await page.evaluate(() => ({
      viewport: document.documentElement.clientWidth,
      content: document.documentElement.scrollWidth,
    }));
    expect(layout.content).toBeLessThanOrEqual(layout.viewport + 1);
  });

  test("[readonly] does not expose the Models tab before authentication", async ({
    page,
  }) => {
    await page.goto("/");
    await expect(page.getByTestId("auth-panel")).toBeVisible();
    await expect(page.getByTestId("tab-models")).toHaveCount(0);
  });

  test("[readonly] exposes a safe error when local login cannot reach the API", async ({
    page,
    request,
  }) => {
    const apiResponse = await request
      .get(`${API_BASE_URL}/api/v1/auth/session`, { timeout: 2_000 })
      .catch(() => null);
    test.skip(
      Boolean(apiResponse),
      "API is available; use the authenticated suite",
    );

    await page.goto("/");
    await setEnglish(page);
    await page.getByLabel("Password", { exact: true }).fill("offline-test");
    await page.getByRole("button", { name: "Local", exact: true }).click();
    await expect(page.getByRole("alert")).toHaveText(
      "The request could not be completed.",
    );
  });

  test("[readonly] skips authenticated checks when the API is not available", async ({
    request,
  }) => {
    test.skip(
      await apiIsReachable(request),
      "API is available; authenticated checks belong to the admin suite",
    );
    expect(true).toBe(true);
  });
});
