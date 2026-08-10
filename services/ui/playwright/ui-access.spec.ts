import { expect, test } from "@playwright/test";
import {
  API_BASE_URL,
  apiIsReachable,
  assertNoProviderLeakage,
  loginWithCredentials,
} from "./test-helpers";

test("[security][readonly] non-admin cannot see or call model control", async ({
  page,
  request,
}) => {
  const username = process.env.WIKIPEDIARAG_UI_TEST_USER_USERNAME ?? "";
  const password = process.env.WIKIPEDIARAG_UI_TEST_USER_PASSWORD ?? "";
  test.skip(
    !(await apiIsReachable(request)) || !username || !password,
    "API and non-admin UI test credentials are required",
  );

  await page.goto("/");
  await loginWithCredentials(page, username, password);
  await expect(page.getByTestId("tab-models")).toHaveCount(0);

  const response = await page.evaluate(async (apiBaseUrl) => {
    const result = await fetch(`${apiBaseUrl}/api/v1/admin/model-connections`, {
      credentials: "include",
    });
    return { status: result.status, body: await result.text() };
  }, API_BASE_URL);
  expect(response.status).toBe(403);
  assertNoProviderLeakage(response.body);
});
