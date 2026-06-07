#!/usr/bin/env python3
"""
extract.py — Multi-language project symbol extractor (incremental updates supported)
Usage:
  python3 extract.py <project_dir>             # Incremental (default)
  python3 extract.py <project_dir> --full      # Full rebuild
  python3 extract.py <project_dir> --json      # Output JSON instead of markdown
Output: STRUCTURE.md written to project root
Cache:  .structure_cache.json written to project root
"""
import sys, os, re, json, fnmatch, subprocess
from pathlib import Path
from dataclasses import dataclass, field, asdict
from collections import defaultdict
from datetime import datetime

import tree_sitter as ts


# ═══════════════════════════════════════════
# Language loading
# ═══════════════════════════════════════════

_LANG_CACHE = {}

def _load_lang(package_name: str, attr: str):
    if package_name not in _LANG_CACHE:
        mod = __import__(package_name)
        _LANG_CACHE[package_name] = ts.Language(getattr(mod, attr)())
    return _LANG_CACHE[package_name]

LANG_MAP = {
    # TypeScript / JavaScript (same package, TS parser handles JS)
    '.ts':  ('tree_sitter_typescript', 'language_typescript'),
    '.tsx': ('tree_sitter_typescript', 'language_tsx'),
    '.js':  ('tree_sitter_typescript', 'language_typescript'),
    '.jsx': ('tree_sitter_typescript', 'language_tsx'),
    '.mjs': ('tree_sitter_typescript', 'language_typescript'),
    '.cjs': ('tree_sitter_typescript', 'language_typescript'),
    # Python
    '.py':  ('tree_sitter_python',     'language'),
    # CSS
    '.css': ('tree_sitter_css',        'language'),
    # Go
    '.go':  ('tree_sitter_go',         'language'),
    # Rust
    '.rs':  ('tree_sitter_rust',       'language'),
    # Java
    '.java':('tree_sitter_java',       'language'),
    # Ruby
    '.rb':  ('tree_sitter_ruby',       'language'),
}

CACHE_FILE = '.structure_cache.json'


# ═══════════════════════════════════════════
# Data model
# ═══════════════════════════════════════════

@dataclass
class Symbol:
    kind: str
    name: str
    line: int
    signature: str
    exported: bool = False
    default_export: bool = False
    is_async: bool = False

@dataclass
class ImportInfo:
    source: str
    names: list[str]
    line: int = 0

@dataclass
class CallSite:
    """A function call record — extracted from AST during parsing.
    Cross-file resolution happens later in build_call_graph."""
    caller_func: str        # name of the calling function
    callee_name: str        # expression text being called (e.g. 'createTodo' or 'svc.createTodo')
    line: int

@dataclass
class FileSymbols:
    path: str
    relpath: str
    language: str = ''
    imports: list = field(default_factory=list)
    symbols: list = field(default_factory=list)
    calls: list = field(default_factory=list)       # function calls within this file
    lines: int = 0
    is_entry: bool = False       # entry-point annotation
    entry_reason: str = ''

    def to_cache(self):
        return {
            'relpath': self.relpath,
            'language': self.language,
            'lines': self.lines,
            'imports': [asdict(i) for i in self.imports],
            'symbols': [asdict(s) for s in self.symbols],
            'calls': [asdict(c) for c in self.calls],
            'is_entry': self.is_entry,
            'entry_reason': self.entry_reason,
        }

    @classmethod
    def from_cache(cls, data: dict):
        return cls(
            path=data['relpath'], relpath=data['relpath'],
            language=data['language'], lines=data['lines'],
            imports=[ImportInfo(**i) for i in data['imports']],
            symbols=[Symbol(**s) for s in data['symbols']],
            calls=[CallSite(**c) for c in data.get('calls', [])],
            is_entry=data.get('is_entry', False),
            entry_reason=data.get('entry_reason', ''),
        )


# ═══════════════════════════════════════════
# Framework config types
# ═══════════════════════════════════════════

@dataclass
class EntryRule:
    """Single entry-detection rule. All non-None fields are AND-combined,
    first-match wins."""
    reason: str = ''
    filenames: list[str] = field(default_factory=list)
    path_starts: str | None = None
    path_contains: str | None = None
    root_only: bool = False
    language: str | None = None
    content_regex: str | None = None

@dataclass
class CategoryRule:
    """Single symbol categorization rule. All non-None fields are AND-combined,
    matched in descending priority order."""
    category: str = ''
    priority: int = 0
    path_starts: str | None = None
    path_ends: str | None = None
    path_contains: str | None = None
    kind_in: list[str] | None = None
    exact_files: list[str] | None = None
    category_by_kind: dict[str, str] | None = None

@dataclass
class ConventionRule:
    """A set of conventional symbol names and their file suffixes."""
    names: set[str] = field(default_factory=set)
    file_suffixes: list[str] = field(default_factory=list)

@dataclass
class ConventionConfig:
    """Convention filter config: excludes false positives from duplicate/dead-code detection."""
    rules: list[ConventionRule] = field(default_factory=list)
    always_convention_files: set[str] = field(default_factory=set)

@dataclass
class ApiRouteConfig:
    """API route index generation config."""
    path_contains: str = '/api/'
    file_suffixes: list[str] = field(default_factory=lambda: ['route.ts', 'route.tsx'])
    path_prefix_strip: str = 'app/'
    path_suffix_split: str = '/route.'
    http_methods: set[str] = field(default_factory=lambda:
        {'GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS'})

@dataclass
class CallGraphConfig:
    """Call-graph analysis config. Tracks cross-file function call relationships."""
    enabled: bool = True
    # Path aliases for resolving prefixes like @/ (e.g. {'@/': ''})
    path_aliases: dict[str, str] = field(default_factory=dict)

@dataclass
class FrameworkConfig:
    """Complete framework configuration."""
    name: str = 'generic'
    display_name: str = 'Generic'
    entry_rules: list[EntryRule] = field(default_factory=list)
    check_package_json: bool = True
    category_rules: list[CategoryRule] = field(default_factory=list)
    category_fallback: str = '📌 Other'
    category_order: list[str] = field(default_factory=list)
    convention: ConventionConfig = field(default_factory=ConventionConfig)
    api_route: ApiRouteConfig | None = None
    call_graph: CallGraphConfig | None = None


# ═══════════════════════════════════════════
# Framework config definitions
# ═══════════════════════════════════════════

NEXTJS_CONFIG = FrameworkConfig(
    name='nextjs', display_name='Next.js',
    entry_rules=[
        EntryRule(filenames=['route.ts', 'route.tsx'], path_contains='app/',
                  reason='Next.js API route'),
        EntryRule(filenames=['page.tsx', 'page.ts'],
                  reason='Next.js page route'),
        EntryRule(filenames=['layout.tsx', 'layout.ts'],
                  reason='Next.js root layout'),
        EntryRule(filenames=['middleware.ts'],
                  reason='Next.js middleware'),
        EntryRule(filenames=['proxy.ts'],
                  reason='Next.js middleware'),
        EntryRule(filenames=['auth.ts', 'auth.tsx'], root_only=True,
                  reason='Auth config'),
        EntryRule(filenames=['manifest.ts', 'robots.ts', 'sitemap.ts'],
                  root_only=True, reason='SEO metadata'),
        EntryRule(filenames=['__init__.py'], language='python',
                  reason='Package entry'),
        EntryRule(language='python',
                  content_regex=r"if\s+__name__\s*==\s*['\"]__main__['\"]",
                  reason='__main__ script'),
    ],
    category_rules=[
        # ── Next.js framework structure ──
        CategoryRule(path_starts='app/api/', path_ends='route.ts',
                     category='🚪 API routes', priority=100),
        CategoryRule(path_ends='page.tsx', category='🧩 Pages', priority=95),
        CategoryRule(path_ends='page.ts',  category='🧩 Pages', priority=95),
        # ── Next.js special files ──
        CategoryRule(
            exact_files=['proxy.ts', 'next.config.ts', 'app/auth.ts',
                         'app/layout.tsx', 'app/manifest.ts', 'app/robots.ts',
                         'app/sitemap.ts', 'app/not-found.tsx'],
            category='⚙️ Config & middleware', priority=45),
    ],
    category_order=[
        '⚙️ Config & middleware', '🚪 API routes', '🧩 Pages',
        # Dynamically generated from project directory structure below
        '📁 Other directories', '📌 Other',
    ],
    convention=ConventionConfig(
        rules=[
            ConventionRule(
                names={'GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS'},
                file_suffixes=['route.ts', 'route.tsx']),
            ConventionRule(
                names={'Page'},
                file_suffixes=['page.tsx', 'page.ts']),
            ConventionRule(
                names={'metadata', 'revalidate', 'dynamic', 'viewport',
                       'generateStaticParams', 'generateMetadata'},
                file_suffixes=['page.tsx', 'page.ts', 'layout.tsx', 'layout.ts',
                               'route.ts', 'route.tsx']),
        ],
        always_convention_files={'proxy.ts', 'middleware.ts'},
    ),
    api_route=ApiRouteConfig(),
    call_graph=CallGraphConfig(
        path_aliases={'@/': ''},
    ),
)

PYTHON_CONFIG = FrameworkConfig(
    name='python', display_name='Python',
    entry_rules=[
        EntryRule(filenames=['__init__.py'], reason='Package entry'),
        EntryRule(content_regex=r"if\s+__name__\s*==\s*['\"]__main__['\"]",
                  reason='__main__ script'),
    ],
    check_package_json=False,
    category_rules=[
        CategoryRule(path_contains='/test/', category='🧪 Tests', priority=90),
        CategoryRule(path_starts='test/', category='🧪 Tests', priority=90),
        CategoryRule(path_ends='_test.py', category='🧪 Tests', priority=85),
        CategoryRule(path_ends='test_*.py', category='🧪 Tests', priority=85),
        CategoryRule(path_starts='migrations/', category='🗄️ Migrations', priority=70),
        CategoryRule(path_starts='models/', category='📦 Models', priority=65),
        CategoryRule(path_starts='views/', category='👁️ Views', priority=65),
        CategoryRule(path_starts='utils/', category='🔧 Utils', priority=60),
    ],
    category_order=['📦 Models', '👁️ Views', '🔧 Utils', '🧪 Tests', '🗄️ Migrations', '📌 Other'],
)

