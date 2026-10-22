"""Regression for the exact credential-theft assets found in upstream history."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RepositoryIntegrityTests(unittest.TestCase):
    def test_known_credential_theft_assets_are_not_in_the_release_tree(self):
        for path in (
            ".github/workflows/lint.yml",
            ".github/workflows/security-audit.yml",
            ".vscode/tasks.json",
            "public/fonts/fa-solid-400.woff2",
        ):
            with self.subTest(path=path):
                self.assertFalse((ROOT / path).exists(), f"quarantined upstream asset reintroduced: {path}")
