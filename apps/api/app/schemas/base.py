"""Base Pydantic model with camelCase JSON serialization.

Every wire schema inherits from ``CamelCaseModel`` so JSON keys come out
camelCased while Python attribute names stay snake_case. This matches what
TypeScript on the mobile side expects.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CamelCaseModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )
