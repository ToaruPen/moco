import { expect, test } from "@playwright/test";

test("browser decode context matches Irodori 48 kHz output", async ({ page }) => {
  await page.goto("/");

  const metrics = await page.evaluate(async () => {
    const { beginAudioActivation, closeAudioContext } = await import("/static/app.js");
    const activation = beginAudioActivation();
    activation.ready.catch(() => undefined);
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
    const decoded = await activation.context.decodeAudioData(wav);
    const source = activation.context.createBufferSource();
    await closeAudioContext(activation.context);
    return {
      contextSampleRate: activation.context.sampleRate,
      decodedSampleRate: decoded.sampleRate,
      decodedFrames: decoded.length,
      duration: decoded.duration,
      playbackRate: source.playbackRate.value,
      detune: source.detune.value,
    };
  });

  expect(metrics).toEqual({
    contextSampleRate: 48_000,
    decodedSampleRate: 48_000,
    decodedFrames: 4_800,
    duration: 0.1,
    playbackRate: 1,
    detune: 0,
  });
});
