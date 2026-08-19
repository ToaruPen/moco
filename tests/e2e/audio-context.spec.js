import { expect, test } from "@playwright/test";

test("browser decode context matches Irodori 48 kHz output", async ({ page }) => {
  await page.goto("/");

  const metrics = await page.evaluate(async () => {
    const { beginAudioActivation, closeAudioContext } = await import("/static/app.js");
    const activation = beginAudioActivation();
    await activation.ready;
    const frames = 4_800;
    const wav = new ArrayBuffer(44 + frames * 2);
    const view = new DataView(wav);
    const ascii = (offset, value) => {
      for (let index = 0; index < value.length; index += 1) {
        view.setUint8(offset + index, value.charCodeAt(index));
      }
    };
    ascii(0, "RIFF");
    view.setUint32(4, wav.byteLength - 8, true);
    ascii(8, "WAVE");
    ascii(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, 48_000, true);
    view.setUint32(28, 96_000, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    ascii(36, "data");
    view.setUint32(40, frames * 2, true);
    for (let index = 0; index < frames; index += 1) {
      const sample = Math.sin((2 * Math.PI * 440 * index) / 48_000) * 0.25;
      view.setInt16(44 + index * 2, Math.round(sample * 32_767), true);
    }
    const decoded = await activation.context.decodeAudioData(wav);
    const source = activation.context.createBufferSource();
    source.buffer = decoded;
    source.connect(activation.context.destination);
    const ended = new Promise((resolve) => {
      source.addEventListener("ended", () => resolve(true), { once: true });
    });
    source.start();
    const played = await ended;
    const contextState = activation.context.state;
    const playbackRate = source.playbackRate.value;
    const detune = source.detune.value;
    await closeAudioContext(activation.context);
    return {
      contextSampleRate: activation.context.sampleRate,
      contextState,
      decodedSampleRate: decoded.sampleRate,
      decodedFrames: decoded.length,
      duration: decoded.duration,
      playbackRate,
      detune,
      played,
    };
  });

  expect(metrics).toEqual({
    contextSampleRate: 48_000,
    contextState: "running",
    decodedSampleRate: 48_000,
    decodedFrames: 4_800,
    duration: 0.1,
    playbackRate: 1,
    detune: 0,
    played: true,
  });
});