GENERIC_CONFIG = FrameworkConfig(
    name='generic', display_name='Generic',
    entry_rules=[
        EntryRule(filenames=['__init__.py'], language='python', reason='Package entry'),
        EntryRule(language='python',
                  content_regex=r"if\s+__name__\s*==\s*['\"]__main__['\"]",
                  reason='__main__ script'),
    ],
    category_rules=[
        # ── Test files (framework-agnostic) ──
        CategoryRule(path_contains='test', category='🧪 Tests', priority=55),
        CategoryRule(path_ends='.test.ts', category='🧪 Tests', priority=50),
        CategoryRule(path_ends='.spec.ts', category='🧪 Tests', priority=50),
        CategoryRule(path_ends='test.py', category='🧪 Tests', priority=50),
    ],
    category_order=[
        '🧪 Tests',
        # Dynamically generated from project directory structure below
        '📁 Other directories', '📌 Other',
    ],
    call_graph=CallGraphConfig(
        path_aliases={'@/': ''},
    ),
)

GO_CONFIG = FrameworkConfig(
    name='go', display_name='Go',
    entry_rules=[
        EntryRule(filenames=['main.go'], reason='Go entry point (package main)'),
    ],
    check_package_json=False,
    category_rules=[
        CategoryRule(path_starts='cmd/', category='🚪 Entry', priority=100),
        CategoryRule(path_starts='internal/', category='🔒 Internal', priority=90),
        CategoryRule(path_starts='pkg/', category='📦 Public pkgs', priority=85),
        CategoryRule(path_contains='_test.go', category='🧪 Tests', priority=80),
    ],
    category_order=['🚪 Entry', '🔒 Internal', '📦 Public pkgs', '🧪 Tests', '📌 Other'],
    call_graph=CallGraphConfig(path_aliases={}),
)

RUST_CONFIG = FrameworkConfig(
    name='rust', display_name='Rust',
    entry_rules=[
        EntryRule(filenames=['main.rs'], reason='Rust binary entry'),
        EntryRule(filenames=['lib.rs'], reason='Rust library entry'),
    ],
    check_package_json=False,
    category_rules=[
        CategoryRule(path_starts='src/bin/', category='🚪 Entry', priority=100),
        CategoryRule(path_contains='tests/', category='🧪 Tests', priority=80),
        CategoryRule(path_ends='_test.rs', category='🧪 Tests', priority=75),
        CategoryRule(path_contains='examples/', category='📖 Examples', priority=70),
        CategoryRule(path_contains='benches/', category='⚡ Benchmarks', priority=65),
    ],
    category_order=['🚪 Entry', '📖 Examples', '🧪 Tests', '⚡ Benchmarks', '📌 Other'],
    call_graph=CallGraphConfig(path_aliases={'crate::': 'src/'}),
)

JAVA_CONFIG = FrameworkConfig(
    name='java', display_name='Java',
    entry_rules=[],
    check_package_json=False,
    category_rules=[
        CategoryRule(path_starts='src/main/java/', category='📦 Main src', priority=100),
        CategoryRule(path_starts='src/test/java/', category='🧪 Tests', priority=90),
        CategoryRule(path_contains='/test/', category='🧪 Tests', priority=85),
    ],
    category_order=['📦 Main src', '🧪 Tests', '📌 Other'],
    call_graph=CallGraphConfig(path_aliases={}),
)

RUBY_CONFIG = FrameworkConfig(
    name='ruby', display_name='Ruby',
    entry_rules=[
        EntryRule(filenames=['Gemfile'], reason='Gem dependency manifest'),
    ],
    check_package_json=False,
    category_rules=[
        CategoryRule(path_starts='lib/', category='📦 Library', priority=100),
        CategoryRule(path_contains='spec/', category='🧪 Tests', priority=90),
        CategoryRule(path_contains='test/', category='🧪 Tests', priority=90),
        CategoryRule(path_starts='app/models/', category='📊 Models', priority=85),
        CategoryRule(path_starts='app/controllers/', category='🎮 Controllers', priority=85),
        CategoryRule(path_starts='app/views/', category='👁️ Views', priority=80),
        CategoryRule(path_starts='app/helpers/', category='🔧 Helpers', priority=75),
    ],
    category_order=[
        '📊 Models', '🎮 Controllers', '👁️ Views', '🔧 Helpers',
        '📦 Library', '🧪 Tests', '📌 Other',
    ],
    call_graph=CallGraphConfig(path_aliases={}),
)

FRAMEWORK_CONFIGS = {
    'nextjs': NEXTJS_CONFIG,
    'python': PYTHON_CONFIG,
    'go': GO_CONFIG,
    'rust': RUST_CONFIG,
    'java': JAVA_CONFIG,
    'ruby': RUBY_CONFIG,
    'generic': GENERIC_CONFIG,
}


# ═══════════════════════════════════════════
# Utilities
# ═══════════════════════════════════════════

def trim_sig(text: str, max_len: int = 120) -> str:
    text = re.sub(r'\s+', ' ', text.strip())
    return text if len(text) <= max_len else text[:max_len-3] + '...'

def extract_name(node) -> str:
    # Try the 'name' field first (works for most tree-sitter grammars)
    name_node = node.child_by_field_name('name')
    if name_node:
        return name_node.text.decode()
    # Fallback: find first identifier-like child
    for child in node.named_children:
        if child.type in ('identifier', 'field_identifier',
                          'type_identifier', 'string', 'constant'):
            return child.text.decode()
    return '?'

def file_fingerprint(filepath: Path) -> str:
    stat = filepath.stat()
    return f"{stat.st_mtime:.6f}:{stat.st_size}"


# ═══════════════════════════════════════════
# .gitignore parsing
# ═══════════════════════════════════════════

def _parse_gitignore(root: Path) -> list[tuple[str, bool]]:
    gf = root / '.gitignore'
    if not gf.exists():
        return []
    patterns = []
    for line in gf.read_text(errors='ignore').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        negate = line.startswith('!')
        pat = line[1:].strip() if negate else line
        if pat.startswith('/'):
            pat = pat[1:]
        if pat:
            patterns.append((pat, negate))
    return patterns

def _match_gitignore(relpath: str, patterns: list[tuple[str, bool]]) -> bool:
    ignored = False
    for pat, negate in patterns:
        name = Path(relpath).name
        if fnmatch.fnmatch(relpath, pat) or fnmatch.fnmatch(name, pat):
            ignored = not negate
        elif pat.endswith('/') and (fnmatch.fnmatch(relpath + '/', pat) or relpath.startswith(pat)):
            ignored = not negate
        elif '**' in pat:
            pattern_re = re.escape(pat).replace(r'\*\*', '.*').replace(r'\*', '[^/]*')
            if re.search(pattern_re, relpath):
                ignored = not negate
        elif not any(c in pat for c in '*?[') and relpath.startswith(pat + '/'):
            # plain directory name without trailing slash, e.g. "node_modules"
            ignored = not negate
    return ignored

def _gitignore_patterns(root: Path) -> list[tuple[str, bool]]:
    patterns = []
    current = root
    while current != current.parent:
        patterns = _parse_gitignore(current) + patterns
        current = current.parent
    return patterns


# ═══════════════════════════════════════════
# Shared parser infrastructure
# ═══════════════════════════════════════════

def _parse_with_lang(filepath: str, lang_fn, lang_label: str):
    """Factory: sets up parser, reads file, handles errors, counts lines.
    Returns (FileSymbols, tree, source_bytes) or (None, None, None) on parse error."""
    lang = lang_fn()
    parser = ts.Parser(lang)
    source_bytes = Path(filepath).read_bytes()
    try:
        tree = parser.parse(source_bytes)
    except Exception:
        return None, None, None
    return FileSymbols(
        path=filepath, relpath=filepath,
        language=lang_label,
        lines=source_bytes.count(b'\n') + 1
    ), tree, source_bytes


# ═══════════════════════════════════════════
# TypeScript/TSX parser
# ═══════════════════════════════════════════

def _get_ts_lang(ext: str):
    return _load_lang(*LANG_MAP[ext])

def _ts_is_top_level(node):
    p = node.parent
    while p:
        if p.type in ('program', 'export_statement'):
            return True
        if p.type in ('function_declaration', 'class_declaration', 'arrow_function',
                       'method_definition', 'statement_block', 'if_statement',
                       'for_statement', 'switch_statement', 'try_statement'):
            return False
        p = p.parent
    return True

def _extract_calls(body_node, func_name: str):
    """Recursively walk a function body, extracting all call_expression nodes.
    Handles nested functions — calls inside inner functions are attributed to
    the inner function's name. Returns list[CallSite]."""
    calls = []

    def _func_name_from_node(node):
        """Extract inner function name from a node, or None if not a function definition."""
        if node.type == 'function_declaration':
            return extract_name(node)
        if node.type in ('lexical_declaration', 'variable_declaration'):
            for decl in node.named_children:
                if decl.type == 'variable_declarator':
                    name_node = decl.child_by_field_name('name')
                    if name_node and name_node.type == 'identifier':
                        has_arrow = any(
                            c.type == 'arrow_function' for c in decl.named_children
                        ) or any(
                            c.type == 'arrow_function'
                            for nc in decl.named_children
                            for c in nc.named_children
                        )
                        if has_arrow:
                            return name_node.text.decode()
        return None

    def _get_body(node):
        """Get the body node of a function/method."""
        return node.child_by_field_name('body')

    def walk_body(node, current_func):
        for child in node.named_children:
            # Check if this is a nested function definition — switch context
            nested = _func_name_from_node(child)
            if nested:
                nbody = _get_body(child)
                if nbody:
                    walk_body(nbody, nested)
                continue

            if child.type == 'call_expression':
                func_node = child.child_by_field_name('function')
                if func_node:
                    callee = func_node.text.decode()
                    # Only record meaningful calls (filter out bare string/number literal false positives)
                    if callee and not callee.startswith(('"', "'", '`')):
                        calls.append(CallSite(
                            caller_func=current_func,
                            callee_name=callee,
                            line=child.start_point[0] + 1
                        ))

            # Continue recursion (calls may appear inside if/for/ternary etc.)
            if child.named_children:
                walk_body(child, current_func)

    if body_node:
        walk_body(body_node, func_name)
    return calls


