"""Fail-closed validation and metadata sync for KodeShuttle MDX sources."""
from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

import yaml
from markdown_it import MarkdownIt
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

EXPECTED_PROJECT_REF = "fkykbmfmserjbpispzdu"
ALLOWED_FIELDS = {
    "id", "title", "slug", "description", "category", "tags",
    "featured_image", "seo_title", "seo_description",
}
REQUIRED_FIELDS = {"id", "title", "slug", "description", "category", "tags", "featured_image"}
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
POOLER_HOST_RE = re.compile(r"^aws-[0-9]+-[a-z0-9-]+\.pooler\.supabase\.com$")
UNSAFE_ENCODED_PATH_RE = re.compile(r"%(?:2e|2f|5c)", re.IGNORECASE)
WORD_RE = re.compile(r"[\w'-]+", re.UNICODE)


class ContentSyncError(Exception):
    """A sanitized, user-actionable validation or sync error."""


class UniqueSafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate frontmatter keys."""


def _construct_unique_mapping(loader: UniqueSafeLoader, node: MappingNode, deep: bool = False) -> dict:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError("while constructing a mapping", node.start_mark,
                                   "found an unhashable key", key_node.start_mark) from exc
        if duplicate:
            raise ConstructorError("while constructing a mapping", node.start_mark,
                                   f"duplicate key {key!r}", key_node.start_mark)
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


@dataclass(frozen=True)
class ContentEntry:
    source_id: str
    content_type: str
    github_path: str
    commit: str
    digest: str
    metadata: dict[str, Any]
    reading_minutes: int


@dataclass(frozen=True)
class Snapshot:
    commit: str
    entries: tuple[ContentEntry, ...]


def _frontmatter(source: str, source_path: str) -> tuple[dict[str, Any], str]:
    if not source.startswith("---\n") and not source.startswith("---\r\n"):
        raise ContentSyncError(f"{source_path}: missing YAML frontmatter")
    lines = source.splitlines(keepends=True)
    closing = None
    for index in range(1, len(lines)):
        if lines[index].strip() in {"---", "..."}:
            closing = index
            break
    if closing is None:
        raise ContentSyncError(f"{source_path}: unterminated YAML frontmatter")
    yaml_text = "".join(lines[1:closing])
    body = "".join(lines[closing + 1:])
    try:
        parsed = yaml.load(yaml_text, Loader=UniqueSafeLoader)
    except yaml.YAMLError as exc:
        raise ContentSyncError(f"{source_path}: invalid YAML frontmatter") from exc
    if not isinstance(parsed, dict) or any(not isinstance(k, str) for k in parsed):
        raise ContentSyncError(f"{source_path}: frontmatter must be a string-keyed mapping")
    return parsed, body


def _safe_relative_path(value: str, *, base: PurePosixPath, source_path: str) -> str:
    if not value or value != value.strip() or "\\" in value or ":" in value:
        raise ContentSyncError(f"{source_path}: unsafe asset path")
    if any(ord(ch) < 32 or ch.isspace() for ch in value):
        raise ContentSyncError(f"{source_path}: unsafe asset path")
    if value.startswith("/") or value.startswith("//") or UNSAFE_ENCODED_PATH_RE.search(value):
        raise ContentSyncError(f"{source_path}: unsafe asset path")
    decoded = urllib.parse.unquote(value)
    if decoded.startswith("/") or "\\" in decoded or UNSAFE_ENCODED_PATH_RE.search(decoded):
        raise ContentSyncError(f"{source_path}: unsafe asset path")
    raw = PurePosixPath(decoded)
    if any(part in {"", ".", ".."} for part in raw.parts):
        raise ContentSyncError(f"{source_path}: unsafe asset path")
    joined = (base / raw) if not value.startswith("./") else (base / PurePosixPath(value[2:]))
    stack: list[str] = []
    for part in joined.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not stack:
                raise ContentSyncError(f"{source_path}: asset path escapes repository")
            stack.pop()
        else:
            stack.append(part)
    resolved = PurePosixPath(*stack).as_posix()
    if resolved in {"", "."} or resolved.startswith("../"):
        raise ContentSyncError(f"{source_path}: asset path escapes repository")
    return resolved


def _is_url_or_anchor(value: str) -> bool:
    parsed = urllib.parse.urlsplit(value)
    return bool(parsed.scheme or parsed.netloc or value.startswith("#"))


def _markdown_tokens(body: str, source_path: str) -> list[Any]:
    parser = MarkdownIt("commonmark", {"html": True, "linkify": False})
    try:
        tokens = parser.parse(body)
    except Exception as exc:
        raise ContentSyncError(f"{source_path}: invalid Markdown/MDX syntax") from exc
    for token in tokens:
        if token.type in {"html_block"}:
            raise ContentSyncError(f"{source_path}: raw HTML is not allowed")
        if token.type != "inline":
            continue
        for child in token.children or []:
            if child.type == "html_inline":
                raise ContentSyncError(f"{source_path}: raw HTML is not allowed")
            if child.type == "text":
                if "{" in child.content or "}" in child.content:
                    raise ContentSyncError(f"{source_path}: MDX expressions are not allowed")
                if re.search(r"<\s*/?\s*[A-Za-z][A-Za-z0-9_.:-]*(?:\s|/|>)", child.content):
                    raise ContentSyncError(f"{source_path}: JSX components are not allowed")
    # MDX ESM is only valid at block boundaries. Fenced code is removed before this check.
    code_ranges = []
    for token in tokens:
        if token.type in {"fence", "code_block"} and token.map:
            code_ranges.extend(range(token.map[0], token.map[1]))
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if index in code_ranges:
            continue
        if re.match(r"^\s*(?:import|export)\s+(?:[^\n]+)", line):
            raise ContentSyncError(f"{source_path}: MDX imports/exports are not allowed")
    return tokens


def _referenced_paths(tokens: Sequence[Any], source_path: str) -> list[str]:
    found: set[str] = set()
    parent = PurePosixPath(source_path).parent
    for token in tokens:
        if token.type != "inline":
            continue
        for child in token.children or []:
            if child.type not in {"image", "link_open"}:
                continue
            attrs = dict(child.attrs or {})
            target = attrs.get("src") if child.type == "image" else attrs.get("href")
            if not isinstance(target, str) or not target:
                raise ContentSyncError(f"{source_path}: empty link or image reference")
            parsed = urllib.parse.urlsplit(target)
            if parsed.scheme:
                if parsed.scheme.lower() not in {"https", "mailto"}:
                    raise ContentSyncError(f"{source_path}: only HTTPS external links are allowed")
                if parsed.scheme.lower() == "https" and not parsed.netloc:
                    raise ContentSyncError(f"{source_path}: malformed external link")
                if child.type == "image" and parsed.scheme.lower() != "https":
                    raise ContentSyncError(f"{source_path}: images must be local or HTTPS")
                continue
            if parsed.netloc:
                raise ContentSyncError(f"{source_path}: protocol-relative URLs are not allowed")
            if target.startswith("#"):
                continue
            if parsed.query or parsed.fragment:
                target_path = parsed.path
            else:
                target_path = target
            resolved = _safe_relative_path(target_path, base=parent, source_path=source_path)
            # Article-to-article links are validated as existing paths but are not assets in
            # this document's content digest. Local binary/media files are included.
            if child.type == "image" or PurePosixPath(resolved).suffix.lower() not in {".md", ".mdx"}:
                found.add(resolved)
    return sorted(found)


def _checked_file(root: Path, relative_path: str, source_path: str) -> tuple[Path, bytes]:
    current = root
    for part in PurePosixPath(relative_path).parts:
        current = current / part
        if current.is_symlink():
            raise ContentSyncError(f"{source_path}: symlink assets are not allowed")
    try:
        resolved = current.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
        if not resolved.is_file():
            raise ContentSyncError(f"{source_path}: referenced asset is not a file")
        data = resolved.read_bytes()
    except (OSError, ValueError) as exc:
        raise ContentSyncError(f"{source_path}: referenced asset is missing or unsafe") from exc
    return resolved, data


def _digest(source_path: str, source_bytes: bytes, asset_bytes: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    records = [(source_path, source_bytes), *sorted(asset_bytes.items())]
    for path, contents in records:
        path_bytes = path.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(len(contents).to_bytes(8, "big"))
        digest.update(contents)
    return digest.hexdigest()


def _validate_metadata(front: Mapping[str, Any], *, source_path: str,
                       category_path: str, slug_path: str,
                       active_categories: set[str] | None,
                       root: Path) -> tuple[dict[str, Any], list[str]]:
    missing = REQUIRED_FIELDS - set(front)
    unknown = set(front) - ALLOWED_FIELDS
    if missing:
        raise ContentSyncError(f"{source_path}: missing required frontmatter fields")
    if unknown:
        raise ContentSyncError(f"{source_path}: unknown frontmatter fields")
    sid = front["id"]
    if not isinstance(sid, str) or not sid or sid != sid.strip() or len(sid) > 255 or any(ch.isspace() for ch in sid):
        raise ContentSyncError(f"{source_path}: id must be a nonempty stable string (max 255, no whitespace)")
    for field in ("title", "description"):
        value = front[field]
        if not isinstance(value, str) or not value.strip():
            raise ContentSyncError(f"{source_path}: {field} must be a nonempty string")
    for field in ("slug", "category"):
        value = front[field]
        if not isinstance(value, str) or not SLUG_RE.fullmatch(value):
            raise ContentSyncError(f"{source_path}: {field} must be a lowercase hyphenated slug")
    if front["category"] != category_path:
        raise ContentSyncError(f"{source_path}: category must match its directory")
    if active_categories is not None and front["category"] not in active_categories:
        raise ContentSyncError(f"{source_path}: category is not active")
    if front["slug"] != slug_path:
        raise ContentSyncError(f"{source_path}: slug must match its directory")
    tags = front["tags"]
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        raise ContentSyncError(f"{source_path}: tags must be an array of strings")
    for field in ("seo_title", "seo_description"):
        if field in front and (not isinstance(front[field], str) or not front[field].strip()):
            raise ContentSyncError(f"{source_path}: optional SEO fields must be nonempty strings")
    featured = front["featured_image"]
    if not isinstance(featured, str):
        raise ContentSyncError(f"{source_path}: featured_image must be a local path")
    feature_base = PurePosixPath() if featured.startswith(("articles/", "troubleshooting/")) else PurePosixPath(source_path).parent
    canonical_featured = _safe_relative_path(
        featured, base=feature_base, source_path=source_path
    )
    if not canonical_featured.startswith(("articles/", "troubleshooting/")):
        raise ContentSyncError(f"{source_path}: featured_image must resolve inside a content directory")
    _checked_file(root, canonical_featured, source_path)
    metadata = {
        "title": front["title"].strip(),
        "slug": front["slug"],
        "description": front["description"].strip(),
        "category": front["category"],
        "tags": tags,
        # Store canonical repository-relative paths accepted by the SQL check.
        "featured_image": canonical_featured,
    }
    for field in ("seo_title", "seo_description"):
        if field in front:
            metadata[field] = front[field].strip()
    return metadata, [canonical_featured]


def _walk_content(root: Path) -> list[Path]:
    content_files: list[Path] = []
    for content_type in ("articles", "troubleshooting"):
        base = root / content_type
        if not base.is_dir() or base.is_symlink():
            raise ContentSyncError(f"content snapshot is incomplete: expected {content_type}/ directory")
        for directory, dirs, files in os.walk(base, followlinks=False):
            current = Path(directory)
            for name in dirs:
                if (current / name).is_symlink():
                    raise ContentSyncError("content snapshot contains a symlink directory")
            for name in files:
                path = current / name
                if path.is_symlink():
                    raise ContentSyncError("content snapshot contains a symlink file")
                if path.suffix.lower() == ".md":
                    raise ContentSyncError("legacy .md files under managed roots must be converted to .mdx before sync")
                if path.suffix.lower() == ".mdx":
                    content_files.append(path)
    content_files.sort(key=lambda path: path.relative_to(root).as_posix())
    if not content_files:
        raise ContentSyncError("content snapshot has no MDX files; refusing deletion reconciliation")
    return content_files


def validate_snapshot(root: str | Path, commit: str,
                      active_categories: Iterable[str] | None = None) -> Snapshot:
    root_path = Path(root).resolve(strict=True)
    if not SHA_RE.fullmatch(commit):
        raise ContentSyncError("source commit must be a full 40-character lowercase Git SHA")
    category_set = set(active_categories) if active_categories is not None else None
    entries: list[ContentEntry] = []
    ids: set[str] = set()
    route_keys: set[tuple[str, str]] = set()
    for path in _walk_content(root_path):
        relative = path.relative_to(root_path).as_posix()
        parts = PurePosixPath(relative).parts
        if len(parts) != 4 or parts[0] not in {"articles", "troubleshooting"} or parts[3] != "index.mdx":
            raise ContentSyncError(f"{relative}: expected <type>/<category>/<slug>/index.mdx")
        content_type = "article" if parts[0] == "articles" else "troubleshooting"
        try:
            source_bytes = path.read_bytes()
            source = source_bytes.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ContentSyncError(f"{relative}: file must be readable UTF-8") from exc
        front, body = _frontmatter(source, relative)
        metadata, asset_paths = _validate_metadata(
            front, source_path=relative, category_path=parts[1], slug_path=parts[2],
            active_categories=category_set, root=root_path,
        )
        tokens = _markdown_tokens(body, relative)
        referenced = _referenced_paths(tokens, relative)
        asset_paths = sorted(set(asset_paths) | set(referenced))
        assets: dict[str, bytes] = {}
        for asset_path in asset_paths:
            _, assets[asset_path] = _checked_file(root_path, asset_path, relative)
        source_id = front["id"]
        route_key = (content_type, metadata["slug"])
        if source_id in ids:
            raise ContentSyncError("duplicate stable source id in snapshot")
        if route_key in route_keys:
            raise ContentSyncError("duplicate slug within content type in snapshot")
        ids.add(source_id)
        route_keys.add(route_key)
        digest = _digest(relative, source_bytes, assets)
        word_count = len(WORD_RE.findall(body))
        reading_minutes = max(1, (word_count + 199) // 200)
        entries.append(ContentEntry(
            source_id=source_id,
            content_type=content_type,
            github_path=relative,
            commit=commit,
            digest=digest,
            metadata=metadata,
            reading_minutes=reading_minutes,
        ))
    return Snapshot(commit=commit, entries=tuple(entries))


def plan_sync(snapshot: Snapshot, existing_rows: Sequence[Mapping[str, Any]]) -> dict[str, list[Any]]:
    existing = {str(row["source_id"]): row for row in existing_rows}
    incoming = {entry.source_id: entry for entry in snapshot.entries}
    inserts: list[ContentEntry] = []
    updates: list[tuple[ContentEntry, str]] = []
    deletions: list[str] = []
    for entry in snapshot.entries:
        row = existing.get(entry.source_id)
        next_update_status = (
            "update_available"
            if row and row.get("published_digest") and row["published_digest"] != entry.digest
            else "none"
        )
        if row is None:
            inserts.append(entry)
            continue
        if row.get("type") != entry.content_type:
            raise ContentSyncError("stable source id changed content type; operator review required")
        changed = (
            row.get("github_path") != entry.github_path
            or row.get("latest_digest") != entry.digest
            or bool(row.get("source_deleted"))
            or row.get("update_status") != next_update_status
            or row.get("sync_status") != "synced"
        )
        if changed:
            updates.append((entry, next_update_status))
    for source_id, row in existing.items():
        if source_id not in incoming and not bool(row.get("source_deleted")):
            # The schema only permits article/troubleshooting MDX paths here; records outside
            # that contract are not automatically deleted by this reconciler.
            path = row.get("github_path") or ""
            if path.startswith(("articles/", "troubleshooting/")) and path.endswith(".mdx"):
                deletions.append(source_id)
    return {"inserts": inserts, "updates": updates, "deletions": sorted(deletions)}


class PostgresContentAdapter:
    """Dedicated sync-role adapter. It never reads or writes outside content."""
    SELECT_EXISTING = """
        SELECT source_id, type, github_path, latest_digest, published_digest,
               source_deleted, update_status, sync_status
        FROM public.content
    """
    INSERT_SQL = """
        INSERT INTO public.content (
          source_id, type, github_path, latest_commit, latest_digest,
          latest_metadata, latest_reading_minutes, sync_status, update_status
        ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, 'synced', %s)
    """
    UPDATE_SQL = """
        UPDATE public.content
        SET github_path = %s,
            latest_commit = %s,
            latest_digest = %s,
            latest_metadata = %s::jsonb,
            latest_reading_minutes = %s,
            sync_status = 'synced',
            update_status = %s,
            source_deleted = false
        WHERE source_id = %s
    """
    DELETE_SQL = """
        UPDATE public.content
        SET source_deleted = true,
            publish_status = 'unpublished',
            is_pinned = false,
            pin_order = NULL
        WHERE source_id = %s AND NOT source_deleted
    """

    def __init__(self, connection: Any):
        self.connection = connection

    def preview(self, snapshot: Snapshot) -> dict[str, int]:
        """Read-only sync plan; returns counts without exposing source IDs or metadata."""
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute(self.SELECT_EXISTING)
                rows = cursor.fetchall()
                operations = plan_sync(snapshot, rows)
        return {key: len(value) for key, value in operations.items()}

    def sync(self, snapshot: Snapshot) -> dict[str, int]:
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(self.SELECT_EXISTING)
                rows = cursor.fetchall()
                operations = plan_sync(snapshot, rows)
                # A single transaction means a failed upsert or deletion rolls back all work.
                for entry in operations["inserts"]:
                    cursor.execute(self.INSERT_SQL, (
                        entry.source_id, entry.content_type, entry.github_path,
                        entry.commit, entry.digest,
                        json.dumps(entry.metadata, ensure_ascii=False, separators=(",", ":")),
                        entry.reading_minutes, "none",
                    ))
                for entry, update_status in operations["updates"]:
                    cursor.execute(self.UPDATE_SQL, (
                        entry.github_path, entry.commit, entry.digest,
                        json.dumps(entry.metadata, ensure_ascii=False, separators=(",", ":")),
                        entry.reading_minutes, update_status, entry.source_id,
                    ))
                    if cursor.rowcount != 1:
                        raise ContentSyncError("content row changed during sync; transaction aborted")
                # Reconciliation is deliberately last, after the full snapshot validated and
                # all present sources were processed, and remains inside the same transaction.
                for source_id in operations["deletions"]:
                    cursor.execute(self.DELETE_SQL, (source_id,))
                    if cursor.rowcount != 1:
                        raise ContentSyncError("deletion reconciliation changed unexpectedly")
        return {key: len(value) for key, value in operations.items()}


def fetch_active_categories(supabase_url: str, publishable_key: str,
                            project_ref: str) -> set[str]:
    if project_ref != EXPECTED_PROJECT_REF:
        raise ContentSyncError("configured Supabase project reference does not match the primary project")
    parsed = urllib.parse.urlsplit(supabase_url)
    if parsed.scheme != "https" or parsed.hostname != f"{project_ref}.supabase.co":
        raise ContentSyncError("SUPABASE_URL must be the HTTPS URL for the configured project")
    endpoint = f"{supabase_url.rstrip('/')}/rest/v1/categories_public?select=slug"
    request = urllib.request.Request(endpoint, headers={
        "apikey": publishable_key,
        "Accept": "application/json",
        "Cache-Control": "no-store",
    }, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status != 200:
                raise ContentSyncError("could not retrieve active categories")
            payload = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ContentSyncError("could not retrieve active categories") from exc
    if not isinstance(payload, list) or any(not isinstance(row, dict) or not isinstance(row.get("slug"), str) for row in payload):
        raise ContentSyncError("active category response had an unexpected shape")
    return {row["slug"] for row in payload}


def postgres_connection_from_env() -> Any:
    """Build a libpq connection with hostname and CA verification; no URL/password logging."""
    required = (
        "SUPABASE_PROJECT_REF", "SUPABASE_POOLER_HOST", "SUPABASE_POOLER_CA_CERT",
        "SUPABASE_CONTENT_SYNC_PASSWORD",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise ContentSyncError("required database configuration is missing")
    if os.environ["SUPABASE_PROJECT_REF"] != EXPECTED_PROJECT_REF:
        raise ContentSyncError("configured Supabase project reference does not match the primary project")
    pooler_host = os.environ["SUPABASE_POOLER_HOST"]
    if not POOLER_HOST_RE.fullmatch(pooler_host):
        raise ContentSyncError("SUPABASE_POOLER_HOST must be the exact shared session-pooler host from Supabase Connect")
    ca_path = Path(os.environ["SUPABASE_POOLER_CA_CERT"]).resolve(strict=True)
    if not ca_path.is_file():
        raise ContentSyncError("pooler CA certificate path is invalid")
    # Imported lazily so validation and unit tests do not require a live DB driver.
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise ContentSyncError("Psycopg is required for database sync") from exc
    return psycopg.connect(
        host=pooler_host,
        port=5432,
        dbname="postgres",
        user=f"kodeshuttle_content_sync.{EXPECTED_PROJECT_REF}",
        password=os.environ["SUPABASE_CONTENT_SYNC_PASSWORD"],
        sslmode="verify-full",
        sslrootcert=str(ca_path),
        connect_timeout=10,
        application_name="kodeshuttle-content-sync",
        options="-c statement_timeout=30000 -c lock_timeout=5000",
        row_factory=dict_row,
    )


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Validate or sync one complete KodeShuttle content snapshot")
    parser.add_argument("command", choices=("validate", "plan", "sync"))
    parser.add_argument("--root", default=".")
    parser.add_argument("--commit", default=os.environ.get("GITHUB_SHA", ""))
    args = parser.parse_args()
    try:
        if args.command == "validate":
            active = None
            if os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_PUBLISHABLE_KEY"):
                active = fetch_active_categories(
                    os.environ["SUPABASE_URL"], os.environ["SUPABASE_PUBLISHABLE_KEY"],
                    os.environ.get("SUPABASE_PROJECT_REF", ""),
                )
            snapshot = validate_snapshot(args.root, args.commit, active)
            print(f"PASS: validated {len(snapshot.entries)} MDX records; no database writes performed")
            return 0
        if args.command == "sync":
            if os.environ.get("KODESHUTTLE_CONTENT_SYNC_ENABLED") != "1":
                raise ContentSyncError("content writes are disabled; enable them only after the protected-main preflight")
            # This is a guard against accidental use. Branch/environment protection is
            # the actual workflow trust boundary, not caller-controlled environment values.
            if os.environ.get("GITHUB_REF") != "refs/heads/main":
                raise ContentSyncError("content writes are restricted to the protected-main workflow")
            if os.environ.get("GITHUB_SHA") != args.commit:
                raise ContentSyncError("requested commit does not match the workflow snapshot")
        # Validate every file and active category before opening the database connection.
        active = fetch_active_categories(
            os.environ.get("SUPABASE_URL", ""),
            os.environ.get("SUPABASE_PUBLISHABLE_KEY", ""),
            os.environ.get("SUPABASE_PROJECT_REF", ""),
        )
        snapshot = validate_snapshot(args.root, args.commit, active)
        connection = postgres_connection_from_env()
        try:
            adapter = PostgresContentAdapter(connection)
            if args.command == "plan":
                counts = adapter.preview(snapshot)
                print(f"PASS: read-only plan; {counts['inserts']} inserts, {counts['updates']} updates, {counts['deletions']} soft deletions")
                return 0
            counts = adapter.sync(snapshot)
        finally:
            connection.close()
        print(f"PASS: content metadata sync completed; {counts['inserts']} inserts, {counts['updates']} updates, {counts['deletions']} soft deletions; no publication fields were promoted")
        return 0
    except ContentSyncError as exc:
        print(f"ERROR: {exc}")
        return 1
    except Exception:
        # Do not print driver exceptions because server diagnostics can contain query data.
        print("ERROR: content sync failed; transaction was rolled back where applicable")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
