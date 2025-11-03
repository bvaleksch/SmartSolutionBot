# utils/sentinels.py
from typing import Self, ClassVar, Optional
from pydantic_core import core_schema
from pydantic import PydanticUserError
from pydantic.json_schema import JsonSchemaValue

class Missing:
	_instance: ClassVar[Optional["Missing"]] = None

	def __new__(cls) -> Self:
		if cls._instance is None:
			cls._instance = super().__new__(cls)
		return cls._instance

	def __repr__(self) -> str:
		return "MISSING"

	# ---- Pydantic v2: core schema (runtime validation) ----
	@classmethod
	def __get_pydantic_core_schema__(cls, _source, _handler) -> core_schema.CoreSchema:
		# Accept only the singleton instance; reject any other value.
		def validate(v):
			if v is cls._instance:
				return v
			raise PydanticUserError('missing_sentinel', 'value is not the Missing sentinel')
		return core_schema.no_info_plain_validator_function(validate)

	# ---- Pydantic v2: JSON Schema (documentation / OpenAPI) ----
	@classmethod
	def __get_pydantic_json_schema__(cls, _core_schema: core_schema.CoreSchema, _handler) -> JsonSchemaValue:
		# Represent the sentinel as an internal constant (not something clients should send)
		return {
			"title": "Missing sentinel (internal)",
			"type": "string",
			"const": "MISSING",		  # purely documentary; clients shouldn’t send it
			"description": "Internal placeholder meaning 'not provided'.",
			"readOnly": True,
			"writeOnly": True,		   # signals 'not a client-supplied value'
			"x-internal": True		   # vendor extension; many tools just ignore it
		}


MISSING = Missing()

