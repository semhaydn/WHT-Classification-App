from django.conf import settings

from reference_data.models import CostType
from transactions.models import (
    AutomaticCostTypeRecommendationClaim,
    CostTypeAISettings,
    CostTypeRecommendation as StoredCostTypeRecommendation,
    MaterialData,
    OpenItem,
)

from .openai_provider import OpenAICostTypeRecommendationProvider
from .provider import (
    ClassificationContext,
    CostTypeOption,
    CostTypeRecommendation,
    CostTypeRecommendationProvider,
    RecommendationResult,
    RecommendationUnavailable,
)


def recommendations_enabled() -> bool:
    return (
        settings.LLM_COST_TYPE_RECOMMENDATIONS_ENABLED
        and CostTypeAISettings.objects.filter(pk=1, enabled=True).exists()
    )


def store_recommendation(
    open_item: OpenItem,
    matched_material: MaterialData | None,
) -> StoredCostTypeRecommendation:
    if not recommendations_enabled():
        raise RecommendationUnavailable("AI Cost Type recommendations are disabled.")
    result = recommend_cost_types(open_item, matched_material)
    return StoredCostTypeRecommendation.objects.create(
        open_item=open_item,
        recommendations=[
            {
                "code": candidate.cost_type.code,
                "name": candidate.cost_type.name,
                "reason": candidate.reason,
            }
            for candidate in result.recommendations
        ],
        abstain_reason=result.abstain_reason,
        model_identifier=settings.OPENAI_COST_TYPE_MODEL,
    )


def automatic_recommendation(
    open_item: OpenItem,
    matched_material: MaterialData | None,
) -> StoredCostTypeRecommendation | None:
    latest = open_item.cost_type_recommendations.first()
    if latest is not None or not recommendations_enabled():
        return latest
    _, created = AutomaticCostTypeRecommendationClaim.objects.get_or_create(
        open_item=open_item,
    )
    if not created:
        return open_item.cost_type_recommendations.first()
    # A manual re-run may have completed while the claim was being acquired.
    latest = open_item.cost_type_recommendations.first()
    if latest is not None:
        return latest
    return store_recommendation(open_item, matched_material)


def _classification_context(
    open_item: OpenItem,
    matched_material: MaterialData | None,
) -> ClassificationContext:
    return ClassificationContext(
        gl_account=open_item.gl_account,
        gl_account_description=open_item.gl_account_description,
        accounting_description=open_item.text,
        assignment=open_item.assignment,
        material_description=open_item.material_description,
        material_data_short_text=(
            matched_material.short_text if matched_material is not None else ""
        ),
        material_group=(
            matched_material.material_group
            if matched_material is not None
            else open_item.material_group
        ),
        purchasing_document_type=(
            matched_material.purchasing_document_type
            if matched_material is not None
            else ""
        ),
    )


def recommend_cost_types(
    open_item: OpenItem,
    matched_material: MaterialData | None,
    provider: CostTypeRecommendationProvider | None = None,
) -> RecommendationResult:
    active_cost_types = list(CostType.objects.filter(active=True).order_by("code"))
    if not active_cost_types:
        return RecommendationResult(
            recommendations=(),
            abstain_reason="There are no active Cost Types to recommend.",
        )

    options = [
        CostTypeOption(
            code=cost_type.code,
            name=cost_type.name,
            description=cost_type.description,
        )
        for cost_type in active_cost_types
    ]
    provider = provider or OpenAICostTypeRecommendationProvider.from_settings()
    response = provider.recommend(
        _classification_context(open_item, matched_material),
        options,
    )
    if response.abstain:
        return RecommendationResult(
            recommendations=(),
            abstain_reason=response.abstain_reason
            or "The available evidence was insufficient for a recommendation.",
        )

    active_by_code = {cost_type.code: cost_type for cost_type in active_cost_types}
    validated: list[CostTypeRecommendation] = []
    seen_codes: set[str] = set()
    for candidate in response.recommendations:
        if len(validated) == 3:
            break
        code = candidate.cost_type_code.strip()
        cost_type = active_by_code.get(code)
        reason = candidate.reason.strip()
        if cost_type is None or code in seen_codes or not reason:
            continue
        validated.append(
            CostTypeRecommendation(cost_type=cost_type, reason=reason)
        )
        seen_codes.add(code)

    return RecommendationResult(
        recommendations=tuple(validated),
        abstain_reason=(
            response.abstain_reason
            or "No valid active Cost Type recommendation was returned."
            if not validated
            else ""
        ),
    )