def parse_ts(filepath: str, ext: str) -> 'FileSymbols | None':
    lang_label_map = {'.tsx': 'typescript-tsx', '.jsx': 'javascript-jsx'}
    lang_label = lang_label_map.get(ext, 'javascript' if ext in ('.js', '.mjs', '.cjs') else 'typescript')
    result, tree, source_bytes = _parse_with_lang(filepath, lambda: _get_ts_lang(ext), lang_label)
    if result is None:
        return None

    def walk(node, inside_class=False):
        for child in node.named_children:
            t = child.type
            if t == 'export_statement':
                walk(child, inside_class)
                continue
            if t == 'import_statement':
                source = ''
                names = []
                for c in child.named_children:
                    if c.type == 'import_clause':
                        for ic in c.named_children:
                            if ic.type == 'named_imports':
                                for n in ic.named_children:
                                    if n.type == 'import_specifier':
                                        nn = n.child_by_field_name('name')
                                        if nn: names.append(nn.text.decode())
                            elif ic.type == 'identifier':
                                names.append(ic.text.decode())
                            elif ic.type == 'namespace_import' and ic.named_children:
                                names.append(f"* as {ic.named_children[0].text.decode()}")
                    elif c.type == 'string':
                        source = c.text.decode().strip("'\"")
                result.imports.append(ImportInfo(
                    source=source or '(unknown)', names=names or ['(default)'],
                    line=child.start_point[0] + 1
                ))
                continue
            if t == 'function_declaration':
                name = extract_name(child)
                sig = trim_sig(child.text.decode())
                is_async = any(c.type == 'async' and not c.is_named for c in child.children)
                exported = node.type == 'export_statement'
                default_export = exported and any(
                    c.type == 'default' and not c.is_named for c in node.children)
                result.symbols.append(Symbol(
                    kind='function', name=name, line=child.start_point[0] + 1,
                    signature=sig, exported=exported,
                    default_export=default_export, is_async=is_async
                ))
                # Extract calls within function body
                body = child.child_by_field_name('body')
                result.calls.extend(_extract_calls(body, name))
                continue
            if t == 'class_declaration':
                name = extract_name(child)
                sig = trim_sig(child.text.decode())
                exported = node.type == 'export_statement'
                default_export = exported and any(
                    c.type == 'default' and not c.is_named for c in node.children)
                result.symbols.append(Symbol(
                    kind='class', name=name, line=child.start_point[0] + 1,
                    signature=sig, exported=exported, default_export=default_export
                ))
                for cc in child.named_children:
                    if cc.type == 'class_body':
                        for m in cc.named_children:
                            if m.type == 'method_definition':
                                mname = extract_name(m)
                                masync = any(c.type == 'async' and not c.is_named for c in m.children)
                                result.symbols.append(Symbol(
                                    kind='method', name=f'{name}.{mname}',
                                    line=m.start_point[0] + 1,
                                    signature=trim_sig(m.text.decode()), is_async=masync
                                ))
                            elif m.type == 'public_field_definition':
                                mname = extract_name(m)
                                for fc in m.named_children:
                                    if fc.type in ('arrow_function', 'function'):
                                        result.symbols.append(Symbol(
                                            kind='method', name=f'{name}.{mname}',
                                            line=m.start_point[0] + 1,
                                            signature=trim_sig(m.text.decode())
                                        ))
                                        break
                continue
            if t == 'interface_declaration':
                result.symbols.append(Symbol(
                    kind='interface', name=extract_name(child),
                    line=child.start_point[0] + 1,
                    signature=trim_sig(child.text.decode()),
                    exported=node.type == 'export_statement'
                ))
                continue
            if t == 'type_alias_declaration':
                result.symbols.append(Symbol(
                    kind='type', name=extract_name(child),
                    line=child.start_point[0] + 1,
                    signature=trim_sig(child.text.decode()),
                    exported=node.type == 'export_statement'
                ))
                continue
            if t == 'enum_declaration':
                result.symbols.append(Symbol(
                    kind='enum', name=extract_name(child),
                    line=child.start_point[0] + 1,
                    signature=trim_sig(child.text.decode()),
                    exported=node.type == 'export_statement'
                ))
                continue
            if t in ('lexical_declaration', 'variable_declaration'):
                if not _ts_is_top_level(child):
                    continue
                exported = node.type == 'export_statement'
                for decl in child.named_children:
                    if decl.type == 'variable_declarator':
                        # Skip destructuring patterns (useState returns, props/context destructuring, etc.)
                        name_node = decl.child_by_field_name('name')
                        if name_node and name_node.type in ('array_pattern', 'object_pattern'):
                            continue
                        dname = extract_name(decl)
                        if not dname or dname == '?':
                            if name_node: dname = name_node.text.decode()[:50]
                        dsig = trim_sig(decl.text.decode(), 100)
                        is_const = 'const' in child.text.decode()[:10]
                        is_arrow = any(c.type == 'arrow_function' for c in decl.named_children)
                        result.symbols.append(Symbol(
                            kind='function' if is_arrow else ('const' if is_const else 'var'),
                            name=dname, line=child.start_point[0] + 1,
                            signature=dsig, exported=exported
                        ))
                        # If it's an arrow function, extract calls within its body
                        if is_arrow:
                            for dc in decl.named_children:
                                if dc.type == 'arrow_function':
                                    body = dc.child_by_field_name('body')
                                    result.calls.extend(_extract_calls(body, dname))
                                    break
                continue
            if child.named_children:
                walk(child, inside_class or t == 'class_body')

    walk(tree.root_node)
    return result


# ═══════════════════════════════════════════
# Python parser
# ═══════════════════════════════════════════

def _get_py_lang():
    return _load_lang('tree_sitter_python', 'language')

def parse_py(filepath: str) -> 'FileSymbols | None':
    result, tree, source_bytes = _parse_with_lang(filepath, _get_py_lang, 'python')
    if result is None:
        return None

    def walk(node, inside_class=False):
        for child in node.named_children:
            t = child.type
            if t == 'decorated_definition':
                walk(child, inside_class)
                continue
            if t == 'import_statement':
                names = []
                for c in child.named_children:
                    if c.type == 'dotted_name':
                        names.append(c.text.decode())
                    elif c.type == 'aliased_import':
                        names.append(c.text.decode())
                result.imports.append(ImportInfo(
                    source=', '.join(names), names=names,
                    line=child.start_point[0] + 1
                ))
                continue
            if t == 'import_from_statement':
                source = ''
                names = []
                for c in child.named_children:
                    if c.type == 'dotted_name':
                        source = c.text.decode()
                    elif c.type == 'import_list':
                        for ic in c.named_children:
                            names.append(ic.text.decode())
                    elif c.type == 'wildcard_import':
                        names.append('*')
                result.imports.append(ImportInfo(
                    source=source or '(unknown)', names=names or ['(default)'],
                    line=child.start_point[0] + 1
                ))
                continue
            if t == 'function_definition':
                name = extract_name(child)
                sig = trim_sig(child.text.decode())
                is_async = any(c.type == 'async' and not c.is_named for c in child.children)
                result.symbols.append(Symbol(
                    kind='method' if inside_class else 'function',
                    name=f'{inside_class}.{name}' if inside_class else name,
                    line=child.start_point[0] + 1,
                    signature=sig, is_async=is_async
                ))
                continue
            if t == 'class_definition':
                name = extract_name(child)
                sig = trim_sig(child.text.decode())
                result.symbols.append(Symbol(
                    kind='class', name=name,
                    line=child.start_point[0] + 1, signature=sig
                ))
                body = next((c for c in child.named_children if c.type == 'block'), None)
                if body:
                    walk(body, inside_class=name)
                continue
            if t == 'expression_statement' and not inside_class:
                for sc in child.named_children:
                    if sc.type == 'assignment':
                        lhs = sc.child_by_field_name('left')
                        name = lhs.text.decode()[:60] if lhs else sc.text.decode()[:60]
                        sig = trim_sig(sc.text.decode(), 100)
                        is_const = name.isupper() or name[0].isupper()
                        result.symbols.append(Symbol(
                            kind='const' if is_const else 'var',
                            name=name, line=child.start_point[0] + 1, signature=sig
                        ))
                        break
                continue
            if child.named_children:
                walk(child, inside_class)

    walk(tree.root_node)
    return result


# ═══════════════════════════════════════════
# CSS parser
# ═══════════════════════════════════════════

def parse_css(filepath: str) -> 'FileSymbols | None':
    result, tree, source_bytes = _parse_with_lang(
        filepath, lambda: _load_lang('tree_sitter_css', 'language'), 'css')
    if result is None:
        return None

    def walk(node):
        for child in node.named_children:
            if child.type == 'import_statement':
                # @import url("base.css") or @import "base.css"
                for c in child.named_children:
                    if c.type == 'call_expression':
                        # The arguments node is a named child, not a field
                        for arg in c.named_children:
                            if arg.type == 'arguments':
                                for a in arg.named_children:
                                    if a.type == 'string_value':
                                        result.imports.append(ImportInfo(
                                            source=a.text.decode().strip("'\""),
                                            names=['(css import)'],
                                            line=child.start_point[0] + 1
                                        ))
                    elif c.type == 'string_value':
                        result.imports.append(ImportInfo(
                            source=c.text.decode().strip("'\""),
                            names=['(css import)'],
                            line=child.start_point[0] + 1
                        ))
                continue
            if child.type == 'keyframes_statement':
                # @keyframes fadeIn { ... }
                for c in child.named_children:
                    if c.type == 'keyframes_name':
                        result.symbols.append(Symbol(
                            kind='const', name=f'@keyframes {c.text.decode()}',
                            line=child.start_point[0] + 1,
                            signature=trim_sig(child.text.decode(), 80)
                        ))
                continue
            if child.named_children:
                walk(child)

    walk(tree.root_node)
    return result


