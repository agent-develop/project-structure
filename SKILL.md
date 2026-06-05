---
name: project-structure
description: Analyze project structure with tree-sitter to extract symbols (functions/classes/interfaces/types/constants/imports) from all files, generating STRUCTURE.md at the project root. Supports incremental updates, reverse references, global symbol index, .gitignore auto-exclusion, and entry-point annotation. AI loads this file as context to quickly understand project layout.
category: software-development
---

# project-structure — Multi-language symbol extraction

## When to use

- User says "analyze project structure", "generate STRUCTURE.md", "extract project symbols"
- First time entering a new project — proactively suggest generating it
- After significant project changes — update the index

## Usage

```bash
# Incremental + compact (default) — summary + index, ~1000 lines
python3 scripts/extract.py <project_root>

# Full rebuild — ignore cache, re-parse all files
python3 scripts/extract.py <project_root> --full

# Full detail — include per-file symbol breakdown (~4x larger)
python3 scripts/extract.py <project_root> --detail

# JSON output — output STRUCTURE.json instead of markdown
python3 scripts/extract.py <project_root> --json
```

> **Output**: STRUCTURE.md written to `<project_root>/STRUCTURE.md`
> **Cache**: `.structure_cache.json` written to `<project_root>/`

## Five capabilities

### 1. 📤 Reverse references

Tracks every file that imports a given file. See impact at a glance before changing code.

```
**📤 Referenced by:**
- `api/astro/route.ts:10` (compare)
```

### 2. 🔍 Global symbol index

All symbols sorted alphabetically, for quickly answering "which file defines `fetchAstroData`?"

| Symbol | Kind | File:line |
|--------|------|-----------|
| `fetchAstroData` | 🔧 function | `api/astro/route.ts:51` |
| `useTodos` | 🔧 function | `calendar/hooks/useTodos.ts:14` |

### 3. 📝 Incremental change summary

In incremental mode, the terminal shows per-file symbol diffs:

```
🔄 1 file(s) changed, re-parsing...
  [1/1] api/astro/route.ts  [+3, -1 symbols]
📝 1 file(s) with symbol changes:
  api/astro/route.ts:  +fetchData, +cacheResult, +transform  -oldFetcher
```

### 4. 🚫 Auto-read .gitignore

Parses `.gitignore` (walking up to root), automatically excluding matched paths. No manual exclusion lists needed.

### 5. 🚪 Entry-point annotation

Auto-detects and labels project entry files:

- Next.js: `page.tsx`/`layout.tsx`/`route.ts`/`middleware.ts` → page routes / API routes
- Root special: `auth.ts` → auth config, `manifest.ts`/`robots.ts`/`sitemap.ts` → SEO metadata
- Python: `__init__.py` → package entry, `if __name__ == '__main__'` → script entry
- `package.json` `main`/`module` fields
- Go: `main.go` → binary entry
- Rust: `main.rs` → binary entry, `lib.rs` → library entry

## Incremental update algorithm

First run does a full scan and generates `.structure_cache.json`. Subsequent runs default to incremental:

1. Walk all source files, compare `mtime + size` against cached records
2. Changed/new files → re-parse with tree-sitter
3. Unchanged files → read from cache directly
4. Deleted files → remove from results
5. Update cache, regenerate STRUCTURE.md

**Benchmark** (astrology-web, 92 source files):
- Full: ~5 sec
- Incremental (no changes): <0.5 sec
- Incremental (1 file changed): ~1 sec

## Supported languages

| Extensions | Language | tree-sitter package |
|------------|----------|---------------------|
| `.ts` `.tsx` `.js` `.jsx` `.mjs` `.cjs` | TypeScript / JavaScript / JSX | tree-sitter-typescript |
| `.py` | Python | tree-sitter-python |
| `.css` | CSS | tree-sitter-css |
| `.go` | Go | tree-sitter-go |
| `.rs` | Rust | tree-sitter-rust |
| `.java` | Java | tree-sitter-java |
| `.rb` | Ruby | tree-sitter-ruby |

To add a new language: add a mapping in the script's `LANG_MAP` dict, install the corresponding `tree-sitter-xxx` package, and implement a `parse_xxx()` function.

## Prerequisites

```bash
pip3 install tree-sitter tree-sitter-typescript tree-sitter-python tree-sitter-css tree-sitter-go tree-sitter-rust tree-sitter-java tree-sitter-ruby
```

The script supports lazy loading — install only the packages needed for the current project.

## Output structure

The generated STRUCTURE.md has these sections (compact mode, default):

1. **概览** — file/symbol/import counts, git commit
2. **⚠️ Duplicate symbols** — same-named symbols across files (framework conventions excluded)
3. **🫥 Unused code** — exported symbols never imported internally
4. **📦 External dependencies** — npm/pip packages ranked by usage
5. **🔥 Complexity hotspots** — top-10 largest files by line count
6. **🔗 Most-depended-on modules** — files most frequently imported by others
7. **🚪 API route index** — all API endpoints with method, auth, line count
8. **🔗 Call graph** — cross-file function call relationships, grouped by category
9. **🔍 Global symbol index** — all symbols categorized, with file:line
10. **📁 File tree** — files grouped by directory with line counts and entry markers
11. **🚪 Entry-point list** — summary of all detected entry files

Use `--detail` to also include per-file import lists, reverse references, and symbol signatures.

## How AI uses STRUCTURE.md

Inject STRUCTURE.md into context at the start of a conversation so the AI can:

- Quickly locate which file and line a function/class lives on (global symbol index)
- Understand cross-file call relationships (call graph — who calls whom)
- Spot duplicate code (duplicate symbols) and dead code at a glance
- Identify complexity hotspots and most-depended-on modules before making changes
- See the API route surface area and tech stack in one view
- Know entry points at a glance

Per-file import details and symbol signatures are omitted in compact mode — when the AI
needs those, it reads the source file directly, which is faster than scanning a 4000-line index.

## CI integration

Add to `.github/workflows` or a husky pre-commit hook:

```bash
python3 scripts/extract.py . --full
git add STRUCTURE.md
```

## Notes

- Auto-excludes `node_modules`, `.next`, `dist`, `__pycache__`, `.venv`, `venv`, `.git`, and dotfiles (`.env`, `.eslintrc`, etc.)
- Auto-reads `.gitignore` to exclude matching paths
- Extracts top-level symbols only (locals inside functions are ignored)
- Compact mode (default) outputs ~1000 lines; `--detail` adds ~3000 lines of per-file breakdown
- Consider adding `.structure_cache.json` to `.gitignore` if you don't want the cache file in VCS
