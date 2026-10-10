import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from content_sync import (  # noqa: E402
    ContentSyncError,
    PostgresContentAdapter,
    main,
    plan_sync,
    postgres_connection_from_env,
    validate_snapshot,
)

COMMIT = "a" * 40


def write_article(root: Path, *, slug="hello-world", source_id="ks-docker-001",
                  body="A short practical article.", featured="./featured.webp",
                  category="docker", filename_slug=None):
    directory_slug = filename_slug or slug
    (root / "articles").mkdir(exist_ok=True)
    (root / "troubleshooting").mkdir(exist_ok=True)
    folder = root / "articles" / category / directory_slug
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "featured.webp").write_bytes(b"image-one")
    (folder / "index.mdx").write_text(
        "---\n"
        f"id: {source_id}\n"
        "title: Docker Article\n"
        f"slug: {slug}\n"
        "description: Learn a safe Docker workflow.\n"
        f"category: {category}\n"
        "tags:\n  - docker\n  - devops\n"
        f"featured_image: {featured}\n"
        "seo_title: Docker SEO\n"
        "---\n\n"
        + body,
        encoding="utf-8",
    )
    return folder / "index.mdx"


def valid_snapshot(root: Path):
    (root / "articles").mkdir(exist_ok=True)
    (root / "troubleshooting").mkdir(exist_ok=True)
    write_article(root)
    return validate_snapshot(root, COMMIT, {"docker"})


class Transaction:
    def __init__(self, connection):
        self.connection = connection
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc, tb):
        self.connection.rolled_back = exc_type is not None
        self.connection.committed = exc_type is None
        return False


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.rowcount = 1
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def execute(self, sql, params=None):
        self.connection.calls.append((sql, params))
        if self.connection.fail_on and self.connection.fail_on in sql:
            raise RuntimeError("mock database failure")
        if sql == PostgresContentAdapter.SELECT_EXISTING:
            self.connection.rows = self.connection.existing
    def fetchall(self):
        return self.connection.rows


class FakeConnection:
    def __init__(self, existing=None, fail_on=None):
        self.existing = existing or []
        self.fail_on = fail_on
        self.calls = []
        self.rows = []
        self.committed = False
        self.rolled_back = False
    def transaction(self):
        return Transaction(self)
    def cursor(self):
        return Cursor(self)
    def close(self):
        self.closed = True