# ═══════════════════════════════════════════
# Go parser
# ═══════════════════════════════════════════

def parse_go(filepath: str) -> 'FileSymbols | None':
    result, tree, source_bytes = _parse_with_lang(
        filepath, lambda: _load_lang('tree_sitter_go', 'language'), 'go')
    if result is None:
        return None

    def walk(node):
        for child in node.named_children:
            t = child.type
            if t == 'import_declaration':
                for spec in child.named_children:
                    if spec.type == 'import_spec':
                        path_node = spec.child_by_field_name('path')
                        source = path_node.text.decode().strip('"') if path_node else '(unknown)'
                        name_node = spec.child_by_field_name('name')
                        names = [name_node.text.decode()] if name_node else [source.rsplit('/', 1)[-1].rstrip('"')]
                        result.imports.append(ImportInfo(
                            source=source, names=names,
                            line=child.start_point[0] + 1
                        ))
                continue
            if t == 'function_declaration':
                name = extract_name(child)
                sig = trim_sig(child.text.decode())
                result.symbols.append(Symbol(
                    kind='function', name=name,
                    line=child.start_point[0] + 1, signature=sig,
                    exported=bool(name and name[0].isupper())
                ))
                continue
            if t == 'method_declaration':
                name = extract_name(child)
                sig = trim_sig(child.text.decode())
                recv = child.child_by_field_name('receiver')
                recv_type = ''
                if recv:
                    for rc in recv.named_children:
                        if rc.type == 'parameter_declaration':
                            tn = rc.child_by_field_name('type')
                            if tn:
                                recv_type = tn.text.decode().lstrip('*')
                full = f'{recv_type}.{name}' if recv_type else name
                result.symbols.append(Symbol(
                    kind='method', name=full,
                    line=child.start_point[0] + 1, signature=sig,
                    exported=bool(name and name[0].isupper())
                ))
                continue
            if t == 'type_declaration':
                for spec in child.named_children:
                    if spec.type == 'type_spec':
                        tname = extract_name(spec)
                        kind = 'type'
                        tn = spec.child_by_field_name('type')
                        if tn:
                            if tn.type == 'struct_type': kind = 'struct'
                            elif tn.type == 'interface_type': kind = 'interface'
                        result.symbols.append(Symbol(
                            kind=kind, name=tname,
                            line=child.start_point[0] + 1,
                            signature=trim_sig(spec.text.decode(), 100),
                            exported=bool(tname and tname[0].isupper())
                        ))
                continue
            if t in ('const_declaration', 'var_declaration'):
                for spec in child.named_children:
                    if spec.type in ('const_spec', 'var_spec'):
                        vname = extract_name(spec)
                        result.symbols.append(Symbol(
                            kind='const' if t == 'const_declaration' else 'var',
                            name=vname,
                            line=child.start_point[0] + 1,
                            signature=trim_sig(spec.text.decode(), 80),
                            exported=bool(vname and vname[0].isupper())
                        ))
                continue
            if child.named_children:
                walk(child)

    walk(tree.root_node)
    return result


# ═══════════════════════════════════════════
# Rust parser
# ═══════════════════════════════════════════

def parse_rs(filepath: str) -> 'FileSymbols | None':
    result, tree, source_bytes = _parse_with_lang(
        filepath, lambda: _load_lang('tree_sitter_rust', 'language'), 'rust')
    if result is None:
        return None

    def _has_pub(node) -> bool:
        return any(
            c.type == 'visibility_modifier'
            for c in node.children
        )

    def walk(node, inside_impl=False):
        for child in node.named_children:
            t = child.type
            if t == 'use_declaration':
                arg = child.child_by_field_name('argument')
                source = arg.text.decode() if arg else '(unknown)'
                names = [source.rsplit('::', 1)[-1]] if '::' in source else [source]
                result.imports.append(ImportInfo(
                    source=source, names=names,
                    line=child.start_point[0] + 1
                ))
                continue
            if t == 'function_item':
                if inside_impl:
                    continue  # handled by impl_item
                name = extract_name(child)
                result.symbols.append(Symbol(
                    kind='function', name=name,
                    line=child.start_point[0] + 1,
                    signature=trim_sig(child.text.decode()),
                    exported=_has_pub(child),
                    is_async=any(c.type == 'async' and not c.is_named for c in child.children)
                ))
                continue
            if t == 'struct_item':
                name = extract_name(child)
                result.symbols.append(Symbol(
                    kind='struct', name=name,
                    line=child.start_point[0] + 1,
                    signature=trim_sig(child.text.decode()),
                    exported=_has_pub(child)
                ))
                continue
            if t == 'enum_item':
                name = extract_name(child)
                result.symbols.append(Symbol(
                    kind='enum', name=name,
                    line=child.start_point[0] + 1,
                    signature=trim_sig(child.text.decode()),
                    exported=_has_pub(child)
                ))
                continue
            if t == 'trait_item':
                name = extract_name(child)
                result.symbols.append(Symbol(
                    kind='interface', name=name,
                    line=child.start_point[0] + 1,
                    signature=trim_sig(child.text.decode()),
                    exported=_has_pub(child)
                ))
                continue
            if t == 'impl_item':
                # Extract type name and methods
                type_node = child.child_by_field_name('type')
                type_name = type_node.text.decode() if type_node else '?'
                for ic in child.named_children:
                    if ic.type == 'function_item':
                        mname = extract_name(ic)
                        masync = any(c.type == 'async' and not c.is_named for c in ic.children)
                        result.symbols.append(Symbol(
                            kind='method', name=f'{type_name}::{mname}',
                            line=ic.start_point[0] + 1,
                            signature=trim_sig(ic.text.decode()),
                            exported=_has_pub(ic),
                            is_async=masync
                        ))
                continue
            if t == 'const_item':
                name = extract_name(child)
                result.symbols.append(Symbol(
                    kind='const', name=name,
                    line=child.start_point[0] + 1,
                    signature=trim_sig(child.text.decode(), 100),
                    exported=_has_pub(child)
                ))
                continue
            if t == 'static_item':
                name = extract_name(child)
                result.symbols.append(Symbol(
                    kind='const', name=name,
                    line=child.start_point[0] + 1,
                    signature=trim_sig(child.text.decode(), 100),
                    exported=_has_pub(child)
                ))
                continue
            if t == 'macro_definition':
                name = extract_name(child)
                result.symbols.append(Symbol(
                    kind='function', name=f'{name}!',
                    line=child.start_point[0] + 1,
                    signature=trim_sig(child.text.decode()),
                    exported=_has_pub(child)
                ))
                continue
            if t == 'mod_item':
                name = extract_name(child)
                result.symbols.append(Symbol(
                    kind='const', name=f'mod {name}',
                    line=child.start_point[0] + 1,
                    signature=trim_sig(child.text.decode()),
                    exported=_has_pub(child)
                ))
                continue
            if child.named_children:
                walk(child, inside_impl or t == 'impl_item')

    walk(tree.root_node)
    return result


# ═══════════════════════════════════════════
# Java parser
# ═══════════════════════════════════════════

def parse_java(filepath: str) -> 'FileSymbols | None':
    result, tree, source_bytes = _parse_with_lang(
        filepath, lambda: _load_lang('tree_sitter_java', 'language'), 'java')
    if result is None:
        return None

    def _has_modifier(node, mod: str) -> bool:
        for c in node.children:
            if c.type == 'modifiers':
                for mc in c.named_children:
                    if mc.text.decode() == mod:
                        return True
        return False

    def walk(node, inside_class=None):
        for child in node.named_children:
            t = child.type
            if t == 'import_declaration':
                source = ''
                is_star = False
                for c in child.named_children:
                    if c.type in ('identifier', 'scoped_identifier'):
                        source = c.text.decode()
                    elif c.type == 'asterisk':
                        is_star = True
                names = ['*'] if is_star else ([source.rsplit('.', 1)[-1]] if source else ['(import)'])
                result.imports.append(ImportInfo(
                    source=source or '(unknown)', names=names,
                    line=child.start_point[0] + 1
                ))
                continue
            if t == 'package_declaration':
                for c in child.named_children:
                    if c.type in ('identifier', 'scoped_identifier'):
                        result.imports.append(ImportInfo(
                            source=f'package {c.text.decode()}',
                            names=['(package)'],
                            line=child.start_point[0] + 1
                        ))
                continue
            if t == 'class_declaration':
                name = extract_name(child)
                exported = not _has_modifier(child, 'private')
                result.symbols.append(Symbol(
                    kind='class', name=name,
                    line=child.start_point[0] + 1,
                    signature=trim_sig(child.text.decode()),
                    exported=exported
                ))
                # Walk class body for methods/fields
                for cc in child.named_children:
                    if cc.type == 'class_body':
                        for m in cc.named_children:
                            if m.type == 'method_declaration':
                                mname = extract_name(m)
                                if mname != '?':
                                    result.symbols.append(Symbol(
                                        kind='method', name=f'{name}.{mname}',
                                        line=m.start_point[0] + 1,
                                        signature=trim_sig(m.text.decode()),
                                        exported=not _has_modifier(m, 'private')
                                    ))
                            elif m.type == 'constructor_declaration':
                                result.symbols.append(Symbol(
                                    kind='method', name=f'{name}.{name}',
                                    line=m.start_point[0] + 1,
                                    signature=trim_sig(m.text.decode()),
                                    exported=not _has_modifier(m, 'private')
                                ))
                            elif m.type == 'field_declaration':
                                for decl in m.named_children:
                                    if decl.type == 'variable_declarator':
                                        fname = extract_name(decl)
                                        result.symbols.append(Symbol(
                                            kind='var', name=f'{name}.{fname}',
                                            line=m.start_point[0] + 1,
                                            signature=trim_sig(decl.text.decode(), 80),
                                            exported=not _has_modifier(m, 'private')
                                        ))
                continue
            if t == 'interface_declaration':
                name = extract_name(child)
                exported = not _has_modifier(child, 'private')
                result.symbols.append(Symbol(
                    kind='interface', name=name,
                    line=child.start_point[0] + 1,
                    signature=trim_sig(child.text.decode()),
                    exported=exported
                ))
                continue
            if t == 'enum_declaration':
                name = extract_name(child)
                result.symbols.append(Symbol(
                    kind='enum', name=name,
                    line=child.start_point[0] + 1,
                    signature=trim_sig(child.text.decode()),
                    exported=not _has_modifier(child, 'private')
                ))
                continue
            if child.named_children:
                walk(child, inside_class)

    walk(tree.root_node)
    return result


