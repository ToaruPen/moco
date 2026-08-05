export const THEME_STORAGE_KEY = "moco.theme.v1";
export const EDITABLE_TOKENS = Object.freeze([
  "background",
  "surface",
  "surfaceRaised",
  "border",
  "text",
  "textMuted",
  "accent",
  "actionAccent",
]);
export const DEFAULT_THEME = Object.freeze({
  v: 1,
  preset: "system",
  overrides: Object.freeze({}),
});

const HEX_COLOR = /^#[0-9a-f]{6}$/i;
const PRESET_COLORS = Object.freeze({
  porcelain: palette({
    background: "#f6f7f9",
    surface: "#ffffff",
    surfaceRaised: "#eef1f5",
    border: "#7b8592",
    text: "#1b2028",
    textMuted: "#596473",
    accent: "#5565d8",
    actionAccent: "#a65f00",
  }),
  paper: palette({
    background: "#f7f5f0",
    surface: "#fffdf8",
    surfaceRaised: "#eee9df",
    border: "#8f8372",
    text: "#24211c",
    textMuted: "#665f54",
    accent: "#6068c5",
    actionAccent: "#94611a",
  }),
  mist: palette({
    background: "#f1f5f7",
    surface: "#fbfdfe",
    surfaceRaised: "#e5ecef",
    border: "#738893",
    text: "#192329",
    textMuted: "#566a74",
    accent: "#3f7186",
    actionAccent: "#9a5e29",
  }),
  sage: palette({
    background: "#f1f5f0",
    surface: "#fbfdfb",
    surfaceRaised: "#e4ece3",
    border: "#738777",
    text: "#202720",
    textMuted: "#5d695e",
    accent: "#3f7560",
    actionAccent: "#8a5a20",
  }),
  rose: palette({
    background: "#f8f3f4",
    surface: "#fffafb",
    surfaceRaised: "#f0e5e8",
    border: "#8d777e",
    text: "#2b2225",
    textMuted: "#746268",
    accent: "#95586c",
    actionAccent: "#916020",
  }),
  midnight: palette({
    background: "#090c12",
    surface: "#111721",
    surfaceRaised: "#171f2c",
    border: "#59687a",
    text: "#edf1f7",
    textMuted: "#8c98a8",
    accent: "#9da8ff",
    actionAccent: "#f5b65a",
  }),
  graphite: palette({
    background: "#17191d",
    surface: "#202329",
    surfaceRaised: "#292d35",
    border: "#6b727d",
    text: "#e8eaee",
    textMuted: "#a4aab4",
    accent: "#8eb6b1",
    actionAccent: "#d6a766",
  }),
  ocean: palette({
    background: "#081116",
    surface: "#0e1b22",
    surfaceRaised: "#142630",
    border: "#587b8c",
    text: "#e7f0f3",
    textMuted: "#8fa5ae",
    accent: "#72b6c7",
    actionAccent: "#e2a75c",
  }),
  forest: palette({
    background: "#0b120f",
    surface: "#121d18",
    surfaceRaised: "#19271f",
    border: "#5e7b69",
    text: "#ebf1ed",
    textMuted: "#94a69a",
    accent: "#78b792",
    actionAccent: "#d9a457",
  }),
  aubergine: palette({
    background: "#130d15",
    surface: "#1d1420",
    surfaceRaised: "#281b2c",
    border: "#765d7c",
    text: "#f1eaf3",
    textMuted: "#ad99b1",
    accent: "#bd99d1",
    actionAccent: "#e1a65f",
  }),
  "high-contrast-light": palette({
    background: "#ffffff",
    surface: "#ffffff",
    surfaceRaised: "#f0f0f0",
    border: "#000000",
    text: "#000000",
    textMuted: "#333333",
    accent: "#003cc5",
    actionAccent: "#7a3e00",
  }),
  "high-contrast-dark": palette({
    background: "#000000",
    surface: "#0a0a0a",
    surfaceRaised: "#151515",
    border: "#ffffff",
    text: "#ffffff",
    textMuted: "#d6d6d6",
    accent: "#84adff",
    actionAccent: "#ffd45c",
  }),
});

