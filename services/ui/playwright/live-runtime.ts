import {
  API_BASE_URL,
  configuredCredential,
  UI_BASE_URL,
} from "./test-helpers";

export async function liveRuntimeBlockers(): Promise<string[]> {
  const blockers: string[] = [];
  const ui = await fetchWithTimeout(UI_BASE_URL);
  if (!ui || ui.status >= 500) {
    blockers.push(`UI is unavailable at ${UI_BASE_URL}`);
  }

  const ready = await fetchWithTimeout(`${API_BASE_URL}/ready`);
  if (!ready || ready.status !== 200) {
    blockers.push(
      `API readiness endpoint is unavailable at ${API_BASE_URL}/ready`,
    );
  } else {
    const payload = (await ready.json().catch(() => null)) as {
      status?: unknown;
    } | null;
    if (payload?.status !== "ok") blockers.push("API readiness is not ok");
  }

  const session = await fetchWithTimeout(`${API_BASE_URL}/api/v1/auth/session`);
  if (!session || session.status !== 200) {
    blockers.push("auth session endpoint is unavailable");
  }

  const localLogin = await fetchWithTimeout(
    `${API_BASE_URL}/api/v1/auth/local/login`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: `playwright-preflight-${crypto.randomUUID()}`,
        password: crypto.randomUUID(),
      }),
    },
  );
  if (!localLogin || localLogin.status >= 500) {
    blockers.push("local login endpoint is unavailable");
  } else if (localLogin.status === 403) {
    blockers.push("local login is disabled in the active runtime");
  } else if (![401, 422].includes(localLogin.status)) {
    blockers.push(
      `local login preflight returned unexpected status ${localLogin.status}`,
    );
  }

  if (!configuredCredential("username") || !configuredCredential("password")) {
    blockers.push(
      "configure WIKIPEDIARAG_UI_TEST_ADMIN_USERNAME and WIKIPEDIARAG_UI_TEST_ADMIN_PASSWORD, or set WIKIPEDIARAG_E2E_ALLOW_DEV_DEFAULTS=1 for localhost development only",
    );
  }
  return blockers;
}

async function fetchWithTimeout(url: string, init?: RequestInit) {
  try {
    return await fetch(url, { ...init, signal: AbortSignal.timeout(5_000) });
  } catch {
    return null;
  }
}
