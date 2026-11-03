#!/usr/bin/env python3
"""glosa — floating Swedish ↔ English lookup popup.

Bind it to a key and it opens a layer-shell popup: type (with fuzzy
suggestions), pick a word, get the translation plus a short explanation.
Enter copies the primary translation.

    glosa                # open the popup (or focus the running one)
    glosa hus            # CLI lookup, prints to stdout
    glosa -r nyckelpiga  # CLI lookup + Swedish explanation (fetches wiktionary)
    glosa --version

https://github.com/mikul-/glosa
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402
from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402

from lookup import Dictionary, shorten  # noqa: E402

VERSION = "1.1.0"
IDLE_EXIT_SECONDS = 600
DIRS = {"sv": ("SV", "EN"), "en": ("EN", "SV")}
POS_ABBR = {}  # wiki POS name → abbreviation, filled after POS_FULL
POS_FULL = {
    "subst.": "Substantiv", "verb": "Verb", "adj.": "Adjektiv",
    "adv.": "Adverb", "pron.": "Pronomen", "prep.": "Preposition",
    "konj.": "Konjunktion", "interj.": "Interjektion", "räkn.": "Räkneord",
    "namn": "Egennamn", "fras": "Fras", "prefix": "Prefix", "suffix": "Suffix",
}
POS_ABBR.update({full: abbr for abbr, full in POS_FULL.items()})

HINT = ("<b>↵</b> kopiera &amp; stäng   <b>ctrl+↵</b> kopiera   "
        "<b>ctrl+←→</b> välj ord   <b>⇥</b> byt riktning   <b>esc</b> stäng")

DEFAULTS = {
    "surface": "#12161c",
    "surface_container": "#1c232b",
    "surface_container_high": "#273039",
    "on_surface": "#e6ecf3",
    "on_surface_variant": "#a9b6c4",
    "primary": "#7fb4ff",
    "secondary": "#8fd6c9",
    "tertiary": "#c7a8ff",
    "outline": "#4a5764",
    "error": "#ff8a8a",
}

CSS = """
window.background { background: transparent; }

#card {
  background-color: alpha(@surface, 0.97);
  border: 1px solid alpha(@outline, 0.55);
  border-radius: 18px;
  box-shadow: 0 10px 34px 2px rgba(0, 0, 0, 0.55);
}

#header { padding: 14px 18px 10px 18px; }

entry#search {
  background: transparent;
  border: none;
  box-shadow: none;
  font-size: 20px;
  padding: 0;
  color: @on_surface;
  caret-color: @primary;
}
entry#search:focus { outline: none; }

button#dir {
  background-color: alpha(@primary, 0.14);
  color: @primary;
  border: none;
  border-radius: 999px;
  padding: 4px 12px;
  font-size: 11px;
  font-weight: bold;
  letter-spacing: 1px;
  min-height: 0;
}
button#dir:hover { background-color: alpha(@primary, 0.24); }

#detail { padding: 6px 18px 14px 18px; }
#word { font-size: 15px; color: @on_surface_variant; }
#pos { font-size: 12px; color: alpha(@on_surface_variant, 0.75); }
#trans {
  font-size: 27px;
  font-weight: 400;
  color: @primary;
  margin-top: 2px;
}
#svdef { font-size: 14px; color: @on_surface; margin-top: 6px; }
#endef { font-size: 12px; color: alpha(@on_surface_variant, 0.85); margin-top: 4px; }

#lister { border-top: 1px solid alpha(@outline, 0.35); }
list#suggestions { background: transparent; }
list#suggestions > row {
  padding: 5px 18px;
  border-radius: 8px;
  background: transparent;
}
list#suggestions > row:hover { background-color: alpha(@on_surface, 0.05); }
list#suggestions > row:selected { background-color: alpha(@primary, 0.16); }
#sugword { font-size: 14px; color: @on_surface; }
#sugtrans { font-size: 13px; color: alpha(@on_surface_variant, 0.9); }