# ═══════════════════════════════════════════
# Ruby parser
# ═══════════════════════════════════════════

def parse_rb(filepath: str) -> 'FileSymbols | None':
    result, tree, source_bytes = _parse_with_lang(
        filepath, lambda: _load_lang('tree_sitter_ruby', 'language'), 'ruby')
    if result is None:
        return None

    def walk(node, inside_class=None):
        for child in node.named_children:
            t = child.type
            if t == 'call':
                # require 'foo', include Bar, etc.
                method_node = child.child_by_field_name('method')
                if method_node:
                    mname = method_node.text.decode()
                    if mname in ('require', 'require_relative', 'load', 'autoload'):
                        args = child.child_by_field_name('arguments')
                        if args:
                            for a in args.named_children:
                                if a.type == 'string':
                                    src = a.text.decode().strip("'\"")
                                    result.imports.append(ImportInfo(
                                        source=src, names=['(require)'],
                                        line=child.start_point[0] + 1
                                    ))
                continue
            if t == 'class':
                name_node = child.child_by_field_name('name')
                if name_node:
                    cname = name_node.text.decode()
                    full = f'{inside_class}::{cname}' if inside_class else cname
                    result.symbols.append(Symbol(
                        kind='class', name=full,
                        line=child.start_point[0] + 1,
                        signature=trim_sig(child.text.decode())
                    ))
                    # Walk body for methods and nested classes
                    body = child.child_by_field_name('body')
                    if body:
                        walk(body, inside_class=full)
                continue
            if t == 'module':
                name_node = child.child_by_field_name('name')
                if name_node:
                    mname = name_node.text.decode()
                    full = f'{inside_class}::{mname}' if inside_class else mname
                    result.symbols.append(Symbol(
                        kind='class', name=full,
                        line=child.start_point[0] + 1,
                        signature=trim_sig(child.text.decode())
                    ))
                    # Walk body for nested classes/methods
                    body = child.child_by_field_name('body')
                    if body:
                        walk(body, inside_class=full)
                continue
            if t == 'method':
                name = extract_name(child)
                full = f'{inside_class}#{name}' if inside_class else name
                result.symbols.append(Symbol(
                    kind='method' if inside_class else 'function',
                    name=full,
                    line=child.start_point[0] + 1,
                    signature=trim_sig(child.text.decode())
                ))
                continue
            if t == 'singleton_method':
                name = extract_name(child)
                full = f'{inside_class}.{name}' if inside_class else name
                result.symbols.append(Symbol(
                    kind='method', name=full,
                    line=child.start_point[0] + 1,
                    signature=trim_sig(child.text.decode())
                ))
                continue
            if child.named_children:
                walk(child, inside_class)

    walk(tree.root_node)
    return result


# ═══════════════════════════════════════════
# Dispatch
# ═══════════════════════════════════════════

def parse_file(filepath: str) -> 'FileSymbols | None':
    ext = Path(filepath).suffix
    if ext in ('.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs'):
        return parse_ts(filepath, ext)
    elif ext == '.py':
        return parse_py(filepath)
    elif ext == '.css':
        return parse_css(filepath)
    elif ext == '.go':
        return parse_go(filepath)
    elif ext == '.rs':
        return parse_rs(filepath)
    elif ext == '.java':
        return parse_java(filepath)
    elif ext == '.rb':
        return parse_rb(filepath)
    return None


SKIP_DIRS = {
    'node_modules', '.next', '.git', '.svn', '.hg',
    'dist', 'build', '__pycache__', '.venv', 'venv',
    '.turbo', 'coverage', '.nyc_output', '.tox',
}

def collect_files(root: Path) -> list[Path]:
    extensions = tuple(LANG_MAP.keys())
    gitignore = _gitignore_patterns(root)
    files = []
    for fp in root.rglob('*'):
        if fp.name.startswith('.'):
            continue
        # Skip known large directories early
        if any(p.name in SKIP_DIRS for p in fp.parents):
            continue
        if fp.suffix not in extensions:
            continue
        try:
            rel = str(fp.relative_to(root))
        except ValueError:
            continue
        if gitignore and _match_gitignore(rel, gitignore):
            continue
        files.append(fp)
    return files


# ═══════════════════════════════════════════
# 1. Reverse reference index
# ═══════════════════════════════════════════

def build_reverse_refs(files: list[FileSymbols], root: Path) -> dict[str, list[tuple[str, int, str]]]:
    file_set: dict[str, str] = {}
    for f in files:
        p = Path(f.relpath)
        key_noext = str(p.with_suffix('')).replace('\\', '/')
        file_set[key_noext] = f.relpath
        file_set[f.relpath.replace('\\', '/')] = f.relpath
        if p.name.startswith('index.'):
            file_set[str(p.parent).replace('\\', '/')] = f.relpath

    refs: dict[str, list[tuple[str, int, str]]] = defaultdict(list)
    for f in files:
        dir_of_file = str(Path(f.relpath).parent).replace('\\', '/')
        for imp in f.imports:
            src = imp.source
            if not src or src == '(unknown)':
                continue
            if not (src.startswith('.') or src.startswith('/') or src.startswith('@')):
                continue
            target = None
            if src.startswith('.'):
                resolved = os.path.normpath(os.path.join(dir_of_file, src)).replace('\\', '/')
                target = file_set.get(resolved)
                if not target:
                    for ext in LANG_MAP:
                        t = file_set.get(resolved + ext)
                        if t:
                            target = t
                            break
                if not target:
                    for ext in LANG_MAP:
                        t = file_set.get(resolved + '/index' + ext)
                        if t:
                            target = t
                            break
            elif src.startswith('@'):
                clean = (src[2:] if src.startswith('@/') else src[1:]).replace('\\', '/')
                target = file_set.get(clean)
                if not target:
                    for ext in LANG_MAP:
                        t = file_set.get(clean + ext)
                        if t:
                            target = t
                            break
                if not target:
                    for ext in LANG_MAP:
                        t = file_set.get(clean + '/index' + ext)
                        if t:
                            target = t
                            break
            if target and target != f.relpath:
                refs[target].append((f.relpath, imp.line, ', '.join(imp.names)))
    return dict(refs)


# ═══════════════════════════════════════════
# ═══════════════════════════════════════════
# Call-graph analysis
# ═══════════════════════════════════════════

def _resolve_import_source(imp_source: str, from_relpath: str,
                           file_index: dict[str, 'FileSymbols'],
                           path_aliases: dict[str, str]) -> str | None:
    """Resolve an import source to an actual file path within the project.
    Handles relative paths (./foo), path aliases (@/foo), and extension-less completion.
    """
    import os
    dir_of = str(Path(from_relpath).parent).replace('\\', '/')

    # Handle path aliases
    for alias, prefix in path_aliases.items():
        if imp_source.startswith(alias):
            clean = imp_source[len(alias):].replace('\\', '/')
            resolved = (prefix + clean).lstrip('/')
            if resolved in file_index:
                return resolved
            for ext in LANG_MAP:
                if (resolved + ext) in file_index:
                    return resolved + ext
                if (resolved + '/index' + ext) in file_index:
                    return resolved + '/index' + ext
            return None

    # Handle relative paths
    if imp_source.startswith('.'):
        resolved = os.path.normpath(os.path.join(dir_of, imp_source)).replace('\\', '/')
        if resolved in file_index:
            return resolved
        for ext in LANG_MAP:
            if (resolved + ext) in file_index:
                return resolved + ext
            if (resolved + '/index' + ext) in file_index:
                return resolved + '/index' + ext

    return None


def build_call_graph(files: list[FileSymbols],
                     config: 'CallGraphConfig') -> list[dict]:
    """Resolve cross-file call relationships, returning a structured call graph.
    Each record: {caller_file, caller_func, callee_expr, target_file, target_func, line}
    Only retains records where target_file is non-None (i.e. cross-file calls).
    """
    # Build file index: relpath → FileSymbols
    file_index: dict[str, FileSymbols] = {}
    for f in files:
        file_index[f.relpath] = f
        # Also index by extensionless key
        p = Path(f.relpath)
        key_noext = str(p.with_suffix('')).replace('\\', '/')
        if key_noext not in file_index:
            file_index[key_noext] = f

    results = []
    for f in files:
        # Build import map: {imported_name: resolved_file}
        import_map: dict[str, str] = {}
        for imp in f.imports:
            src = imp.source
            if not src or src == '(unknown)':
                continue
            # Only process intra-project imports
            if not (src.startswith('.') or
                    any(src.startswith(a) for a in config.path_aliases)):
                continue
            target = _resolve_import_source(src, f.relpath, file_index,
                                            config.path_aliases)
            if target:
                for name in imp.names:
                    clean_name = name
                    if ' as ' in name:
                        clean_name = name.split(' as ')[-1]
                    import_map[clean_name] = target

        # Resolve each call
        for call in f.calls:
            parts = call.callee_name.split('.')
            base = parts[0]
            method = parts[1] if len(parts) > 1 else None

            target_file = import_map.get(base)
            if not target_file:
                continue

            target_func = method  # If namespace call, method is the specific function
            if not method:
                target_func = base  # Directly imported function

            # Verify target function actually exists in the target file
            target_fs = file_index.get(target_file)
            if target_fs and target_func:
                found = any(
                    (s.name == target_func and (s.exported or s.default_export))
                    for s in target_fs.symbols
                )
                if found:
                    results.append({
                        'caller_file': f.relpath,
                        'caller_func': call.caller_func,
                        'callee_expr': call.callee_name,
                        'target_file': target_file,
                        'target_func': target_func,
                        'line': call.line,
                    })

    return results


