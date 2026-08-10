import { expect, type APIRequestContext, type Page } from "@playwright/test";

export const API_BASE_URL =
  process.env.VITE_API_BASE_URL ?? "http://localhost:8000";

export function configuredCredential(name: "username" | "password") {
  const adminName =
    name === "username"
      ? process.env.WIKIPEDIARAG_UI_TEST_ADMIN_USERNAME
      : process.env.WIKIPEDIARAG_UI_TEST_ADMIN_PASSWORD;
  const genericName =
    name === "username"
      ? process.env.WIKIPEDIARAG_UI_TEST_USERNAME
      : process.env.WIKIPEDIARAG_UI_TEST_PASSWORD;
  const evalName =
    name === "username"
      ? process.env.EVAL_AUTH_USERNAME
      : process.env.EVAL_AUTH_PASSWORD;
  return adminName ?? genericName ?? evalName ?? "";
}

export async function apiIsReachable(request: APIRequestContext) {
  try {
    const response = await request.get(`${API_BASE_URL}/api/v1/auth/session`, {
      timeout: 5_000,
    });
    return response.status() < 500;
  } catch {
    return false;
  }
}

export async function setEnglish(page: Page) {
  const englishButton = page.getByRole("button", { name: "EN", exact: true });
  if (await englishButton.isVisible()) await englishButton.click();
}

export async function loginAsConfiguredAdmin(page: Page) {
  const username = configuredCredential("username");
  const password = configuredCredential("password");
  expect(username, "UI test username is not configured").not.toBe("");
  expect(password, "UI test password is not configured").not.toBe("");

  await loginWithCredentials(page, username, password);
}

export async function loginWithCredentials(
  page: Page,
  username: string,
  password: string,
) {
  await setEnglish(page);
  await page.getByLabel("Username", { exact: true }).fill(username);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Local", exact: true }).click();
  await expect(page.getByTestId("workspace-tabs")).toBeVisible({
    timeout: 15_000,
  });
}

export function assertNoProviderLeakage(text: string) {
  expect(text).not.toMatch(/JSONDecodeError|provider payload/i);
  expect(text).not.toMatch(/(?:sk-|api[_-]?key|authorization)\s*[:=]/i);
}