export const PRESET_OPTIONS = Object.freeze([
  presetOption("system", "System", "automatic", "system", PRESET_COLORS.midnight),
  presetOption("porcelain", "Porcelain", "light", "light", PRESET_COLORS.porcelain),
  presetOption("paper", "Paper", "light", "light", PRESET_COLORS.paper),
  presetOption("mist", "Mist", "light", "light", PRESET_COLORS.mist),
  presetOption("sage", "Sage", "light", "light", PRESET_COLORS.sage),
  presetOption("rose", "Rose", "light", "light", PRESET_COLORS.rose),
  presetOption("midnight", "Midnight", "dark", "dark", PRESET_COLORS.midnight),
  presetOption("graphite", "Graphite", "dark", "dark", PRESET_COLORS.graphite),
  presetOption("ocean", "Ocean", "dark", "dark", PRESET_COLORS.ocean),
  presetOption("forest", "Forest", "dark", "dark", PRESET_COLORS.forest),
  presetOption("aubergine", "Aubergine", "dark", "dark", PRESET_COLORS.aubergine),
  presetOption(
    "high-contrast-light",
    "High Contrast Light",
    "accessibility",
    "light",
    PRESET_COLORS["high-contrast-light"],
  ),
  presetOption(
    "high-contrast-dark",
    "High Contrast Dark",
    "accessibility",
    "dark",
    PRESET_COLORS["high-contrast-dark"],
  ),
]);
export const PRESETS = Object.freeze(PRESET_OPTIONS.map(({ id }) => id));

