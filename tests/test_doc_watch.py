import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "doc_watch.py"
spec = importlib.util.spec_from_file_location("doc_watch", MODULE_PATH)
doc_watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(doc_watch)


class DocumentPathFilteringTests(unittest.TestCase):
    def test_accepts_document_files_in_root_and_knowledge_dirs(self):
        self.assertTrue(doc_watch.is_document_path("README.md"))
        self.assertTrue(doc_watch.is_document_path("index.md"))
        self.assertTrue(doc_watch.is_document_path("notes/python-imports.md"))
        self.assertTrue(doc_watch.is_document_path("projects/search/debugging.txt"))
        self.assertTrue(doc_watch.is_document_path("references/book-notes.rst"))

    def test_rejects_runtime_automation_and_non_document_paths(self):
        rejected = [
            ".git/config",
            ".codex-doc-watch/state.json",
            ".codex-doc-watch/logs/watcher.log",
            "scripts/doc_watch.py",
            "tests/test_doc_watch.py",
            "docs/superpowers/plans/2026-04-26-doc-watch.md",
            "notes/draft.md.swp",
            "assets/diagram.png",
        ]

        for path in rejected:
            with self.subTest(path=path):
                self.assertFalse(doc_watch.is_document_path(path))


class GitStatusParsingTests(unittest.TestCase):
    def test_parse_porcelain_z_returns_changed_document_paths(self):
        status = (
            b" M README.md\0"
            b"?? inbox/raw-note.md\0"
            b" D notes/old-note.md\0"
            b"?? assets/image.png\0"
            b" R references/old-name.md\0references/new-name.md\0"
        )

        self.assertEqual(
            doc_watch.parse_changed_document_paths(status),
            [
                "README.md",
                "inbox/raw-note.md",
                "notes/old-note.md",
                "references/old-name.md",
                "references/new-name.md",
            ],
        )


class CooldownTests(unittest.TestCase):
    def test_seconds_until_allowed_without_previous_attempt(self):
        self.assertEqual(doc_watch.seconds_until_allowed({}, now=1000, cooldown=900), 0)

    def test_seconds_until_allowed_blocks_until_cooldown_expires(self):
        state = {"last_attempt_epoch": 1000}

        self.assertEqual(
            doc_watch.seconds_until_allowed(state, now=1200, cooldown=900),
            700,
        )
        self.assertEqual(
            doc_watch.seconds_until_allowed(state, now=1900, cooldown=900),
            0,
        )


class PromptTests(unittest.TestCase):
    def test_prompt_contains_required_workflow_and_paths(self):
        prompt = doc_watch.build_codex_prompt(
            changed_paths=["README.md", "inbox/raw-note.md"],
            remote="origin",
            branch="main",
        )

        self.assertIn("README.md", prompt)
        self.assertIn("inbox/raw-note.md", prompt)
        self.assertIn("git status --short", prompt)
        self.assertIn("git diff --stat", prompt)
        self.assertIn("index.md", prompt)
        self.assertIn("docs: organize knowledge base updates", prompt)
        self.assertIn("git push origin HEAD:main", prompt)
        self.assertIn("不要编辑", prompt)


class StatusPanelTests(unittest.TestCase):
    def test_format_status_panel_includes_process_state_and_runtime_metadata(self):
        panel = doc_watch.format_status_panel(
            root=Path("/repo/howindocs"),
            pid=123,
            running=True,
            child_processes=[
                "456 123 Sl 00:20 node /usr/bin/codex exec --cd /repo/howindocs -",
            ],
            state={
                "last_attempt_at": "2026-04-26T12:41:35+00:00",
                "last_result_at": "2026-04-26T12:46:41+00:00",
                "last_result_code": 0,
                "pending_paths": ["notes/new.md"],
                "next_run_at": "2026-04-26T12:56:35+00:00",
            },
            git_status="## main...origin/main\n M index.md\n?? .codex\n",
            watcher_log=Path("/repo/howindocs/.codex-doc-watch/logs/watcher.log"),
            latest_codex_log=Path("/repo/howindocs/.codex-doc-watch/logs/codex-20260426-124135.log"),
        )

        self.assertIn("Document Watcher Status", panel)
        self.assertIn("Watcher: running (pid 123)", panel)
        self.assertIn("Codex job: running", panel)
        self.assertIn("456 123 Sl 00:20 node /usr/bin/codex exec", panel)
        self.assertIn("Last attempt: 2026-04-26T12:41:35+00:00", panel)
        self.assertIn("Last result: success (0) at 2026-04-26T12:46:41+00:00", panel)
        self.assertIn("Next eligible run: 2026-04-26T12:56:35+00:00", panel)
        self.assertIn("Pending paths:", panel)
        self.assertIn("- notes/new.md", panel)
        self.assertIn("Git status:", panel)
        self.assertIn("M index.md", panel)
        self.assertIn("?? .codex", panel)
        self.assertIn("watcher.log", panel)
        self.assertIn("codex-20260426-124135.log", panel)

    def test_format_status_panel_handles_idle_clean_state(self):
        panel = doc_watch.format_status_panel(
            root=Path("/repo/howindocs"),
            pid=123,
            running=True,
            child_processes=[],
            state={"last_result_code": 1},
            git_status="## main...origin/main\n",
            watcher_log=Path("/repo/howindocs/.codex-doc-watch/logs/watcher.log"),
            latest_codex_log=None,
        )

        self.assertIn("Watcher: running (pid 123)", panel)
        self.assertIn("Codex job: idle", panel)
        self.assertIn("Last result: failed (1)", panel)
        self.assertIn("Pending paths: none", panel)
        self.assertIn("Git status: clean", panel)
        self.assertIn("Latest Codex log: none", panel)


if __name__ == "__main__":
    unittest.main()
