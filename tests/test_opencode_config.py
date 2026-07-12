import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
CONFIG_PATH = ROOT / "opencode" / "opencode.example.json"
MODEL_SOURCES = {
    "gpt-oss-uncensored-16k.Modelfile": "huihui_ai/gpt-oss-abliterated:20b",
    "qwen3-uncensored-16k.Modelfile": "huihui_ai/qwen3-abliterated:8b",
    "nemotron-uncensored-16k.Modelfile": (
        "huihui_ai/nemotron-v1-abliterated:8b-llama-3.1-nano"
    ),
    "mistral-adler-16k.Modelfile": "huihui_ai/mistral-small-abliterated:24b",
}


class OpenCodeConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = CONFIG_PATH.read_text(encoding="utf-8")
        cls.config = json.loads(cls.raw)
        cls.provider = cls.config["provider"]["adler"]

    def test_provider_uses_environment_placeholders(self):
        options = self.provider["options"]

        self.assertEqual("@ai-sdk/openai-compatible", self.provider["npm"])
        self.assertEqual("{env:ADLER_OLLAMA_URL}/v1", options["baseURL"])
        self.assertEqual(
            "Basic {env:ADLER_BASIC_B64}", options["headers"]["Authorization"]
        )
        self.assertEqual(900000, options["timeout"])
        self.assertIsNone(re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", self.raw))
        self.assertNotIn("C:\\Users\\", self.raw)

    def test_declares_only_the_validated_models(self):
        expected = {
            "gpt-oss-uncensored:16k",
            "mistral-adler:16k",
            "qwen3-uncensored:16k",
            "nemotron-uncensored:16k",
        }
        models = self.provider["models"]

        self.assertEqual(expected, set(models))
        for model in models.values():
            self.assertEqual(16384, model["limit"]["context"])
            self.assertEqual(4096, model["limit"]["output"])
            self.assertTrue(model["tool_call"])

    def test_default_and_small_models_are_distinct(self):
        self.assertEqual(
            "adler/gpt-oss-uncensored:16k", self.config["model"]
        )
        self.assertEqual(
            "adler/qwen3-uncensored:16k", self.config["small_model"]
        )
        self.assertNotEqual(self.config["model"], self.config["small_model"])

    def test_global_agent_rules_are_packaged(self):
        rules = ROOT / "opencode" / "AGENTS.md"

        self.assertTrue(rules.is_file())
        self.assertIn("native tool-call API", rules.read_text(encoding="utf-8"))

    def test_validated_model_tags_are_reproducible(self):
        model_dir = ROOT / "opencode" / "models"

        self.assertEqual(set(MODEL_SOURCES), {path.name for path in model_dir.iterdir()})
        for filename, source in MODEL_SOURCES.items():
            contents = (model_dir / filename).read_text(encoding="utf-8")
            self.assertIn(f"FROM {source}", contents)
            self.assertIn("PARAMETER num_ctx 16384", contents)
            self.assertIn("PARAMETER num_predict 4096", contents)

    def test_mistral_template_separates_multiple_tool_calls(self):
        template = (
            ROOT / "opencode" / "models" / "mistral-adler-16k.Modelfile"
        ).read_text(encoding="utf-8")

        self.assertIn("range $toolIndex, $toolCall := .ToolCalls", template)
        self.assertIn("{{ if $toolIndex }},{{ end }}", template)


if __name__ == "__main__":
    unittest.main()
