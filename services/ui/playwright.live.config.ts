import { defineConfig } from "@playwright/test";
import { UI_BASE_URL } from "./playwright/test-helpers";

export default defineConfig({
  testDir: "./playwright",
  testMatch: "**/live-*.spec.ts",
  forbidOnly: !!process.env.CI,
  fullyParallel: false,
  workers: 1,
  timeout: 300_000,
  expect: {
    timeout: 20_000,
  },
  globalSetup: "./playwright/live-global-setup.ts",
  use: {
    baseURL: UI_BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
});
