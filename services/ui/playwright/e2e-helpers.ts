import {
  expect,
  test as base,
  type Page,
  type TestInfo,
} from "@playwright/test";
import {
  API_BASE_URL,
  apiIsReachable,
  configuredCredential,
  loginAsConfiguredAdmin,
} from "./test-helpers";

type BrowserDiagnostics = {
  consoleErrors: string[];
  pageErrors: string[];
  failedRequests: string[];
  failedResponses: string[];
  allowResponseFailure: (url: string) => void;
  allowFailedRequest: (pattern: RegExp) => void;
  allowConsoleError: (pattern: RegExp) => void;
};

export type TestKnowledgeBase = {
  id: string;
  name: string;
};

export type UploadedFixture = TestKnowledgeBase & {
  filename: string;
  marker: string;
};

type E2EFixtures = {
  diagnostics: BrowserDiagnostics;
  knowledgeBase: TestKnowledgeBase;
  uploadedFixture: UploadedFixture;
};

export const test = base.extend<E2EFixtures>({
  diagnostics: [
    async ({ page }, provideFixture, testInfo) => {
      const allowedResponseFailures = new Set<string>();
      const allowedFailedRequests: RegExp[] = [];
      const allowedConsoleErrors: RegExp[] = [];
      const diagnostics: BrowserDiagnostics = {
        consoleErrors: [],
        pageErrors: [],
        failedRequests: [],
        failedResponses: [],
        allowResponseFailure: (url) => allowedResponseFailures.add(url),
        allowFailedRequest: (pattern) => allowedFailedRequests.push(pattern),
        allowConsoleError: (pattern) => allowedConsoleErrors.push(pattern),
      };
      page.on("console", (message) => {
        if (
          message.type() === "error" &&
          !allowedConsoleErrors.some((pattern) => pattern.test(message.text()))
        )
          diagnostics.consoleErrors.push(message.text());
      });
      page.on("pageerror", (error) =>
        diagnostics.pageErrors.push(error.message),
      );
      page.on("requestfailed", (request) => {
        const value = `${request.method()} ${request.url()} (${request.failure()?.errorText ?? "unknown"})`;
        if (!allowedFailedRequests.some((pattern) => pattern.test(value))) {
          diagnostics.failedRequests.push(value);
        }
      });
      page.on("response", (response) => {
        if (
          response.status() >= 400 &&
          !allowedResponseFailures.has(response.url())
        )
          diagnostics.failedResponses.push(response.url());
      });

      await provideFixture(diagnostics);

      const unexpected = [
        ...diagnostics.consoleErrors.map((item) => `console: ${item}`),
        ...diagnostics.pageErrors.map((item) => `pageerror: ${item}`),
        ...diagnostics.failedRequests.map((item) => `requestfailed: ${item}`),
        ...diagnostics.failedResponses.map((item) => `HTTP failure: ${item}`),
      ];
      await testInfo.attach("browser-diagnostics", {
        body: unexpected.join("\n") || "No unexpected browser diagnostics.",
        contentType: "text/plain",
      });
      expect(unexpected, "unexpected browser diagnostics").toEqual([]);
    },
    { auto: true },
  ],

  knowledgeBase: async ({ page, request }, provideFixture, testInfo) => {
    await requireAuthenticatedStack(page, request, testInfo);
    const knowledgeBase = await createKnowledgeBase(page, uniqueName("e2e-kb"));
    try {
      await provideFixture(knowledgeBase);
    } finally {
      await deleteKnowledgeBase(page, knowledgeBase.id);
    }
  },

  uploadedFixture: async (
    { page, knowledgeBase, diagnostics },
    provideFixture,
    testInfo,
  ) => {
    testInfo.setTimeout(Math.max(testInfo.timeout, 120_000));
    const marker = `WIKIPEDIARAG-E2E-MARKER-${crypto.randomUUID()}`;
    const filename = `e2e-${crypto.randomUUID()}.txt`;
    diagnostics.allowFailedRequest(
      /^PUT http:\/\/localhost:9000\/rag-artifacts\/uploads\/.*\(net::ERR_ABORTED\)$/,
    );
    await uploadReadyTextFixture(page, filename, marker);
    await provideFixture({ ...knowledgeBase, filename, marker });
  },
});

export { expect };

export async function requireAuthenticatedStack(
  page: Page,
  request: Parameters<typeof apiIsReachable>[0],
  testInfo: TestInfo,
) {
  testInfo.skip(
    !(await apiIsReachable(request)),
    "BLOCKED: API is unavailable; start the local API, worker, storage, search, and model services.",
  );
  testInfo.skip(
    !configuredCredential("username") || !configuredCredential("password"),
    "BLOCKED: configure WIKIPEDIARAG_UI_TEST_ADMIN_USERNAME and WIKIPEDIARAG_UI_TEST_ADMIN_PASSWORD.",
  );
  await page.goto("/");
  await loginAsConfiguredAdmin(page);
}

