# i18n.py
import json
from pathlib import Path
from typing import Optional, Any
from smart_solution.config import Settings

def lang_code2language(lang_code: Optional[str]) -> str:
	if lang_code is None:
		return Settings().default_language

	d = {"en": "english", "ru": "russian"}
	if lang_code in d:
		return d[lang_code]

	return Settings().default_language

class Localizer:
	def __init__(self, lang: Optional[str] = None):
		self._templates: dict[str, str] = {}
		self.lang = lang if lang is not None else Settings().default_language
		self.i18n_dir = Path(__file__).parent / "data" / "locales" / lang

	def _load_template(self, key: str) -> str:
		parts = key.split('.')
		path = self.i18n_dir
		keys = parts.copy()

		while keys:
			k = keys.pop(0)
			dir_candidate = path / k
			if dir_candidate.is_dir():
				path = dir_candidate
				continue

			file_candidate = path / f"{k}.json"
			if not file_candidate.exists():
				raise KeyError(f"Key(file) {key} is not found. File path is {file_candidate}")

			path = file_candidate
			break

		if not keys:
			raise KeyError(f"Key(key is empty) {key} is not found")

		with open(path) as file:
			ans = json.load(file)

		for k in keys:
			if not isinstance(ans, dict):
				raise KeyError(f"Key {key} is not found")
			if k not in ans:
				raise KeyError(f"Key {key} is not found")

			ans = ans.get(k)

		if not isinstance(ans, str):
			raise KeyError(f"Key {key} is not full")

		self._templates[key] = ans
		return ans

	def get(self, key: str, **kwargs: Any) -> str:
		template = self._templates.get(key)
		if template is None:
			template = self._load_template(key)
		return template.format(**kwargs)

	def __call__(self, key: str, **kwargs: Any) -> str:
		return self.get(key, **kwargs)