const CSS_PROPERTIES = Object.freeze(
  Object.fromEntries(
    EDITABLE_TOKENS.map((token) => [
      token,
      `--c-${token.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`)}`,
    ]),
  ),
);

export function parseStoredTheme(raw) {
  if (raw === null) {
    return { ok: true, value: DEFAULT_THEME };
  }
  try {
    const parsed = JSON.parse(raw);
    if (
      parsed === null ||
      Array.isArray(parsed) ||
      typeof parsed !== "object" ||
      parsed.v !== 1 ||
      !PRESETS.includes(parsed.preset) ||
      parsed.overrides === null ||
      Array.isArray(parsed.overrides) ||
      typeof parsed.overrides !== "object"
    ) {
      return invalidTheme();
    }
    const keys = Object.keys(parsed);
    if (keys.some((key) => !["v", "preset", "overrides"].includes(key))) {
      return invalidTheme();
    }
    const overrides = {};
    for (const [key, value] of Object.entries(parsed.overrides)) {
      if (!EDITABLE_TOKENS.includes(key) || typeof value !== "string" || !HEX_COLOR.test(value)) {
        return invalidTheme();
      }
      overrides[key] = value.toLowerCase();
    }
    return { ok: true, value: { v: 1, preset: parsed.preset, overrides } };
  } catch {
    return invalidTheme();
  }
}

export function serializeTheme(theme) {
  const parsed = parseStoredTheme(
    JSON.stringify({ v: theme.v, preset: theme.preset, overrides: theme.overrides }),
  );
  if (!parsed.ok) {
    throw new TypeError("theme must match the public theme schema");
  }
  return JSON.stringify(parsed.value);
}

export function contrastRatio(first, second) {
  const firstLuminance = relativeLuminance(first);
  const secondLuminance = relativeLuminance(second);
  const lighter = Math.max(firstLuminance, secondLuminance);
  const darker = Math.min(firstLuminance, secondLuminance);
  return (lighter + 0.05) / (darker + 0.05);
}

export function isEditableTarget(target) {
  if (!target || typeof target !== "object") {
    return false;
  }
  const tagName = String(target.tagName ?? "").toUpperCase();
  if (["INPUT", "SELECT", "TEXTAREA"].includes(tagName) || target.isContentEditable === true) {
    return true;
  }
  return typeof target.closest === "function" && target.closest("[data-theme-panel]") !== null;
}

export function watchSystemTheme(controller, mediaQuery, onRender) {
  const handleChange = () => {
    if (controller.theme.preset === "system") {
      controller.apply();
      onRender();
    }
  };
  mediaQuery.addEventListener("change", handleChange);
  return () => mediaQuery.removeEventListener("change", handleChange);
}

export class ThemeController {
  constructor({ root, storage, onWarning, prefersDark = defaultPrefersDark }) {
    this.root = root;
    this.storage = storage;
    this.onWarning = onWarning;
    this.prefersDark = prefersDark;
    this.theme = DEFAULT_THEME;
  }

  load() {
    const parsed = parseStoredTheme(this.storage.getItem(THEME_STORAGE_KEY));
    if (!parsed.ok) {
      this.onWarning(parsed.code);
      this.storage.removeItem(THEME_STORAGE_KEY);
      this.theme = DEFAULT_THEME;
    } else {
      this.theme = parsed.value;
    }
    this.apply();
    return this.theme;
  }

  selectPreset(preset) {
    if (!PRESETS.includes(preset)) {
      throw new TypeError("unknown theme preset");
    }
    this.theme = { v: 1, preset, overrides: {} };
    this.apply();
    this.#persist();
  }

  setOverride(token, value) {
    if (!EDITABLE_TOKENS.includes(token) || !HEX_COLOR.test(value)) {
      throw new TypeError("invalid theme override");
    }
    this.theme = {
      v: 1,
      preset: this.theme.preset,
      overrides: { ...this.theme.overrides, [token]: value.toLowerCase() },
    };
    this.apply();
    this.#persist();
  }

  resetOverride(token) {
    if (!EDITABLE_TOKENS.includes(token)) {
      throw new TypeError("unknown theme token");
    }
    const overrides = { ...this.theme.overrides };
    delete overrides[token];
    this.theme = { ...this.theme, overrides };
    this.apply();
    this.#persist();
  }

  resetOverrides() {
    this.theme = { v: 1, preset: this.theme.preset, overrides: {} };
    this.apply();
    this.#persist();
  }

  apply() {
    const { palette: colors, resolvedPreset } = this.#resolvedTheme();
    this.root.dataset.theme = this.theme.preset;
    this.root.dataset.polarity = PRESET_OPTIONS.find(({ id }) => id === resolvedPreset).polarity;
    this.root.style.colorScheme = this.root.dataset.polarity;
    for (const token of EDITABLE_TOKENS) {
      this.root.style.setProperty(CSS_PROPERTIES[token], colors[token]);
    }
    return colors;
  }

  contrastWarnings() {
    const { palette: colors } = this.#resolvedTheme();
    const checks = [
      ["text", "surface", 4.5],
      ["textMuted", "surface", 3],
      ["accent", "surface", 3],
      ["accent", "surfaceRaised", 3],
      ["border", "surface", 3],
    ];
    return checks
      .map(([foreground, background, minimum]) => ({
        foreground,
        background,
        minimum,
        ratio: contrastRatio(colors[foreground], colors[background]),
      }))
      .filter((check) => check.ratio < check.minimum);
  }

  #resolvedTheme() {
    const resolvedPreset =
      this.theme.preset === "system"
        ? this.prefersDark()
          ? "midnight"
          : "porcelain"
        : this.theme.preset;
    return {
      palette: { ...PRESET_COLORS[resolvedPreset], ...this.theme.overrides },
      resolvedPreset,
    };
  }

  #persist() {
    this.storage.setItem(THEME_STORAGE_KEY, serializeTheme(this.theme));
  }
}

function invalidTheme() {
  return { ok: false, code: "theme_config_invalid" };
}

function palette(colors) {
  return Object.freeze(colors);
}

function presetOption(id, label, group, polarity, colors) {
  return Object.freeze({
    id,
    label,
    group,
    polarity,
    preview: Object.freeze([colors.background, colors.surfaceRaised, colors.accent]),
  });
}

function defaultPrefersDark() {
  return globalThis.matchMedia?.("(prefers-color-scheme: dark)").matches ?? true;
}

function relativeLuminance(color) {
  if (!HEX_COLOR.test(color)) {
    throw new TypeError("contrast colors must use #rrggbb");
  }
  const channels = [1, 3, 5].map((offset) => Number.parseInt(color.slice(offset, offset + 2), 16));
  const linear = channels.map((channel) => {
    const value = channel / 255;
    return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
}
