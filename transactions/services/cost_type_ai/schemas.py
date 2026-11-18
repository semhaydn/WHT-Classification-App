from pydantic import BaseModel, ConfigDict


class CostTypeRecommendationCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    cost_type_code: str
    reason: str


class CostTypeRecommendationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    abstain: bool
    abstain_reason: str
    recommendations: list[CostTypeRecommendationCandidate]
