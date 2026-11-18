from dataclasses import dataclass
from typing import Protocol, Sequence

from reference_data.models import CostType

from .schemas import CostTypeRecommendationResponse


@dataclass(frozen=True)
class ClassificationContext:
    gl_account: str
    gl_account_description: str
    accounting_description: str
    assignment: str
    material_description: str
    material_data_short_text: str
    material_group: str
    purchasing_document_type: str


@dataclass(frozen=True)
class CostTypeOption:
    code: str
    name: str
    description: str


@dataclass(frozen=True)
class CostTypeRecommendation:
    cost_type: CostType
    reason: str


@dataclass(frozen=True)
class RecommendationResult:
    recommendations: tuple[CostTypeRecommendation, ...]
    abstain_reason: str


class CostTypeRecommendationProvider(Protocol):
    def recommend(
        self,
        context: ClassificationContext,
        catalogue: Sequence[CostTypeOption],
    ) -> CostTypeRecommendationResponse: ...


class RecommendationUnavailable(Exception):
    pass