class ContentSyncTests(unittest.TestCase):
    def make_root(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def test_valid_frontmatter_and_repository_relative_featured_path(self):
        root = self.make_root()
        (root / "articles").mkdir()
        (root / "troubleshooting").mkdir()
        write_article(root, featured="articles/docker/hello-world/featured.webp")
        snapshot = validate_snapshot(root, COMMIT, {"docker"})
        self.assertEqual(snapshot.entries[0].metadata["featured_image"], "articles/docker/hello-world/featured.webp")

    def test_optional_seo_fields_and_metadata_mapping(self):
        root = self.make_root()
        snapshot = valid_snapshot(root)
        entry = snapshot.entries[0]
        self.assertEqual(entry.source_id, "ks-docker-001")
        self.assertEqual(entry.content_type, "article")
        self.assertEqual(entry.metadata["tags"], ["docker", "devops"])
        self.assertEqual(entry.metadata["seo_title"], "Docker SEO")
        self.assertGreaterEqual(entry.reading_minutes, 1)

    def test_missing_required_frontmatter_rejected(self):
        root = self.make_root()
        path = write_article(root)
        path.write_text("---\ntitle: Missing id\n---\nBody", encoding="utf-8")
        with self.assertRaisesRegex(ContentSyncError, "missing required"):
            validate_snapshot(root, COMMIT, {"docker"})

    def test_unknown_fields_rejected(self):
        root = self.make_root()
        path = write_article(root)
        path.write_text(path.read_text().replace("title: Docker Article", "title: Docker Article\nadmin: true"))
        with self.assertRaisesRegex(ContentSyncError, "unknown frontmatter"):
            validate_snapshot(root, COMMIT, {"docker"})

    def test_duplicate_yaml_keys_rejected(self):
        root = self.make_root()
        path = write_article(root)
        path.write_text(path.read_text().replace("title: Docker Article", "title: First\ntitle: Second"))
        with self.assertRaisesRegex(ContentSyncError, "invalid YAML"):
            validate_snapshot(root, COMMIT, {"docker"})

    def test_tags_must_be_strings(self):
        root = self.make_root()
        path = write_article(root)
        path.write_text(path.read_text().replace("  - devops", "  - 7"))
        with self.assertRaisesRegex(ContentSyncError, "tags must be an array"):
            validate_snapshot(root, COMMIT, {"docker"})

    def test_category_must_be_active(self):
        root = self.make_root()
        (root / "articles").mkdir()
        (root / "troubleshooting").mkdir()
        write_article(root)
        with self.assertRaisesRegex(ContentSyncError, "not active"):
            validate_snapshot(root, COMMIT, {"linux"})

    def test_category_and_slug_must_match_paths(self):
        root = self.make_root()
        (root / "articles").mkdir()
        (root / "troubleshooting").mkdir()
        write_article(root, slug="wrong-slug", filename_slug="hello-world")
        with self.assertRaisesRegex(ContentSyncError, "slug must match"):
            validate_snapshot(root, COMMIT, {"docker"})

    def test_duplicate_ids_and_slugs_rejected(self):
        root = self.make_root()
        (root / "articles").mkdir()
        (root / "troubleshooting").mkdir()
        write_article(root)
        write_article(root, slug="another-slug", source_id="ks-docker-001")
        with self.assertRaisesRegex(ContentSyncError, "duplicate stable source id"):
            validate_snapshot(root, COMMIT, {"docker"})

    def test_duplicate_slug_with_different_id_rejected(self):
        root = self.make_root()
        (root / "articles").mkdir()
        (root / "troubleshooting").mkdir()
        write_article(root)
        folder = root / "articles" / "kubernetes" / "hello-world"
        folder.mkdir(parents=True)
        (folder / "featured.webp").write_bytes(b"image-two")
        (folder / "index.mdx").write_text(
            "---\nid: ks-k8s-002\ntitle: Second\nslug: hello-world\ndescription: Another\ncategory: kubernetes\ntags: []\nfeatured_image: ./featured.webp\n---\nBody",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ContentSyncError, "duplicate slug"):
            validate_snapshot(root, COMMIT, {"docker", "kubernetes"})

    def test_unsafe_mdx_import_component_expression_and_html_rejected(self):
        for body, expected in [
            ("import Thing from 'evil'\n\n# Title", "imports/exports"),
            ("<Callout/>", "not allowed"),
            ("Text {process.env.SECRET}", "expressions"),
            ("<script>alert(1)</script>", "raw HTML"),
        ]:
            with self.subTest(expected=expected):
                root = self.make_root()
                (root / "articles").mkdir()
                (root / "troubleshooting").mkdir()
                write_article(root, body=body)
                with self.assertRaisesRegex(ContentSyncError, expected):
                    validate_snapshot(root, COMMIT, {"docker"})

    def test_code_samples_are_not_treated_as_mdx_execution(self):
        root = self.make_root()
        (root / "articles").mkdir()
        (root / "troubleshooting").mkdir()
        write_article(root, body="```js\nimport x from 'y'\nconst v = { secret: true }\n```\n")
        self.assertEqual(len(validate_snapshot(root, COMMIT, {"docker"}).entries), 1)

    def test_unsafe_paths_and_encoded_separators_rejected(self):
        for value in ("../../../outside.webp", "./%2e%2e/secret", "./images%2fsecret.webp", "/etc/passwd"):
            with self.subTest(value=value):
                root = self.make_root()
                (root / "articles").mkdir()
                (root / "troubleshooting").mkdir()
                write_article(root, featured=value)
                with self.assertRaises(ContentSyncError):
                    validate_snapshot(root, COMMIT, {"docker"})

    def test_missing_or_symlink_assets_fail_closed(self):
        root = self.make_root()
        (root / "articles").mkdir()
        (root / "troubleshooting").mkdir()
        path = write_article(root, body="![image](./missing.png)")
        with self.assertRaisesRegex(ContentSyncError, "missing or unsafe"):
            validate_snapshot(root, COMMIT, {"docker"})
        (path.parent / "missing.png").symlink_to(path.parent / "featured.webp")
        with self.assertRaisesRegex(ContentSyncError, "symlink"):
            validate_snapshot(root, COMMIT, {"docker"})

    def test_full_40_character_commit_required(self):
        root = self.make_root()
        (root / "articles").mkdir()
        (root / "troubleshooting").mkdir()
        write_article(root)
        with self.assertRaisesRegex(ContentSyncError, "40-character"):
            validate_snapshot(root, "A" * 40, {"docker"})

    def test_digest_is_deterministic_and_changes_with_asset(self):
        root = self.make_root()
        first = valid_snapshot(root).entries[0].digest
        self.assertEqual(first, valid_snapshot(root).entries[0].digest)
        (root / "articles/docker/hello-world/featured.webp").write_bytes(b"image-two")
        self.assertNotEqual(first, validate_snapshot(root, COMMIT, {"docker"}).entries[0].digest)

    def test_digest_changes_on_rename(self):
        root = self.make_root()
        snapshot = valid_snapshot(root)
        old = snapshot.entries[0].digest
        old_dir = root / "articles/docker/hello-world"
        new_dir = root / "articles/docker/new-path"
        old_dir.rename(new_dir)
        (new_dir / "index.mdx").write_text((new_dir / "index.mdx").read_text().replace("slug: hello-world", "slug: new-path"))
        renamed = validate_snapshot(root, COMMIT, {"docker"}).entries[0]
        self.assertNotEqual(old, renamed.digest)
        self.assertEqual(renamed.source_id, "ks-docker-001")

    def test_empty_or_incomplete_scan_never_reconciles_deletions(self):
        root = self.make_root()
        (root / "articles").mkdir()
        (root / "troubleshooting").mkdir()
        with self.assertRaisesRegex(ContentSyncError, "no MDX files"):
            validate_snapshot(root, COMMIT, {"docker"})
        incomplete = self.make_root()
        (incomplete / "articles").mkdir()
        with self.assertRaisesRegex(ContentSyncError, "expected troubleshooting"):
            validate_snapshot(incomplete, COMMIT, {"docker"})

    def test_plan_new_source_as_draft_insert(self):
        root = self.make_root()
        snapshot = valid_snapshot(root)
        plan = plan_sync(snapshot, [])
        self.assertEqual(len(plan["inserts"]), 1)
        self.assertEqual(plan["inserts"][0].content_type, "article")
        self.assertEqual(plan["updates"], [])
        self.assertEqual(plan["deletions"], [])
        self.assertNotIn("publish_status", PostgresContentAdapter.INSERT_SQL)

    def test_changed_latest_revision_preserves_approved_snapshot(self):
        root = self.make_root()
        snapshot = valid_snapshot(root)
        entry = snapshot.entries[0]
        existing = [{
            "source_id": entry.source_id, "type": "article", "github_path": entry.github_path,
            "latest_digest": "0" * 64, "published_digest": "f" * 64,
            "source_deleted": False, "update_status": "none", "sync_status": "synced",
        }]
        plan = plan_sync(snapshot, existing)
        self.assertEqual(plan["updates"][0][1], "update_available")
        sql = PostgresContentAdapter.UPDATE_SQL.lower()
        for protected in ("published_digest", "published_commit", "published_metadata", "approved_by", "approved_at", "publish_status", "is_pinned"):
            self.assertNotIn(protected, sql)
        self.assertIsNone(__import__("re").search(r"(?<!latest_)\breading_minutes\b", sql))

    def test_rename_updates_only_latest_path_and_keeps_source_identity(self):
        root = self.make_root()
        snapshot = valid_snapshot(root)
        entry = snapshot.entries[0]
        prior = [{"source_id": entry.source_id, "type": "article", "github_path": "articles/docker/old/index.mdx",
                  "latest_digest": "0" * 64, "published_digest": "f" * 64,
                  "source_deleted": False, "update_status": "none", "sync_status": "synced"}]
        plan = plan_sync(snapshot, prior)
        self.assertEqual(plan["updates"][0][0].source_id, entry.source_id)
        self.assertIn("github_path = %s", PostgresContentAdapter.UPDATE_SQL)
        self.assertNotIn("published_github_path", PostgresContentAdapter.UPDATE_SQL)

    def test_source_deletion_is_soft_and_unpublishes_unpins(self):
        root = self.make_root()
        snapshot = valid_snapshot(root)
        entry = snapshot.entries[0]
        prior = [{"source_id": entry.source_id, "type": "article", "github_path": entry.github_path,
                  "latest_digest": entry.digest, "published_digest": None,
                  "source_deleted": False, "update_status": "none", "sync_status": "synced"}]
        plan = plan_sync(type(snapshot)(snapshot.commit, tuple()), prior)
        self.assertEqual(plan["deletions"], [entry.source_id])
        self.assertNotIn("DELETE FROM", PostgresContentAdapter.DELETE_SQL.upper())
        for required in ("source_deleted = true", "publish_status = 'unpublished'", "is_pinned = false", "pin_order = NULL"):
            self.assertIn(required, PostgresContentAdapter.DELETE_SQL)

    def test_reappearing_source_is_not_republished(self):
        root = self.make_root()
        snapshot = valid_snapshot(root)
        entry = snapshot.entries[0]
        prior = [{"source_id": entry.source_id, "type": "article", "github_path": entry.github_path,
                  "latest_digest": "0" * 64, "published_digest": "f" * 64,
                  "source_deleted": True, "update_status": "update_available", "sync_status": "synced"}]
        plan = plan_sync(snapshot, prior)
        self.assertEqual(len(plan["updates"]), 1)
        self.assertIn("source_deleted = false", PostgresContentAdapter.UPDATE_SQL)
        self.assertNotIn("publish_status", PostgresContentAdapter.UPDATE_SQL)

    def test_type_change_for_stable_id_requires_operator_review(self):
        root = self.make_root()
        snapshot = valid_snapshot(root)
        entry = snapshot.entries[0]
        prior = [{"source_id": entry.source_id, "type": "troubleshooting", "github_path": entry.github_path,
                  "latest_digest": entry.digest, "published_digest": None,
                  "source_deleted": False, "update_status": "none", "sync_status": "synced"}]
        with self.assertRaisesRegex(ContentSyncError, "changed content type"):
            plan_sync(snapshot, prior)

    def test_idempotent_snapshot_produces_no_write_operations(self):
        root = self.make_root()
        snapshot = valid_snapshot(root)
        entry = snapshot.entries[0]
        prior = [{"source_id": entry.source_id, "type": entry.content_type,
                  "github_path": entry.github_path, "latest_digest": entry.digest,
                  "published_digest": None, "source_deleted": False,
                  "update_status": "none", "sync_status": "synced"}]
        plan = plan_sync(snapshot, prior)
        self.assertFalse(plan["inserts"] or plan["updates"] or plan["deletions"])

    def test_sync_rejects_non_main_workflow_context(self):
        env = {
            "KODESHUTTLE_CONTENT_SYNC_ENABLED": "1",
            "GITHUB_REF": "refs/heads/feature/test",
            "GITHUB_SHA": COMMIT,
        }
        with patch.dict(os.environ, env, clear=True), patch.object(sys, "argv", ["content_sync.py", "sync", "--commit", COMMIT]):
            output = __import__("io").StringIO()
            with redirect_stdout(output):
                result = main()
        self.assertEqual(result, 1)
        self.assertIn("protected-main workflow", output.getvalue())
        self.assertNotIn(COMMIT, output.getvalue())

    def test_sql_uses_bound_parameters_and_does_not_embed_untrusted_values(self):
        root = self.make_root()
        write_article(root, source_id="x');DROP_TABLE_public.content;--")
        snapshot = validate_snapshot(root, COMMIT, {"docker"})
        connection = FakeConnection()
        PostgresContentAdapter(connection).sync(snapshot)
        insert_sql, params = next(call for call in connection.calls if call[0] == PostgresContentAdapter.INSERT_SQL)
        self.assertNotIn("DROP TABLE", insert_sql)
        self.assertIn("%s", insert_sql)
        self.assertIn("x');DROP_TABLE_public.content;--", params)
        self.assertTrue(connection.committed)

    def test_preview_uses_read_only_transaction_and_never_applies_deletion(self):
        root = self.make_root()
        snapshot = valid_snapshot(root)
        existing = [{
            "source_id": "old-source", "type": "article",
            "github_path": "articles/docker/removed/index.mdx",
            "latest_digest": "0" * 64, "published_digest": None,
            "source_deleted": False, "update_status": "none", "sync_status": "synced",
        }]
        connection = FakeConnection(existing=existing)
        counts = PostgresContentAdapter(connection).preview(snapshot)
        self.assertEqual(counts, {"inserts": 1, "updates": 0, "deletions": 1})
        self.assertEqual(connection.calls[0][0], "SET TRANSACTION READ ONLY")
        self.assertEqual(connection.calls[1][0], PostgresContentAdapter.SELECT_EXISTING)
        self.assertTrue(connection.committed)
        self.assertFalse(any("INSERT INTO" in sql or "UPDATE public.content" in sql or "DELETE FROM" in sql for sql, _ in connection.calls))
        self.assertEqual(connection.existing, existing)

    def test_preview_cli_outputs_aggregate_counts_only(self):
        root = self.make_root()
        snapshot = valid_snapshot(root)
        connection = FakeConnection()
        env = {"GITHUB_SHA": COMMIT}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(sys, "argv", ["content_sync.py", "plan", "--commit", COMMIT]), \
             patch("content_sync.fetch_active_categories", return_value={"docker"}), \
             patch("content_sync.validate_snapshot", return_value=snapshot), \
             patch("content_sync.postgres_connection_from_env", return_value=connection):
            output = __import__("io").StringIO()
            with redirect_stdout(output):
                result = main()
        self.assertEqual(result, 0)
        self.assertIn("1 inserts, 0 updates, 0 soft deletions", output.getvalue())
        for private_value in ("ks-docker-001", "Docker Article", "github_path", "metadata"):
            self.assertNotIn(private_value, output.getvalue())

    def test_invalid_snapshot_aborts_before_database_connection(self):
        with patch.dict(os.environ, {
            "KODESHUTTLE_CONTENT_SYNC_ENABLED": "1",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_SHA": COMMIT,
        }, clear=True), \
             patch.object(sys, "argv", ["content_sync.py", "sync", "--commit", COMMIT]), \
             patch("content_sync.fetch_active_categories", return_value={"docker"}), \
             patch("content_sync.validate_snapshot", side_effect=ContentSyncError("invalid snapshot")), \
             patch("content_sync.postgres_connection_from_env") as connect:
            output = __import__("io").StringIO()
            with redirect_stdout(output):
                result = main()
        self.assertEqual(result, 1)
        self.assertIn("invalid snapshot", output.getvalue())
        connect.assert_not_called()

    def test_sync_write_gate_defaults_to_disabled(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(sys, "argv", ["content_sync.py", "sync", "--commit", COMMIT]), \
             patch("content_sync.fetch_active_categories") as fetch, \
             patch("content_sync.postgres_connection_from_env") as connect:
            output = __import__("io").StringIO()
            with redirect_stdout(output):
                result = main()
        self.assertEqual(result, 1)
        self.assertIn("writes are disabled", output.getvalue())
        fetch.assert_not_called()
        connect.assert_not_called()

    def test_pooler_host_rejects_non_supabase_hosts_before_connecting(self):
        env = {
            "SUPABASE_PROJECT_REF": "fkykbmfmserjbpispzdu",
            "SUPABASE_POOLER_HOST": "attacker.example",
            "SUPABASE_POOLER_CA_CERT": "/does/not/need/to/exist.pem",
            "SUPABASE_CONTENT_SYNC_PASSWORD": "test-only-placeholder",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ContentSyncError, "shared session-pooler host"):
                postgres_connection_from_env()

    def test_sync_requires_explicit_environment_enable_flag(self):
        with patch.dict(os.environ, {"KODESHUTTLE_CONTENT_SYNC_ENABLED": "0"}, clear=True), patch.object(sys, "argv", ["content_sync.py", "sync"]):
            output = __import__("io").StringIO()
            with redirect_stdout(output):
                result = main()
        self.assertEqual(result, 1)
        self.assertIn("writes are disabled", output.getvalue())
        self.assertNotIn("SUPABASE_CONTENT_SYNC_PASSWORD", output.getvalue())

    def test_db_error_rolls_back_entire_sync_and_is_not_logged_by_adapter(self):
        root = self.make_root()
        snapshot = valid_snapshot(root)
        connection = FakeConnection(fail_on="INSERT INTO")
        with self.assertRaisesRegex(RuntimeError, "mock database failure"):
            PostgresContentAdapter(connection).sync(snapshot)
        self.assertTrue(connection.rolled_back)
        self.assertFalse(connection.committed)

    def test_unchanged_or_deleted_sources_only_reconcile_managed_mdx_paths(self):
        root = self.make_root()
        snapshot = type(valid_snapshot(root))(COMMIT, tuple())
        prior = [
            {"source_id": "managed", "github_path": "articles/docker/a/index.mdx", "source_deleted": False},
            {"source_id": "legacy", "github_path": "old/docker.md", "source_deleted": False},
        ]
        self.assertEqual(plan_sync(snapshot, prior)["deletions"], ["managed"])


if __name__ == "__main__":
    unittest.main()
