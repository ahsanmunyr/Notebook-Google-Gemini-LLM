#!/usr/bin/env python3
"""AI Book Generator — write a whole book from ONE input file, with the Gemini API.

The simple way: put everything in a single file, ``book-source.md`` — your style,
your characters/world, and a short brief for every chapter — then run:

    python generate.py

The tool generates each chapter in order, keeps the story consistent by threading
a running "story so far" (summaries of previous chapters), and merges everything
into ``output/book.md``.

Single-file format (``book-source.md``)::

    Title: My Great Book

    (Everything here, before the first "# Chapter", is shared context sent with
     EVERY chapter: your style/tone/POV/length rules, characters, world, outline.)

    # Chapter 1: The Arrival
    A short brief describing what happens in chapter 1.

    # Chapter 2: The Threshold
    A short brief for chapter 2.

    ... and so on for as many chapters as you want.

Anything inside <!-- HTML comments --> is ignored, so you can leave notes to
yourself in the file.

Usage:
    python generate.py                 # generate all chapters, then merge book.md
    python generate.py --dry-run       # show the plan; no API calls
    python generate.py --only 5        # (re)generate just chapter 5
    python generate.py --range 10 20   # generate chapters 10..20 inclusive
    python generate.py --force         # regenerate even if output already exists
    python generate.py --merge-only    # rebuild book.md from existing chapter outputs
    python generate.py --input FILE    # use a different single-file source
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

try:
    from google import genai
    from google.genai import types as genai_types
except ImportError:  # pragma: no cover - friendlier message than a raw traceback
    print(
        "Missing dependency 'google-genai'. Install requirements first:\n"
        "    pip install -r requirements.txt",
        file=sys.stderr,
    )
    raise


# --------------------------------------------------------------------------- #
# Paths & config
# --------------------------------------------------------------------------- #

ROOT = Path(__file__).resolve().parent

DEFAULT_CONFIG = {
    "model": "gemini-2.5-pro",
    "summary_model": "gemini-2.5-flash",  # cheaper model for summaries
    "temperature": 0.9,
    "summary_temperature": 0.3,
    "max_output_tokens": 8192,
    "summary_max_tokens": 512,
    "max_retries": 5,
    "retry_base_delay": 2.0,  # seconds; exponential backoff
    "request_delay": 0.0,  # seconds to sleep between chapters (rate limiting)
    "paths": {
        "book_source": "book-source.md",  # the single input file (primary mode)
        "chapters": "chapters",  # directory mode (fallback)
        "style_guide": "style_guide.txt",  # optional, appended to context if present
        "output_dir": "output",
        "output_chapters": "output/chapters",
        "summaries": "output/summaries.json",
        "book": "output/book.md",
    },
    # How many previous chapter summaries to include in the prompt. 0 = all.
    "summary_window": 0,
    "book_title": "My Book",
}


@dataclass
class Config:
    model: str
    summary_model: str
    temperature: float
    summary_temperature: float
    max_output_tokens: int
    summary_max_tokens: int
    max_retries: int
    retry_base_delay: float
    request_delay: float
    summary_window: int
    book_title: str
    # resolved absolute paths
    book_source: Path
    chapters_dir: Path
    style_guide: Path
    output_dir: Path
    output_chapters: Path
    summaries: Path
    book: Path


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(config_path: Path) -> Config:
    data = dict(DEFAULT_CONFIG)
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as fh:
            user_cfg = yaml.safe_load(fh) or {}
        data = _deep_merge(data, user_cfg)

    p = data["paths"]
    return Config(
        model=data["model"],
        summary_model=data["summary_model"],
        temperature=float(data["temperature"]),
        summary_temperature=float(data["summary_temperature"]),
        max_output_tokens=int(data["max_output_tokens"]),
        summary_max_tokens=int(data["summary_max_tokens"]),
        max_retries=int(data["max_retries"]),
        retry_base_delay=float(data["retry_base_delay"]),
        request_delay=float(data["request_delay"]),
        summary_window=int(data["summary_window"] or 0),
        book_title=str(data["book_title"]),
        book_source=(ROOT / p["book_source"]).resolve(),
        chapters_dir=(ROOT / p["chapters"]).resolve(),
        style_guide=(ROOT / p["style_guide"]).resolve(),
        output_dir=(ROOT / p["output_dir"]).resolve(),
        output_chapters=(ROOT / p["output_chapters"]).resolve(),
        summaries=(ROOT / p["summaries"]).resolve(),
        book=(ROOT / p["book"]).resolve(),
    )


# --------------------------------------------------------------------------- #
# Chapter model
# --------------------------------------------------------------------------- #

@dataclass
class Chapter:
    number: int
    title: str = ""
    brief: str = ""  # the chapter's source text (single-file mode)
    source_path: Path | None = None  # directory mode

    @property
    def stem_number(self) -> str:
        return f"{self.number:03d}"

    @property
    def label(self) -> str:
        return self.source_path.name if self.source_path else f"source for ch {self.number}"

    def load_brief(self) -> str:
        if self.source_path is not None:
            return self.source_path.read_text(encoding="utf-8").strip()
        return self.brief.strip()


# --------------------------------------------------------------------------- #
# Single-file parsing (primary mode)
# --------------------------------------------------------------------------- #

_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_CHAPTER_HEADER = re.compile(
    r"^\s{0,3}#{1,6}\s*chapter\s+(\d+)\s*[:.\-)]?\s*(.*)$", re.IGNORECASE
)
_TITLE_LINE = re.compile(r"^\s*title\s*[:\-]\s*(.+)$", re.IGNORECASE)


@dataclass
class ParsedBook:
    title: str = ""
    context: str = ""  # shared preamble sent with every chapter
    chapters: list[Chapter] = field(default_factory=list)


def parse_book_file(path: Path) -> ParsedBook:
    """Parse the single input file into (title, shared context, chapters)."""
    raw = path.read_text(encoding="utf-8")
    raw = _HTML_COMMENT.sub("", raw)  # drop <!-- notes to self -->

    preamble_lines: list[str] = []
    chapters: list[Chapter] = []
    current: Chapter | None = None
    current_body: list[str] = []

    def flush() -> None:
        if current is not None:
            current.brief = "\n".join(current_body).strip()
            chapters.append(current)

    for line in raw.splitlines():
        header = _CHAPTER_HEADER.match(line)
        if header:
            flush()
            number = int(header.group(1))
            title = header.group(2).strip()
            current = Chapter(number=number, title=title)
            current_body = []
        elif current is None:
            preamble_lines.append(line)
        else:
            current_body.append(line)
    flush()

    preamble = "\n".join(preamble_lines).strip()

    # Pull an explicit "Title:" line out of the preamble if present.
    title = ""
    kept: list[str] = []
    for line in preamble.splitlines():
        m = _TITLE_LINE.match(line)
        if m and not title:
            title = m.group(1).strip()
            continue
        kept.append(line)
    context = "\n".join(kept).strip()

    # de-duplicate chapter numbers (first wins)
    seen: dict[int, Chapter] = {}
    for ch in chapters:
        if ch.number in seen:
            print(
                f"  ! duplicate '# Chapter {ch.number}' in {path.name}; keeping the first",
                file=sys.stderr,
            )
            continue
        seen[ch.number] = ch
    ordered = [seen[n] for n in sorted(seen)]

    return ParsedBook(title=title, context=context, chapters=ordered)


# --------------------------------------------------------------------------- #
# Directory discovery (fallback mode): natural sort, 1.txt vs 001.txt
# --------------------------------------------------------------------------- #

_LEADING_NUM = re.compile(r"^(\d+)")


def discover_chapters(chapters_dir: Path) -> list[Chapter]:
    if not chapters_dir.is_dir():
        return []
    found: dict[int, Chapter] = {}
    for p in chapters_dir.iterdir():
        if not p.is_file() or p.name.startswith("."):
            continue
        if p.suffix.lower() not in {".txt", ".md"}:
            continue
        m = _LEADING_NUM.match(p.name)
        if not m:
            print(f"  ! skipping (no leading number): {p.name}", file=sys.stderr)
            continue
        number = int(m.group(1))
        if number in found:
            print(
                f"  ! duplicate chapter number {number}: keeping "
                f"{found[number].source_path.name}",
                file=sys.stderr,
            )
            continue
        found[number] = Chapter(number=number, source_path=p)
    return [found[n] for n in sorted(found)]


# --------------------------------------------------------------------------- #
# Summaries store
# --------------------------------------------------------------------------- #

def load_summaries(path: Path) -> dict[int, str]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"  ! {path.name} is corrupt; starting summaries fresh", file=sys.stderr)
        return {}
    return {int(k): v for k, v in raw.items()}


def save_summaries(path: Path, summaries: dict[int, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = {str(k): summaries[k] for k in sorted(summaries)}
    path.write_text(json.dumps(ordered, indent=2, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Prompt building
# --------------------------------------------------------------------------- #

def read_text_if_exists(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def build_story_so_far(summaries: dict[int, str], up_to: int, window: int) -> str:
    previous = [n for n in sorted(summaries) if n < up_to]
    if window and window > 0:
        previous = previous[-window:]
    if not previous:
        return ""
    return "\n\n".join(f"Chapter {n}: {summaries[n].strip()}" for n in previous)


def build_chapter_prompt(
    chapter: Chapter, context: str, story_so_far: str
) -> str:
    brief = chapter.load_brief()
    heading = f"Chapter {chapter.number}"
    if chapter.title:
        heading += f": {chapter.title}"

    parts: list[str] = [
        "You are a professional author writing one chapter of a longer book. "
        "Write the chapter in full prose, ready for publication. Output ONLY the "
        "chapter text (no preamble, no notes, no commentary, no chapter heading)."
    ]
    if context:
        parts.append(
            "=== BOOK CONTEXT (style, characters, world, outline — follow strictly) ===\n"
            + context
        )
    if story_so_far:
        parts.append(
            "=== STORY SO FAR (summaries of previous chapters — keep continuity) ===\n"
            + story_so_far
        )
    else:
        parts.append(
            "=== STORY SO FAR ===\n(This is the first chapter. Establish the opening "
            "as directed by the book context.)"
        )
    parts.append(f"=== BRIEF FOR {heading} ===\n{brief or '(no brief given; continue the story.)'}")
    parts.append(
        f"Now write {heading} in full. Stay consistent with the story so far and the "
        "book context."
    )
    return "\n\n".join(parts)


def build_summary_prompt(chapter: Chapter, text: str) -> str:
    return (
        "Summarize the following chapter in 4-8 sentences. Capture plot events, "
        "character developments, new locations, and any facts future chapters must "
        "stay consistent with (promises, injuries, revelations, timeline). Write "
        "plainly; no preamble.\n\n"
        f"=== CHAPTER {chapter.number} ===\n{text}"
    )


# --------------------------------------------------------------------------- #
# Gemini calls with retry
# --------------------------------------------------------------------------- #

def make_client() -> "genai.Client":
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print(
            "GEMINI_API_KEY is not set. Copy .env.example to .env and add your key "
            "(get one at https://aistudio.google.com/apikey).",
            file=sys.stderr,
        )
        sys.exit(1)
    return genai.Client(api_key=api_key)


def generate_text(
    client: "genai.Client",
    cfg: Config,
    *,
    model: str,
    prompt: str,
    temperature: float,
    max_output_tokens: int,
    label: str,
) -> str:
    config = genai_types.GenerateContentConfig(
        temperature=temperature,
        max_output_tokens=max_output_tokens,
    )
    last_err: Exception | None = None
    for attempt in range(1, cfg.max_retries + 1):
        try:
            response = client.models.generate_content(
                model=model, contents=prompt, config=config
            )
            text = (response.text or "").strip()
            if not text:
                raise RuntimeError(
                    "empty response (possibly blocked or truncated: "
                    f"{getattr(response, 'prompt_feedback', None)})"
                )
            return text
        except Exception as err:  # broad: SDK raises various transient errors
            last_err = err
            if attempt == cfg.max_retries:
                break
            delay = cfg.retry_base_delay * (2 ** (attempt - 1))
            print(
                f"    retry {attempt}/{cfg.max_retries - 1} for {label} after error: "
                f"{err} (sleeping {delay:.1f}s)",
                file=sys.stderr,
            )
            time.sleep(delay)
    raise RuntimeError(
        f"Failed to generate {label} after {cfg.max_retries} attempts: {last_err}"
    )


# --------------------------------------------------------------------------- #
# Per-chapter pipeline
# --------------------------------------------------------------------------- #

def output_path_for(cfg: Config, chapter: Chapter) -> Path:
    return cfg.output_chapters / f"{chapter.stem_number}.md"


def generate_chapter(
    client: "genai.Client",
    cfg: Config,
    chapter: Chapter,
    context: str,
    summaries: dict[int, str],
) -> str:
    story_so_far = build_story_so_far(summaries, chapter.number, cfg.summary_window)
    prompt = build_chapter_prompt(chapter, context, story_so_far)

    text = generate_text(
        client, cfg,
        model=cfg.model,
        prompt=prompt,
        temperature=cfg.temperature,
        max_output_tokens=cfg.max_output_tokens,
        label=f"chapter {chapter.number}",
    )

    out_path = output_path_for(cfg, chapter)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")

    summary = generate_text(
        client, cfg,
        model=cfg.summary_model,
        prompt=build_summary_prompt(chapter, text),
        temperature=cfg.summary_temperature,
        max_output_tokens=cfg.summary_max_tokens,
        label=f"summary {chapter.number}",
    )
    summaries[chapter.number] = summary
    save_summaries(cfg.summaries, summaries)
    return text


# --------------------------------------------------------------------------- #
# Merge
# --------------------------------------------------------------------------- #

def merge_book(cfg: Config, chapters: list[Chapter], title: str) -> int:
    cfg.book.parent.mkdir(parents=True, exist_ok=True)
    pieces: list[str] = [f"# {title}\n"]
    written = 0
    for chapter in chapters:
        path = output_path_for(cfg, chapter)
        if not path.exists():
            continue
        heading = f"Chapter {chapter.number}"
        if chapter.title:
            heading += f": {chapter.title}"
        body = path.read_text(encoding="utf-8").strip()
        pieces.append(f"\n\n## {heading}\n\n{body}\n")
        written += 1
    cfg.book.write_text("\n".join(pieces).strip() + "\n", encoding="utf-8")
    return written


# --------------------------------------------------------------------------- #
# Load chapters: single-file mode (preferred) or directory mode (fallback)
# --------------------------------------------------------------------------- #

def load_project(cfg: Config, input_override: Path | None) -> tuple[list[Chapter], str, str]:
    """Return (chapters, shared_context, book_title)."""
    source = input_override or cfg.book_source

    if source.exists():
        parsed = parse_book_file(source)
        if not parsed.chapters:
            print(
                f"No '# Chapter N' headings found in {source.name}. Add at least one, "
                "e.g.  '# Chapter 1: The Arrival'.",
                file=sys.stderr,
            )
            sys.exit(1)
        context = parsed.context
        extra_style = read_text_if_exists(cfg.style_guide)
        if extra_style:
            context = (context + "\n\n" + extra_style).strip()
        title = parsed.title or cfg.book_title
        print(f"Single-file mode: {source.name}")
        return parsed.chapters, context, title

    # Fallback: directory mode
    chapters = discover_chapters(cfg.chapters_dir)
    if chapters:
        context_parts = [
            read_text_if_exists(cfg.style_guide),
            read_text_if_exists(cfg.chapters_dir.parent / "master_context.txt"),
        ]
        context = "\n\n".join(p for p in context_parts if p).strip()
        print(f"Directory mode: {cfg.chapters_dir.name}/ ({len(chapters)} files)")
        return chapters, context, cfg.book_title

    print(
        f"No input found. Create '{cfg.book_source.name}' (single-file mode) or add "
        f"chapter files to '{cfg.chapters_dir.name}/'.",
        file=sys.stderr,
    )
    sys.exit(1)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a book with the Gemini API.")
    parser.add_argument("--config", default=str(ROOT / "config.yaml"), help="Path to config.yaml")
    parser.add_argument("--input", metavar="FILE", help="Single-file source (default: book-source.md)")
    parser.add_argument("--only", type=int, metavar="N", help="Generate only chapter N")
    parser.add_argument("--range", nargs=2, type=int, metavar=("START", "END"),
                        help="Generate chapters START..END inclusive")
    parser.add_argument("--force", action="store_true",
                        help="Regenerate chapters even if output already exists")
    parser.add_argument("--merge-only", action="store_true",
                        help="Skip generation; just rebuild book.md from existing outputs")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be generated without calling the API")
    return parser.parse_args(argv)


def select_chapters(args: argparse.Namespace, chapters: list[Chapter]) -> list[Chapter]:
    if args.only is not None:
        return [c for c in chapters if c.number == args.only]
    if args.range is not None:
        lo, hi = args.range
        return [c for c in chapters if lo <= c.number <= hi]
    return chapters


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    load_dotenv(ROOT / ".env")
    cfg = load_config(Path(args.config))

    input_override = Path(args.input).resolve() if args.input else None
    all_chapters, context, title = load_project(cfg, input_override)

    print(f"\"{title}\" — {len(all_chapters)} chapter(s): "
          f"{all_chapters[0].number}..{all_chapters[-1].number}")

    if args.merge_only:
        count = merge_book(cfg, all_chapters, title)
        print(f"Merged {count} chapter(s) into {cfg.book}")
        return 0

    targets = select_chapters(args, all_chapters)
    if not targets:
        print("No chapters matched the selection.", file=sys.stderr)
        return 1

    summaries = load_summaries(cfg.summaries)

    pending: list[Chapter] = []
    for chapter in targets:
        if not args.force and output_path_for(cfg, chapter).exists():
            print(f"  = chapter {chapter.number}: already generated (skip; --force to redo)")
            continue
        pending.append(chapter)

    if args.dry_run:
        print("\nDry run — would generate:")
        for chapter in pending:
            tail = f": {chapter.title}" if chapter.title else ""
            print(f"  chapter {chapter.number}{tail}")
        print(f"\n({len(pending)} chapter(s))")
        return 0

    if not pending:
        print("Nothing to generate. Rebuilding book.md.")
        merge_book(cfg, all_chapters, title)
        return 0

    client = make_client()

    for idx, chapter in enumerate(pending, start=1):
        tail = f": {chapter.title}" if chapter.title else ""
        print(f"[{idx}/{len(pending)}] Generating chapter {chapter.number}{tail} "
              f"with {cfg.model} ...")
        try:
            text = generate_chapter(client, cfg, chapter, context, summaries)
        except Exception as err:
            print(f"  ✗ chapter {chapter.number} failed: {err}", file=sys.stderr)
            print("  Stopping. Fix the issue and re-run; finished chapters are saved "
                  "and will be skipped.", file=sys.stderr)
            return 2
        print(f"  ✓ chapter {chapter.number} written ({len(text.split())} words)")
        if cfg.request_delay > 0 and idx < len(pending):
            time.sleep(cfg.request_delay)

    count = merge_book(cfg, all_chapters, title)
    print(f"\nDone. Merged {count} chapter(s) into {cfg.book}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
