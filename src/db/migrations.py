"""
Safe, Non-Destructive Versioned SQLite Migration Runner.

Guarantees:
1. State-machine SQL lexer/tokenizer inspects statements cleanly across strings,
   quoted/bracketed identifiers, and SQL comments; destructive DDL/DML (DROP*,
   ALTER TABLE ... DROP [COLUMN], TRUNCATE, ATTACH/DETACH, unscoped or
   trivially-scoped DELETE/UPDATE, and dangerous PRAGMAs) are rejected before execution.
2. Python migration handlers MUST carry explicit @reviewed_migration_handler approval.
3. SQLite C-engine connection authorizer (`conn.set_authorizer`) enforces non-destructive
   guardrails at runtime for both SQL statements and reviewed Python handlers.
4. File-backed databases automatically create an integrity-verified online backup
   (`backup_type='BEFORE_UPGRADE'`) BEFORE EACH migration under exclusive maintenance.
5. Each migration executes atomically and re-verifies PRAGMA integrity_check and
   PRAGMA foreign_key_check before committing.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Sequence, Tuple, Union

if TYPE_CHECKING:
    from src.db.connection import DatabaseManager


class MigrationSafetyError(RuntimeError):
    """Raised when a migration violates safety guarantees or fails verification."""


@dataclass(frozen=True)
class SqlToken:
    """Lexical token emitted by the state-machine SQL tokenizer."""

    kind: str  # 'WORD', 'QUOTED_ID', 'STRING', 'NUMBER', 'SYMBOL'
    value: str

    @property
    def upper(self) -> str:
        return self.value.upper() if self.kind == "WORD" else self.value


def reviewed_migration_handler(
    *,
    reviewer: str,
    reason: str,
) -> Callable[[Callable[[sqlite3.Connection], None]], Callable[[sqlite3.Connection], None]]:
    """
    Explicit approval decorator required for any Python callable migration handler.
    Prevents accidental or unreviewed handler callables from bypassing SQL validation.
    """
    if not reviewer or not reviewer.strip():
        raise ValueError("reviewed_migration_handler requires a non-empty reviewer.")
    if not reason or not reason.strip():
        raise ValueError("reviewed_migration_handler requires a non-empty reason.")

    def decorator(func: Callable[[sqlite3.Connection], None]) -> Callable[[sqlite3.Connection], None]:
        if not callable(func):
            raise TypeError("reviewed_migration_handler can only decorate a callable function.")
        setattr(func, "_is_reviewed_migration_handler", True)
        setattr(
            func,
            "_migration_review_meta",
            {"reviewer": reviewer.strip(), "reason": reason.strip()},
        )
        return func

    return decorator


def is_reviewed_migration_handler(handler: Optional[Callable[..., None]]) -> bool:
    """Return True only when handler is callable and explicitly marked via @reviewed_migration_handler."""
    return callable(handler) and getattr(handler, "_is_reviewed_migration_handler", False) is True


@dataclass(frozen=True)
class Migration:
    """Represents a single forward, non-destructive schema migration step."""

    version: int
    description: str
    statements: Tuple[str, ...] = ()
    handler: Optional[Callable[[sqlite3.Connection], None]] = None

    def __post_init__(self) -> None:
        if self.version < 2:
            raise ValueError(
                f"Migration version must be >= 2 (version 1 is baseline schema.sql), got {self.version}"
            )
        if not self.description or not self.description.strip():
            raise ValueError("Migration description cannot be empty.")
        if not self.statements and self.handler is None:
            raise ValueError(f"Migration v{self.version} must define statements or handler.")
        if self.handler is not None and not callable(self.handler):
            raise TypeError(f"Migration v{self.version} handler must be callable.")


@dataclass
class MigrationReport:
    """Summary of executed migrations and safety backups created."""

    previous_version: int
    current_version: int
    applied_versions: List[int] = field(default_factory=list)
    backups_created: List[Path] = field(default_factory=list)


_SQL_NON_COLUMN_WORDS = frozenset(
    {
        "TRUE",
        "FALSE",
        "NULL",
        "UNKNOWN",
        "IS",
        "NOT",
        "AND",
        "OR",
        "IN",
        "BETWEEN",
        "LIKE",
        "GLOB",
        "REGEXP",
        "MATCH",
        "ESCAPE",
        "EXISTS",
        "CASE",
        "WHEN",
        "THEN",
        "ELSE",
        "END",
        "CAST",
        "AS",
        "SELECT",
        "FROM",
        "WHERE",
        "JOIN",
        "INNER",
        "LEFT",
        "RIGHT",
        "OUTER",
        "CROSS",
        "NATURAL",
        "ON",
        "USING",
        "GROUP",
        "BY",
        "HAVING",
        "ORDER",
        "ASC",
        "DESC",
        "LIMIT",
        "OFFSET",
        "UNION",
        "INTERSECT",
        "EXCEPT",
        "ALL",
        "ANY",
        "SOME",
        "DISTINCT",
        "COLLATE",
        "NULLS",
        "FIRST",
        "LAST",
    }
)

_DANGEROUS_PRAGMAS = frozenset(
    {
        "writable_schema",
        "ignore_check_constraints",
        "trusted_schema",
        "legacy_file_format",
    }
)

_COMPARISON_SYMBOLS = frozenset({"=", "==", "<", ">", "<=", ">="})
_TWO_CHAR_SYMBOLS = frozenset({"!=", "<>", "<=", ">=", "==", "||", "->"})

_SQLITE_ALTER_TABLE = getattr(sqlite3, "SQLITE_ALTER_TABLE", 26)
_SQLITE_FUNCTION = getattr(sqlite3, "SQLITE_FUNCTION", 31)
_SQLITE_PRAGMA = getattr(sqlite3, "SQLITE_PRAGMA", 19)

_DENIED_AUTHORIZER_CODES = frozenset(
    code
    for code in (
        getattr(sqlite3, "SQLITE_DROP_INDEX", 10),
        getattr(sqlite3, "SQLITE_DROP_TABLE", 11),
        getattr(sqlite3, "SQLITE_DROP_TEMP_INDEX", 12),
        getattr(sqlite3, "SQLITE_DROP_TEMP_TABLE", 13),
        getattr(sqlite3, "SQLITE_DROP_TEMP_TRIGGER", 14),
        getattr(sqlite3, "SQLITE_DROP_TEMP_VIEW", 15),
        getattr(sqlite3, "SQLITE_DROP_TRIGGER", 16),
        getattr(sqlite3, "SQLITE_DROP_VIEW", 17),
        getattr(sqlite3, "SQLITE_DROP_VTABLE", 30),
        getattr(sqlite3, "SQLITE_ATTACH", 24),
        getattr(sqlite3, "SQLITE_DETACH", 25),
    )
)


def tokenize_sql(sql: str) -> List[SqlToken]:
    """
    State-machine SQL tokenizer handling single-quoted strings ('...'),
    double-quoted ("..."), backtick (`...`), and bracketed ([...]) identifiers,
    line comments (-- ...), block comments (/* ... */), numeric literals, and operators.
    """
    tokens: List[SqlToken] = []
    n = len(sql)
    i = 0

    while i < n:
        ch = sql[i]

        # 1. Whitespace
        if ch.isspace():
            i += 1
            continue

        # 2. Line comment: -- ... \n
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            i += 2
            while i < n and sql[i] != "\n":
                i += 1
            continue

        # 3. Block comment: /* ... */
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            end_idx = sql.find("*/", i + 2)
            if end_idx == -1:
                raise MigrationSafetyError(
                    "Unsafe migration rejected: unterminated SQL block comment (/* ... */)."
                )
            i = end_idx + 2
            continue

        # 4. Blob literal X'...' / x'...'
        if ch in ("x", "X") and i + 1 < n and sql[i + 1] == "'":
            start = i
            i += 2
            closed = False
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    closed = True
                    break
                i += 1
            if not closed:
                raise MigrationSafetyError(
                    "Unsafe migration rejected: unterminated SQL blob literal."
                )
            tokens.append(SqlToken("STRING", sql[start:i]))
            continue

        # 5. Single-quoted string literal '...'
        if ch == "'":
            start = i
            i += 1
            closed = False
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    closed = True
                    break
                i += 1
            if not closed:
                raise MigrationSafetyError(
                    "Unsafe migration rejected: unterminated SQL string literal."
                )
            tokens.append(SqlToken("STRING", sql[start:i]))
            continue

        # 6. Double-quoted or backtick-quoted identifier
        if ch in ('"', "`"):
            quote = ch
            start = i
            i += 1
            closed = False
            while i < n:
                if sql[i] == quote:
                    if i + 1 < n and sql[i + 1] == quote:
                        i += 2
                        continue
                    i += 1
                    closed = True
                    break
                i += 1
            if not closed:
                raise MigrationSafetyError(
                    f"Unsafe migration rejected: unterminated quoted identifier ({quote}...{quote})."
                )
            tokens.append(SqlToken("QUOTED_ID", sql[start:i]))
            continue

        # 7. Bracketed identifier [...]
        if ch == "[":
            end_idx = sql.find("]", i + 1)
            if end_idx == -1:
                raise MigrationSafetyError(
                    "Unsafe migration rejected: unterminated bracketed identifier ([...])."
                )
            tokens.append(SqlToken("QUOTED_ID", sql[i : end_idx + 1]))
            i = end_idx + 1
            continue

        # 8. Numeric literals (digits or .digits)
        if ch.isdigit() or (ch == "." and i + 1 < n and sql[i + 1].isdigit()):
            start = i
            has_dot = ch == "."
            i += 1
            while i < n:
                cur = sql[i]
                if cur.isdigit():
                    i += 1
                elif cur == "." and not has_dot:
                    has_dot = True
                    i += 1
                elif cur in ("e", "E") and i + 1 < n:
                    j = i + 1
                    if sql[j] in ("+", "-"):
                        j += 1
                    if j < n and sql[j].isdigit():
                        i = j + 1
                        while i < n and sql[i].isdigit():
                            i += 1
                    else:
                        break
                else:
                    break
            tokens.append(SqlToken("NUMBER", sql[start:i]))
            continue

        # 9. Keywords / unquoted identifiers
        if ch.isalpha() or ch == "_":
            start = i
            i += 1
            while i < n and (sql[i].isalnum() or sql[i] in ("_", "$")):
                i += 1
            tokens.append(SqlToken("WORD", sql[start:i]))
            continue

        # 10. Symbols & multi-char operators
        if i + 2 < n and sql[i : i + 3] == "->>":
            tokens.append(SqlToken("SYMBOL", "->>"))
            i += 3
            continue
        if i + 1 < n and sql[i : i + 2] in _TWO_CHAR_SYMBOLS:
            tokens.append(SqlToken("SYMBOL", sql[i : i + 2]))
            i += 2
            continue

        tokens.append(SqlToken("SYMBOL", ch))
        i += 1

    return tokens


def _format_tokens_preview(tokens: Sequence[SqlToken], limit: int = 80) -> str:
    text = " ".join(t.value for t in tokens[:20])
    return text[:limit]


def _is_trigger_statement(stmt_tokens: Sequence[SqlToken]) -> bool:
    if len(stmt_tokens) >= 2 and stmt_tokens[0].upper == "CREATE":
        idx = 1
        if idx < len(stmt_tokens) and stmt_tokens[idx].upper in ("TEMP", "TEMPORARY"):
            idx += 1
        return idx < len(stmt_tokens) and stmt_tokens[idx].upper == "TRIGGER"
    return False


def split_sql_token_statements(tokens: Sequence[SqlToken]) -> List[List[SqlToken]]:
    """Split a token list into top-level SQL statements on semicolon tokens outside BEGIN...END."""
    statements: List[List[SqlToken]] = []
    current: List[SqlToken] = []
    in_trigger_body = False
    case_depth = 0
    paren_depth = 0

    for tok in tokens:
        if tok.kind == "SYMBOL" and tok.value == "(":
            paren_depth += 1
        elif tok.kind == "SYMBOL" and tok.value == ")" and paren_depth > 0:
            paren_depth -= 1

        if tok.kind == "WORD":
            word = tok.upper
            if word == "CASE":
                case_depth += 1
            elif word == "END":
                if case_depth > 0:
                    case_depth -= 1
                elif in_trigger_body and paren_depth == 0:
                    in_trigger_body = False
            elif word == "BEGIN" and paren_depth == 0 and _is_trigger_statement(current):
                in_trigger_body = True

        if tok.kind == "SYMBOL" and tok.value == ";" and paren_depth == 0 and not in_trigger_body:
            if current:
                statements.append(current)
                current = []
            continue

        current.append(tok)

    if current:
        statements.append(current)
    return statements


def _strip_outer_parens(tokens: Sequence[SqlToken]) -> List[SqlToken]:
    toks = list(tokens)
    while len(toks) >= 2 and toks[0].kind == "SYMBOL" and toks[0].value == "(":
        depth = 0
        matched_end = -1
        for idx, tok in enumerate(toks):
            if tok.kind == "SYMBOL" and tok.value == "(":
                depth += 1
            elif tok.kind == "SYMBOL" and tok.value == ")":
                depth -= 1
                if depth == 0:
                    matched_end = idx
                    break
        if matched_end == len(toks) - 1:
            toks = toks[1:-1]
        else:
            break
    return toks


def _split_top_level_keyword(tokens: Sequence[SqlToken], keyword: str) -> List[List[SqlToken]]:
    parts: List[List[SqlToken]] = []
    current: List[SqlToken] = []
    depth = 0
    for tok in tokens:
        if tok.kind == "SYMBOL" and tok.value == "(":
            depth += 1
        elif tok.kind == "SYMBOL" and tok.value == ")" and depth > 0:
            depth -= 1
        elif depth == 0 and tok.kind == "WORD" and tok.upper == keyword:
            parts.append(current)
            current = []
            continue
        current.append(tok)
    parts.append(current)
    return parts


def _clause_has_column_identifier(tokens: Sequence[SqlToken]) -> bool:
    """
    Return True only if predicate tokens reference at least one column or table identifier
    (excluding SQL keywords, literals, and scalar function calls like LENGTH('a')).
    """
    n = len(tokens)
    for idx, tok in enumerate(tokens):
        if tok.kind == "QUOTED_ID":
            return True
        if tok.kind == "WORD":
            if tok.upper in _SQL_NON_COLUMN_WORDS:
                continue
            # Function call identifier followed directly by '(' is not itself a column reference
            if idx + 1 < n and tokens[idx + 1].kind == "SYMBOL" and tokens[idx + 1].value == "(":
                continue
            return True
    return False


def _is_self_tautology(tokens: Sequence[SqlToken]) -> bool:
    """Detect trivial self-comparisons such as `col = col`, `t.col IS t.col`, or `col >= col`."""
    toks = _strip_outer_parens(tokens)
    if len(toks) < 3:
        return False

    depth = 0
    top_cmp_indices: List[int] = []
    for idx, tok in enumerate(toks):
        if tok.kind == "SYMBOL" and tok.value == "(":
            depth += 1
        elif tok.kind == "SYMBOL" and tok.value == ")" and depth > 0:
            depth -= 1
        elif depth == 0:
            if tok.kind == "SYMBOL" and tok.value in _COMPARISON_SYMBOLS:
                top_cmp_indices.append(idx)
            elif tok.kind == "WORD" and tok.upper == "IS":
                # Do not misclassify `IS NOT ...` unless comparing two identical operands
                if idx + 1 < len(toks) and toks[idx + 1].kind == "WORD" and toks[idx + 1].upper == "NOT":
                    continue
                top_cmp_indices.append(idx)

    if len(top_cmp_indices) != 1:
        return False

    cmp_idx = top_cmp_indices[0]
    lhs = _strip_outer_parens(toks[:cmp_idx])
    rhs = _strip_outer_parens(toks[cmp_idx + 1 :])
    if not lhs or not rhs or len(lhs) != len(rhs):
        return False
    return [(t.kind, t.upper) for t in lhs] == [(t.kind, t.upper) for t in rhs]


def _assert_non_trivial_where_clause(
    where_tokens: Sequence[SqlToken],
    cmd: str,
    stmt_preview: str,
) -> None:
    stripped = _strip_outer_parens(where_tokens)
    if not stripped:
        raise MigrationSafetyError(
            f"Unsafe migration rejected: empty WHERE clause in {cmd} ({stmt_preview})"
        )

    # Check every OR-disjunct: any trivially-true or unreferenced constant disjunct
    # (e.g., `id = 5 OR 1 = 1` or `id = 5 OR TRUE`) widens mutation to all rows.
    or_branches = _split_top_level_keyword(stripped, "OR")
    for branch in or_branches:
        branch_clean = _strip_outer_parens(branch)
        if not branch_clean:
            raise MigrationSafetyError(
                f"Unsafe migration rejected: malformed WHERE clause in {cmd} ({stmt_preview})"
            )

        and_parts = _split_top_level_keyword(branch_clean, "AND")
        branch_has_real_scope = False
        for part in and_parts:
            part_clean = _strip_outer_parens(part)
            if not part_clean:
                raise MigrationSafetyError(
                    f"Unsafe migration rejected: malformed WHERE clause in {cmd} ({stmt_preview})"
                )
            if _is_self_tautology(part_clean):
                raise MigrationSafetyError(
                    f"Unsafe migration rejected: trivial WHERE tautology in {cmd} ({stmt_preview})"
                )
            if _clause_has_column_identifier(part_clean):
                branch_has_real_scope = True

        if not branch_has_real_scope:
            raise MigrationSafetyError(
                f"Unsafe migration rejected: broad {cmd} with trivial WHERE clause ({stmt_preview})"
            )


def _strip_explain_and_cte_prefix(tokens: Sequence[SqlToken]) -> List[SqlToken]:
    """Skip leading EXPLAIN [QUERY PLAN] and WITH [RECURSIVE] cte AS (...) prefixes."""
    idx = 0
    n = len(tokens)
    if idx < n and tokens[idx].kind == "WORD" and tokens[idx].upper == "EXPLAIN":
        idx += 1
        if idx + 1 < n and tokens[idx].upper == "QUERY" and tokens[idx + 1].upper == "PLAN":
            idx += 2

    if idx < n and tokens[idx].kind == "WORD" and tokens[idx].upper == "WITH":
        idx += 1
        if idx < n and tokens[idx].kind == "WORD" and tokens[idx].upper == "RECURSIVE":
            idx += 1
        while idx < n:
            # Skip CTE name and optional `(col1, ...)` column list until `AS`
            while idx < n:
                if tokens[idx].kind == "SYMBOL" and tokens[idx].value == "(":
                    depth = 1
                    idx += 1
                    while idx < n and depth > 0:
                        if tokens[idx].kind == "SYMBOL" and tokens[idx].value == "(":
                            depth += 1
                        elif tokens[idx].kind == "SYMBOL" and tokens[idx].value == ")":
                            depth -= 1
                        idx += 1
                    continue
                if tokens[idx].kind == "WORD" and tokens[idx].upper == "AS":
                    idx += 1
                    break
                idx += 1

            # Skip CTE body parentheses `(...)` (with optional MATERIALIZED / NOT MATERIALIZED before it)
            while idx < n and tokens[idx].kind == "WORD" and tokens[idx].upper in ("NOT", "MATERIALIZED"):
                idx += 1
            if idx < n and tokens[idx].kind == "SYMBOL" and tokens[idx].value == "(":
                depth = 1
                idx += 1
                while idx < n and depth > 0:
                    if tokens[idx].kind == "SYMBOL" and tokens[idx].value == "(":
                        depth += 1
                    elif tokens[idx].kind == "SYMBOL" and tokens[idx].value == ")":
                        depth -= 1
                    idx += 1
            if idx < n and tokens[idx].kind == "SYMBOL" and tokens[idx].value == ",":
                idx += 1
                continue
            break

    return list(tokens[idx:])


def _extract_trigger_body_statements(stmt_tokens: Sequence[SqlToken]) -> List[List[SqlToken]]:
    """Extract inner statements between BEGIN and matching END in CREATE TRIGGER statements."""
    begin_idx = -1
    depth = 0
    for idx, tok in enumerate(stmt_tokens):
        if tok.kind == "SYMBOL" and tok.value == "(":
            depth += 1
        elif tok.kind == "SYMBOL" and tok.value == ")" and depth > 0:
            depth -= 1
        elif depth == 0 and tok.kind == "WORD" and tok.upper == "BEGIN":
            begin_idx = idx
            break

    if begin_idx == -1:
        return []

    body_tokens: List[SqlToken] = []
    case_depth = 0
    depth = 0
    for tok in stmt_tokens[begin_idx + 1 :]:
        if tok.kind == "SYMBOL" and tok.value == "(":
            depth += 1
        elif tok.kind == "SYMBOL" and tok.value == ")" and depth > 0:
            depth -= 1
        elif depth == 0 and tok.kind == "WORD":
            if tok.upper == "CASE":
                case_depth += 1
            elif tok.upper == "END":
                if case_depth > 0:
                    case_depth -= 1
                else:
                    break
        body_tokens.append(tok)

    return split_sql_token_statements(body_tokens)


def _validate_statement_tokens(tokens: Sequence[SqlToken]) -> None:
    if not tokens:
        return

    preview = _format_tokens_preview(tokens)
    main_tokens = _strip_explain_and_cte_prefix(tokens)
    if not main_tokens:
        return

    lead = main_tokens[0].upper if main_tokens[0].kind == "WORD" else ""

    if lead == "DROP":
        what = main_tokens[1].upper if len(main_tokens) > 1 and main_tokens[1].kind == "WORD" else "OBJECT"
        raise MigrationSafetyError(
            f"Unsafe migration rejected: DROP {what} is forbidden in migrations ({preview})"
        )

    if lead == "TRUNCATE":
        raise MigrationSafetyError(
            f"Unsafe migration rejected: TRUNCATE is forbidden in migrations ({preview})"
        )

    if lead in ("ATTACH", "DETACH"):
        raise MigrationSafetyError(
            f"Unsafe migration rejected: {lead} is forbidden in migrations ({preview})"
        )

    if lead == "PRAGMA":
        for tok in main_tokens[1:]:
            if tok.kind == "SYMBOL" and tok.value in ("=", "("):
                break
            if tok.kind == "WORD" and tok.value.lower() in _DANGEROUS_PRAGMAS:
                raise MigrationSafetyError(
                    f"Unsafe migration rejected: PRAGMA {tok.value} is forbidden in migrations ({preview})"
                )

    if lead == "ALTER" and len(main_tokens) >= 4 and main_tokens[1].upper == "TABLE":
        # ALTER TABLE [schema .] table_name <operation>
        op_idx = 3
        if len(main_tokens) > 4 and main_tokens[3].kind == "SYMBOL" and main_tokens[3].value == ".":
            op_idx = 5
        if op_idx < len(main_tokens) and main_tokens[op_idx].kind == "WORD" and main_tokens[op_idx].upper == "DROP":
            raise MigrationSafetyError(
                f"Unsafe migration rejected: ALTER TABLE ... DROP is forbidden in migrations ({preview})"
            )

    if _is_trigger_statement(main_tokens):
        for inner_stmt in _extract_trigger_body_statements(main_tokens):
            _validate_statement_tokens(inner_stmt)
        return

    if lead in ("DELETE", "UPDATE"):
        depth = 0
        where_idx = -1
        end_idx = len(main_tokens)

        for idx, tok in enumerate(main_tokens):
            if tok.kind == "SYMBOL" and tok.value == "(":
                depth += 1
            elif tok.kind == "SYMBOL" and tok.value == ")" and depth > 0:
                depth -= 1
            elif depth == 0 and tok.kind == "WORD":
                if tok.upper == "WHERE" and where_idx == -1:
                    where_idx = idx
                elif where_idx != -1 and tok.upper in ("ORDER", "LIMIT", "RETURNING"):
                    end_idx = idx
                    break

        if where_idx == -1:
            raise MigrationSafetyError(
                f"Unsafe migration rejected: broad {lead} without WHERE clause ({preview})"
            )

        where_tokens = main_tokens[where_idx + 1 : end_idx]
        _assert_non_trivial_where_clause(where_tokens, lead, preview)


def assert_non_destructive_sql(sql: str) -> None:
    """
    Tokenize SQL cleanly and raise MigrationSafetyError if any statement is destructive,
    unscoped, or attempts dangerous SQLite directives.
    """
    tokens = tokenize_sql(sql)
    if not tokens:
        return

    for stmt_tokens in split_sql_token_statements(tokens):
        _validate_statement_tokens(stmt_tokens)


def validate_migrations(migrations: Sequence[Migration]) -> List[Migration]:
    """Validate uniqueness, ordering, handler approval, and non-destructive guarantees."""
    seen_versions = set()
    ordered = sorted(migrations, key=lambda m: m.version)
    for m in ordered:
        if m.version in seen_versions:
            raise MigrationSafetyError(f"Duplicate migration version detected: v{m.version}")
        seen_versions.add(m.version)
        for stmt in m.statements:
            assert_non_destructive_sql(stmt)
        if m.handler is not None and not is_reviewed_migration_handler(m.handler):
            handler_name = getattr(m.handler, "__name__", repr(m.handler))
            raise MigrationSafetyError(
                f"Unsafe migration rejected: Python handler '{handler_name}' for v{m.version} "
                "must carry @reviewed_migration_handler approval."
            )
    return ordered


@reviewed_migration_handler(
    reviewer="stage_b_database_reliability",
    reason="Additive backwards-compatible schema v2 columns (owner_name, upi_id) on company_settings",
)
def _migrate_v2_company_owner_and_upi(conn: sqlite3.Connection) -> None:
    """
    Add shop-specific `owner_name` and `upi_id` columns to `company_settings`
    without modifying any existing shop settings or application records.
    """
    cursor = conn.cursor()
    try:
        cursor.execute("PRAGMA table_info(company_settings);")
        existing_columns = {str(row[1]).lower() for row in cursor.fetchall()}
        if not existing_columns:
            raise MigrationSafetyError(
                "Cannot apply v2 migration: table 'company_settings' does not exist."
            )
        if "owner_name" not in existing_columns:
            cursor.execute("ALTER TABLE company_settings ADD COLUMN owner_name TEXT;")
        if "upi_id" not in existing_columns:
            cursor.execute("ALTER TABLE company_settings ADD COLUMN upi_id TEXT;")
    finally:
        cursor.close()


@reviewed_migration_handler(
    reviewer="step2b1_accounting_foundation",
    reason="Idempotently ensure core double-entry system accounts (Sundry Debtors, Sundry Creditors, Operating Expenses) exist in ledger_accounts.",
)
def _migrate_v3_core_ledger_accounts(conn: sqlite3.Connection) -> None:
    """
    Ensure system ledger accounts for Sundry Debtors, Sundry Creditors,
    and Operating Expenses exist in ledger_accounts without disturbing existing accounts.
    """
    cursor = conn.cursor()
    try:
        system_accounts = [
            ("Sundry Debtors", "ASSETS", 1),
            ("Sundry Creditors", "LIABILITIES", 1),
            ("Operating Expenses", "EXPENSE", 1),
        ]
        for name, group, is_sys in system_accounts:
            cursor.execute(
                "INSERT OR IGNORE INTO ledger_accounts (account_name, account_group, is_system) VALUES (?, ?, ?);",
                (name, group, is_sys),
            )
    finally:
        cursor.close()


@reviewed_migration_handler(
    reviewer="step2b2_migration_v4_foundation",
    reason="Additive schema v4: purchase returns debit notes, settlements, supplier credits, dynamic accounts, and financial deletion triggers.",
)
def _migrate_v4_purchase_returns_and_supplier_credits(conn: sqlite3.Connection) -> None:
    """
    Migration v4:
    1. Reclassify Purchase Return Account from INCOME to EXPENSE with balance reconciliation.
    2. Dynamically ensure Supplier Credits Receivable (ASSETS) and ITC Reversal Suspense Account (LIABILITIES) exist.
    3. Add nullable snapshot and lifecycle columns to purchase_returns (preserving historical NULLs).
    4. Add nullable snapshot and rate columns to purchase_return_items (referencing purchase_items(item_id), preserving historical NULLs).
    5. Create supplier_credits table with unique return_id index.
    6. Create purchase_return_settlements table with constraints.
    7. Create partial unique indexes on purchase_returns for idempotency and voucher references.
    8. Install 4 financial-record BEFORE DELETE immutability triggers.
    """
    cursor = conn.cursor()
    try:
        # 1. Purchase Return Account Reclassification & Balance Reconciliation
        cursor.execute(
            "SELECT account_id, account_group, opening_balance, current_balance "
            "FROM ledger_accounts WHERE account_name = 'Purchase Return Account';"
        )
        pr_acc = cursor.fetchone()
        if not pr_acc:
            cursor.execute(
                "INSERT INTO ledger_accounts (account_name, account_group, is_system, opening_balance, current_balance) "
                "VALUES ('Purchase Return Account', 'EXPENSE', 1, 0.0, 0.0);"
            )
        else:
            acc_id = pr_acc[0] if isinstance(pr_acc, (tuple, list)) else pr_acc["account_id"]
            op_bal = float(pr_acc[2] if isinstance(pr_acc, (tuple, list)) else pr_acc["opening_balance"] or 0.0)

            cursor.execute(
                "SELECT COALESCE(SUM(debit_amount), 0) as total_dr, COALESCE(SUM(credit_amount), 0) as total_cr "
                "FROM ledger_entries WHERE account_id = ?;",
                (acc_id,),
            )
            entry_row = cursor.fetchone()
            total_dr = float(entry_row[0] if isinstance(entry_row, (tuple, list)) else entry_row["total_dr"])
            total_cr = float(entry_row[1] if isinstance(entry_row, (tuple, list)) else entry_row["total_cr"])

            # Under EXPENSE (normal debit): current_balance = opening_balance + (total_dr - total_cr)
            recalculated_balance = op_bal + (total_dr - total_cr)

            cursor.execute(
                "UPDATE ledger_accounts SET account_group = 'EXPENSE', current_balance = ? WHERE account_id = ?;",
                (recalculated_balance, acc_id),
            )

        # 2. Dynamic System Accounts (fail safely if existing account has wrong group)
        system_accounts = [
            ("Supplier Credits Receivable", "ASSETS"),
            ("ITC Reversal Suspense Account", "LIABILITIES"),
        ]
        for name, expected_group in system_accounts:
            cursor.execute("SELECT account_id, account_group FROM ledger_accounts WHERE account_name = ?;", (name,))
            existing = cursor.fetchone()
            if existing:
                grp = existing[1] if isinstance(existing, (tuple, list)) else existing["account_group"]
                if grp != expected_group:
                    raise MigrationSafetyError(
                        f"Cannot apply v4 migration: system account '{name}' already exists with group '{grp}', "
                        f"expected '{expected_group}'."
                    )
            else:
                cursor.execute(
                    "INSERT INTO ledger_accounts (account_name, account_group, opening_balance, current_balance, is_system) "
                    "VALUES (?, ?, 0.0, 0.0, 1);",
                    (name, expected_group),
                )

        # 3. Add columns to purchase_returns (Nullable snapshot/status fields without fabricated defaults)
        cursor.execute("PRAGMA table_info(purchase_returns);")
        pr_cols = {str(row[1]).lower() for row in cursor.fetchall()}
        add_pr = [
            ("total_cgst", "REAL CHECK (total_cgst IS NULL OR total_cgst >= 0)"),
            ("total_sgst", "REAL CHECK (total_sgst IS NULL OR total_sgst >= 0)"),
            ("total_igst", "REAL CHECK (total_igst IS NULL OR total_igst >= 0)"),
            ("supplier_credit_note_no", "TEXT"),
            ("supplier_credit_note_date", "TEXT"),
            ("status", "TEXT CHECK (status IS NULL OR status IN ('POSTED', 'CANCELLED'))"),
            ("itc_status", "TEXT CHECK (itc_status IS NULL OR itc_status IN ('CLAIMED_REVERSED', 'NOT_CLAIMED', 'PENDING_CONFIRMATION', 'CONFIRMED_REVERSED'))"),
            ("voucher_id", "INTEGER REFERENCES vouchers(voucher_id) ON DELETE RESTRICT"),
            ("itc_adjustment_voucher_id", "INTEGER REFERENCES vouchers(voucher_id) ON DELETE RESTRICT"),
            ("reversal_voucher_id", "INTEGER REFERENCES vouchers(voucher_id) ON DELETE RESTRICT"),
            ("itc_adjustment_reversal_voucher_id", "INTEGER REFERENCES vouchers(voucher_id) ON DELETE RESTRICT"),
            ("cancelled_at", "TEXT"),
            ("cancel_reason", "TEXT"),
            ("idempotency_key", "TEXT"),
            ("request_fingerprint", "TEXT"),
        ]
        for col, defn in add_pr:
            if col.lower() not in pr_cols:
                cursor.execute(f"ALTER TABLE purchase_returns ADD COLUMN {col} {defn};")

        # 4. Add columns to purchase_return_items (Nullable snapshot fields referencing purchase_items(item_id))
        cursor.execute("PRAGMA table_info(purchase_return_items);")
        pri_cols = {str(row[1]).lower() for row in cursor.fetchall()}
        add_pri = [
            ("purchase_item_id", "INTEGER REFERENCES purchase_items(item_id) ON DELETE RESTRICT"),
            ("discount_amount", "REAL CHECK (discount_amount IS NULL OR discount_amount >= 0)"),
            ("net_rate", "REAL CHECK (net_rate IS NULL OR net_rate >= 0)"),
            ("cgst_rate", "REAL CHECK (cgst_rate IS NULL OR cgst_rate >= 0)"),
            ("cgst_amount", "REAL CHECK (cgst_amount IS NULL OR cgst_amount >= 0)"),
            ("sgst_rate", "REAL CHECK (sgst_rate IS NULL OR sgst_rate >= 0)"),
            ("sgst_amount", "REAL CHECK (sgst_amount IS NULL OR sgst_amount >= 0)"),
            ("igst_rate", "REAL CHECK (igst_rate IS NULL OR igst_rate >= 0)"),
            ("igst_amount", "REAL CHECK (igst_amount IS NULL OR igst_amount >= 0)"),
        ]
        for col, defn in add_pri:
            if col.lower() not in pri_cols:
                cursor.execute(f"ALTER TABLE purchase_return_items ADD COLUMN {col} {defn};")

        # 5. Create supplier_credits table & indexes
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS supplier_credits (
                credit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                credit_no TEXT NOT NULL UNIQUE,
                supplier_id INTEGER NOT NULL REFERENCES suppliers(supplier_id) ON DELETE RESTRICT,
                return_id INTEGER NOT NULL REFERENCES purchase_returns(return_id) ON DELETE RESTRICT,
                credit_date TEXT NOT NULL,
                original_amount REAL NOT NULL CHECK (original_amount > 0),
                balance_amount REAL NOT NULL CHECK (balance_amount >= 0 AND balance_amount <= original_amount),
                status TEXT NOT NULL DEFAULT 'AVAILABLE' CHECK (status IN ('AVAILABLE', 'PARTIALLY_USED', 'FULLY_USED', 'CANCELLED')),
                notes TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_supplier_credits_return_id ON supplier_credits(return_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_supplier_credits_supplier ON supplier_credits(supplier_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_supplier_credits_status ON supplier_credits(status);")

        # 6. Create purchase_return_settlements table & indexes
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS purchase_return_settlements (
                settlement_id INTEGER PRIMARY KEY AUTOINCREMENT,
                return_id INTEGER NOT NULL REFERENCES purchase_returns(return_id) ON DELETE RESTRICT,
                settlement_type TEXT NOT NULL CHECK (settlement_type IN ('PAYABLE_REDUCTION', 'REFUND_CASH', 'REFUND_BANK', 'SUPPLIER_CREDIT')),
                amount REAL NOT NULL CHECK (amount > 0),
                account_id INTEGER REFERENCES ledger_accounts(account_id) ON DELETE RESTRICT,
                credit_id INTEGER REFERENCES supplier_credits(credit_id) ON DELETE RESTRICT,
                reference_no TEXT,
                notes TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                CHECK (
                    (settlement_type = 'SUPPLIER_CREDIT' AND credit_id IS NOT NULL) OR
                    (settlement_type != 'SUPPLIER_CREDIT' AND credit_id IS NULL)
                ),
                CHECK (
                    (settlement_type IN ('REFUND_CASH', 'REFUND_BANK') AND account_id IS NOT NULL) OR
                    (settlement_type NOT IN ('REFUND_CASH', 'REFUND_BANK'))
                )
            );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pr_settlements_return ON purchase_return_settlements(return_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pr_settlements_credit ON purchase_return_settlements(credit_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pr_settlements_account ON purchase_return_settlements(account_id);")

        # 7. Add partial unique indexes on purchase_returns and indexes on purchase_return_items
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_purchase_returns_idempotency_key ON purchase_returns(idempotency_key) WHERE idempotency_key IS NOT NULL;")
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_purchase_returns_voucher_id ON purchase_returns(voucher_id) WHERE voucher_id IS NOT NULL;")
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_purchase_returns_itc_adj_voucher_id ON purchase_returns(itc_adjustment_voucher_id) WHERE itc_adjustment_voucher_id IS NOT NULL;")
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_purchase_returns_rev_voucher_id ON purchase_returns(reversal_voucher_id) WHERE reversal_voucher_id IS NOT NULL;")
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_purchase_returns_itc_adj_rev_voucher_id ON purchase_returns(itc_adjustment_reversal_voucher_id) WHERE itc_adjustment_reversal_voucher_id IS NOT NULL;")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_purchase_returns_purchase ON purchase_returns(purchase_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_purchase_returns_supplier ON purchase_returns(supplier_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_purchase_returns_date ON purchase_returns(return_date);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pr_items_return ON purchase_return_items(return_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pr_items_batch ON purchase_return_items(batch_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pr_items_product ON purchase_return_items(product_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pr_items_purchase_item ON purchase_return_items(purchase_item_id);")

        # 8. Add immutability triggers (preventing DELETE)
        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_prevent_purchase_returns_delete
            BEFORE DELETE ON purchase_returns
            BEGIN
                SELECT RAISE(ABORT, 'Financial audit protection: Deletion of purchase returns is prohibited. Use cancellation reversal workflow.');
            END;
        """)
        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_prevent_purchase_return_items_delete
            BEFORE DELETE ON purchase_return_items
            BEGIN
                SELECT RAISE(ABORT, 'Financial audit protection: Deletion of purchase return items is prohibited.');
            END;
        """)
        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_prevent_purchase_return_settlements_delete
            BEFORE DELETE ON purchase_return_settlements
            BEGIN
                SELECT RAISE(ABORT, 'Financial audit protection: Deletion of purchase return settlements is prohibited.');
            END;
        """)
        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_prevent_supplier_credits_delete
            BEFORE DELETE ON supplier_credits
            BEGIN
                SELECT RAISE(ABORT, 'Financial audit protection: Deletion of supplier credits is prohibited.');
            END;
        """)
    finally:
        cursor.close()


@reviewed_migration_handler(
    reviewer="Antigravity Phase 2B-5B",
    reason="Strictly additive Migration v5 adding nullable idempotency_key and request_fingerprint columns plus partial unique indexes to sales and purchases without altering historical rows.",
)
def _migrate_v5_sales_and_purchase_idempotency(conn: sqlite3.Connection) -> None:
    """
    Migration v5: Add idempotency_key and request_fingerprint columns and partial unique indexes
    to `sales` and `purchases` for safe idempotent submission replay & conflict detection.
    """
    cursor = conn.cursor()
    try:
        # 1. Sales table additive columns & partial unique index (when table exists)
        cursor.execute("PRAGMA table_info(sales);")
        sales_cols = {str(r[1]).lower() for r in cursor.fetchall()}
        if sales_cols:
            for col_name in ("idempotency_key", "request_fingerprint"):
                if col_name not in sales_cols:
                    cursor.execute(f"ALTER TABLE sales ADD COLUMN {col_name} TEXT;")
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_sales_idempotency_key "
                "ON sales(idempotency_key) WHERE idempotency_key IS NOT NULL;"
            )

        # 2. Purchases table additive columns & partial unique index (when table exists)
        cursor.execute("PRAGMA table_info(purchases);")
        purchases_cols = {str(r[1]).lower() for r in cursor.fetchall()}
        if purchases_cols:
            for col_name in ("idempotency_key", "request_fingerprint"):
                if col_name not in purchases_cols:
                    cursor.execute(f"ALTER TABLE purchases ADD COLUMN {col_name} TEXT;")
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_purchases_idempotency_key "
                "ON purchases(idempotency_key) WHERE idempotency_key IS NOT NULL;"
            )
    finally:
        cursor.close()


@reviewed_migration_handler(
    reviewer="Antigravity Phase 2B-5D-2",
    reason="Strictly additive Migration v6 adding nullable idempotency_key and request_fingerprint columns plus partial unique indexes to vouchers and expenses without altering historical rows.",
)
def _migrate_v6_accounting_idempotency(conn: sqlite3.Connection) -> None:
    """
    Migration v6: Add idempotency_key and request_fingerprint columns and partial unique indexes
    to `vouchers` and `expenses` for safe idempotent replay & conflict detection on
    customer receipts, supplier payments, and daily shop expenses.
    """
    cursor = conn.cursor()
    try:
        # 1. Vouchers table additive columns & partial unique index (when table exists)
        cursor.execute("PRAGMA table_info(vouchers);")
        vouchers_cols = {str(r[1]).lower() for r in cursor.fetchall()}
        if vouchers_cols:
            for col_name in ("idempotency_key", "request_fingerprint"):
                if col_name not in vouchers_cols:
                    cursor.execute(f"ALTER TABLE vouchers ADD COLUMN {col_name} TEXT;")
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_vouchers_idempotency_key "
                "ON vouchers(idempotency_key) WHERE idempotency_key IS NOT NULL;"
            )

        # 2. Expenses table additive columns & partial unique index (when table exists)
        cursor.execute("PRAGMA table_info(expenses);")
        expenses_cols = {str(r[1]).lower() for r in cursor.fetchall()}
        if expenses_cols:
            for col_name in ("idempotency_key", "request_fingerprint"):
                if col_name not in expenses_cols:
                    cursor.execute(f"ALTER TABLE expenses ADD COLUMN {col_name} TEXT;")
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_expenses_idempotency_key "
                "ON expenses(idempotency_key) WHERE idempotency_key IS NOT NULL;"
            )
    finally:
        cursor.close()


# Built-in application migrations above v1 baseline schema.
# Add strictly additive, non-destructive migrations (version 2, 3, ...) here as schema evolves.
BUILTIN_MIGRATIONS: Tuple[Migration, ...] = (
    Migration(
        version=2,
        description="Add owner_name and upi_id columns to company_settings",
        handler=_migrate_v2_company_owner_and_upi,
    ),
    Migration(
        version=3,
        description="Add core double-entry system ledger accounts: Sundry Debtors, Sundry Creditors, Operating Expenses",
        handler=_migrate_v3_core_ledger_accounts,
    ),
    Migration(
        version=4,
        description="Purchase Returns, Debit Notes, Supplier Credits, and Audit Immutability Triggers",
        handler=_migrate_v4_purchase_returns_and_supplier_credits,
    ),
    Migration(
        version=5,
        description="Add idempotency_key and request_fingerprint to sales and purchases",
        handler=_migrate_v5_sales_and_purchase_idempotency,
    ),
    Migration(
        version=6,
        description="Add idempotency_key and request_fingerprint to vouchers and expenses",
        handler=_migrate_v6_accounting_idempotency,
    ),
)


class MigrationRunner:
    """Executes forward-only, backed-up, integrity-verified SQLite schema migrations."""

    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager

    def _ensure_schema_version_table(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_version (
                version_id INTEGER PRIMARY KEY AUTOINCREMENT,
                version_number INTEGER NOT NULL UNIQUE,
                description TEXT NOT NULL,
                applied_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

    def get_current_version(self) -> int:
        return self.db.get_schema_version()

    def run_pending_migrations(
        self,
        migrations: Optional[Sequence[Migration]] = None,
        backup_dir: Optional[Union[str, Path]] = None,
        maintenance_timeout: float = 10.0,
    ) -> MigrationReport:
        """
        Apply all pending migrations (> current schema_version) under exclusive maintenance lock.
        Enforces SQL validation, reviewed handler gating, SQLite connection authorizer,
        BEFORE_UPGRADE online backups, and atomic post-check verification.
        """
        migration_list = validate_migrations(
            list(migrations) if migrations is not None else list(BUILTIN_MIGRATIONS)
        )

        # Quick check without maintenance lock when no migrations are pending
        initial_version = self.get_current_version()
        initial_pending = [m for m in migration_list if m.version > initial_version]
        if not initial_pending:
            return MigrationReport(previous_version=initial_version, current_version=initial_version)

        from src.services.backup_service import BackupService

        with self.db.exclusive_maintenance(timeout=maintenance_timeout, reason="schema_migration"):
            current_version = self.get_current_version()
            report = MigrationReport(previous_version=current_version, current_version=current_version)
            pending = [m for m in migration_list if m.version > current_version]
            if not pending:
                return report

            backup_svc: Optional[BackupService] = None
            is_file_db = self.db.db_path != ":memory:" and Path(self.db.db_path).exists()
            if is_file_db:
                integrity = self.db.check_integrity()
                if integrity != ["ok"]:
                    raise MigrationSafetyError(
                        f"Cannot run migrations on unhealthy database: PRAGMA integrity_check={integrity}"
                    )
                backup_svc = BackupService(self.db)

            for migration in pending:
                if migration.handler is not None and not is_reviewed_migration_handler(migration.handler):
                    raise MigrationSafetyError(
                        f"Unsafe migration rejected: unreviewed Python handler for v{migration.version}."
                    )

                # Mandatory verified pre-migration online backup BEFORE EACH migration step
                if backup_svc is not None and is_file_db:
                    backup_path = backup_svc.create_backup(
                        backup_dir=Path(backup_dir) if backup_dir else None,
                        backup_type="BEFORE_UPGRADE",
                    )
                    report.backups_created.append(backup_path)

                auth_state: Dict[str, Optional[str]] = {"violation": None}

                def _migration_authorizer(
                    action_code: int,
                    arg1: Any,
                    arg2: Any,
                    arg3: Any,
                    arg4: Any,
                ) -> int:
                    if action_code in _DENIED_AUTHORIZER_CODES:
                        auth_state["violation"] = (
                            f"forbidden SQLite action code {action_code} ({arg1 or arg2 or ''})"
                        )
                        return sqlite3.SQLITE_DENY
                    if action_code == _SQLITE_ALTER_TABLE and arg3 is not None:
                        auth_state["violation"] = (
                            f"ALTER TABLE DROP COLUMN '{arg3}' on '{arg2}' is forbidden"
                        )
                        return sqlite3.SQLITE_DENY
                    if action_code == _SQLITE_FUNCTION and str(arg2 or "").lower() == "sqlite_drop_column":
                        auth_state["violation"] = "SQLite sqlite_drop_column is forbidden"
                        return sqlite3.SQLITE_DENY
                    if action_code == _SQLITE_PRAGMA and str(arg1 or "").lower() in _DANGEROUS_PRAGMAS:
                        auth_state["violation"] = f"PRAGMA {arg1} is forbidden during migrations"
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK

                conn = self.db._create_raw_connection(track=True)
                try:
                    self._ensure_schema_version_table(conn)
                    conn.execute("BEGIN IMMEDIATE;")
                    conn.set_authorizer(_migration_authorizer)
                    try:
                        cursor = conn.cursor()

                        for stmt in migration.statements:
                            assert_non_destructive_sql(stmt)
                            cursor.execute(stmt)

                        if migration.handler is not None:
                            migration.handler(conn)

                        cursor.execute(
                            "INSERT INTO schema_version (version_number, description) VALUES (?, ?);",
                            (migration.version, migration.description),
                        )

                        # Verify integrity and foreign keys BEFORE committing
                        cursor.execute("PRAGMA integrity_check;")
                        integrity_rows = [str(r[0]) for r in cursor.fetchall() if r]
                        if integrity_rows != ["ok"]:
                            raise MigrationSafetyError(
                                f"Post-migration PRAGMA integrity_check failed for v{migration.version}: {integrity_rows}"
                            )

                        cursor.execute("PRAGMA foreign_key_check;")
                        fk_rows = cursor.fetchall()
                        if fk_rows:
                            raise MigrationSafetyError(
                                f"Post-migration PRAGMA foreign_key_check failed for v{migration.version}: {len(fk_rows)} violation(s)"
                            )

                        cursor.close()
                    finally:
                        try:
                            conn.set_authorizer(lambda *_args: sqlite3.SQLITE_OK)
                        except Exception:
                            pass

                    conn.commit()
                    report.applied_versions.append(migration.version)
                    report.current_version = migration.version
                except Exception as exc:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    if auth_state["violation"]:
                        raise MigrationSafetyError(
                            f"Unsafe migration rejected by SQLite authorizer for v{migration.version}: "
                            f"{auth_state['violation']} ({exc})"
                        ) from exc
                    if isinstance(exc, MigrationSafetyError):
                        raise
                    raise MigrationSafetyError(
                        f"Migration v{migration.version} ({migration.description}) failed cleanly with rollback: {exc}"
                    ) from exc
                finally:
                    conn.close()

            self.db._save_cloud_snapshot()
            return report
