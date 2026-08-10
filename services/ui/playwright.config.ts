import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./playwright",
  forbidOnly: !!process.env.CI,
  timeout: 30_000,
  expect: {
    timeout: 10_000,
  },
  use: {
    baseURL: "http://localhost:5173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
  },
  webServer: {
    command: "pnpm dev --host 127.0.0.1",
    url: "http://localhost:5173",
    reuseExistingServer: true,
  },
});
