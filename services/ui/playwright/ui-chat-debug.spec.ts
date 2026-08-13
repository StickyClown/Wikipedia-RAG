import { test, expect, selectRetrievalKnowledgeBase } from "./e2e-helpers";

test("[e2e][chat] renders an answer, cited source, and retrieval debugger", async ({
  page,
  uploadedFixture,
}) => {
  await selectRetrievalKnowledgeBase(page, uploadedFixture.name);
  await page.getByTestId("tab-chat").click();
  await expect(page.getByTestId("panel-chat")).toBeVisible();

  const chatPanel = page.getByTestId("panel-chat");
  await chatPanel
    .getByLabel("Chat", { exact: true })
    .fill(
      `What exact marker appears in the uploaded fixture? Answer with ${uploadedFixture.marker}.`,
    );
  await chatPanel.getByRole("button", { name: "Ask", exact: true }).click();
  await expect(
    chatPanel.getByRole("heading", { name: "Answer", exact: true }),
  ).toBeVisible({
    timeout: 90_000,
  });
  await expect(
    chatPanel.getByRole("heading", { name: /sources/i }),
  ).toBeVisible();
  await expect(
    chatPanel.locator(".sources").getByText(uploadedFixture.marker).first(),
  ).toBeVisible();
  const source = chatPanel.locator(".sources article").first();
  const sourceTitle = (await source.locator("a").innerText()).replace(
    /^\[S\d+\]\s*/,
    "",
  );
  await source
    .getByRole("button", { name: "Open document", exact: true })
    .click();
  await expect(page.getByRole("heading", { name: sourceTitle })).toBeVisible();
  await expect(page.getByText(uploadedFixture.marker).first()).toBeVisible();
  await page.getByTestId("tab-chat").click();
  await expect(chatPanel).toBeVisible();

  const debugButton = chatPanel.getByRole("button", {
    name: "Debug",
    exact: true,
  });
  await expect(debugButton).toBeEnabled();
  await debugButton.click();
  await expect(
    chatPanel.getByRole("heading", { name: "Timeline", exact: true }),
  ).toBeVisible();
});

test("[e2e][debug] search result remains available after retrieval-scope selection", async ({
  page,
  uploadedFixture,
}) => {
  await selectRetrievalKnowledgeBase(page, uploadedFixture.name);
  await expect(page.getByTestId("panel-search")).toBeVisible();
});
