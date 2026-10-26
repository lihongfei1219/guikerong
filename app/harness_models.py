"""Human commands are explicit typed inputs, never model-generated side effects."""

from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from app.models import Document, Model, Status, SupplementDetails, VerificationNeed


class VersionedCommand(Model):
    expected_version: int = Field(ge=1)


class ChatCommand(VersionedCommand):
    message: str = Field(min_length=1, max_length=12000)


class SupplementCommand(VersionedCommand):
    documents: list[Document] = Field(min_length=1, max_length=100)
    supplement: SupplementDetails | None = None


class DocumentDeleteCommand(VersionedCommand):
    document_id: str = Field(min_length=1, max_length=128)


class DocumentEditCommand(VersionedCommand):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    document_id: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1)

    @field_validator("content")
    @classmethod
    def nonblank_content(cls, value):
        if not value.strip():
            raise ValueError("文件内容不能为空")
        return value


class MatchCommand(VersionedCommand):
    product_ids: list[str] = Field(default_factory=list, max_length=100)


class ReviewCommand(VersionedCommand):
    run_id: str = Field(min_length=1)
    product_id: str = Field(min_length=1)
    condition_id: str = Field(min_length=1)
    status: Status
    verification_needs: list[VerificationNeed] = Field(default_factory=list, max_length=2)
    reviewer: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=4000)
    evidence: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def check_needs(self):
        if self.status == Status.UNKNOWN and not self.verification_needs:
            raise ValueError("待核验必须选择材料确认或专业解释原因")
        if self.status != Status.UNKNOWN and self.verification_needs:
            raise ValueError("明确结论不能附带待核验类型")
        self.verification_needs = list(dict.fromkeys(self.verification_needs))
        return self


class KnowledgeDraft(Model):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=12000)
    product_id: str | None = None
    condition_quote: str = Field(default="", max_length=8000)

    @model_validator(mode="after")
    def scope(self):
        if self.product_id and not self.condition_quote:
            raise ValueError("产品专属知识必须填写条款关键词或近似表述")
        return self


class KnowledgeDecision(Model):
    expected_version: int = Field(ge=1)
    status: Literal["PUBLISHED", "RETIRED"]
