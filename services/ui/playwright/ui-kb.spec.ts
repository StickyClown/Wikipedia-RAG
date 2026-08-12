import { test, expect } from "./e2e-helpers";

test("[e2e][kb] creates and selects an isolated knowledge base", async ({
  page,
  knowledgeBase,
}) => {
  await expect(
    page.getByLabel("Primary knowledge base", { exact: true }),
  ).toHaveValue(knowledgeBase.id);

  await page.getByTestId("tab-search").click();
  await expect(page.getByTestId("panel-search")).toBeVisible();
  const retrievalScope = page.getByLabel(knowledgeBase.name, { exact: true });
  if (!(await retrievalScope.isChecked())) await retrievalScope.check();
  await expect(retrievalScope).toBeChecked();
});