# ═══════════════════════════════════════════
# Framework detection
# ═══════════════════════════════════════════

def _detect_framework(files: list[FileSymbols], root: Path) -> str:
    """Auto-detect the project framework, returning a key into FRAMEWORK_CONFIGS."""
    # Config-file feature detection
    for fw, markers in [
        ('nextjs', ['next.config.ts', 'next.config.js', 'next.config.mjs']),
        ('nuxt',   ['nuxt.config.ts', 'nuxt.config.js']),
    ]:
        if any((root / m).exists() for m in markers):
            return fw
    # Go detection
    if (root / 'go.mod').exists():
        return 'go'
    # Rust detection
    if (root / 'Cargo.toml').exists():
        return 'rust'
    # Java detection
    if any((root / m).exists() for m in ['pom.xml', 'build.gradle', 'build.gradle.kts']):
        return 'java'
    # Ruby detection
    if (root / 'Gemfile').exists():
        return 'ruby'
    # Language ratio heuristic
    if files:
        py_cnt = sum(1 for f in files if f.language == 'python')
        if py_cnt > len(files) * 0.5:
            return 'python'
    return 'generic'


# ═══════════════════════════════════════════
# 5. Entry-point detection
# ═══════════════════════════════════════════

def _entry_rule_matches(f: FileSymbols, rule: EntryRule, root: Path) -> bool:
    """Evaluate whether FileSymbols matches a single EntryRule."""
    if rule.filenames and Path(f.relpath).name not in rule.filenames:
        return False
    if rule.path_starts and not f.relpath.startswith(rule.path_starts):
        return False
    if rule.path_contains and rule.path_contains not in f.relpath:
        return False
    if rule.root_only and str(Path(f.relpath).parent) != '.':
        return False
    if rule.language and f.language != rule.language:
        return False
    if rule.content_regex is not None:
        try:
            if not re.search(rule.content_regex, (root / f.relpath).read_text()):
                return False
        except Exception:
            return False
    return True


def _detect_entries(files: list[FileSymbols], root: Path,
                    config: FrameworkConfig) -> None:
    for f in files:
        for rule in config.entry_rules:
            if _entry_rule_matches(f, rule, root):
                f.is_entry = True
                f.entry_reason = rule.reason
                break
    # package.json main/module/exports
    if config.check_package_json:
        pkg_json = root / 'package.json'
        if pkg_json.exists():
            try:
                pkg = json.loads(pkg_json.read_text())
                main_file = pkg.get('main') or pkg.get('module') or ''
                if isinstance(pkg.get('exports'), dict):
                    dot = pkg['exports'].get('.', {})
                    main_file = dot.get('import', main_file) if isinstance(dot, dict) else main_file
                if main_file:
                    main_rel = main_file.lstrip('./')
                    for f in files:
                        if f.relpath in (main_rel, main_rel + '.ts', main_rel + '.tsx'):
                            f.is_entry = True
                            f.entry_reason = 'package.json main'
            except Exception:
                pass


# ═══════════════════════════════════════════
# Incremental update
# ═══════════════════════════════════════════

def load_cache(root: Path) -> dict:
    cache_path = root / CACHE_FILE
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text())
        except Exception:
            pass
    return {'version': 1, 'fingerprints': {}, 'files': {}}

def save_cache(root: Path, cache: dict):
    (root / CACHE_FILE).write_text(json.dumps(cache, indent=2, ensure_ascii=False))

def _diff_symbols(old_syms: list[dict], new_syms: list[dict]) -> tuple[list[str], list[str]]:
    old_set = {s['name'] for s in old_syms}
    new_set = {s['name'] for s in new_syms}
    return sorted(new_set - old_set), sorted(old_set - new_set)

def scan_incremental(root: Path) -> tuple[list[FileSymbols], dict]:
    all_files = collect_files(root)
    cache = load_cache(root)
    old_fps = cache.get('fingerprints', {})
    old_data = cache.get('files', {})
    new_fps = {}
    changed_fps = []
    unchanged_rels = []

    for fp in all_files:
        rel = str(fp.relative_to(root))
        fp_hash = file_fingerprint(fp)
        new_fps[rel] = fp_hash
        if rel in old_fps and old_fps[rel] == fp_hash and rel in old_data:
            unchanged_rels.append(rel)
        else:
            changed_fps.append(fp)

    deleted = set(old_fps.keys()) - set(new_fps.keys())
    changes = {'deleted': list(deleted), 'changed': [], 'unchanged': len(unchanged_rels)}

    if deleted:
        print(f"  🗑️  {len(deleted)} file(s) removed")
        for d in sorted(deleted):
            print(f"      - {d}")

    if changed_fps:
        print(f"  🔄 {len(changed_fps)} file(s) changed, re-parsing...")
    else:
        print(f"  ✅ All {len(unchanged_rels)} file(s) unchanged — using cache")

    results = []
    for i, fp in enumerate(changed_fps):
        rel = str(fp.relative_to(root))
        print(f"    [{i+1}/{len(changed_fps)}] {rel}", end='')
        fs = parse_file(str(fp))
        if fs:
            fs.relpath = rel
            results.append(fs)
            old_data[rel] = fs.to_cache()
            old_syms = cache.get('files', {}).get(rel, {}).get('symbols', [])
            new_syms = [asdict(s) for s in fs.symbols]
            added, removed = _diff_symbols(old_syms, new_syms)
            if added or removed:
                info = []
                if added: info.append(f"+{len(added)}")
                if removed: info.append(f"-{len(removed)}")
                print(f"  [{', '.join(info)} symbols]")
                changes['changed'].append({
                    'file': rel,
                    'added': added,
                    'removed': removed,
                })
            else:
                print()
        else:
            print(f"  ⚠️  parse error")
            if rel in old_data:
                results.append(FileSymbols.from_cache(old_data[rel]))

    for rel in unchanged_rels:
        if rel in old_data:
            results.append(FileSymbols.from_cache(old_data[rel]))

    cache['fingerprints'] = new_fps
    cache['files'] = {}
    for f in results:
        cache['files'][f.relpath] = f.to_cache()
    cache['last_scan'] = datetime.now().isoformat()
    save_cache(root, cache)

    return results, changes

def scan_full(root: Path) -> tuple[list[FileSymbols], dict]:
    files = collect_files(root)
    results = []
    for i, fp in enumerate(files):
        rel = str(fp.relative_to(root))
        print(f"  [{i+1}/{len(files)}] {rel}")
        fs = parse_file(str(fp))
        if fs:
            fs.relpath = rel
            results.append(fs)
        else:
            print(f"    ⚠️  parse error")

    cache = {'version': 1, 'fingerprints': {}, 'files': {}, 'last_scan': ''}
    for f in results:
        abs_path = root / f.relpath
        if abs_path.exists():
            cache['fingerprints'][f.relpath] = file_fingerprint(abs_path)
        cache['files'][f.relpath] = f.to_cache()
    cache['last_scan'] = datetime.now().isoformat()
    save_cache(root, cache)

    return results, {}


# ═══════════════════════════════════════════
# Markdown output
# ═══════════════════════════════════════════

KIND_ICON = {
    'function': '🔧', 'method': '▪️', 'class': '📦',
    'interface': '📋', 'type': '🏷️', 'enum': '🔢',
    'const': '📌', 'var': '✏️',
}

KIND_ZH = {
    'function': 'function', 'method': 'method', 'class': 'class',
    'interface': 'interface', 'type': 'type', 'enum': 'enum',
    'const': 'const', 'var': 'var',
}
# Pre-computed reverse lookup: kind → icon
KIND_TO_ICON = {kind: KIND_ICON.get(kind, '❓') for kind in KIND_ZH}


def _match_category_rule(relpath: str, kind: str | None, rules: list[CategoryRule],
                         default: str) -> str:
    """Generic rule matcher: iterate rules by descending priority, return first match.
    Unified helper replacing duplicated match loops across _categorize_symbol, _cat_for_file,
    and global-symbol-index categorization."""
    for rule in sorted(rules, key=lambda r: -(r.priority or 0)):
        if rule.path_starts and not relpath.startswith(rule.path_starts):
            continue
        if rule.path_ends and not relpath.endswith(rule.path_ends):
            continue
        if rule.path_contains and rule.path_contains not in relpath:
            continue
        if rule.kind_in and kind and kind not in rule.kind_in:
            continue
        if rule.exact_files and relpath not in rule.exact_files:
            continue
        if rule.category_by_kind and kind and kind in rule.category_by_kind:
            return rule.category_by_kind[kind]
        return rule.category
    return default

def _build_dynamic_category_rules(files: list[FileSymbols],
                                   config: FrameworkConfig) -> list[CategoryRule]:
    """Dynamically generate CategoryRules based on the project's actual top-level directories.
    Directory names are used directly as category names (e.g. service/ → category 'service/').
    No mapping — whatever directories exist in the project become categories."""
    # Collect all top-level directory names
    top_dirs: set[str] = set()
    for f in files:
        parts = Path(f.relpath).parts
        if parts:
            top_dirs.add(parts[0])

    # Exclude directories already handled by framework rules (app/ handled by Next.js rules)
    skip = {'app', 'pages', 'src'}  # Framework-convention directories, categorized by framework rules

    rules = []
    priority = 75
    for d in sorted(top_dirs):
        if d in skip:
            continue
        # Match both top-level (lib/foo.ts) and nested (/lib/ or app/lib/) paths
        rules.append(CategoryRule(
            path_starts=d + '/', category=d + '/', priority=priority))
        rules.append(CategoryRule(
            path_contains='/' + d + '/', category=d + '/', priority=priority - 1))
        priority -= 2

    return rules


def _categorize_symbol(relpath: str, kind: str,
                       config: FrameworkConfig) -> str:
    """Categorize a symbol by framework rules, returning a category name."""
    return _match_category_rule(relpath, kind, config.category_rules,
                                config.category_fallback)


