import { expect, test } from "@playwright/test";
import {
  apiIsReachable,
  assertNoProviderLeakage,
  configuredCredential,
  loginAsConfiguredAdmin,
} from "./test-helpers";

test.describe("read-only authenticated UI", () => {
  test.beforeEach(async ({ page, request }) => {
    test.skip(
      !(await apiIsReachable(request)),
      "API is not available; set up the working local stack for authenticated UI checks",
    );
    test.skip(
      !configuredCredential("username") || !configuredCredential("password"),
      "UI test credentials are not configured",
    );
    await page.goto("/");
    await loginAsConfiguredAdmin(page);
  });

  test("[readonly] exposes accessible workspace tabs", async ({ page }) => {
    const tabs = ["chat", "search", "research", "knowledge", "models"];
    for (const tab of tabs) {
      const tabButton = page.getByTestId(`tab-${tab}`);
      await expect(tabButton).toBeVisible();
      await tabButton.click();
      await expect(tabButton).toHaveAttribute("aria-selected", "true");
      await expect(page.getByTestId(`panel-${tab}`)).toBeVisible();
    }

    const chatTab = page.getByTestId("tab-chat");
    await chatTab.focus();
    await chatTab.press("ArrowRight");
    await expect(page.getByTestId("tab-search")).toBeFocused();
    await page.getByTestId("tab-search").press("End");
    await expect(page.getByTestId("tab-models")).toBeFocused();
    await page.getByTestId("tab-models").press("Home");
    await expect(chatTab).toBeFocused();
  });

  test("[admin][readonly] renders model control data without secrets", async ({
    page,
  }) => {
    await page.getByTestId("tab-models").click();
    await expect(page.getByTestId("panel-models")).toBeVisible();
    await expect(page.getByTestId("model-connections")).toBeVisible();
    await expect(page.getByTestId("model-catalog")).toBeVisible();
    await expect(page.getByTestId("model-stage-assignments")).toBeVisible();

    const bodyText = await page.getByTestId("panel-models").innerText();
    assertNoProviderLeakage(bodyText);
    await expect(
      page.getByRole("button", { name: "Validate draft", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Activate", exact: true }),
    ).toBeVisible();
  });

  test("[readonly] keeps non-active panels hidden after navigation", async ({
    page,
  }) => {
    await page.getByTestId("tab-search").click();
    await expect(page.getByTestId("panel-search")).toBeVisible();
    await expect(page.getByTestId("panel-chat")).toBeHidden();
    await expect(page.getByTestId("panel-models")).toBeHidden();
  });
});
