# Project Structure

> Multi-language project symbol extractor that generates a single `STRUCTURE.md` index — purpose-built as context for AI coding assistants.

![Languages](https://img.shields.io/badge/languages-7-blue) ![License](https://img.shields.io/badge/license-MIT-green)

## What it does

`project-structure` scans a codebase and generates **one file** (`STRUCTURE.md`) containing everything an AI (or human) needs to navigate the project at a glance:

- **Call graph** — who calls whom, grouped by module category
- **Symbol index** — every function/class/interface/type with exact `file:line`
- **API route index** — all endpoints with method, auth requirements, line counts
- **Reverse references** — for any file, see every file that imports it
- **Duplicate detection** — same-named symbols across files
- **Unused code hints** — exported symbols never imported internally
- **Complexity hotspots** — largest files ranked by line count

## Quick start

```bash
# Install dependencies (only what your project needs)
pip3 install tree-sitter tree-sitter-typescript

# Generate STRUCTURE.md for your project
python3 scripts/extract.py /path/to/your/project
```

That's it. A `STRUCTURE.md` and `.structure_cache.json` appear at the project root.

## Use in Claude Code

### Install the skill

Copy the skill folder into Claude Code's skills directory:

```bash
cp -r project-structure ~/.claude/skills/project-structure
```

Restart Claude Code, and `/project-structure` is ready.

### Trigger the skill

There are three ways to invoke it:

| Trigger | Example |
|---------|---------|
| **Slash command** | `/project-structure` |
| **Ask Claude** | "analyze project structure" / "update STRUCTURE.md" |
| **Auto-trigger** | Claude auto-runs it after creating, deleting, or renaming source files |

### How Claude uses STRUCTURE.md

Once generated, Claude loads `STRUCTURE.md` as context at the start of each session to:

- Jump to any symbol by name without grep
- Trace call chains before editing
- Assess impact before refactoring
- Find the right file to edit instantly

## Key features

| Feature | Description |
|---------|-------------|
| 🔄 **Incremental updates** | Only re-parses changed files. Full scan on first run, <0.5s subsequent runs when nothing changes |
| 🚀 **Multi-language** | TypeScript, JavaScript, TSX, JSX, Python, CSS, Go, Rust, Java, Ruby |
| 🚫 **Auto .gitignore** | Reads `.gitignore` (walking up to root). No manual exclusion lists |
| 🚪 **Entry-point detection** | Auto-labels page routes, API routes, middleware, SEO files |
| 🧠 **AI-optimized output** | Compact by default (~1000 lines). Symbols only, not per-file detail — AI reads source files directly when needed |
| ⚙️ **Framework-aware** | Detects Next.js, Go, Rust, Python, Java, Ruby projects. Config-driven — add new frameworks without touching core logic |

## Usage

```bash
# Incremental (default) — uses cache, fast
python3 scripts/extract.py <project_root>

# Full rebuild — ignore cache
python3 scripts/extract.py <project_root> --full

# Full detail — include per-file symbol breakdown
python3 scripts/extract.py <project_root> --detail

# JSON output
python3 scripts/extract.py <project_root> --json
```

## Output sections

The generated `STRUCTURE.md` contains these sections:

| # | Section | What it tells you |
|---|---------|-------------------|
| 1 | **Overview** | File/symbol/import counts, git commit |
| 2 | **⚠️ Duplicate symbols** | Same names across files (framework conventions excluded) |
| 3 | **🫥 Unused code** | Exported but never imported internally |
| 4 | **📦 External dependencies** | npm/pip packages ranked by usage |
| 5 | **🔥 Complexity hotspots** | Top-10 largest files |
| 6 | **🔗 Most-depended-on modules** | Files imported by the most other files |
| 7 | **🚪 API route index** | All endpoints: method, path, auth, line count |
| 8 | **🔗 Call graph** | Cross-file function calls, grouped by category |
| 9 | **🔍 Global symbol index** | Every symbol categorized with `file:line` |
| 10 | **📁 File tree** | Files per directory with entry markers |
| 11 | **🚪 Entry points** | All detected entry files summarized |

## How incremental mode works

```
First run:  walk all files → parse with tree-sitter → save cache
Next runs:  compare mtime+size → re-parse only changed → merge with cached
```

**Benchmark** (92 source files):
- Full scan: ~5s
- No changes: <0.5s
- 1 file changed: ~1s

## Supported languages

| Extensions | Language | Package |
|------------|----------|---------|
| `.ts` `.tsx` `.js` `.jsx` `.mjs` `.cjs` | TypeScript / JavaScript | `tree-sitter-typescript` |
| `.py` | Python | `tree-sitter-python` |
| `.css` | CSS | `tree-sitter-css` |
| `.go` | Go | `tree-sitter-go` |
| `.rs` | Rust | `tree-sitter-rust` |
| `.java` | Java | `tree-sitter-java` |
| `.rb` | Ruby | `tree-sitter-ruby` |

Adding a language: install the `tree-sitter-xxx` package, add a mapping in `LANG_MAP`, implement a `parse_xxx()` function. The framework is config-driven.

## AI integration

AI coding assistants load `STRUCTURE.md` once at the start of a conversation to:

- Locate symbols without grep (`"which file defines fetchAstroData?"` → index lookup)
- Understand call chains before editing (call graph)
- Assess impact before refactoring (reverse references)
- Identify the right file to edit (file tree + API route index)

**Result:** fewer tool calls, less context waste, faster responses.

## CI / pre-commit

```bash
# GitHub Actions
- name: Update STRUCTURE.md
  run: |
    python3 scripts/extract.py . --full
    git add STRUCTURE.md

# Husky pre-commit
python3 scripts/extract.py . --full && git add STRUCTURE.md
```

## File overview

```
project-structure/
├── SKILL.md              # Claude Code skill definition
├── README.md             # This file
└── scripts/
    └── extract.py        # Main script
```

## Design principles

- **Config-driven.** Framework behavior lives in `FrameworkConfig` dataclasses, not scattered conditionals. Adding Next.js support required zero changes to core logic.
- **Incremental by default.** `mtime + size` fingerprints. First run is full; subsequent runs are near-instant.
- **Compact output for AI.** Omits per-file detail by default. When AI needs a function's signature, it reads the source file.

## License

MIT — see [LICENSE](LICENSE) file.
