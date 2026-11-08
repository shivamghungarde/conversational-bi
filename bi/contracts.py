"""Typed HTTP contracts. Domain validation remains in the semantic layer."""
from __future__ import annotations

import datetime as dt
from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Period(Contract):
    start: StrictStr
    end: StrictStr
    label: StrictStr = ""

    @model_validator(mode="after")
    def valid_range(self):
        start, end = dt.date.fromisoformat(self.start), dt.date.fromisoformat(self.end)
        if start >= end or (end - start).days > 366 * 200:
            raise ValueError("Expected an increasing date range of at most 200 years.")
        return self


class FilterInput(Contract):
    dimension: StrictStr
    op: Literal["in", "not_in"] = "in"
    values: list[Union[StrictStr, StrictInt, float]] = Field(min_length=1, max_length=1000)


class QueryInput(Contract):
    metrics: list[StrictStr] = Field(default_factory=list, max_length=20)
    dimensions: list[StrictStr] = Field(default_factory=list, max_length=8)
    filters: list[FilterInput] = Field(default_factory=list, max_length=20)
    time_range: Optional[Period] = None
    time_grain: Optional[Literal["day", "week", "month", "quarter", "year"]] = None
    compare: Optional[Literal["previous_period", "previous_year"]] = None
    order_by: Optional[StrictStr] = None
    order_dir: Literal["asc", "desc"] = "desc"
    limit: Optional[StrictInt] = None
    explain: bool = False


class ClarifiedValue(Contract):
    term: StrictStr
    dimension: StrictStr
    value: Union[StrictStr, StrictInt, float]


class Clarifications(Contract):
    dataset: Optional[StrictStr] = None
    metric: Optional[StrictStr] = None
    dimension: Optional[StrictStr] = None
    time: Optional[Period] = None
    compare: Optional[Literal["previous_period", "previous_year"]] = None
    values: list[ClarifiedValue] = Field(default_factory=list, max_length=20)


class AskRequest(Contract):
    dataset: Optional[StrictStr] = "auto"
    question: StrictStr = Field(min_length=1, max_length=500)
    context: Optional[QueryInput] = None
    context_dataset: Optional[StrictStr] = None
    clarifications: Optional[Clarifications] = None

    @field_validator("question")
    @classmethod
    def meaningful_question(cls, value):
        if not value.strip():
            raise ValueError("Ask a question.")
        return value.strip()


class QueryRequest(Contract):
    dataset: Optional[StrictStr] = None
    query: QueryInput
