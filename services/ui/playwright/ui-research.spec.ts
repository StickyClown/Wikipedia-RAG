import { test, expect, selectResearchKnowledgeBase } from "./e2e-helpers";

test("[e2e][research] starts an isolated Deep Research run and exposes lifecycle state", async ({
  page,
  uploadedFixture,
}) => {
  await selectResearchKnowledgeBase(page, uploadedFixture.name);
  const panel = page.getByTestId("panel-research");
  await expect(panel).toBeVisible();
  const topic = `Research ${uploadedFixture.marker}`;
  await panel
    .getByLabel("Research topic for the selected knowledge base", {
      exact: true,
    })
    .fill(topic);
  await panel.getByRole("button", { name: "Quick run", exact: true }).click();
  await expect(panel.getByText(topic, { exact: true })).toBeVisible();
  await expect(
    panel.getByText(/received|running|completed|failed|cancelled/i),
  ).toBeVisible({
    timeout: 30_000,
  });

  const pause = panel.getByRole("button", { name: "Pause", exact: true });
  const resume = panel.getByRole("button", { name: "Resume", exact: true });
  const cancel = panel.getByRole("button", { name: "Cancel", exact: true });
  if (await pause.isEnabled()) {
    await pause.click();
    await expect(resume).toBeEnabled();
    await expect(panel.getByText(/paused/i)).toBeVisible();
    await resume.click();
    await expect(pause).toBeEnabled();
    await cancel.click();
    await expect(panel.getByText(/cancelled/i)).toBeVisible({
      timeout: 30_000,
    });
  } else {
    await expect(
      panel.getByRole("heading", { name: "Report", exact: true }),
    ).toBeVisible({ timeout: 90_000 });
  }
});
