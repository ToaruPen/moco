import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { JSDOM } from "jsdom";

import {
  contrastRatio,
  DEFAULT_THEME,
  EDITABLE_TOKENS,
  isEditableTarget,
  PRESET_OPTIONS,
  PRESETS,
  parseStoredTheme,
  serializeTheme,
  ThemeController,
  watchSystemTheme,
} from "../../src/moco/web/static/theme.js";

describe("theme persistence", () => {
  it("accepts only versioned allowlisted theme data", () => {
    assert.deepEqual(
      parseStoredTheme('{"v":1,"preset":"midnight","overrides":{"accent":"#abcdef"}}'),
      {
        ok: true,
        value: { v: 1, preset: "midnight", overrides: { accent: "#abcdef" } },
      },
    );
    assert.deepEqual(
      parseStoredTheme('{"v":1,"preset":"midnight","overrides":{"capability":"secret"}}'),
      { ok: false, code: "theme_config_invalid" },
    );
  });

  it("rejects malformed, unknown, and non-hex values", () => {
    for (const raw of [
      "not-json",
      '{"v":2,"preset":"midnight","overrides":{}}',
      '{"v":1,"preset":"unknown","overrides":{}}',
      '{"v":1,"preset":"midnight","overrides":{"accent":"red"}}',
    ]) {
      assert.deepEqual(parseStoredTheme(raw), {
        ok: false,
        code: "theme_config_invalid",
      });
    }
  });

  it("uses the system theme when nothing has been stored", () => {
    assert.deepEqual(parseStoredTheme(null), { ok: true, value: DEFAULT_THEME });
  });

  it("serializes only the strict public theme shape", () => {
    assert.equal(
      serializeTheme({
        v: 1,
        preset: "graphite",
        overrides: { text: "#eeeeee" },
        ignored: "secret",
      }),
      '{"v":1,"preset":"graphite","overrides":{"text":"#eeeeee"}}',
    );
  });
});

describe("theme accessibility", () => {
  it("groups a ChatGPT-scale set of light, dark, and accessible presets", () => {
    assert.equal(PRESETS.length, 13);
    assert.deepEqual(
      Object.fromEntries(Object.entries(Object.groupBy(PRESET_OPTIONS, (option) => option.group))),
      {
        automatic: [PRESET_OPTIONS[0]],
        light: PRESET_OPTIONS.slice(1, 6),
        dark: PRESET_OPTIONS.slice(6, 11),
        accessibility: PRESET_OPTIONS.slice(11, 13),
      },
    );
    assert.deepEqual(
      PRESET_OPTIONS.map((option) => option.id),
      PRESETS,
    );
    assert.equal(EDITABLE_TOKENS.length, 8);
  });

  it("computes WCAG contrast ratios", () => {
    assert.equal(contrastRatio("#000000", "#ffffff"), 21);
    assert.equal(contrastRatio("#ffffff", "#ffffff"), 1);
  });

  it("ships every preset without contrast warnings", () => {
    const dom = new JSDOM("<!doctype html><html></html>");
    const controller = new ThemeController({
      root: dom.window.document.documentElement,
      storage: {
        getItem: () => null,
        removeItem: () => {},
        setItem: () => {},
      },
      onWarning: () => {},
      prefersDark: () => true,
    });

    for (const preset of PRESETS) {
      controller.selectPreset(preset);
      assert.deepEqual(controller.contrastWarnings(), [], preset);
    }
  });

  it("recognizes controls that suppress fallback hotkeys", () => {
    assert.equal(isEditableTarget({ tagName: "INPUT", closest: () => null }), true);
    assert.equal(
      isEditableTarget({
        tagName: "DIV",
        closest: (query) => (query === "[data-theme-panel]" ? {} : null),
      }),
      true,
    );
    assert.equal(isEditableTarget({ tagName: "DIV", closest: () => null }), false);
  });
});

describe("ThemeController", () => {
  it("reports invalid storage before resetting to the default", () => {
    const dom = new JSDOM('<button id="toggle"></button><section id="panel"></section>');
    const warnings = [];
    const removed = [];
    const storage = {
      getItem: () => "invalid",
      removeItem: (key) => removed.push(key),
      setItem: () => {},
    };
    const controller = new ThemeController({
      root: dom.window.document.documentElement,
      storage,
      onWarning: (code) => warnings.push(code),
      prefersDark: () => true,
    });

    controller.load();

    assert.deepEqual(warnings, ["theme_config_invalid"]);
    assert.deepEqual(removed, ["moco.theme.v1"]);
    assert.equal(dom.window.document.documentElement.dataset.theme, "system");
  });

  it("applies custom overrides and can reset them", () => {
    const dom = new JSDOM("<!doctype html><html></html>");
    const written = [];
    const controller = new ThemeController({
      root: dom.window.document.documentElement,
      storage: {
        getItem: () => null,
        removeItem: () => {},
        setItem: (key, value) => written.push([key, value]),
      },
      onWarning: () => {},
      prefersDark: () => true,
    });
    controller.load();

    controller.selectPreset("midnight");
    controller.setOverride("accent", "#abcdef");
    assert.equal(
      dom.window.document.documentElement.style.getPropertyValue("--c-accent"),
      "#abcdef",
    );
    controller.resetOverrides();
    assert.notEqual(
      dom.window.document.documentElement.style.getPropertyValue("--c-accent"),
      "#abcdef",
    );
    assert.ok(written.length >= 3);
  });

  it("warns when an editable border disappears into its surface", () => {
    const dom = new JSDOM("<!doctype html><html></html>");
    const controller = new ThemeController({
      root: dom.window.document.documentElement,
      storage: {
        getItem: () => null,
        removeItem: () => {},
        setItem: () => {},
      },
      onWarning: () => {},
      prefersDark: () => true,
    });
    controller.load();
    controller.selectPreset("midnight");
    controller.setOverride("border", "#111721");

    assert.ok(
      controller
        .contrastWarnings()
        .some(({ foreground, background }) => foreground === "border" && background === "surface"),
    );
  });

  it("reapplies System when the OS color preference changes", () => {
    let listener;
    let removed;
    let renders = 0;
    const controller = { theme: { preset: "system" }, apply: () => (renders += 1) };
    const stop = watchSystemTheme(
      controller,
      {
        addEventListener: (_event, next) => (listener = next),
        removeEventListener: (_event, next) => (removed = next),
      },
      () => (renders += 1),
    );

    listener();
    assert.equal(renders, 2);
    controller.theme.preset = "midnight";
    listener();
    assert.equal(renders, 2);
    stop();
    assert.equal(removed, listener);
  });
});
