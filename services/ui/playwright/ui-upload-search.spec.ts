import { test, expect, selectRetrievalKnowledgeBase } from "./e2e-helpers";

test("[e2e][critical-smoke] uploads, searches, and opens an exact TXT result", async ({
  page,
  uploadedFixture,
}) => {
  await selectRetrievalKnowledgeBase(page, uploadedFixture.name);
  const searchPanel = page.getByTestId("panel-search");
  await searchPanel
    .getByLabel("Search documents", { exact: true })
    .fill(uploadedFixture.marker);
  await searchPanel
    .getByRole("button", { name: "Search", exact: true })
    .click();
  await expect(searchPanel.getByText(uploadedFixture.marker)).toBeVisible({
    timeout: 30_000,
  });

  await searchPanel
    .getByRole("button", { name: "Open in viewer", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: uploadedFixture.filename }),
  ).toBeVisible();
  await expect(page.getByText(uploadedFixture.marker)).toBeVisible();
});
