"""Contracts for raw documents, product assessments, and the chat workspace API."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Status(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class VerificationNeed(StrEnum):
    MATERIAL_CONFIRMATION = "MATERIAL_CONFIRMATION"
    PROFESSIONAL_REVIEW = "PROFESSIONAL_REVIEW"


class ProductDecision(StrEnum):
    FULLY_SATISFIED = "FULLY_SATISFIED"
    PENDING_VERIFICATION = "PENDING_VERIFICATION"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"


class SupplementDetails(Model):
    verification: Literal["CONFIRMED", "UNVERIFIED"] = "UNVERIFIED"
    product_id: str | None = Field(default=None, max_length=128)
    condition_quote: str = Field(default="", max_length=8000)

    @model_validator(mode="after")
    def validate_scope(self):
        if self.condition_quote and not self.product_id:
            raise ValueError("指定条款必须先选择产品")
        return self


class HumanSupplement(SupplementDetails):
    recorded_at: datetime
    application_conditions: str | None = None
    legacy: bool = False


class Document(Model):
    # Keep input bytes/line numbering meaningful for evidence and fingerprints.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    document_id: str = Field(min_length=1, max_length=128)
    filename: str = Field(min_length=1, max_length=512)
    content: str = Field(min_length=1)
    source_date: date | None = None
    human_supplement: HumanSupplement | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("document_id", "filename")
    @classmethod
    def nonblank_identifier(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("document_id 和 filename 不能为空")
        return value.strip()

    @field_validator("content")
    @classmethod
    def nonblank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Markdown 正文不能为空")
        return value

    @field_validator("filename")
    @classmethod
    def markdown_only(cls, value: str) -> str:
        if not value.lower().endswith(".md"):
            raise ValueError("filename 必须以 .md 结尾")
        return value


class CompanyInput(Model):
    name: str = Field(min_length=1, max_length=200)
    unified_social_credit_code: str | None = Field(default=None, pattern=r"^[0-9A-Z]{18}$")


class MatchRequest(Model):
    company: CompanyInput
    documents: list[Document] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_documents(self) -> MatchRequest:
        ids = [doc.document_id for doc in self.documents]
        if len(ids) != len(set(ids)):
            raise ValueError("document_id 必须唯一")
        return self


class Evidence(Model):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    evidence_id: str
    document_id: str
    filename: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    quote: str
    source_text: str = ""
    source_urls: list[str] = Field(default_factory=list)
    subject_id: str = "company"
    observed_at: date | None = None


class Product(Model):
    # Product text and CSV cells must reach the model exactly as imported.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    product_id: str
    product_name: str
    institution: str
    detail_url: str
    introduction: str
    application_conditions: str
    applicable_customers: str = ""
    raw_fields: dict[str, str] = Field(default_factory=dict)
    source_rows: list[int] = Field(default_factory=list)
    availability_status: Literal["SNAPSHOT", "ACTIVE", "UNAVAILABLE"] = "SNAPSHOT"
    unavailable_reason: str | None = None


class Catalog(Model):
    products: list[Product]
    version: str
    raw_record_count: int
    warnings: list[str] = Field(default_factory=list)


class ConditionResult(Model):
    # Located product citations retain their original whitespace and newlines.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    condition_id: str
    field_id: str | None = None
    source_field: str
    source_quote: str
    source_quote_verified: bool = True
    subject_id: str
    status: Status
    assessment_basis: Literal["EVIDENCE", "PRELIMINARY_POLICY"] = "EVIDENCE"
    verification_needs: list[VerificationNeed] = Field(default_factory=list)
    reason_code: str
    explanation: str
    evidence_ids: list[str] = Field(default_factory=list)
    required_material: str | None = None
    score_contribution: float = Field(default=0.0, ge=0, le=100)
    score_deduction: float = Field(default=0.0, ge=0, le=100)


class EvaluationResult(Model):
    status: Status
    assessment_basis: Literal["EVIDENCE", "PRELIMINARY_POLICY"] = "EVIDENCE"
    verification_needs: list[VerificationNeed] = Field(default_factory=list)
    logic_expression: str
    condition_results: list[ConditionResult]
    explanation: str = ""
    evidence_ids: list[str] = Field(default_factory=list)


class ModelCitation(Model):
    """A verbatim excerpt from an uploaded document, located after inference."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    document_id: str = Field(min_length=1)
    quote: str = Field(min_length=1)


