import { expect, test } from "@playwright/test";

async function loadConversation(page) {
  await page.goto("/");
  await page.evaluate(async () => {
    const { TranscriptView } = await import("/static/app.js");
    const container = document.querySelector("#transcript");
    const latestButton = document.querySelector("#transcript-latest");
    // Drive the real view with synthetic text, without opening live media or agent connections.
    window.conversationFixture = new TranscriptView(container, { latestButton });
    for (let i = 0; i < 24; i += 1) {
      window.conversationFixture.update(
        i % 2 === 0 ? "user" : "assistant",
        i % 2 === 0
          ? `確認用の会話 ${i + 1}。今日の作業を整理してください。`
          : "まず、今日取り組むことを一緒に整理しましょう。優先したい作業から教えてください。",
        true,
      );
    }
  });
}

test("conversation keeps the reading position and resumes from the latest button", async ({ page }) => {
  await loadConversation(page);
  const log = page.getByRole("log", { name: "会話の内容" });
  const latest = page.getByRole("button", { name: "最新へ", exact: true }).first();
  await expect.poll(() => log.evaluate((el) => el.scrollHeight > el.clientHeight)).toBe(true);
  await log.evaluate((el) => { el.scrollTop = 100; });
  await expect(latest).toBeVisible();
  await page.evaluate(() => {
    window.conversationFixture.update("assistant", "追加の応答を受信しています。", false);
    window.conversationFixture.update("assistant", "追加の応答が届きました。", true);
  });
  expect(await log.evaluate((el) => el.scrollTop)).toBe(100);
  await latest.focus();
  await page.keyboard.press("Enter");
  await expect(latest).toBeHidden();
  await expect(log).toBeFocused();
  await expect.poll(() => log.evaluate((el) => el.scrollHeight - el.clientHeight - el.scrollTop)).toBeLessThan(2);
});

for (const width of [320, 820, 1280]) {
  test(`conversation layout remains usable at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await loadConversation(page);
    await page.evaluate(() => {
      window.conversationFixture.update("assistant", "long-unbroken-text".repeat(50), true);
      document.querySelector("#transcript").scrollTop = 100;
    });
    await expect(page.locator("#transcript-latest")).toBeVisible();
    const metrics = await page.evaluate(() => ({
      width: document.documentElement.clientWidth,
      contentWidth: document.body.scrollWidth,
      logWidth: document.querySelector("#transcript").clientWidth,
      logContentWidth: document.querySelector("#transcript").scrollWidth,
    }));
    expect(metrics.contentWidth).toBeLessThanOrEqual(metrics.width);
    expect(metrics.logContentWidth).toBeLessThanOrEqual(metrics.logWidth);
    await expect(page.getByRole("button", { name: "音声入力を開始" })).toBeInViewport();
    await page.screenshot({ path: testInfo.outputPath(`conversation-${width}.png`), fullPage: true });
  });
}

test("header fits long runtime states with smartphone pairing available", async ({ page }) => {
  await page.goto("/");
  await page.evaluate(() => {
    document.querySelector("#state").textContent = "VOICE_RECONNECT_REQUIRED";
    document.querySelector("#pairing-open").hidden = false;
  });
  for (const width of [320, 1121, 1180, 1280]) {
    await page.setViewportSize({ width, height: 900 });
    expect(await page.evaluate(() => document.body.scrollWidth)).toBeLessThanOrEqual(width);
    await expect(page.getByRole("button", { name: "スマホ接続" })).toBeInViewport();
    await expect(page.getByRole("button", { name: "配色", exact: true })).toBeInViewport();
  }
});

test("first-use guidance and microphone-stop meaning remain readable on mobile", async ({ page }, testInfo) => {
  await page.goto("/");
  await expect(page.getByRole("log", { name: "会話の内容" })).toContainText("「接続」を押してマイクを許可");
  await expect(page.getByText("入力停止はマイクだけを止めます。処理と音声再生は続きます。", { exact: true })).toBeVisible();
  for (const name of ["接続", "配色", "消去"]) {
    const button = page.getByRole("button", { name, exact: true });
    const box = await button.boundingBox();
    expect(box?.height).toBeGreaterThanOrEqual(44);
  }
  // Include the normally pointer-transparent dock in hit testing to detect visual occlusion.
  await page.addStyleTag({ content: "body::after { pointer-events: auto; }" });
  for (const name of ["音声入力を開始", "音声入力を停止"]) {
    const control = page.getByRole("button", { name });
    expect(await control.evaluate((button) => {
      const rect = button.getBoundingClientRect();
      const front = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2);
      return button === front || button.contains(front);
    })).toBe(true);
    await expect(control).toBeInViewport();
  }
  await page.screenshot({ path: testInfo.outputPath("first-use.png") });
});

test("conversation respects light, dark, and high contrast themes", async ({ page }, testInfo) => {
  await page.goto("/");
  for (const preset of ["porcelain", "midnight", "high-contrast-dark"]) {
    await page.evaluate((preset) => {
      localStorage.setItem("moco.theme.v1", JSON.stringify({ v: 1, preset, overrides: {} }));
    }, preset);
    await page.reload();
    await expect(page.locator("html")).toHaveAttribute("data-theme", preset);
    await page.getByRole("button", { name: "配色", exact: true }).click();
    await expect(page.getByRole("dialog", { name: "配色" })).toBeVisible();
    await expect(page.locator("#theme-validation")).toHaveAttribute("data-status", "ok");
    await page.locator("#theme-close").click();
    await expect(page.getByRole("button", { name: "配色", exact: true })).toBeFocused();
    await page.screenshot({ path: testInfo.outputPath(`${preset}.png`) });
  }
});
