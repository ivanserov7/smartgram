"""Server-injected research configuration takes precedence over the local file."""

import os
import unittest
from unittest.mock import patch

from research import ResearchError
from research_config import load_research_settings


class ResearchEnvironmentTests(unittest.TestCase):
    def test_environment_only_key_and_settings(self):
        with patch("research_config.dotenv_values", return_value={}), patch.dict(
            os.environ, {"OPENAI_API_KEY": "fixture-env-key", "RESEARCH_SEARCH_MODEL": "configured-model"}, clear=True
        ):
            key, settings = load_research_settings()
        self.assertEqual(key, "fixture-env-key")
        self.assertEqual(settings.search_model, "configured-model")

    def test_environment_overrides_file_without_discarding_other_defaults(self):
        with patch("research_config.dotenv_values", return_value={
            "OPENAI_API_KEY": "fixture-file-key", "RESEARCH_MAX_ROUNDS": "2",
            "RESEARCH_ASSESSMENT_TIMEOUT_SECONDS": "31",
        }), patch.dict(os.environ, {"OPENAI_API_KEY": "fixture-env-key", "RESEARCH_MAX_ROUNDS": "1"}, clear=True):
            key, settings = load_research_settings()
        self.assertEqual(key, "fixture-env-key")
        self.assertEqual(settings.max_rounds, 1)
        self.assertEqual(settings.assessment_timeout_seconds, 31)

    def test_explicit_empty_environment_key_does_not_reuse_file_account(self):
        with patch("research_config.dotenv_values", return_value={"OPENAI_API_KEY": "fixture-file-key"}), \
             patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=True):
            with self.assertRaisesRegex(ResearchError, "OPENAI_API_KEY"):
                load_research_settings()


if __name__ == "__main__":
    unittest.main()
