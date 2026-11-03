"""
Task 1: Pydantic Validation Models for MCP Server
Strict schema definitions for input validation and mock database structures.
"""

import re
from typing import Optional
from pydantic import BaseModel, Field, field_validator


CUSTOMER_ID_REGEX = r"^CUST-[A-Za-z0-9]{5}$"
CUSTOMER_ID_PATTERN = re.compile(CUSTOMER_ID_REGEX)


class GetCustomerRecordInput(BaseModel):
    """Input parameters for get_customer_record tool."""
    customer_id: str = Field(
        ...,
        description="Unique customer identifier formatted strictly as CUST-XXXXX (e.g., CUST-10001, CUST-A99B2).",
        examples=["CUST-10001"]
    )

    @field_validator("customer_id")
    @classmethod
    def validate_customer_id(cls, v: str) -> str:
        v = v.strip()
        if not CUSTOMER_ID_PATTERN.match(v):
            raise ValueError(
                f"Invalid customer_id format '{v}'. Expected format 'CUST-XXXXX' with 5 alphanumeric characters."
            )
        return v


class TriggerRefundInput(BaseModel):
    """Input parameters for trigger_refund tool."""
    customer_id: str = Field(
        ...,
        description="Unique customer identifier formatted strictly as CUST-XXXXX.",
        examples=["CUST-10001"]
    )
    amount: float = Field(
        ...,
        gt=0.0,
        description="Positive monetary refund amount in USD. Must be strictly greater than 0.",
        examples=[49.99]
    )
    reason: str = Field(
        ...,
        min_length=10,
        description="Detailed justification for the refund (minimum 10 characters required).",
        examples=["Customer received defective item and requested immediate replacement refund."]
    )

    @field_validator("customer_id")
    @classmethod
    def validate_customer_id(cls, v: str) -> str:
        v = v.strip()
        if not CUSTOMER_ID_PATTERN.match(v):
            raise ValueError(
                f"Invalid customer_id format '{v}'. Expected format 'CUST-XXXXX' with 5 alphanumeric characters."
            )
        return v

    @field_validator("reason")
    @classmethod
    def validate_reason_content(cls, v: str) -> str:
        stripped = v.strip()
        if len(stripped) < 10:
            raise ValueError("Refund reason must contain at least 10 non-whitespace characters.")
        return stripped
