import { test, expect, requireAuthenticatedStack } from "./e2e-helpers";

test("[e2e][auth] local login opens the authenticated workspace", async ({
  page,
  request,
}, testInfo) => {
  await requireAuthenticatedStack(page, request, testInfo);
  await expect(page.getByTestId("workspace-tabs")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Logout", exact: true }),
  ).toBeVisible();
});