#footer {
  font-size: 11px;
  color: alpha(@on_surface_variant, 0.7);
  padding: 8px 18px;
  border-top: 1px solid alpha(@outline, 0.35);
}
"""


def load_colors() -> dict:
    for path in ("~/.config/matugen/colors.json", "~/.cache/matugen/colors.json"):
        try:
            data = json.loads(Path(path).expanduser().read_text())
            if "surface" in data:
                return {**DEFAULTS, **{k: v for k, v in data.items()
                                       if isinstance(v, str) and v.startswith("#")}}
        except Exception:
            pass
    return DEFAULTS


class Glosa(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id="se.mikul.glosa")
        self.dic = Dictionary()
        self.win: Gtk.Window | None = None
        self.entry: Gtk.Entry | None = None
        self.listbox: Gtk.ListBox | None = None
        self.labels: dict[str, Gtk.Label] = {}
        self.dir_btn: Gtk.Button | None = None
        self.footer: Gtk.Label | None = None
        self.override: str | None = None
        self.last_lang = "sv"
        self.gen = 0
        self.cache: dict[tuple[str, str], dict] = {}
        self.gloss: dict[str, dict] = {}
        self.recent: list[str] = []
        self.selected_word: str | None = None
        self.selected_lang: str | None = None
        self._idle_source = 0
        self._trans_local: list[str] = []
        self._pos_local = ""
        self._last_res: dict | None = None
        self._last_lang = ""
        self._merged: list[str] = []
        self._pick_idx = 0
        self.pick_word: str | None = None
        self._picked = ""

    # ---------------- window ----------------
    def do_activate(self) -> None:
        if self.win is None:
            self._build()
        self._cancel_exit()
        self.override = None
        self.selected_word = None
        self.pick_word = None
        self.entry.set_text("")
        self.win.present()
        self.entry.grab_focus()
        self._refresh()

    def _build(self) -> None:
        provider = Gtk.CssProvider()
        colors = "\n".join(f"@define-color {k} {v};" for k, v in load_colors().items())
        provider.load_from_string(colors + CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        win = Gtk.Window(application=self)
        win.set_decorated(False)
        win.set_resizable(False)
        win.set_default_size(780, 520)
        win.add_css_class("background")

        LayerShell.init_for_window(win)
        LayerShell.set_namespace(win, "glosa")
        LayerShell.set_layer(win, LayerShell.Layer.OVERLAY)
        LayerShell.set_anchor(win, LayerShell.Edge.TOP, True)
        LayerShell.set_margin(win, LayerShell.Edge.TOP, 110)
        LayerShell.set_keyboard_mode(win, LayerShell.KeyboardMode.EXCLUSIVE)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.set_margin_top(14)
        root.set_margin_bottom(14)
        root.set_margin_start(14)
        root.set_margin_end(14)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.set_name("card")
        card.set_vexpand(True)
        root.append(card)
        win.set_child(root)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header.set_name("header")
        self.entry = Gtk.Entry()
        self.entry.set_name("search")
        self.entry.set_placeholder_text("Skriv ett ord …")
        self.entry.set_hexpand(True)
        self.entry.connect("changed", self._on_changed)
        self.dir_btn = Gtk.Button(label="SV → EN")
        self.dir_btn.set_name("dir")
        self.dir_btn.set_can_focus(False)
        self.dir_btn.connect("clicked", lambda *_: self._toggle_direction())
        header.append(self.entry)
        header.append(self.dir_btn)
        card.append(header)

        detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        detail.set_name("detail")
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.labels["word"] = self._label("word")
        self.labels["pos"] = self._label("pos")
        top.append(self.labels["word"])
        top.append(self.labels["pos"])
        detail.append(top)
        for key in ("trans", "svdef", "endef"):
            self.labels[key] = self._label(key)
            detail.append(self.labels[key])
        card.append(detail)

        lister = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        lister.set_name("lister")
        lister.set_vexpand(True)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_margin_top(6)
        scroll.set_margin_bottom(6)
        self.listbox = Gtk.ListBox()
        self.listbox.set_name("suggestions")
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.set_activate_on_single_click(True)
        self.listbox.connect("row-selected", self._on_row_selected)
        self.listbox.connect("row-activated", lambda *_: self._copy_and_close())
        scroll.set_child(self.listbox)
        lister.append(scroll)
        card.append(lister)

        self.footer = Gtk.Label()
        self.footer.set_name("footer")
        self.footer.set_markup(HINT)
        self.footer.set_xalign(0)
        card.append(self.footer)

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        win.add_controller(keys)

        focus = Gtk.EventControllerFocus()
        focus.connect("leave", self._on_focus_leave)
        win.add_controller(focus)

        win.connect("close-request", self._on_close_request)
        self.win = win

    @staticmethod
    def _label(name: str) -> Gtk.Label:
        lab = Gtk.Label(xalign=0)
        lab.set_name(name)
        lab.set_wrap(True)
        lab.set_wrap_mode(2)
        return lab

    # ---------------- events ----------------
    def _on_changed(self, *_args) -> None:
        self._refresh()

    def _on_key(self, _c, keyval, _code, state) -> bool:
        if keyval == Gdk.KEY_Escape:
            self._close()
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self._copy_and_close(
                keep_open=bool(state & Gdk.ModifierType.CONTROL_MASK))
            return True
        if (state & Gdk.ModifierType.CONTROL_MASK
                and keyval in (Gdk.KEY_Left, Gdk.KEY_Right)):
            return self._pick(-1 if keyval == Gdk.KEY_Left else 1)
        if keyval in (Gdk.KEY_Up, Gdk.KEY_Down):
            rows = list(self._rows())
            if not rows:
                return False
            sel = self.listbox.get_selected_row()
            cur = rows.index(sel) if sel in rows else 0
            step = -1 if keyval == Gdk.KEY_Up else 1
            self.listbox.select_row(rows[max(0, min(len(rows) - 1, cur + step))])
            return True
        if keyval == Gdk.KEY_Tab:
            self._toggle_direction()
            return True
        return False

    def _on_focus_leave(self, *_args) -> None:
        GLib.timeout_add(120, self._hide_if_unfocused)

    def _hide_if_unfocused(self) -> bool:
        if self.win and self.win.get_visible() and not self.win.is_active():
            self.win.set_visible(False)
            self._schedule_exit()
        return False

    def _on_close_request(self, *_args) -> bool:
        self._close()
        return True

    def _close(self) -> None:
        if self.win:
            self.win.set_visible(False)
        self._schedule_exit()

    # ---------------- state ----------------
    def _effective_lang(self, text: str) -> str:
        if self.override:
            return self.override
        lang, _ = self.dic.guess_lang(text)
        if lang:
            self.last_lang = lang
            return lang
        return self.last_lang

    def _toggle_direction(self) -> None:
        text = self.entry.get_text().strip()
        cur = self._effective_lang(text) if text else (self.override or "sv")
        self.override = "en" if cur == "sv" else "sv"
        self.gen += 1
        self.selected_word = None
        self.pick_word = None
        self._refresh()

    def _set_dir_badge(self, lang: str, ambiguous: bool) -> None:
        a, b = DIRS[lang]
        label = f"{a} → {b}" + (" ?" if ambiguous and not self.override else "")
        if self.dir_btn.get_label() != label:
            self.dir_btn.set_label(label)

    def _refresh(self) -> None:
        if not self.entry:
            return
        text = self.entry.get_text().strip()
        self.gen += 1
        gen = self.gen

        if not text:
            self.override = None
            self._set_dir_badge("sv", False)
            self._show_recent()
            return

        lang = self._effective_lang(text)
        _, ambiguous = self.dic.guess_lang(text)
        self._set_dir_badge(lang, ambiguous)

        words = self.dic.suggest(text, lang, limit=8)
        self._clear_rows()
        if not words:
            self.selected_word = None
            self._render_empty(text)
            return
        for word in words[:7]:
            self._add_row(word, self._preview(word, lang), lang, gen)
        self.listbox.select_row(self.listbox.get_row_at_index(0))

    def _show_recent(self) -> None:
        self._clear_rows()
        self.selected_word = None
        for key in ("word", "pos", "trans", "endef"):
            self.labels[key].set_text("")
        if not self.recent:
            self.labels["svdef"].set_text("Skriv ett ord för att översätta.")
            return
        self.labels["svdef"].set_text("Senast:")
        for word in self.recent[:8]:
            lang = self._effective_lang(word)
            self._add_row(word, self._preview(word, lang), lang, self.gen)

    def _render_empty(self, text: str) -> None:
        self.labels["word"].set_text("")
        self.labels["pos"].set_text("")
        self.labels["trans"].set_text("–")
        self.labels["svdef"].set_text(f"Inget ord hittades för “{text}”.")
        self.labels["endef"].set_text("Testa ⇥ för att byta riktning.")

    def _rows(self):
        i = 0
        while (row := self.listbox.get_row_at_index(i)) is not None:
            yield row
            i += 1

    def _clear_rows(self) -> None:
        while (row := self.listbox.get_row_at_index(0)) is not None:
            self.listbox.remove(row)

    def _add_row(self, word: str, trans: str, lang: str, gen: int) -> None:
        row = Gtk.ListBoxRow()
        row.glosa_word = word
        row.glosa_lang = lang
        row.glosa_gen = gen
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        w = Gtk.Label(label=word, xalign=0)
        w.set_name("sugword")
        w.set_hexpand(True)
        t = Gtk.Label(label=trans, xalign=1)
        t.set_name("sugtrans")
        row.glosa_trans_label = t
        box.append(w)
        box.append(t)
        row.set_child(box)
        self.listbox.append(row)

    def _preview(self, word: str, lang: str) -> str:
        res = self.cache.get((word, lang))
        if res is None:
            res = self.dic.translate(word, lang)
            self.cache[(word, lang)] = res
        return ", ".join(res["translations"][:2])

    def _on_row_selected(self, _lb, row) -> None:
        if row is None or not hasattr(row, "glosa_word"):
            return
        if row.glosa_gen != self.gen:
            return
        if row.glosa_word != self.selected_word:
            self.selected_word = row.glosa_word
            self.selected_lang = row.glosa_lang
            self.pick_word = None
            self._start_lookup(row.glosa_word, row.glosa_lang, self.gen)

    # ---------------- lookup ----------------
    def _start_lookup(self, word: str, lang: str, gen: int) -> None:
        key = (word, lang)
        self.labels["word"].set_text(word)
        cached = self.cache.get(key)
        if cached is not None:
            self._adopt(cached, lang)
            if (cached.get("sv_word") or word) not in self.gloss:
                self._spawn_gloss(gen, word, lang, cached)
            return
        self.labels["trans"].set_text("…")
        self.labels["svdef"].set_text("")
        self.labels["endef"].set_text("")
        self._merged = []
        self._pick_idx = 0
        self._picked = ""
        threading.Thread(target=self._work, args=(gen, word, lang),
                         daemon=True).start()

    def _adopt(self, res: dict, lang: str) -> None:
        """Make `res` the current entry and paint it."""
        self._last_res = res
        self._last_lang = lang
        self._trans_local = res.get("translations") or []
        self._paint_entry()

    def _work(self, gen: int, word: str, lang: str) -> None:
        try:
            res = self.dic.translate(word, lang)
        except Exception:
            res = {"translations": [], "entries": [], "sv_word": ""}
        self.cache[(word, lang)] = res
        GLib.idle_add(self._render, gen, word, lang, res)
        self._spawn_gloss(gen, word, lang, res)

    def _spawn_gloss(self, gen: int, word: str, lang: str, res: dict) -> None:
        sv_word = res.get("sv_word") or (word if lang == "sv" else "")
        if not sv_word or sv_word in self.gloss:
            return
        threading.Thread(target=self._gloss_work, args=(gen, word, sv_word),
                         daemon=True).start()

    def _gloss_work(self, gen: int, word: str, sv_word: str) -> None:
        try:
            data = self.dic.wiktionary(sv_word)
        except Exception:
            data = {"defs": [], "translations": []}
        self.gloss[sv_word] = data
        GLib.idle_add(self._apply_gloss_for, gen, word, data)

    def _apply_gloss_for(self, gen: int, word: str, data: dict) -> bool:
        if gen == self.gen and self.selected_word == word and self._last_res:
            self._paint_entry()
        return False

    def _paint_entry(self) -> None:
        res = self._last_res or {}
        lang = self._last_lang or ""
        trans = res.get("translations") or []
        entries = res.get("entries") or []
        self.labels["word"].set_text(
            res.get("sv_word") if lang == "en" else res.get("query", ""))
        if not trans:
            self.labels["trans"].set_text("–")
            self.labels["pos"].set_text("")
            self.labels["endef"].set_text("")
            self.labels["svdef"].set_text("Söker i wiktionary …")
            self._merged = []
            self._pick_idx = 0
            self.pick_word = None
            self._picked = ""
            return

        sv_word = res.get("sv_word") or ""
        gloss = self.gloss.get(sv_word)
        pos_local = entries[0]["pos"] if entries else ""
        self._pos_local = pos_local

        # merge wiktionary's preferred translations in front
        merged = list(trans)
        wikt_def_pos = ""
        if gloss and gloss.get("defs"):
            by_pos = gloss.get("trans_pos") or {}
            wikt = by_pos.get(POS_FULL.get(pos_local, ""), [])
            if not wikt:
                wikt = gloss.get("translations") or []
            if lang == "sv" and wikt:
                merged = []
                for t in list(wikt[:2]) + list(trans):
                    if t not in merged:
                        merged.append(t)
            wikt_def_pos = POS_ABBR.get(gloss["defs"][0].get("pos", ""), "")

        primary = merged[0] if merged else ""
        self._merged = merged
        # keep the marked translation across updates (e.g. a late gloss),
        # falling back to the primary one
        pick = merged.index(self.pick_word) if self.pick_word in merged else 0
        self.pick_word = merged[pick]
        self._pick_idx = pick
        self._picked = merged[pick]
        entry = next((e for e in entries if e.get("word") == primary),
                     entries[0] if entries else {})
        defs = entry.get("defs") or {}
        endef = defs.get(wikt_def_pos) or entry.get("def") or ""
        pos = wikt_def_pos if (wikt_def_pos and wikt_def_pos in defs) else entry.get("pos", "")

        self.labels["trans"].set_markup(self._trans_markup(merged))
        self.labels["pos"].set_text(f"· {pos}" if pos else "")
        endef = re.sub(r"^\([^)]*\)\s*", "", endef).strip()
        self.labels["endef"].set_text(shorten(endef, 16) if endef else "")
        gloss_text = ""
        for d in (gloss.get("defs") or []) if gloss else []:
            if d.get("def"):
                gloss_text = shorten(d["def"], 15)
                break
        self.labels["svdef"].set_text(gloss_text)

        row = self.listbox.get_selected_row() if self.listbox else None
        if row is not None and getattr(row, "glosa_word", "") == self.selected_word:
            label = getattr(row, "glosa_trans_label", None)
            if label is not None:
                label.set_text(", ".join(merged[:2]))

    def _render(self, gen: int, word: str, lang: str, res: dict) -> bool:
        if gen != self.gen or self.selected_word != word:
            return False
        self._adopt(res, lang)
        return False

    def _trans_markup(self, words: list[str]) -> str:
        """Render translations, with the marked one heavier/brighter."""
        parts = []
        for i, word in enumerate(words):
            esc = GLib.markup_escape_text(word)
            if i == self._pick_idx:
                parts.append(f'<span weight="800">{esc}</span>')
            else:
                parts.append(f'<span weight="400" alpha="60%">{esc}</span>')
        return ", ".join(parts)

    def _pick(self, step: int) -> bool:
        """Mark the previous/next translation (Ctrl+←/→)."""
        if len(self._merged) < 2:
            return False
        idx = (self._pick_idx + step) % len(self._merged)
        self.pick_word = self._merged[idx]
        self._paint_entry()
        return True

    # ---------------- actions ----------------
    def _copy_and_close(self, keep_open: bool = False) -> None:
        row = self.listbox.get_selected_row() if self.listbox else None
        word = getattr(row, "glosa_word", "") if row else ""
        lang = getattr(row, "glosa_lang", "") if row else ""
        if not word:
            word = self.entry.get_text().strip()
        if not word:
            return
        lang = lang or self.override or self._effective_lang(word)
        # one word on the clipboard: the marked translation only
        if word == self.selected_word and self._picked:
            text = self._picked
        else:
            res = self.cache.get((word, lang)) or self.dic.translate(word, lang)
            candidates = res.get("translations") or []
            text = candidates[0] if candidates else ""
        if not text:
            self._flash("Ingen översättning hittad")
            return
        Gdk.Display.get_default().get_clipboard().set(text)
        self.recent = [word] + [w for w in self.recent if w != word]
        del self.recent[12:]
        self._notify(f"{word} → {text}")
        if keep_open:
            self._flash(f"Kopierat: {text}")
        else:
            self._close()

    def _flash(self, message: str) -> None:
        self.footer.set_markup(message)
        GLib.timeout_add(1500, self._reset_footer)

    def _reset_footer(self) -> bool:
        self.footer.set_markup(HINT)
        return False

    def _notify(self, message: str) -> None:
        if os.environ.get("GLOSA_NOTIFY", "1") == "0":
            return
        try:
            subprocess.Popen(
                ["notify-send", "-a", "glosa", "-u", "low", "-t", "2500",
                 "glosa", message],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    # ---------------- lifecycle ----------------
    def _schedule_exit(self) -> None:
        self._cancel_exit()
        self._idle_source = GLib.timeout_add_seconds(IDLE_EXIT_SECONDS, self._quit)

    def _cancel_exit(self) -> None:
        if self._idle_source:
            GLib.source_remove(self._idle_source)
            self._idle_source = 0

    def _quit(self) -> bool:
        if self.win and self.win.get_visible():
            return False
        self.quit()
        return False


# ---------------- CLI ----------------
def cli(args: list[str]) -> int:
    if any(a in ("-v", "--version") for a in args):
        print(f"glosa {VERSION}")
        return 0
    dic = Dictionary()
    explain = False
    words = []
    for a in args:
        if a in ("-r", "--explain"):
            explain = True
        else:
            words.append(a)
    if not words:
        print(__doc__.strip())
        return 1
    text = " ".join(words)
    lang, ambiguous = dic.guess_lang(text)
    lang = lang or "sv"
    res = dic.translate(text, lang)
    target = "EN" if lang == "sv" else "SV"
    print(f"{text}  ({lang.upper()}→{target}{', ambiguous' if ambiguous else ''})")
    for e in res["entries"]:
        tr = ", ".join(e["trans"]) if e["trans"] else e["word"]
        print(f"  {e['pos']:<8} {tr}")
        if e["def"]:
            print(f"           {e['def']}")
    if not res["entries"]:
        print("  (ingen träff i den lokala ordboken)")
    if explain:
        data = dic.wiktionary(res.get("sv_word") or text)
        for d in data["defs"][:3]:
            print(f"  sv: {d['def']}")
        if data["translations"] and not res["entries"]:
            print("  via wiktionary:", ", ".join(data["translations"][:6]))
    return 0


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--gapplication")]
    prefill = ""
    if "--prefill" in args:
        i = args.index("--prefill")
        prefill = args[i + 1] if i + 1 < len(args) else ""
        del args[i:i + 2]
    if args:
        return cli(args)
    try:
        app = Glosa()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    if prefill:
        app.connect_after("activate", _apply_prefill, prefill)
    return app.run([sys.argv[0]])


def _apply_prefill(_app, text: str) -> None:
    _app.entry.set_text(text)
    _app.entry.set_position(-1)


if __name__ == "__main__":
    sys.exit(main())
