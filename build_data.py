#!/usr/bin/env python3
"""Build the offline dictionary + word lists for glosa.

Sources:
  * FreeDict eng-swe (WikDict build of Wiktionary, ~44k headwords, CC-BY-SA)
  * FrequencyWords 50k lists (sv/en) for suggestion ranking

Run:  python3 build_data.py [--force]
Output: data/glosa.db, data/sv.txt, data/en.txt
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import tarfile
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
CACHE = HERE / ".cache"
NS = "{http://www.tei-c.org/ns/1.0}"

FREQ = {
    "sv": "https://raw.githubusercontent.com/hermitdave/FrequencyWords/master/content/2018/sv/sv_50k.txt",
    "en": "https://raw.githubusercontent.com/hermitdave/FrequencyWords/master/content/2018/en/en_50k.txt",
}
UA = "glosa/1.0 (+https://github.com/mikul-/glosa)"

LETTERS = re.compile(r"^[a-zåäöéüøæ]{2,}(?:-[a-zåäöéüøæ]+)*$")


def get(url: str, dest: Path, force: bool = False) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and not force and dest.stat().st_size > 0:
        return dest
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    print(f"  downloading {url}")
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        f.write(r.read())
    return dest


def freedict_src() -> Path:
    """Resolve the newest eng-swe src release and download it."""
    meta = CACHE / "freedict-database.json"
    get("https://freedict.org/freedict-database.json", meta)
    info = json.loads(meta.read_text())
    entry = next(e for e in info if e.get("name") == "eng-swe")
    rel = next(
        r for r in entry["releases"]
        if r["platform"] == "src" and r["URL"].endswith(".src.tar.xz")
    )
    version = rel["version"]
    return get(rel["URL"], CACHE / f"eng-swe-{version}.src.tar.xz")


def read_freq(lang: str) -> dict[str, int]:
    txt = get(FREQ[lang], CACHE / f"{lang}_50k.txt").read_text(errors="ignore")
    out: dict[str, int] = {}
    for i, line in enumerate(txt.splitlines(), 1):
        parts = line.split()
        if parts and LETTERS.match(parts[0]):
            out.setdefault(parts[0].lower(), i)
    return out


def text_of(el) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def build() -> None:
    started = time.time()
    DATA.mkdir(parents=True, exist_ok=True)
    print("Resolving FreeDict eng-swe …")
    archive = freedict_src()

    print("Reading FreeDict …")
    tei = None
    with tarfile.open(archive) as tar:
        for m in tar:
            if m.name.endswith(".tei"):
                tei = tar.extractfile(m).read()
                break
    if tei is None:
        sys.exit("no .tei in archive")

    db_path = DATA / "glosa.db"
    tmp = DATA / "glosa.db.tmp"
    for p in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
        p.unlink(missing_ok=True)

    db = sqlite3.connect(tmp)
    db.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE entries(
            id INTEGER PRIMARY KEY,
            word TEXT NOT NULL,
            pos TEXT,
            def TEXT
        );
        CREATE TABLE trans(entry_id INTEGER NOT NULL, t TEXT NOT NULL);
        CREATE TABLE wiki(
            word TEXT PRIMARY KEY,
            def TEXT,
            extra TEXT,
            fetched INTEGER NOT NULL
        );
        """
    )

    rows = 0
    sv_words: dict[str, int] = {}
    en_words: dict[str, int] = {}

    # Streaming parse: 23 MB, ~1 s.
    for _event, el in ET.iterparse(
        __import__("io").BytesIO(tei), events=("end",)
    ):
        if el.tag != NS + "entry":
            continue
        word = text_of(el.find(NS + "form/" + NS + "orth")).strip().lower()
        pos = text_of(el.find(NS + "gramGrp/" + NS + "pos"))
        if not word:
            el.clear()
            continue
        en_words.setdefault(word, 100000)
        for sense in el.findall(NS + "sense"):
            trans = [
                text_of(q).strip()
                for cit in sense.findall(NS + "cit")
                if cit.get("type") == "trans"
                for q in cit.findall(NS + "quote")
            ]
            trans = [t for t in trans if t]
            if not trans:
                continue
            dfn = text_of(sense.find(NS + "sense/" + NS + "def"))
            cur = db.execute(
                "INSERT INTO entries(word, pos, def) VALUES(?,?,?)", (word, pos, dfn)
            )
            eid = cur.lastrowid
            db.executemany(
                "INSERT INTO trans(entry_id, t) VALUES(?,?)",
                [(eid, t) for t in trans],
            )
            for t in trans:
                tl = t.lower()
                if LETTERS.match(tl):
                    sv_words.setdefault(tl, 100000)
            rows += 1
        el.clear()

    print(f"  {rows} senses")

    # Merge frequency ranks (lower rank = more common).
    for lang, words in (("sv", sv_words), ("en", en_words)):
        freq = read_freq(lang)
        for w, rank in freq.items():
            if w in words:
                words[w] = rank
        ordered = sorted(words.items(), key=lambda kv: (kv[1], len(kv[0]), kv[0]))
        (DATA / f"{lang}.txt").write_text("".join(w + "\n" for w, _ in ordered))
        print(f"  {lang}: {len(ordered)} suggestion words")

    db.executescript(
        """
        CREATE INDEX idx_entries_word ON entries(word);
        CREATE INDEX idx_trans_t ON trans(t);
        CREATE INDEX idx_trans_entry ON trans(entry_id);
        ANALYZE;
        """
    )
    db.commit()
    db.execute("PRAGMA journal_mode=DELETE")
    db.close()
    tmp.replace(db_path)
    print(f"Done in {time.time() - started:.1f}s → {db_path} "
          f"({db_path.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    build()
