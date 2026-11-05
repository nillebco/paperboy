"""Fetch the Reader's text-to-speech files, pinned and hash-checked.

Listening runs entirely in the reader's browser: Kokoro (kokoro-js, on
transformers.js/onnxruntime-web) synthesises, and ephone (eSpeak NG compiled to
WASM) turns French, Italian, Spanish and Portuguese text into the phonemes
Kokoro reads. Matins serves every file itself, so a reader's page talks to no
third party and the Content-Security-Policy stays `connect-src 'self'`.

None of this is committed: the model alone is ~420 MB. The Docker build runs
this script; for local development run it once:

    python scripts/vendor_tts.py            # into ./tts-assets
    python scripts/vendor_tts.py --check    # verify what is there

Every file is pinned by hash. npm tarballs are checked against the registry's
sha512 integrity; Hugging Face files are fetched at a fixed commit and checked
by sha256. A mismatch is a build failure, never a warning.

Licences: kokoro-js and transformers.js are Apache-2.0, the Kokoro weights are
Apache-2.0, ephone is GPL-3.0-or-later (eSpeak NG). ephone is served unmodified
as separate files, with its COPYING beside it; Matins' own code does not bundle it.
"""

import argparse
import base64
import hashlib
import io
import os
import sys
import tarfile
import urllib.request
from pathlib import Path

NPM = [
    # (tarball, sha512 integrity, {path inside the tarball: destination})
    (
        "https://registry.npmjs.org/kokoro-js/-/kokoro-js-1.2.1.tgz",
        "sha512-oq0HZJWis3t8lERkMJh84WLU86dpYD0EuBPtqYnLlQzyFP1OkyBRDcweAqCfhNOpltyN9j/azp1H6uuC47gShw==",
        {
            "package/dist/kokoro.web.js": "kokoro.web.js",
            "package/LICENSE": "LICENSE.kokoro-js",
        },
    ),
    (
        # The onnxruntime-web build that transformers.js 3.5.1 (bundled inside
        # kokoro.web.js) loads at runtime. It would otherwise come from jsdelivr.
        "https://registry.npmjs.org/@huggingface/transformers/-/transformers-3.5.1.tgz",
        "sha512-qWsPoJMBPYcrGuzRMVL//3dwcLXED9r+j+Hzw+hZpBfrMPdyq57ehNs7lew0D34Paj0DO0E0kZQjOZcIVN17hQ==",
        {
            "package/dist/ort-wasm-simd-threaded.jsep.mjs": "ort/ort-wasm-simd-threaded.jsep.mjs",
            "package/dist/ort-wasm-simd-threaded.jsep.wasm": "ort/ort-wasm-simd-threaded.jsep.wasm",
            "package/LICENSE": "LICENSE.transformers",
        },
    ),
    (
        "https://registry.npmjs.org/ephone/-/ephone-1.0.2.tgz",
        "sha512-Ziw4PP2P2WkFOUwGk1g5ldkdGTZNFUjcrTfDS1+ltBk0qFlwM8nz8oYSykPBN1/TxOkZ63hGu6KXBQ1IGHRMgg==",
        {
            "package/ephone.js": "ephone/ephone.js",
            # Romance languages only: fr, it, es, pt, pt-BR. English goes through
            # kokoro-js's own phonemizer, which Kokoro was trained against.
            "package/lang/roa.js": "ephone/lang/roa.js",
            "package/COPYING": "ephone/COPYING",
        },
    ),
]

HF_REPO = "onnx-community/Kokoro-82M-v1.0-ONNX"
HF_REVISION = "1939ad2a8e416c0acfeecc08a694d14ef25f2231"

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEST = ROOT / "tts-assets"
PINS = Path(__file__).with_name("vendor_tts.sha256")


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "matins-vendor-tts"})
    with urllib.request.urlopen(request, timeout=300) as response:
        return response.read()


def _sha512_integrity(data: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()


def _write(dest: Path, relative: str, data: bytes) -> None:
    target = dest / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, target)


def _hf_pins() -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in PINS.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            digest, name = line.split(None, 1)
            pins[name] = digest
    return pins


def vendor(dest: Path) -> None:
    for url, integrity, files in NPM:
        print(f"npm  {url.rsplit('/', 1)[-1]}", file=sys.stderr)
        blob = _get(url)
        if _sha512_integrity(blob) != integrity:
            sys.exit(f"integrity mismatch for {url}")
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            for member, relative in files.items():
                extracted = tar.extractfile(member)
                if extracted is None:
                    sys.exit(f"{member} missing from {url}")
                _write(dest, relative, extracted.read())

    for name, digest in _hf_pins().items():
        target = dest / "model" / name
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
            continue
        print(f"hf   {name}", file=sys.stderr)
        data = _get(f"https://huggingface.co/{HF_REPO}/resolve/{HF_REVISION}/{name}")
        if hashlib.sha256(data).hexdigest() != digest:
            sys.exit(f"sha256 mismatch for {name}")
        _write(dest, f"model/{name}", data)


def check(dest: Path) -> int:
    missing = [
        name
        for name, digest in _hf_pins().items()
        if not (dest / "model" / name).exists()
        or hashlib.sha256((dest / "model" / name).read_bytes()).hexdigest() != digest
    ]
    for name in missing:
        print(f"missing or changed: model/{name}", file=sys.stderr)
    return 1 if missing else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("dest", nargs="?", type=Path, default=DEFAULT_DEST)
    parser.add_argument("--check", action="store_true", help="verify, do not download")
    args = parser.parse_args()
    if args.check:
        return check(args.dest)
    vendor(args.dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