class ModelJudgement(Model):
    status: Status
    assessment_basis: Literal["EVIDENCE", "PRELIMINARY_POLICY"] = "EVIDENCE"
    verification_needs: list[VerificationNeed] = Field(default_factory=list)
    explanation: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    citations: list[ModelCitation]
    requires_human_verification: bool = Field(strict=True)
    required_material: str | None = None


class ModelCondition(ModelJudgement):
    condition_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    source_field: Literal["application_conditions", "申请条件"]
    source_quote: str = Field(min_length=1)


class SupplementFeedback(Model):
    document_id: str = Field(min_length=1, max_length=128)
    status: Literal["ADOPTED", "NOT_USED", "CONFLICT", "NEEDS_CONFIRMATION", "NOT_REPORTED",
                    "OUT_OF_SCOPE", "CONDITIONS_CHANGED"]
    reason: str = Field(min_length=1, max_length=3000)
    impact: str = Field(default="", max_length=3000)
    condition_ids: list[str] = Field(default_factory=list, max_length=100)


class ModelProductAssessment(Model):
    eligibility: ModelJudgement
    conditions: list[ModelCondition] = Field(min_length=1)
    supplement_feedback: list[SupplementFeedback] = Field(default_factory=list, max_length=100,
                                                         exclude_if=lambda value: not value)


class RequiredAction(Model):
    condition_id: str
    field_id: str | None = None
    subject_id: str
    reason_code: str
    material: str
    verification_needs: list[VerificationNeed] = Field(default_factory=list)


class MatchBreakdown(Model):
    """Equal-weight coverage of the model's independently evaluated requirements."""

    total_conditions: int = Field(ge=0)
    matched_conditions: int = Field(ge=0)
    unmatched_conditions: int = Field(ge=0)
    unknown_conditions: int = Field(ge=0)
    points_per_condition: float = Field(ge=0, le=100)
    method: Literal["equal_weight"] = "equal_weight"


class ProductMatch(Model):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    product_id: str
    product_name: str
    application_conditions: str = ""
    institution: str
    detail_url: str
    decision: ProductDecision
    verification_needs: list[VerificationNeed] = Field(default_factory=list)
    eligibility: EvaluationResult
    required_actions: list[RequiredAction] = Field(default_factory=list)
    product_terms: dict[str, str | None] = Field(default_factory=dict)
    model_assessment: ModelProductAssessment | None = None
    validation_warnings: list[str] = Field(default_factory=list)
    match_score: float = Field(default=0.0, ge=0, le=100)
    match_breakdown: MatchBreakdown | None = None


class UnavailableProduct(Model):
    product_id: str
    product_name: str
    reason: str


class FailedProduct(Model):
    """A product whose model evaluation failed, separate from eligibility and source quality."""

    product_id: str
    product_name: str
    code: str
    reason: str | None = None
    message: str
    retryable: bool = False


class Summary(Model):
    catalog_total: int
    raw_record_count: int
    evaluated_count: int
    unavailable_count: int
    fully_satisfied_count: int
    pending_count: int
    not_matched_count: int
    failed_count: int = 0


class MatchResponse(Model):
    request_id: str
    status: Literal["COMPLETED", "PARTIAL"] = "COMPLETED"
    evaluated_at: datetime
    company: CompanyInput
    versions: dict[str, str]
    input_fingerprint: str
    summary: Summary
    matches: list[ProductMatch] = Field(default_factory=list)
    unavailable_products: list[UnavailableProduct]
    failed_products: list[FailedProduct] = Field(default_factory=list)
    evidence: dict[str, Evidence]
    warnings: list[str] = Field(default_factory=list)
