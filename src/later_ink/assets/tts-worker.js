// Reads an article aloud: Kokoro-82M in the reader's browser, never on the server.
//
// The page sends the article's paragraphs ("speak") and then pulls audio one
// chunk at a time ("next"), a few ahead of what is playing. Each reply carries
// the run id it was asked for, so a Stop or a jump discards whatever was still
// being synthesised.
//
// Kokoro reads phonemes, at most 510 tokens per call (about two paragraphs).
// Text is cut at sentences, long sentences at commas, and a chunk that still
// phonemizes too long is halved until it fits.
//
// English goes through kokoro-js's own phonemizer, the one the English voices
// were trained against. French, Italian, Spanish and Portuguese go through
// ephone (eSpeak NG in WASM, GPL-3.0, served unmodified beside this file), which
// is what Kokoro's Python package uses for those languages too.
//
// Every file is served by Matins (/assets/tts/). transformers.js asks
// huggingface.co for the model; the fetch below answers from here instead, and
// refuses anything else that would leave the origin.

const HF = "https://huggingface.co/onnx-community/Kokoro-82M-v1.0-ONNX/resolve/main/";
const LOCAL = new URL("./model/", self.location.href).href;
const realFetch = self.fetch.bind(self);
self.fetch = (input, init) => {
  let url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  if (url.startsWith(HF)) url = LOCAL + url.slice(HF.length);
  else if (new URL(url, self.location.href).origin !== self.location.origin) {
    return Promise.reject(new TypeError(`listening fetches nothing off-site: ${url}`));
  }
  return realFetch(url, init);
};

