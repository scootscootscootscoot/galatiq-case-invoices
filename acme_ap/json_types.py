"""Shared recursive JSON type, used wherever a raw payload dict moves around.

Only used in places that never touch pydantic's schema generation -- pydantic
recurses indefinitely on a recursive alias. For pydantic models and FastAPI
routes, `object` is the right escape hatch; this alias exists so the normal
module (the xAI client) can type its nested JSON lookups precisely.
"""

from typing import TypeAlias

JsonValue: TypeAlias = str | int | float | bool | None | dict[str, "JsonValue"] | list["JsonValue"]
JsonDict: TypeAlias = dict[str, JsonValue]
