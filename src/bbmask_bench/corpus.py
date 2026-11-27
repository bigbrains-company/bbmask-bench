"""Corpus discovery and deterministic sampling.

Two layouts are understood:

* **Case layout** (the layout of the bundled legal corpus)::

      <corpus>/<case-id>/dilekce.txt          the petition
      <corpus>/<case-id>/ekler/*.txt          text of each attachment
      <corpus>/<case-id>/meta.json            optional request metadata

* **Flat layout**: any directory tree of ``.txt`` files. Every file is one document.

Public identifiers are short hashes of the relative path. File names and request metadata
may carry personal data, so they never leave this module.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

PETITION_FILE = "dilekce.txt"
ATTACHMENT_DIR = "ekler"
META_FILE = "meta.json"

KIND_PETITION = "petition"
KIND_ATTACHMENT = "attachment"
KIND_DOCUMENT = "document"

# Original formats the attachment text was extracted from ("03_report.pdf.txt" → "pdf").
_SOURCE_FORMATS = {"udf", "pdf", "jpeg", "jpg", "png", "tif", "tiff", "docx", "doc", "xlsx", "xls"}
_SIZE_BUCKETS = ((5_000, "small"), (20_000, "medium"))


@dataclass(frozen=True)
class Document:
    doc_id: str
    case_id: str
    kind: str
    path: Path
    n_bytes: int
    case_type: str | None = None
    petition_type: str | None = None
    source_format: str | None = None

    @property
    def size_bucket(self) -> str:
        for limit, name in _SIZE_BUCKETS:
            if self.n_bytes < limit:
                return name
        return "large"

    def read_text(self) -> str:
        return self.path.read_text(encoding="utf-8", errors="replace")


def _short_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]


def _source_format(path: Path) -> str | None:
    suffixes = [s.lstrip(".").lower() for s in path.suffixes]
    if len(suffixes) >= 2 and suffixes[-2] in _SOURCE_FORMATS:
        fmt = suffixes[-2]
        return {"jpg": "jpeg", "tiff": "tif"}.get(fmt, fmt)
    return None


def _read_meta(case_dir: Path) -> dict:
    meta_path = case_dir / META_FILE
    if not meta_path.is_file():
        return {}
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _is_case_layout(root: Path) -> bool:
    for child in root.iterdir():
        if child.is_dir() and (
            (child / PETITION_FILE).is_file() or (child / ATTACHMENT_DIR).is_dir()
        ):
            return True
    return False


def discover(root: str | Path) -> list[Document]:
    """Return every document under ``root`` in a stable order."""
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError(f"Corpus directory not found: {root}")

    documents: list[Document] = []
    if not _is_case_layout(root):
        for path in sorted(root.rglob("*.txt")):
            rel = path.relative_to(root).as_posix()
            documents.append(
                Document(
                    doc_id=_short_hash(rel),
                    case_id=_short_hash(path.parent.relative_to(root).as_posix()),
                    kind=KIND_DOCUMENT,
                    path=path,
                    n_bytes=path.stat().st_size,
                )
            )
        return documents

    for case_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        meta = _read_meta(case_dir)
        case_id = _short_hash(case_dir.name)
        common = {
            "case_id": case_id,
            "case_type": meta.get("dava_turu") or None,
            "petition_type": meta.get("dilekce_tipi_id") or None,
        }
        petition = case_dir / PETITION_FILE
        if petition.is_file():
            documents.append(
                Document(
                    doc_id=_short_hash(f"{case_dir.name}/{PETITION_FILE}"),
                    kind=KIND_PETITION,
                    path=petition,
                    n_bytes=petition.stat().st_size,
                    **common,
                )
            )
        attachments = case_dir / ATTACHMENT_DIR
        if attachments.is_dir():
            for path in sorted(attachments.glob("*.txt")):
                documents.append(
                    Document(
                        doc_id=_short_hash(f"{case_dir.name}/{ATTACHMENT_DIR}/{path.name}"),
                        kind=KIND_ATTACHMENT,
                        path=path,
                        n_bytes=path.stat().st_size,
                        source_format=_source_format(path),
                        **common,
                    )
                )
    return documents


def filter_documents(
    documents: Iterable[Document],
    *,
    kinds: Sequence[str] | None = None,
    min_bytes: int = 0,
    max_bytes: int | None = None,
) -> list[Document]:
    selected = []
    for doc in documents:
        if kinds and doc.kind not in kinds:
            continue
        if doc.n_bytes < min_bytes:
            continue
        if max_bytes is not None and doc.n_bytes > max_bytes:
            continue
        selected.append(doc)
    return selected


def sample(documents: Sequence[Document], limit: int | None, seed: int = 42) -> list[Document]:
    """Deterministic stratified sample.

    Documents are grouped by (kind, size bucket) and drawn round-robin, so a small sample
    still covers petitions and attachments of every size. The same seed always returns the
    same documents, and a larger limit is a superset of a smaller one.
    """
    if limit is None or limit >= len(documents):
        return sorted(documents, key=lambda d: d.doc_id)
    if limit <= 0:
        return []

    strata: dict[tuple[str, str], list[Document]] = defaultdict(list)
    for doc in documents:
        strata[(doc.kind, doc.size_bucket)].append(doc)
    for members in strata.values():
        members.sort(key=lambda d: _short_hash(f"{seed}:{d.doc_id}"), reverse=True)

    picked: list[Document] = []
    keys = sorted(strata)
    while len(picked) < limit:
        for key in keys:
            if strata[key]:
                picked.append(strata[key].pop())
                if len(picked) == limit:
                    break
    return picked


def describe(documents: Sequence[Document]) -> dict:
    """PII-free corpus statistics."""
    sizes = sorted(d.n_bytes for d in documents)

    def pct(q: float) -> int:
        return sizes[min(len(sizes) - 1, int(len(sizes) * q))] if sizes else 0

    def top(values: Iterable[str | None]) -> dict[str, int]:
        return dict(Counter(v or "unknown" for v in values).most_common())

    petitions = [d for d in documents if d.kind == KIND_PETITION]
    return {
        "documents": len(documents),
        "cases": len({d.case_id for d in documents}),
        "by_kind": top(d.kind for d in documents),
        "by_size_bucket": top(d.size_bucket for d in documents),
        "petitions_by_case_type": top(d.case_type for d in petitions),
        "attachments_by_source_format": top(
            d.source_format for d in documents if d.kind == KIND_ATTACHMENT
        ),
        "bytes": {
            "total": sum(sizes),
            "min": sizes[0] if sizes else 0,
            "p50": pct(0.50),
            "p90": pct(0.90),
            "p99": pct(0.99),
            "max": sizes[-1] if sizes else 0,
        },
    }