export async function createKnowledgeBase(page: Page, name: string) {
  await page.getByTestId("tab-knowledge").click();
  await expect(page.getByTestId("panel-knowledge")).toBeVisible();
  await page.getByLabel("New KB name", { exact: true }).fill(name);
  await page.getByRole("button", { name: "Create", exact: true }).click();

  const selector = page.getByLabel("Primary knowledge base", { exact: true });
  const option = selector.getByRole("option", { name, exact: true });
  await expect(option).toHaveCount(1);
  const id = await option.getAttribute("value");
  expect(id, "created knowledge base id").toBeTruthy();
  await selector.selectOption(id!);
  await expect(selector).toHaveValue(id!);
  return { id: id!, name };
}

export async function selectRetrievalKnowledgeBase(page: Page, name: string) {
  await page.getByTestId("tab-search").click();
  await expect(page.getByTestId("panel-search")).toBeVisible();
  const details = page.locator("details.scope-details");
  if (!(await details.evaluate((node) => node.open))) {
    await details.locator("summary").click();
  }
  const scope = page.locator("fieldset.kb-scope");
  const selected = scope.locator('input[type="checkbox"]:checked');
  for (let index = (await selected.count()) - 1; index >= 0; index -= 1) {
    const checkbox = selected.nth(index);
    if (
      (await checkbox.getAttribute("aria-label")) !== name &&
      (await checkbox.isChecked())
    ) {
      await checkbox.uncheck();
    }
  }
  const checkbox = scope.getByLabel(name, { exact: true });
  if (!(await checkbox.isChecked())) await checkbox.check();
  await expect(checkbox).toBeChecked();
  await details.locator("summary").click();
}

export async function selectResearchKnowledgeBase(page: Page, name: string) {
  await page.getByTestId("tab-research").click();
  await expect(page.getByTestId("panel-research")).toBeVisible();
  const primary = page.getByLabel("Primary knowledge base", { exact: true });
  await primary.selectOption({ label: name });
  const scope = page.locator("fieldset.research-kb-scope");
  const selected = scope.locator('input[type="checkbox"]:checked');
  for (let index = (await selected.count()) - 1; index >= 0; index -= 1) {
    const checkbox = selected.nth(index);
    if (
      (await checkbox.evaluate((node) =>
        node.parentElement?.textContent?.trim(),
      )) !== name &&
      (await checkbox.isChecked())
    ) {
      await checkbox.uncheck();
    }
  }
  const checkbox = scope.getByLabel(name, { exact: true });
  if (!(await checkbox.isChecked())) await checkbox.check();
  await expect(checkbox).toBeChecked();
}

export async function uploadReadyTextFixture(
  page: Page,
  filename: string,
  marker: string,
) {
  await page.getByTestId("tab-knowledge").click();
  await expect(page.getByTestId("panel-knowledge")).toBeVisible();
  const fileInput = page.getByLabel("Choose files", { exact: true });
  await fileInput.setInputFiles({
    name: filename,
    mimeType: "text/plain",
    buffer: Buffer.from(`WikipediaRag E2E fixture. Exact marker: ${marker}.`),
  });
  await expect(page.getByText(filename, { exact: true })).toBeVisible();
  await expect(
    page.getByText(/queued|uploading|completing|hashing/i),
  ).toBeVisible();
  await expect(page.getByText(/Batch completed: 1\/1 complete/i)).toBeVisible({
    timeout: 90_000,
  });
  await expect(
    page.getByText("published", { exact: true }).first(),
  ).toBeVisible();
}

export async function deleteKnowledgeBase(page: Page, knowledgeBaseId: string) {
  const request = page.context().request;
  const session = await request.get(`${API_BASE_URL}/api/v1/auth/session`);
  expect(session.ok(), "load authenticated E2E session").toBeTruthy();
  const body = (await session.json()) as { csrf_token?: string };
  const response = await request.delete(
    `${API_BASE_URL}/api/v1/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}`,
    { headers: body.csrf_token ? { "X-CSRF-Token": body.csrf_token } : {} },
  );
  expect(
    { ok: response.ok(), status: response.status() },
    `delete test-owned knowledge base ${knowledgeBaseId}`,
  ).toMatchObject({
    ok: true,
  });
}

function uniqueName(prefix: string) {
  return `${prefix}-${crypto.randomUUID()}`;
}
