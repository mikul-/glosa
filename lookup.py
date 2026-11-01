#!/usr/bin/env python3
"""Dictionary backend for glosa: offline FreeDict lookups, sv.wiktionary glosses."""
from __future__ import annotations

import difflib
import json
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from bisect import bisect_left
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
DB_PATH = DATA / "glosa.db"

UA = "glosa/1.0 (+https://github.com/mikul-/glosa)"
WIKT_API = "https://sv.wiktionary.org/w/api.php"
WIKT_TTL = 60 * 60 * 24 * 30  # refresh glosses monthly

POS_SV = {
    "n": "subst.", "v": "verb", "adj": "adj.", "adv": "adv.",
    "pron": "pron.", "prep": "prep.", "conj": "konj.", "interj": "interj.",
    "num": "räkn.", "det": "best.art.", "art": "art.", "part": "part.",
    "prefix": "prefix", "suffix": "suffix", "phrase": "fras",
    "name": "namn", "propn": "namn", "abbrev": "förk.",
    "Substantiv": "subst.", "Adjektiv": "adj.", "Verb": "verb",
    "Adverb": "adv.", "Pronomen": "pron.", "Preposition": "prep.",
    "Konjunktion": "konj.", "Interjektion": "interj.", "Räkneord": "räkn.",
    "Egennamn": "namn", "Prefix": "prefix", "Suffix": "suffix",
    "Fras": "fras", "Förled": "förled", "Efterled": "efterled",
    "Artikel": "art.", "Partikel": "part.", "Förkortning": "förk.",
}

WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
TEMPLATE = re.compile(r"\{\{[^{}]*\}\}")
COMMENT = re.compile(r"<!--.*?-->", re.S)
QUOTE = re.compile(r"'''.*?'''|''.*?''")
DEF_LINE = re.compile(r"^#(?![#:*])\s*(.+)$")
TR_LINE = re.compile(r"^\*engelska:\s*(.+)$")
POS_LINE = re.compile(r"^===\s*([^=]+?)\s*===\s*$")
LANG_LINE = re.compile(r"^==\s*([^=]+?)\s*==\s*$")