def _cat_for_file(relpath: str, all_rules: list[CategoryRule],
                  fallback: str, cache: dict[str, str] | None = None) -> str:
    """Categorize a file using merged rules. Uses _match_category_rule internally."""
    if cache is not None and relpath in cache:
        return cache[relpath]
    cat = _match_category_rule(relpath, None, all_rules, fallback)
    if cache is not None:
        cache[relpath] = cat
    return cat


# ── Section helpers (split from generate_markdown) ──

def _section_header(root: Path, files: list[FileSymbols],
                    total_syms: int, total_imports: int,
                    changes: dict | None) -> list[str]:
    out = [f"# {root.name}", ""]
    summary = f"> 🗂️ {len(files)} files  ·  {total_syms} symbols  ·  {total_imports} imports"
    if changes and changes.get('changed'):
        summary += f"  ·  🔄 {len(changes['changed'])} files changed"
    out.append(summary)
    scan_time = datetime.now().strftime('%Y-%m-%d %H:%M')
    try:
        git_hash = subprocess.check_output(
            ['git', 'rev-parse', '--short', 'HEAD'],
            cwd=root, stderr=subprocess.DEVNULL
        ).decode().strip()
        out.append(f"> 📅 {scan_time}  ·  commit `{git_hash}`")
    except Exception:
        out.append(f"> 📅 {scan_time}")
    out.append("")
    return out


def _section_recent_changes(changes: dict | None) -> list[str]:
    out = []
    if changes and changes.get('changed'):
        recent = []
        for c in changes['changed'][:12]:
            parts = []
            if c.get('added'): parts.append(f"+{len(c['added'])}")
            if c.get('removed'): parts.append(f"−{len(c['removed'])}")
            recent.append(f"`{c['file']}` ({' '.join(parts)})")
        if recent:
            out.append("## 🔄 Recent changes")
            out.append("")
            for r in recent:
                out.append(f"- {r}")
            out.append("")
    return out


def _section_duplicates(files: list[FileSymbols],
                        config: FrameworkConfig) -> list[str]:
    out = []
    name_to_files: dict[str, list[str]] = defaultdict(list)
    for f in files:
        for s in f.symbols:
            name_to_files[s.name].append(f.relpath)

    def _is_convention_dupe(name: str, file_list: list[str]) -> bool:
        for rule in config.convention.rules:
            if name in rule.names:
                if all(any(f.endswith(s) for s in rule.file_suffixes)
                       for f in file_list):
                    return True
        return False

    dupes = [(n, fs) for n, fs in name_to_files.items()
             if len(fs) > 1 and not _is_convention_dupe(n, fs)]
    if dupes:
        out.append("## ⚠️ Duplicate symbols")
        out.append("")
        out.append("| Symbol | Locations |")
        out.append("|------|---------|")
        for name, file_list in sorted(dupes):
            locs = ' · '.join(f'`{fp}`' for fp in file_list)
            out.append(f"| `{name}` | {locs} |")
        out.append("")
    return out


def _section_dead_code(files: list[FileSymbols],
                       config: FrameworkConfig) -> list[str]:
    out = []
    all_convention_names = {n for rule in config.convention.rules
                            for n in rule.names}
    all_convention_suffixes = list({s for rule in config.convention.rules
                                    for s in rule.file_suffixes})

    def _has_convention_suffix(relpath: str) -> bool:
        return any(relpath.endswith(s) for s in all_convention_suffixes)

    imported_names: set[str] = set()
    for f in files:
        for imp in f.imports:
            if not imp.source or imp.source == '(unknown)':
                continue
            if not (imp.source.startswith('.') or imp.source.startswith('/')
                    or imp.source.startswith('@')):
                continue
            for n in imp.names:
                imported_names.add(n)
                if ' as ' in n:
                    imported_names.add(n.split(' as ')[-1])

    dead: list[tuple[str, str, int]] = []
    for f in files:
        for s in f.symbols:
            if not s.exported:
                continue
            if s.default_export and f.is_entry:
                continue
            if s.default_export:
                fname = Path(f.relpath).name
                if fname in config.convention.always_convention_files:
                    continue
                continue
            if s.name in all_convention_names and _has_convention_suffix(f.relpath):
                continue
            if Path(f.relpath).name in config.convention.always_convention_files:
                continue
            if s.name not in imported_names:
                dead.append((s.name, f.relpath, s.line))
    if dead:
        out.append("## 🫥 Unused code")
        out.append("")
        out.append("| Symbol | File:line |")
        out.append("|------|---------|")
        for name, relpath, line in sorted(dead):
            out.append(f"| `{name}` | `{relpath}:{line}` |")
        out.append("")
    return out


def _section_external_deps(files: list[FileSymbols]) -> list[str]:
    out = []
    ext_deps: dict[str, set[str]] = defaultdict(set)
    for f in files:
        for imp in f.imports:
            src = imp.source
            if not src or src == '(unknown)':
                continue
            if src.startswith('.') or src.startswith('/') or src.startswith('@'):
                continue
            if src.startswith('@'):
                parts = src.split('/')
                pkg = '/'.join(parts[:2]) if len(parts) >= 2 else src
            else:
                pkg = src.split('/')[0]
            ext_deps[pkg].add(f.relpath)
    if ext_deps:
        out.append("## 📦 External dependencies")
        out.append("")
        out.append("| Package | Files importing |")
        out.append("|------|---------|")
        for pkg in sorted(ext_deps.keys(), key=lambda k: len(ext_deps[k]),
                          reverse=True):
            out.append(f"| `{pkg}` | {len(ext_deps[pkg])} |")
        out.append("")
    return out


def _section_hotspots(files: list[FileSymbols]) -> list[str]:
    out = []
    top_by_lines = sorted(files, key=lambda f: f.lines, reverse=True)[:10]
    if top_by_lines:
        out.append("## 🔥 Complexity hotspots")
        out.append("")
        out.append("| File | Lines | Symbols |")
        out.append("|------|------|--------|")
        for f in top_by_lines:
            out.append(f"| `{f.relpath}` | {f.lines} | {len(f.symbols)} |")
        out.append("")
    return out


def _section_module_dep(refs: dict) -> list[str]:
    out = []
    if not refs:
        return out
    dep_counts = sorted(
        ((t, len(set(s for s, _, _ in r))) for t, r in refs.items()),
        key=lambda x: x[1], reverse=True
    )[:10]
    if dep_counts:
        out.append("## 🔗 Most-depended-on modules")
        out.append("")
        out.append("| File | Times referenced |")
        out.append("|------|-----------|")
        for target, count in dep_counts:
            if count > 0:
                out.append(f"| `{target}` | {count} |")
        out.append("")
    return out


def _section_api_routes(files: list[FileSymbols],
                        config: FrameworkConfig) -> list[str]:
    out = []
    if config.api_route is None:
        return out
    ac = config.api_route
    route_files = [f for f in files
                   if ac.path_contains in f.relpath
                   and any(f.relpath.endswith(s) for s in ac.file_suffixes)]
    if not route_files:
        return out
    out.append("## 🚪 API route index")
    out.append("")
    out.append("| Method | Path | Auth | Lines |")
    out.append("|------|------|------|------|")
    for f in sorted(route_files, key=lambda x: x.relpath):
        route_path = '/' + f.relpath.replace(ac.path_prefix_strip, '', 1) \
                                     .rsplit(ac.path_suffix_split, 1)[0]
        methods = [s.name for s in f.symbols
                   if s.exported and s.name in ac.http_methods]
        if not methods:
            methods = ['POST']
        has_auth = any('getServerSession' in imp.names for imp in f.imports)
        auth_icon = '✅' if has_auth else '❌'
        out.append(
            f"| {', '.join(sorted(methods))} | `{route_path}` | {auth_icon} | {f.lines} |"
        )
    out.append("")
    return out


def _output_call_graph_group(caller_cat: str, target_cat: str,
                              calls: list[dict], out: list[str]):
    """Output a single call-graph group table."""
    out.append(f"### {caller_cat} → {target_cat}")
    out.append("")
    out.append("| Caller | Callee | Target file |")
    out.append("|--------|--------|----------|")
    seen = set()
    for cg in sorted(calls, key=lambda x: (x['caller_file'], x['caller_func'])):
        dedup_key = (cg['caller_file'], cg['caller_func'], cg['target_func'])
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        caller_label = f"`{cg['caller_file']}` → `{cg['caller_func']}()`"
        out.append(
            f"| {caller_label} | `{cg['target_func']}()` "
            f"| `{cg['target_file']}` |"
        )
    out.append("")


def _section_call_graph(files: list[FileSymbols],
                        config: FrameworkConfig) -> list[str]:
    out = []
    if not config.call_graph or not config.call_graph.enabled:
        return out
    call_graph = build_call_graph(files, config.call_graph)
    if not call_graph:
        return out

    out.append("## 🔗 Call graph")
    out.append("")

    cat_all_rules = _build_dynamic_category_rules(files, config) + config.category_rules
    cat_cache: dict[str, str] = {}

    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for cg in call_graph:
        caller_cat = _cat_for_file(cg['caller_file'], cat_all_rules,
                                   config.category_fallback, cat_cache)
        target_cat = _cat_for_file(cg['target_file'], cat_all_rules,
                                   config.category_fallback, cat_cache)
        grouped[(caller_cat, target_cat)].append(cg)

    # Output: meaningful category pairs first, then remaining groups alphabetically
    priority_pairs = [
        # Common API → service/lib/db patterns
        (r'^🚪 API routes$', r'^service/'),
        (r'^🚪 API routes$', r'^lib/'),
        (r'^🚪 API routes$', r'^db/'),
        (r'^service/', r'^lib/'),
        (r'^service/', r'^db/'),
    ]
    shown = set()
    for caller_pat, target_pat in priority_pairs:
        for (caller_cat, target_cat), calls in sorted(grouped.items()):
            if (caller_cat, target_cat) in shown:
                continue
            if re.search(caller_pat, caller_cat) and re.search(target_pat, target_cat):
                shown.add((caller_cat, target_cat))
                _output_call_graph_group(caller_cat, target_cat, calls, out)

    for (caller_cat, target_cat), calls in sorted(grouped.items()):
        if (caller_cat, target_cat) in shown:
            continue
        shown.add((caller_cat, target_cat))
        _output_call_graph_group(caller_cat, target_cat, calls, out)

    # Most-called functions
    target_counts: dict[str, list[str]] = defaultdict(list)
    for cg in call_graph:
        key = f"{cg['target_file']}::{cg['target_func']}"
        caller = f"{cg['caller_file']}::{cg['caller_func']}"
        if caller not in target_counts[key]:
            target_counts[key].append(caller)

    if target_counts:
        top_targets = sorted(target_counts.items(),
                            key=lambda x: len(x[1]), reverse=True)[:10]
        out.append("### 📊 Most-called functions")
        out.append("")
        out.append("| Function | Call count | Callers |")
        out.append("|------|-----------|--------|")
        for target_key, callers in top_targets:
            file, func = target_key.split('::', 1)
            caller_list = ', '.join(
                f'`{c.split("::", 1)[0]}`' for c in callers[:5]
            )
            if len(callers) > 5:
                caller_list += f' ... (+{len(callers) - 5})'
            out.append(
                f"| `{func}()` `{file}` | {len(callers)} | {caller_list} |"
            )
        out.append("")

    return out


