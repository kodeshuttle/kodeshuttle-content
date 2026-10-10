import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def load_workflow(name):
    return yaml.load((WORKFLOWS / name).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


class WorkflowSecurityTests(unittest.TestCase):
    def test_pr_workflow_has_read_only_permissions_and_no_database_secrets(self):
        path = WORKFLOWS / "validate-content.yml"
        source = path.read_text(encoding="utf-8")
        workflow = load_workflow(path.name)
        self.assertIn("pull_request", workflow["on"])
        self.assertNotIn("pull_request_target", workflow["on"])
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertNotIn("secrets.", source)
        self.assertNotIn("SUPABASE_CONTENT_SYNC_PASSWORD", source)
        self.assertNotIn("SUPABASE_POOLER_HOST", source)

    def test_sync_validation_job_has_no_database_credentials(self):
        workflow = load_workflow("sync-content.yml")
        validate = workflow["jobs"]["validate"]
        self.assertIn("github.ref == 'refs/heads/main'", validate["if"])
        self.assertNotIn("env", validate)
        source = (WORKFLOWS / "sync-content.yml").read_text(encoding="utf-8")
        validation_job = source.split("  sync:", 1)[0].split("  validate:", 1)[1]
        self.assertNotIn("secrets.", validation_job)
        self.assertNotIn("SUPABASE_CONTENT_SYNC_PASSWORD", validation_job)
        self.assertNotIn("SUPABASE_POOLER_HOST", validation_job)

    def test_preview_and_write_credentials_are_step_scoped(self):
        workflow = load_workflow("sync-content.yml")
        sync = workflow["jobs"]["sync"]
        self.assertNotIn("env", sync)
        self.assertEqual(sync["environment"], "content-sync-primary")
        steps = sync["steps"]
        validation = next(step for step in steps if step.get("name", "").startswith("Validate complete snapshot"))
        preview = next(step for step in steps if step.get("name") == "Run read-only database preview")
        write = next(step for step in steps if step.get("name") == "Apply metadata sync (explicit environment gate)")
        self.assertNotIn("env", validation)
        self.assertIn("SUPABASE_CONTENT_SYNC_PASSWORD", preview["env"])
        self.assertIn("plan --commit", preview["run"])
        self.assertIn("SUPABASE_CONTENT_SYNC_PASSWORD", write["env"])
        self.assertIn("sync --commit", write["run"])
        self.assertIn("KODESHUTTLE_CONTENT_SYNC_ENABLED == '1'", write["if"])
        self.assertNotIn("SUPABASE_CONTENT_SYNC_PASSWORD", workflow.get("env", {}))

    def test_sync_is_main_only_serialized_and_has_no_pull_request_target(self):
        workflow = load_workflow("sync-content.yml")
        self.assertIn("github.ref == 'refs/heads/main'", workflow["jobs"]["validate"]["if"])
        self.assertIn("github.ref == 'refs/heads/main'", workflow["jobs"]["sync"]["if"])
        self.assertEqual(workflow["jobs"]["sync"]["needs"], "validate")
        self.assertEqual(workflow["concurrency"]["group"], "content-sync-primary")
        self.assertEqual(workflow["concurrency"]["cancel-in-progress"], "false")
        self.assertNotIn("pull_request_target", workflow["on"])

    def test_all_external_actions_are_pinned_to_full_commit_shas(self):
        for path in WORKFLOWS.glob("*.yml"):
            workflow = load_workflow(path.name)
            for job in workflow["jobs"].values():
                for step in job["steps"]:
                    action = step.get("uses")
                    if action and not action.startswith("./"):
                        self.assertRegex(action, r"@[0-9a-f]{40}$", path.name)

    def test_tls_hostname_and_certificate_verification_are_mandatory(self):
        source = (ROOT / "scripts" / "content_sync.py").read_text(encoding="utf-8")
        self.assertIn('sslmode="verify-full"', source)
        self.assertIn("sslrootcert=str(ca_path)", source)
        self.assertIn("POOLER_HOST_RE.fullmatch(pooler_host)", source)


if __name__ == "__main__":
    unittest.main()
