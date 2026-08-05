import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:8876",
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "mobile-320",
      use: {
        browserName: "chromium",
        viewport: { width: 320, height: 568 },
        hasTouch: true,
        isMobile: true,
      },
    },
    {
      name: "mobile-390",
      use: {
        browserName: "webkit",
        viewport: { width: 390, height: 844 },
        hasTouch: true,
        isMobile: true,
      },
    },
    {
      name: "mobile-430",
      use: {
        browserName: "chromium",
        viewport: { width: 430, height: 932 },
        hasTouch: true,
        isMobile: true,
      },
    },
  ],
  webServer: {
    command:
      "uv run uvicorn moco.web.app:create_app --factory --host 127.0.0.1 --port 8876 --log-level warning",
    url: "http://127.0.0.1:8876/",
    reuseExistingServer: false,
    timeout: 30_000,
  },
});
