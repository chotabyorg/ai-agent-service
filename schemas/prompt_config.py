"""Pydantic schemas for structured prompt configuration."""

from pydantic import AliasChoices, BaseModel, Field


class CompanyInfo(BaseModel):
    """Company identity information."""

    name: str
    description: str = ""
    business_template: str | None = None


class PersonalityConfig(BaseModel):
    """Agent personality and communication style."""

    tone: str = "friendly"
    languages: list[str] = Field(default_factory=lambda: ["ru"])
    use_emoji: bool = True
    communication_format: str = "messenger_support"


class DataCollectionField(BaseModel):
    """Single data field to collect from user."""

    name: str
    required: bool = True


class Algorithm(BaseModel):
    """Scripted workflow algorithm."""

    name: str
    trigger: str
    steps: list[str]


class PromptConfig(BaseModel):
    """Structured agent prompt configuration."""

    version: int = 1
    company: CompanyInfo
    personality: PersonalityConfig = Field(default_factory=PersonalityConfig)
    responsibilities: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("responsibilities", "capabilities"),
        description="Human business responsibilities; legacy capabilities is an input alias, never runtime grants.",
    )
    data_collection_fields: list[DataCollectionField] = Field(default_factory=list)
    algorithms: list[Algorithm] = Field(default_factory=list)
    greeting_template: str = ""
    restricted_claims: list[str] = Field(default_factory=list)
    fallback_policy: str = ""
