"""Fetch the embedding model that ranks articles by taste, pinned and hash-checked.

potion-multilingual-128M (MinishLab, MIT): a static model2vec embedding, one
256-dimension vector per token of the bge-m3 vocabulary. An article's vector is
the mean of its tokens' vectors, so it needs no neural runtime: numpy and the
tokenizer in matins/embed.py are enough, and it is fast on a small ARM CPU.
It reads many languages, so a liked Italian article counts for a French one.

Not committed (the matrix is ~512 MB). The Docker build runs this script; for
local development run it once:

    python scripts/vendor_embed.py            # into ./embed-model
    python scripts/vendor_embed.py --check
"""

import argparse
import hashlib
import os
import sys
import urllib.request
from pathlib import Path

REPO = "minishlab/potion-multilingual-128M"
REVISION = "73908c3438cf03b6a01bcb9611d62b23d0726f08"
FILES = {
    "model.safetensors": "14b5eb39cb4ce5666da8ad1f3dc6be4346e9b2d601c073302fa0a31bf7943397",
    "tokenizer.json": "19f1909063da3cfe3bd83a782381f040dccea475f4816de11116444a73e1b6a1",
}
DEFAULT_DEST = Path(__file__).resolve().parent.parent / "embed-model"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def vendor(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for name, digest in FILES.items():
        target = dest / name
        if target.exists() and _sha256(target) == digest:
            continue
        print(f"hf   {name}", file=sys.stderr)
        tmp = target.with_suffix(target.suffix + ".part")
        request = urllib.request.Request(
            f"https://huggingface.co/{REPO}/resolve/{REVISION}/{name}",
            headers={"User-Agent": "matins-vendor-embed"},
        )
        with urllib.request.urlopen(request, timeout=600) as response, tmp.open("wb") as out:
            while block := response.read(1 << 20):
                out.write(block)
        if _sha256(tmp) != digest:
            tmp.unlink()
            sys.exit(f"sha256 mismatch for {name}")
        os.replace(tmp, target)


def check(dest: Path) -> int:
    bad = [n for n, d in FILES.items() if not (dest / n).exists() or _sha256(dest / n) != d]
    for name in bad:
        print(f"missing or changed: {name}", file=sys.stderr)
    return 1 if bad else 0


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
