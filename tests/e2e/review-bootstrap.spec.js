import { expect, test } from "@playwright/test";

test("review details preserve whitespace and wrap long content", async ({ page }) => {
  await page.goto("/review");
  const styles = await page.evaluate(() => {
    const selectors = [
      "review-argument",
      "review-cwd",
      "review-change-source",
      "review-change-destination",
    ];
    return selectors.map((className) => {
      const element = document.createElement("div");
      element.className = className;
      document.body.append(element);
      const computed = getComputedStyle(element);
      return {
        className,
        overflowWrap: computed.overflowWrap,
        whiteSpace: computed.whiteSpace,
      };
    });
  });

  for (const style of styles) {
    expect(style, style.className).toMatchObject({
      overflowWrap: "anywhere",
      whiteSpace: "pre-wrap",
    });
  }
});

test("removes the review nonce before the external module loads", async ({ page }) => {
  await page.route("**/static/review.js", (route) => route.abort());

  await page.goto("/review#one-time-nonce");

  await expect.poll(() => page.evaluate(() => window.location.hash)).toBe("");
});