def clean_wiki(text: str) -> str:
    text = COMMENT.sub("", text)
    text = re.sub(r"<ref[^>]*>.*?</ref>", "", text, flags=re.S)
    text = re.sub(r"<[^>]+>", "", text)
    for _ in range(6):  # strip innermost templates (may nest)
        new = TEMPLATE.sub("", text)
        if new == text:
            break
        text = new
    text = WIKILINK.sub(lambda m: m.group(2) or m.group(1), text)
    text = text.replace("'''", "").replace("''", "")
    text = text.replace("&nbsp;", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip(" ,;\u00a0")


def shorten(text: str, words: int = 16) -> str:
    """Trim a gloss to roughly `words` words at a natural boundary."""
    text = text.strip()
    if not text:
        return ""
    parts = text.split()
    if len(parts) <= words:
        return text
    cut = " ".join(parts[:words]).rstrip(",;:-")
    # extend to the end of a sentence if one ends within the next few words
    tail = " ".join(parts[words:words + 6])
    m = re.search(r"[.!?](?=\s|$)", tail)
    if m:
        cut += tail[: m.end()]
    else:
        cut += "…"
    return cut


def parse_sv_wikitext(wikitext: str) -> dict:
    """Extract Swedish definitions and English translations from sv.wiktionary."""
    # keep only the Swedish language section
    lines = wikitext.splitlines()
    start = end = None
    for i, line in enumerate(lines):
        m = LANG_LINE.match(line)
        if m:
            if start is None and m.group(1).lower() == "svenska":
                start = i
            elif start is not None:
                end = i
                break
    if start is None:
        return {"defs": [], "translations": []}
    lines = lines[start:end or len(lines)]

    defs: list[dict] = []
    translations: list[str] = []
    trans_pos: dict[str, list[str]] = {}
    pos = None
    for line in lines:
        m = POS_LINE.match(line)
        if m:
            pos = m.group(1)
            continue
        if line.startswith("===="):  # ====Översättningar==== etc.
            continue
        d = DEF_LINE.match(line)
        if d:
            txt = clean_wiki(d.group(1))
            if txt and len(txt) > 1:
                defs.append({"pos": pos, "def": txt})
            continue
        t = TR_LINE.match(line)
        if t:
            bucket = trans_pos.setdefault(pos or "", [])
            for part in re.findall(r"\{\{ö[^|]*\|en\|([^|}]+)", t.group(1)):
                w = clean_wiki(part)
                if not w:
                    continue
                if w not in translations:
                    translations.append(w)
                if w not in bucket:
                    bucket.append(w)
    return {"defs": defs, "translations": translations, "trans_pos": trans_pos}


class Dictionary:
    def __init__(self, db_path: Path = DB_PATH):
        if not db_path.exists():
            raise RuntimeError(
                f"dictionary not built: {db_path} is missing\n"
                f"run: python3 {HERE / 'build_data.py'}"
            )
        self.db_path = db_path
        self._local = threading.local()
        self._words: dict[str, list[str]] = {}
        self._ranks: dict[str, dict[str, int]] = {}
        self._alpha: dict[str, list[str]] = {}
        self._by_letter: dict[str, dict[str, list[str]]] = {}
        self._lock = threading.Lock()
        self._online = True

    # ---------- storage ----------
    @property
    def db(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=5)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def _load_words(self, lang: str) -> None:
        if lang in self._words:
            return
        with self._lock:
            if lang in self._words:
                return
            path = DATA / f"{lang}.txt"
            words = [w.strip() for w in path.read_text().splitlines() if w.strip()]
            ranks = {w: i for i, w in enumerate(words)}
            alpha = sorted(words)
            by_letter: dict[str, list[str]] = {}
            for w in alpha:
                by_letter.setdefault(w[0], []).append(w)
            self._ranks[lang], self._alpha[lang] = ranks, alpha
            self._by_letter[lang] = by_letter
            self._words[lang] = words

    def knows(self, word: str, lang: str) -> bool:
        self._load_words(lang)
        return word.lower() in self._ranks[lang]

    # ---------- suggestions ----------
    def suggest(self, query: str, lang: str, limit: int = 8) -> list[str]:
        self._load_words(lang)
        q = query.strip().lower()
        if not q:
            return self._words[lang][:limit]
        ranks, alpha = self._ranks[lang], self._alpha[lang]
        scored: dict[str, tuple] = {}

        def score(w: str, cls: int, ratio: float) -> None:
            if w in scored and scored[w][0] <= cls:
                return
            scored[w] = (cls, -ratio, ranks.get(w, 10 ** 9), len(w), w)

        if q in ranks:
            score(q, 0, 1.0)
        # prefix matches
        i = bisect_left(alpha, q)
        n = 0
        while i < len(alpha) and alpha[i].startswith(q) and n < limit * 25:
            w = alpha[i]
            score(w, 1, len(q) / len(w))
            i += 1
            n += 1
        # typo tolerance (edit-distance style)
        if len(scored) < limit and len(q) >= 3:
            pool = [w for w in self._by_letter[lang].get(q[0], alpha)
                    if abs(len(w) - len(q)) <= 2]
            for w in difflib.get_close_matches(q, pool, n=limit * 3, cutoff=0.6):
                score(w, 2, difflib.SequenceMatcher(None, q, w).ratio())
        # substring matches (compound words, e.g. "hals" → "flaskhals")
        if len(scored) < limit and len(q) >= 3:
            for w in alpha:
                if q in w:
                    score(w, 3, len(q) / len(w))
                    if len(scored) >= limit * 5:
                        break
        return [w for w in sorted(scored, key=lambda w: scored[w])[:limit]]

    def guess_lang(self, word: str) -> tuple[str, bool]:
        """Return (language, ambiguous); language is "" when the word is unknown."""
        w = word.strip().lower()
        sv, en = self.knows(w, "sv"), self.knows(w, "en")
        if sv and en:
            return ("sv" if self._ranks["sv"][w] <= self._ranks["en"][w] else "en"), True
        if not sv and not en:
            return "", False
        return ("sv" if sv else "en"), False

    # ---------- offline lookups ----------
    def translate(self, word: str, lang: str) -> dict:
        """Look `word` up in `lang` and translate to the other language."""
        w = word.strip().lower()
        if not w:
            return {"query": word, "direction": f"{lang}-other", "translations": [], "entries": []}
        if lang == "en":
            rows = self.db.execute(
                "SELECT e.id, e.word, e.pos, e.def, GROUP_CONCAT(t.t, '|') AS tr "
                "FROM entries e JOIN trans t ON t.entry_id=e.id "
                "WHERE e.word=? GROUP BY e.id ORDER BY e.id",
                (w,),
            ).fetchall()
            entries, translations = [], []
            seen = set()
            for r in rows:
                tr = [t for t in (r["tr"] or "").split("|") if t]
                pos = POS_SV.get(r["pos"], r["pos"] or "")
                signature = (pos, tuple(tr))
                if signature in seen:
                    continue
                seen.add(signature)
                entries.append({
                    "word": w, "pos": pos,
                    "def": shorten(r["def"], 22), "trans": tr,
                })
                for t in tr:
                    if t not in translations:
                        translations.append(t)
            # prefer a general (non-parenthetical) definition for the head sense
            for e in entries:
                if e["def"] and not e["def"].startswith("("):
                    entries[0]["def"] = e["def"]
                    break
            return {
                "query": word, "direction": "en-sv",
                "translations": translations[:8], "entries": entries[:6],
                "sv_word": translations[0] if translations else "",
            }

        # sv -> en via the reversed eng-swe index
        rows = self.db.execute(
            "SELECT e.word, e.pos, e.def, t.t, e.id FROM trans t JOIN entries e ON e.id=t.entry_id "
            "WHERE t.t=? ORDER BY e.id, t.rowid",
            (w,),
        ).fetchall()
        self._load_words("en")
        ranks = self._ranks["en"]
        best: dict[str, dict] = {}
        for r in rows:
            en = r["word"]
            cur = best.get(en)
            def_ = shorten(r["def"], 22)
            pos = POS_SV.get(r["pos"], r["pos"] or "")
            if cur is None:
                best[en] = {
                    "word": en, "pos": pos,
                    "def": "" if def_.startswith("(") else def_,
                    "defs": {}, "trans": [], "hits": 1, "primary": 0,
                    "first_sense": r["id"],
                }
                cur = best[en]
            else:
                cur["hits"] += 1
                if not cur["def"] and def_ and not def_.startswith("("):
                    cur["def"] = def_
            if pos and def_ and pos not in cur["defs"]:
                cur["defs"][pos] = def_
        # how central this Swedish word is to each English headword:
        # hits / total senses, blended with English frequency
        totals = {}
        if best:
            marks = ",".join("?" * len(best))
            for row in self.db.execute(
                f"SELECT word, COUNT(*) FROM entries WHERE word IN ({marks}) GROUP BY word",
                tuple(best),
            ):
                totals[row[0]] = row[1]
        for en, e in best.items():
            centrality = e["hits"] / max(1, totals.get(en, e["hits"]))
            e["primary"] = ranks.get(en, 10 ** 9) / (1 + 3 * centrality)
        ordered = sorted(
            best.values(),
            key=lambda e: (e["primary"], e["word"]),
        )[:6]
        for e in ordered:
            e.pop("hits", None)
            e.pop("primary", None)
            e.pop("first_sense", None)
        return {
            "query": word, "direction": "sv-en",
            "translations": [e["word"] for e in ordered[:8]],
            "entries": ordered, "sv_word": w,
        }

    # ---------- online glosses (sv.wiktionary) ----------
    def wiktionary(self, word: str, force: bool = False) -> dict:
        """Swedish definition(s) + English translations, cached in sqlite."""
        w = word.strip().lower()
        if not w:
            return {"defs": [], "translations": []}
        if not force:
            row = self.db.execute(
                "SELECT def, extra, fetched FROM wiki WHERE word=?", (w,)
            ).fetchone()
            if row and time.time() - row["fetched"] < WIKT_TTL:
                try:
                    return json.loads(row["extra"])
                except Exception:
                    pass
        if not self._online:
            return {"defs": [], "translations": []}
        try:
            url = (WIKT_API + "?" + urllib.parse.urlencode({
                "action": "parse", "page": w, "prop": "wikitext",
                "redirects": "1", "format": "json", "formatversion": "2",
            }))
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=4) as r:
                payload = json.load(r)
            wikitext = payload.get("parse", {}).get("wikitext", "")
            data = parse_sv_wikitext(wikitext)
            preview = data["defs"][0]["def"] if data["defs"] else ""
            self.db.execute(
                "INSERT INTO wiki(word, def, extra, fetched) VALUES(?,?,?,?) "
                "ON CONFLICT(word) DO UPDATE SET def=excluded.def, "
                "extra=excluded.extra, fetched=excluded.fetched",
                (w, preview, json.dumps(data, ensure_ascii=False), int(time.time())),
            )
            self.db.commit()
            return data
        except Exception:
            return {"defs": [], "translations": []}
