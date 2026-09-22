"""Export Docling HybridChunker chunks for the chunking comparison.

Runs in an environment that has Docling installed (it is deliberately NOT a dependency of
the service: Docling pulls in PyTorch and layout models). Output is plain JSON that
evaluation/chunking_eval.py scores with the same embedder and metrics as every other
strategy.

Usage: <python-with-docling> evaluation/docling_export.py sample_data/knowledge OUT.json
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import tiktoken
from docling.document_converter import DocumentConverter
from docling_core.transforms.chunker.hybrid_chunker import HybridChunker
from docling_core.transforms.chunker.tokenizer.openai import OpenAITokenizer

MAX_TOKENS = 256


def _meta_and_body(path: Path) -> tuple[str, str | None]:
    """Our metadata (doc key) plus, for Markdown/text, the body without front matter."""
    if path.suffix == ".md":
        text = path.read_text()
        _, fm, body = text.split("---", 2)
        key = next(
            line.split(":", 1)[1].strip()
            for line in fm.splitlines()
            if line.startswith("document_id")
        )
        return key, body
    meta = json.loads(path.with_name(path.name + ".meta.json").read_text())
    body = path.read_text() if path.suffix == ".txt" else None
    return meta["document_id"], body


def main(root: Path, out: Path) -> None:
    converter = DocumentConverter()
    chunker = HybridChunker(
        tokenizer=OpenAITokenizer(
            tokenizer=tiktoken.get_encoding("cl100k_base"), max_tokens=MAX_TOKENS
        ),
        merge_peers=True,
    )
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for path in sorted(p for p in root.iterdir() if p.suffix in {".md", ".pdf", ".txt"}):
            key, body = _meta_and_body(path)
            source = path
            if body is not None:  # Docling reads Markdown; plain text is valid Markdown
                source = Path(tmp) / f"{key}.md"
                source.write_text(body)
            doc = converter.convert(source).document
            for chunk in chunker.chunk(dl_doc=doc):
                rows.append(
                    {
                        "doc_key": key,
                        "text": chunk.text,
                        "embed_text": chunker.contextualize(chunk=chunk),
                        "headings": list(chunk.meta.headings or []),
                    }
                )
            print(f"{key}: {sum(r['doc_key'] == key for r in rows)} chunks", file=sys.stderr)
    out.write_text(json.dumps(rows, indent=1))


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