// Imported, not awaited at the top: a module worker runs its event loop during
// a top-level await, and the page's first messages would arrive before
// onmessage is set and be dropped.
// Safari (and every iOS browser) gets onnxruntime built without JSEP: on iOS 26
// WebKit's optimising compiler can spin on the JSEP build until the tab is
// killed (onnxruntime#26827). WebGPU needs JSEP, so Safari stays on WASM.
const WEBKIT =
  /iPhone|iPad|iPod/.test(self.navigator.userAgent) ||
  (/Safari\//.test(self.navigator.userAgent) &&
    !/Chrome\/|Chromium\/|Edg\/|Firefox\//.test(self.navigator.userAgent));
const kokoro = import("./kokoro.web.js").then((module) => {
  module.env.wasmPaths = WEBKIT
    ? {
        mjs: new URL("./ort-plain/ort-wasm-simd-threaded.mjs", self.location.href).href,
        wasm: new URL("./ort-plain/ort-wasm-simd-threaded.wasm", self.location.href).href,
      }
    : new URL("./ort/", self.location.href).href;
  return module;
});

const MAX_TOKENS = 510;
const MAX_WORDS = 50;
const FIRST_WORDS = 12;
// ephone voice names for Kokoro's language letters (the first of a voice id).
const ESPEAK = { f: "fr", i: "it", e: "es", p: "pt-BR" };

let tts = null;
let loading = null;
let ephone = null;
let queue = [];
let run = 0;
let voice = "af_heart";
let work = Promise.resolve();

function describe(error) {
  return String((error && error.message) || error || "").slice(0, 160);
}

// A failure outside a request (loading the model, a WASM abort) would otherwise
// leave the page on its last status forever.
self.addEventListener("unhandledrejection", (event) => {
  self.postMessage({ type: "error", text: "The voice could not start.", detail: describe(event.reason) });
});

function status(text) {
  self.postMessage({ type: "status", text });
}

function mobile() {
  const data = self.navigator.userAgentData;
  if (data && typeof data.mobile === "boolean") return data.mobile;
  return /Android|iPhone|iPad|Mobile/i.test(self.navigator.userAgent);
}

async function gpu() {
  try {
    return Boolean(self.navigator.gpu && (await self.navigator.gpu.requestAdapter()));
  } catch {
    return false;
  }
}

// WebGPU wants the full-precision model; WASM the 8-bit one. A phone gets the
// smaller download even when it has WebGPU.
async function model() {
  if (tts) return tts;
  if (!loading) {
    loading = (async () => {
      const { KokoroTTS } = await kokoro;
      const total = {};
      const progress = (event) => {
        if (event.status !== "progress" || !event.total) return;
        total[event.file] = [event.loaded, event.total];
        const [done, size] = Object.values(total).reduce(
          ([a, b], [c, d]) => [a + c, b + d],
          [0, 0],
        );
        if (size < 5e6) return;
        const percent = Math.round((100 * done) / size);
        // The last step, building the model in memory, is the slow one on a phone.
        status(percent < 100 ? `Downloading the voice… ${percent}%` : "Preparing the voice…");
      };
      status("Loading the voice…");
      if (!WEBKIT && !mobile() && (await gpu())) {
        try {
          return await KokoroTTS.from_pretrained("onnx-community/Kokoro-82M-v1.0-ONNX", {
            dtype: "fp32",
            device: "webgpu",
            progress_callback: progress,
          });
        } catch (error) {
          console.warn("WebGPU unavailable, using WASM", error);
        }
      }
      return KokoroTTS.from_pretrained("onnx-community/Kokoro-82M-v1.0-ONNX", {
        dtype: "q8",
        device: "wasm",
        progress_callback: progress,
      });
    })();
  }
  tts = await loading;
  status("");
  self.postMessage({ type: "ready" });
  return tts;
}

async function phonemizer(lang) {
  if (!ephone) {
    const { default: createEphone, roa } = await import("./ephone/ephone.js");
    ephone = await createEphone(roa);
  }
  ephone.setVoice(lang);
  return ephone;
}

// Sentences, then commas for a sentence that is still too long, then words.
export function chunks(text, maxWords = MAX_WORDS) {
  const out = [];
  for (const sentence of text.split(/(?<=[.!?…;:])\s+/)) {
    const words = sentence.trim().split(/\s+/).filter(Boolean);
    if (!words.length) continue;
    if (words.length <= maxWords) {
      out.push(words.join(" "));
      continue;
    }
    let current = [];
    for (const word of words) {
      current.push(word);
      const atComma = current.length >= maxWords / 2 && /[,—–]$/.test(word);
      if (atComma || current.length >= maxWords) {
        out.push(current.join(" "));
        current = [];
      }
    }
    if (current.length) out.push(current.join(" "));
  }
  return out;
}

function halves(text) {
  const words = text.split(/\s+/);
  const middle = Math.ceil(words.length / 2);
  return [words.slice(0, middle).join(" "), words.slice(middle).join(" ")];
}

async function synthesise(text, speed) {
  const engine = await model();
  const lang = ESPEAK[voice[0]];
  if (!lang) {
    // kokoro-js phonemizes English itself; 50 words stay far under the limit.
    return [await engine.generate(text, { voice, speed })];
  }
  const phonemes = (await phonemizer(lang))
    .textToIpa(text)
    // eSpeak marks French liaison with a hyphen Kokoro has no token for.
    .replace(/-/g, "")
    .replace(/\s+/g, " ")
    .trim();
  const { input_ids: ids } = engine.tokenizer(phonemes, { truncation: false });
  if (ids.dims.at(-1) > MAX_TOKENS && text.includes(" ")) {
    const [first, second] = halves(text);
    return [...(await synthesise(first, speed)), ...(await synthesise(second, speed))];
  }
  return [await engine.generate_from_ids(ids, { voice, speed })];
}

async function next(asked, speed) {
  if (asked !== run) return;
  const item = queue.shift();
  if (!item) {
    self.postMessage({ type: "end", run: asked });
    return;
  }
  try {
    for (const audio of await synthesise(item.text, speed || 1)) {
      if (asked !== run) return;
      const samples = audio.audio;
      self.postMessage(
        { type: "audio", run: asked, block: item.block, samples, rate: audio.sampling_rate },
        [samples.buffer],
      );
    }
  } catch (error) {
    console.error(error);
    self.postMessage({ type: "error", run: asked, text: "Could not read this article aloud.", detail: describe(error) });
  }
}

self.onmessage = (event) => {
  const message = event.data;
  if (message.type === "speak") {
    run = message.run;
    voice = message.voice || "af_heart";
    queue = message.blocks.flatMap((text, i) =>
      chunks(text).map((chunk) => ({ block: message.offset + i, text: chunk })),
    );
    // The first chunk decides how long the reader waits for sound: keep it short.
    if (queue.length) {
      const [first, ...rest] = queue;
      queue = [
        ...chunks(first.text, FIRST_WORDS).map((text) => ({ block: first.block, text })),
        ...rest,
      ];
    }
    // Start loading while the page asks for the first chunks; a failure here is
    // reported once, by the first request.
    model().catch(() => {});
  } else if (message.type === "next") {
    // One synthesis at a time: the model is not reentrant.
    work = work.then(() => next(message.run, message.speed));
  }
};