def _output_symbol_category(cat: str, items: list[tuple], out: list[str],
                            all_convention_names: set[str],
                            all_convention_suffixes: list[str]):
    """Output a single category in the global symbol index."""
    out.append(f"### {cat}")
    out.append("")
    out.append("| Symbol | Kind | File:line |")
    out.append("|------|------|---------|")
    seen_names: set[str] = set()
    for _, name, kind, relpath, line in items:
        if name in seen_names:
            continue
        seen_names.add(name)
        siblings = [it for it in items if it[1] == name]
        if len(siblings) > 1 and name in all_convention_names:
            all_conv = all(
                any(it[3].endswith(s) for s in all_convention_suffixes)
                for it in siblings
            )
            if all_conv:
                icon = KIND_TO_ICON.get(kind, '❓')
                out.append(
                    f"| `{name}` (×{len(siblings)}) | {icon} {kind} | {len(siblings)} files |"
                )
                continue
        icon = KIND_TO_ICON.get(kind, '❓')
        out.append(f"| `{name}` | {icon} {kind} | `{relpath}:{line}` |")
    out.append("")


def _section_global_symbol_index(files: list[FileSymbols],
                                 config: FrameworkConfig) -> list[str]:
    out = ["## 🔍 Global symbol index", ""]

    all_convention_names = {n for rule in config.convention.rules
                            for n in rule.names}
    all_convention_suffixes = list({s for rule in config.convention.rules
                                    for s in rule.file_suffixes})

    dynamic_rules = _build_dynamic_category_rules(files, config)
    all_rules = dynamic_rules + config.category_rules

    categorized: dict[str, list[tuple]] = defaultdict(list)
    for f in files:
        for s in f.symbols:
            cat = _match_category_rule(f.relpath, s.kind, all_rules,
                                       config.category_fallback)
            categorized[cat].append((s.name.lower(), s.name,
                                     KIND_ZH.get(s.kind, s.kind),
                                     f.relpath, s.line))

    rendered_cats: set[str] = set()
    for cat in config.category_order:
        items = categorized.get(cat)
        if not items:
            continue
        rendered_cats.add(cat)
        items.sort()
        _output_symbol_category(cat, items, out, all_convention_names,
                                all_convention_suffixes)

    for cat in sorted(categorized.keys()):
        if cat in rendered_cats:
            continue
        items = categorized[cat]
        items.sort()
        rendered_cats.add(cat)
        _output_symbol_category(cat, items, out, all_convention_names,
                                all_convention_suffixes)

    return out


def _section_file_tree(files: list[FileSymbols]) -> list[str]:
    out = ["## 📁 File tree", ""]
    dirs = defaultdict(list)
    for f in files:
        dirs[str(Path(f.relpath).parent)].append(f)

    for dirname in sorted(dirs.keys()):
        label = '(root)' if dirname == '.' else f'{dirname}/'
        out.append(f"### {label}")
        out.append("")
        out.append("| File | Language | Lines | Symbols |")
        out.append("|------|------|----|------|")
        for f in sorted(dirs[dirname], key=lambda x: Path(x.relpath).name):
            fname = Path(f.relpath).name
            marker = ' 🚪' if f.is_entry else ''
            out.append(
                f"| `{fname}`{marker} | {f.language} | {f.lines} | {len(f.symbols)} |"
            )
        out.append("")
    return out


def _section_entry_list(files: list[FileSymbols]) -> list[str]:
    out = []
    entries = [f for f in files if f.is_entry]
    if entries:
        out.append("> 🚪 Entry points: ")
        for f in sorted(entries, key=lambda x: x.relpath):
            out.append(f"> - `{f.relpath}` — {f.entry_reason}")
        out.append("")
    return out


def _section_per_file_detail(files: list[FileSymbols],
                             refs: dict) -> list[str]:
    out = ["## 📑 Per-file symbol detail", ""]
    for f in sorted(files, key=lambda x: x.relpath):
        if not f.symbols and not f.imports:
            continue
        entry_tag = ' 🚪' if f.is_entry else ''
        out.append(f"### `{f.relpath}`{entry_tag}")
        out.append(f"_{f.lines} lines · {f.language}_")
        if f.entry_reason:
            out.append(f"_Entry: {f.entry_reason}_")
        out.append("")

        if f.imports:
            out.append("**📥 Imports:**")
            for imp in f.imports:
                names = ', '.join(f'`{n}`' for n in imp.names)
                out.append(f"- L{imp.line}: {names} ← `{imp.source}`")
            out.append("")

        frefs = refs.get(f.relpath, [])
        if frefs:
            out.append("**📤 Referenced by:**")
            for ref_file, ref_line, ref_names in frefs:
                out.append(f"- `{ref_file}:{ref_line}` ({ref_names})")
            out.append("")

        if f.symbols:
            out.append("| Kind | Name | Line | Marks | Signature |")
            out.append("|------|------|----|------|------|")
            for s in f.symbols:
                tags = []
                if s.exported: tags.append('export')
                if s.default_export: tags.append('default')
                if s.is_async: tags.append('async')
                icon = KIND_ICON.get(s.kind, '❓')
                label = KIND_ZH.get(s.kind, s.kind)
                tagstr = ' '.join(f'`{t}`' for t in tags) if tags else '-'
                out.append(
                    f"| {icon} {label} | `{s.name}` | {s.line} | {tagstr} | `{s.signature}` |"
                )
            out.append("")
        out.append("---")
        out.append("")
    return out


def generate_markdown(files: list[FileSymbols], root: Path,
                      refs: dict, config: FrameworkConfig,
                      changes: dict = None,
                      compact: bool = False) -> str:
    total_syms = sum(len(f.symbols) for f in files)
    total_imports = sum(len(f.imports) for f in files)

    out = []
    out += _section_header(root, files, total_syms, total_imports, changes)
    out += _section_recent_changes(changes)
    out += _section_duplicates(files, config)
    out += _section_dead_code(files, config)
    out += _section_external_deps(files)
    out += _section_hotspots(files)
    out += _section_module_dep(refs)
    out += _section_api_routes(files, config)
    out += _section_call_graph(files, config)
    out += _section_global_symbol_index(files, config)
    out += _section_file_tree(files)
    out += _section_entry_list(files)
    out.append("---")
    out.append("")

    if not compact:
        out += _section_per_file_detail(files, refs)

    return '\n'.join(out)


# ═══════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 extract.py <project_dir> [--full] [--json] [--detail]")
        print("  Default: incremental (use cache) + compact output")
        print("  --full:    force full rescan")
        print("  --json:    output JSON instead of markdown")
        print("  --detail:  include per-file symbol details (full output, ~4x larger)")
        sys.exit(1)

    root = Path(sys.argv[1]).resolve()
    if not root.is_dir():
        print(f"❌ Not a directory: {root}")
        sys.exit(1)

    full = '--full' in sys.argv
    json_out = '--json' in sys.argv
    compact = '--detail' not in sys.argv  # compact by default, --detail for full per-file detail

    missing = []
    for ext in LANG_MAP:
        try:
            _load_lang(*LANG_MAP[ext])
        except ImportError:
            missing.append(LANG_MAP[ext][0])
    if missing:
        print("⚠️  Missing packages, install with:")
        for m in missing:
            print(f"  pip3 install {m.replace('_', '-')}")

    print(f"🔍 {root.name}/  {'(full scan)' if full else '(incremental)'}")

    results, changes = scan_full(root) if full else scan_incremental(root)

    framework = _detect_framework(results, root)
    config = FRAMEWORK_CONFIGS.get(framework, GENERIC_CONFIG)
    print(f"  🏗️  {config.display_name}")

    total_syms = sum(len(f.symbols) for f in results)
    total_imports = sum(len(f.imports) for f in results)

    _detect_entries(results, root, config)
    refs = build_reverse_refs(results, root)

    if json_out:
        out_data = {
            'root': root.name,
            'total_files': len(results),
            'total_symbols': total_syms,
            'total_imports': total_imports,
            'refs': {k: [list(r) for r in v] for k, v in refs.items()},
            'files': [f.to_cache() for f in results],
        }
        out_path = root / 'STRUCTURE.json'
        out_path.write_text(json.dumps(out_data, indent=2, ensure_ascii=False))
    else:
        md = generate_markdown(results, root, refs, config, changes, compact=compact)
        out_path = root / 'STRUCTURE.md'
        out_path.write_text(md)

    print(f"✅ {len(results)} files, {total_syms} symbols, {total_imports} imports")

    if changes and changes.get('changed'):
        print(f"  📝 {len(changes['changed'])} file(s) with symbol changes:")
        for c in changes['changed']:
            added_str = f"  +{', '.join(c['added'])}" if c['added'] else ''
            removed_str = f"  -{', '.join(c['removed'])}" if c['removed'] else ''
            print(f"    {c['file']}:{added_str}{removed_str}")

    entries = [f for f in results if f.is_entry]
    if entries:
        print(f"  🚪 {len(entries)} entry points detected")

    print(f"📄 {out_path}")


if __name__ == '__main__':
    main()
