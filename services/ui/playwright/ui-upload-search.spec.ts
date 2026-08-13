import {
  test,
  expect,
  createKnowledgeBase,
  corruptUploadedFixturePublication,
  deleteKnowledgeBase,
  selectRetrievalKnowledgeBase,
  uploadReadyTextFixture,
} from "./e2e-helpers";

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
  await expect(
    searchPanel.getByText(uploadedFixture.marker).first(),
  ).toBeVisible({
    timeout: 30_000,
  });

  await searchPanel
    .getByRole("button", { name: "Open in viewer", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: uploadedFixture.filename }),
  ).toBeVisible();
  await expect(page.getByText(uploadedFixture.marker).first()).toBeVisible();
});

test("[e2e][security] staged PostgreSQL chunk stays hidden despite published OpenSearch record", async ({
  page,
  uploadedFixture,
}) => {
  await selectRetrievalKnowledgeBase(page, uploadedFixture.name);
  await corruptUploadedFixturePublication(page, uploadedFixture);
  await page.reload();
  await selectRetrievalKnowledgeBase(page, uploadedFixture.name);
  const searchPanel = page.getByTestId("panel-search");
  await searchPanel
    .getByLabel("Search documents", { exact: true })
    .fill(uploadedFixture.marker);
  await searchPanel
    .getByRole("button", { name: "Search", exact: true })
    .click();
  await expect(searchPanel.getByText(uploadedFixture.marker)).toHaveCount(0);
});

test("[e2e][critical-smoke] changing the visible KB scope changes search results", async ({
  page,
  uploadedFixture,
}) => {
  const second = await createKnowledgeBase(
    page,
    `e2e-search-scope-${crypto.randomUUID()}`,
  );
  const secondMarker = `scope-marker-${crypto.randomUUID()}`;
  try {
    await uploadReadyTextFixture(page, "scope-second.txt", secondMarker);
    const searchPanel = page.getByTestId("panel-search");
    await selectRetrievalKnowledgeBase(page, second.name);
    await searchPanel
      .getByLabel("Search documents", { exact: true })
      .fill(secondMarker);
    await searchPanel
      .getByRole("button", { name: "Search", exact: true })
      .click();
    await expect(searchPanel.getByText(secondMarker).first()).toBeVisible({
      timeout: 30_000,
    });
    await expect(searchPanel.getByText(uploadedFixture.marker)).toHaveCount(0);
    await selectRetrievalKnowledgeBase(page, uploadedFixture.name);
    await searchPanel
      .getByLabel("Search documents", { exact: true })
      .fill(uploadedFixture.marker);
    await searchPanel
      .getByRole("button", { name: "Search", exact: true })
      .click();
    await expect(
      searchPanel.getByText(uploadedFixture.marker).first(),
    ).toBeVisible({
      timeout: 30_000,
    });
    await searchPanel
      .getByRole("button", { name: "Open in viewer", exact: true })
      .click();
    await expect(
      page.getByRole("heading", { name: uploadedFixture.filename }),
    ).toBeVisible();
    await expect(page.getByText(uploadedFixture.marker).first()).toBeVisible();
  } finally {
    await deleteKnowledgeBase(page, second.id);
  }
});
