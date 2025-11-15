# bot/services/action_registry.py
from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar, Dict, Tuple, Optional, Self

from smart_solution.config import Settings

class ActionRegistry:
	_instance: ClassVar[Optional["ActionRegistry"]] = None

	def __new__(cls) -> Self:
		if cls._instance is None:
			cls._instance = super().__new__(cls)
		return cls._instance

	def __init__(self) -> None:
		if getattr(self, "_initialized", False):
			return

		self._store: Dict[Tuple[str, str, str], str] = dict() # Key is (text: str, ui_mode: str, role: str)
		base_dir = getattr(Settings(), "base_dir", None)
		if base_dir is None:
			base_dir = Path(__file__).resolve().parents[2]
		else:
			base_dir = Path(base_dir)
		self._path = base_dir / "data" / "action.json"
		self._load()
		self._initialized = True

	def _load(self) -> None:
		try:
			if self._path.exists():
				data = json.loads(self._path.read_text(encoding="utf-8"))
				if isinstance(data, list):
					for item in data:
						if not isinstance(item, dict):
							continue
						key = item.get("key")
						value = item.get("action")
						if not key or not isinstance(key, list) or len(key) != 3:
							continue
						text, ui_mode, role = key
						self._store[(text, ui_mode, role)] = value
		except Exception:
			return

	def _save(self) -> None:
		try:
			self._path.parent.mkdir(parents=True, exist_ok=True)
			payload = [
				{"key": list(key), "action": value}
				for key, value in self._store.items()
			]
			self._path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
		except Exception:
			return

	@staticmethod
	def _normalize_text(text: str) -> str:
		return text.lower().strip()

	def resolve(self, text: str, ui_mode: str, role: str) -> str:
		text = self._normalize_text(text)
		ui_mode = self._normalize_text(ui_mode)
		role = self._normalize_text(role)
		return self._store.get((text, ui_mode, role), "unregistered")

	def get(self, key: Tuple[str, str, str], default: str | None = None) -> str | None:
		text = self._normalize_text(key[0])
		ui_mode = self._normalize_text(key[1])
		role = self._normalize_text(key[2])
		return self._store.get((text, ui_mode, role), default)

	def register(self, text: str, ui_mode: str, role: str, action: str) -> None:
		text = self._normalize_text(text)
		role = self._normalize_text(role)
		ui_mode = self._normalize_text(ui_mode)
		action = self._normalize_text(action)

		key = (text, ui_mode, role)
		if (self.get(key, "unregistered") != action):
			self._store[key] = action
			self._save()
