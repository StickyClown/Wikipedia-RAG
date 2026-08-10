import { expect, test } from "@playwright/test";
import { assertNoProviderLeakage } from "./test-helpers";

test("[smoke] public shell loads without exposing provider errors", async ({
  page,
}) => {
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));

  await page.goto("/");
  await expect(page.locator("body")).toBeVisible();
  await expect(page.getByTestId("auth-panel")).toBeVisible();
  await expect(page.getByTestId("readiness-status")).toContainText(
    /ready|degraded|offline|checking|готово|частично готово|нет связи|проверка/i,
  );

  const bodyText = await page.locator("body").innerText();
  assertNoProviderLeakage(bodyText);
  expect(consoleErrors).toEqual([]);
  expect(pageErrors).toEqual([]);
});
