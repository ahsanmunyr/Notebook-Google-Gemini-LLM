# AI Book Generator (Gemini API)

Write a whole book (120+ chapters) from **one file**, with **one command**.

You put your style, characters, world, and a short brief for every chapter into
`book-source.md`, run the tool once, and it generates every chapter in order and
merges them into `output/book.md`.

It stays consistent across chapters by summarizing each finished chapter and
feeding a running **"story so far"** into the next one — so chapter 120 still knows
what happened in chapter 1. You do **not** copy-paste context between chapters by
hand; the tool does that for you.

---

## Quick answers (read this first)

**Do I run the command 120 times?**
**No. Only once.** `python generate.py` loops through all 120 chapters by itself
(chapter 1 → 2 → 3 → … → 120), automatically. You just watch the progress.
(If it stops partway — internet drops, rate limit, laptop closes — run the same
command again and it continues from where it stopped.)

**How do I give the tool my chapters?** Two ways — pick ONE:

- **Option A — single file (recommended):** put everything in `book-source.md`.
- **Option B — one file per chapter:** drop `1.txt … 120.txt` into the `chapters/`
  folder. (Good if you already have your chapters as separate files.)

**Does my chapter text go all in one place?** No — in **two parts**:
1. The **shared context** (style, characters, world, outline) — written **once**, at
   the top.
2. **Each chapter's own brief** — pasted **under that chapter's `# Chapter N:` line.**

---

## ⚠️ Paid Gemini app ≠ Gemini API

Your paid **Gemini / NotebookLM subscription is for the chat apps.** This tool uses
the **Gemini API**, which is billed separately:

1. Get an API key at **https://aistudio.google.com/apikey**.
2. In Google AI Studio, **enable billing** on the project for the higher paid API
   rate limits (the free tier works too, just slower/limited).

The upside: unlike the chat app, this tool loops through all chapters by itself and
automatically waits + retries when it hits a rate limit — so the "I can only do one
chapter at a time" limit goes away.

---

## 1. One-time setup

```bash
cd book-generator
python3 -m venv .venv && source .venv/bin/activate   # recommended
pip install -r requirements.txt

cp .env.example .env
# open .env and paste your key:   GEMINI_API_KEY=your_key_here
```

## 2. Add your book

### Option A — single file (recommended): `book-source.md`

Open `book-source.md` and replace the example with your own. There are **two parts**:

```markdown
Title: My Great Book

<!-- PART 1 — SHARED CONTEXT (written once, used by ALL chapters).
     Put your style, characters, world, and outline here. -->
Style: third person, past tense, 1500–2500 words per chapter, prose only.
Characters: ...
World / outline: ...

<!-- PART 2 — ONE HEADING PER CHAPTER. Paste each chapter's context/brief
     UNDER its own "# Chapter N:" line. -->

# Chapter 1: The Arrival
Paste chapter 1's context/brief here.

# Chapter 2: The Threshold
Paste chapter 2's context/brief here.

# Chapter 3: ...
...

# Chapter 120: ...
Paste chapter 120's context/brief here.
```

Rules:
- Everything **above the first `# Chapter`** is the shared context for the whole book.
- Each chapter **must** start with `# Chapter N:` (the `: Title` part is optional).
  This line is how the tool knows where a new chapter begins. If you paste all your
  context in one block **without** these headings, the tool cannot split it into
  chapters.
- Under each heading, paste that chapter's context/brief (a sentence or a few
  paragraphs — whatever you have).
- Add as many chapters as you want — 3 or 300.
- Anything inside `<!-- ... -->` is ignored, so use it for notes to yourself.

### Option B — one file per chapter: the `chapters/` folder

If you already have each chapter's context as a **separate file**, you don't need to
copy-paste anything. Just:

1. Delete (or rename) `book-source.md` so it's not used.
2. Put your files in `chapters/`, named with a leading number: `1.txt`, `2.txt`, …
   or `001.txt`, `002.txt`, … (mixed styles sort correctly). Each file is that
   chapter's brief.
3. (Optional) Put shared style/world notes in `style_guide.txt`.

The tool uses `book-source.md` if it exists; otherwise it falls back to `chapters/`.

## 3. Run it (once)

```bash
# See the plan first — no API calls, no cost:
python generate.py --dry-run

# (Optional) test the voice on the first 3 chapters:
python generate.py --range 1 3

# Generate the whole book — ONE command, runs all chapters:
python generate.py
```

When it finishes, your book is at **`output/book.md`**.

---

## What happens when you run it

For each chapter, in order, the tool:
1. Builds a prompt from your shared context + the "story so far" + that chapter's brief.
2. Asks Gemini to write the chapter → saves it to `output/chapters/NNN.md`.
3. Summarizes the chapter → appends it to `output/summaries.json`.
4. After the last chapter, merges everything into `output/book.md`.

It prints progress like:

```
"My Great Book" — 120 chapter(s): 1..120
[1/120] Generating chapter 1: The Arrival with gemini-2.5-pro ...
  ✓ chapter 1 written (1843 words)
...
Done. Merged 120 chapter(s) into .../output/book.md
```

**It's resumable.** If a run stops (error, rate limit, you close it), just run
`python generate.py` again — chapters already written are skipped. Use `--force`
to redo them.

---

## Commands

```bash
python generate.py                 # generate ALL chapters, then merge book.md (the main one)
python generate.py --dry-run       # show the plan; no API calls
python generate.py --only 5        # (re)generate just chapter 5
python generate.py --range 10 20   # generate chapters 10..20 inclusive
python generate.py --force         # regenerate even if output already exists
python generate.py --merge-only    # just rebuild book.md from existing chapters
python generate.py --input FILE    # use a different source file instead of book-source.md
```

## Output

```
output/
├── chapters/        # 001.md, 002.md, ... (one file per chapter)
├── summaries.json   # the running "story so far"
└── book.md          # ← your final book
```

## Settings (optional)

Everything is tunable in `config.yaml` — which models to use, creativity
(`temperature`), length (`max_output_tokens`), retries, and a `request_delay` to
slow down if you hit rate limits.

**For a long book (120 chapters), set `summary_window: 20` in `config.yaml`.**
This makes each prompt carry the 20 most recent chapter summaries instead of all
previous ones — faster, cheaper, and stays within context limits, while keeping
recent continuity. (`0` means "include all previous summaries".)

## Tips

- Start with `--dry-run`, then `--range 1 3` to check the voice before the full run.
- Each chapter is also saved on its own in `output/chapters/` if you want to edit
  one by hand; `python generate.py --merge-only` rebuilds `book.md` afterward.
- Hitting rate limits a lot? Raise `request_delay` in `config.yaml`.
```
