from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.urls import reverse
from django.test import Client, override_settings
from openai import APITimeoutError, AuthenticationError, RateLimitError

from reference_data.models import CostType, SupplierCountry, WHTMapping
from transactions.models import AuditEvent, ClassificationDecision, OpenItem
from transactions.services.classification import save_final_classification
from transactions.services.cost_type_ai.openai_provider import (
    OpenAICostTypeRecommendationProvider,
)
from transactions.services.cost_type_ai.provider import (
    ClassificationContext,
    CostTypeOption,
    CostTypeRecommendation,
    RecommendationResult,
    RecommendationUnavailable,
)
from transactions.services.cost_type_ai.recommendation import recommend_cost_types
from transactions.services.cost_type_ai.schemas import (
    CostTypeRecommendationCandidate,
    CostTypeRecommendationResponse,
)
from transactions.services.wht_determination import determine_wht


def create_transaction_client() -> Client:
    user = User.objects.create_user(username="ai-classifier")
    group, _ = Group.objects.get_or_create(name="Transaction User")
    user.groups.add(group)
    client = Client()
    client.force_login(user)
    return client


def create_open_item() -> OpenItem:
    call_command("import_open_items")
    return OpenItem.objects.filter(
        gl_account="210020",
        document_currency_amount__gt=0,
    ).first()


def recommendation(
    code: str,
    reason: str = "The description supports this Cost Type.",
) -> CostTypeRecommendation:
    return CostTypeRecommendation(
        cost_type=CostType.objects.get(code=code),
        reason=reason,
    )


class StaticProvider:
    def __init__(self, response: CostTypeRecommendationResponse) -> None:
        self.response = response
        self.context = None
        self.catalogue = None

    def recommend(self, context, catalogue):
        self.context = context
        self.catalogue = catalogue
        return self.response


def test_openai_provider_uses_structured_responses_and_allowed_input() -> None:
    parsed = CostTypeRecommendationResponse(
        abstain=False,
        abstain_reason="",
        recommendations=[
            CostTypeRecommendationCandidate(
                cost_type_code="RENTAL",
                reason="The description refers to equipment rental.",
            )
        ],
    )
    client = Mock()
    client.responses.parse.return_value = SimpleNamespace(
        status="completed",
        output_parsed=parsed,
    )
    provider = OpenAICostTypeRecommendationProvider(client, "test-model")
    context = ClassificationContext(
        gl_account="600100",
        gl_account_description="Synthetic expense",
        accounting_description="Synthetic rental",
        assignment="Synthetic assignment",
        material_description="Synthetic equipment",
        material_data_short_text="Synthetic rental of equipment",
        material_group="Synthetic materials",
        purchasing_document_type="NB",
    )

    result = provider.recommend(
        context,
        [CostTypeOption("RENTAL", "Rental", "Synthetic rental costs")],
    )

    assert result == parsed
    kwargs = client.responses.parse.call_args.kwargs
    assert kwargs["model"] == "test-model"
    assert kwargs["text_format"] is CostTypeRecommendationResponse
    assert kwargs["store"] is False
    sent_input = kwargs["input"][1]["content"]
    assert "Synthetic rental of equipment" in sent_input
    assert "supplier_id" not in sent_input
    assert "document_number" not in sent_input
    assert "document_currency_amount" not in sent_input
    assert "wht_rate" not in sent_input


@pytest.mark.parametrize(
    "error_factory",
    [
        lambda: APITimeoutError(
            request=httpx.Request("POST", "https://api.openai.com/v1/responses")
        ),
        lambda: AuthenticationError(
            "invalid key",
            response=httpx.Response(
                401,
                request=httpx.Request(
                    "POST", "https://api.openai.com/v1/responses"
                ),
            ),
            body=None,
        ),
        lambda: RateLimitError(
            "rate limited",
            response=httpx.Response(
                429,
                request=httpx.Request(
                    "POST", "https://api.openai.com/v1/responses"
                ),
            ),
            body=None,
        ),
    ],
    ids=("timeout", "authentication", "rate-limit"),
)
def test_openai_provider_converts_api_failures_to_unavailable(error_factory) -> None:
    client = Mock()
    client.responses.parse.side_effect = error_factory()
    provider = OpenAICostTypeRecommendationProvider(client, "test-model")

    with pytest.raises(RecommendationUnavailable):
        provider.recommend(
            ClassificationContext("", "", "", "", "", "", "", ""),
            [],
        )


def test_openai_provider_handles_incomplete_or_refused_responses() -> None:
    client = Mock()
    client.responses.parse.return_value = SimpleNamespace(
        status="incomplete",
        output_parsed=None,
    )
    provider = OpenAICostTypeRecommendationProvider(client, "test-model")

    with pytest.raises(RecommendationUnavailable):
        provider.recommend(
            ClassificationContext("", "", "", "", "", "", "", ""),
            [],
        )


@pytest.mark.django_db
def test_recommendation_filters_invalid_inactive_and_duplicate_codes() -> None:
    item = create_open_item()
    active = CostType.objects.create(code="ACTIVE", name="Active type")
    second = CostType.objects.create(code="SECOND", name="Second type")
    third = CostType.objects.create(code="THIRD", name="Third type")
    fourth = CostType.objects.create(code="FOURTH", name="Fourth type")
    CostType.objects.create(code="INACTIVE", name="Inactive type", active=False)
    response = CostTypeRecommendationResponse(
        abstain=False,
        abstain_reason="",
        recommendations=[
            CostTypeRecommendationCandidate(
                cost_type_code="UNKNOWN",
                reason="Invalid catalogue code.",
            ),
            CostTypeRecommendationCandidate(
                cost_type_code="INACTIVE",
                reason="Inactive catalogue code.",
            ),
            CostTypeRecommendationCandidate(
                cost_type_code="ACTIVE",
                reason="Valid active code.",
            ),
            CostTypeRecommendationCandidate(
                cost_type_code="SECOND",
                reason="Second valid code.",
            ),
            CostTypeRecommendationCandidate(
                cost_type_code="THIRD",
                reason="Third valid code.",
            ),
            CostTypeRecommendationCandidate(
                cost_type_code="FOURTH",
                reason="Fourth valid code.",
            ),
            CostTypeRecommendationCandidate(
                cost_type_code="ACTIVE",
                reason="Duplicate code.",
            ),
        ],
    )
    provider = StaticProvider(response)

    result = recommend_cost_types(item, None, provider)

    assert [candidate.cost_type for candidate in result.recommendations] == [
        active,
        second,
        third,
    ]
    assert result.recommendations[0].reason == "Valid active code."
    assert len(result.recommendations) == 3
    assert [option.code for option in provider.catalogue] == [
        "ACTIVE",
        "FOURTH",
        "SECOND",
        "THIRD",
    ]
    assert fourth.active


@pytest.mark.django_db
def test_recommendation_abstains_when_model_abstains_or_no_valid_code_remains() -> None:
    item = create_open_item()
    cost_type = CostType.objects.create(code="ACTIVE", name="Active type")
    abstaining_provider = StaticProvider(
        CostTypeRecommendationResponse(
            abstain=True,
            abstain_reason="Descriptions conflict.",
            recommendations=[],
        )
    )
    invalid_provider = StaticProvider(
        CostTypeRecommendationResponse(
            abstain=False,
            abstain_reason="",
            recommendations=[
                CostTypeRecommendationCandidate(
                    cost_type_code="UNLISTED",
                    reason="Not in the database.",
                )
            ],
        )
    )

    abstained = recommend_cost_types(item, None, abstaining_provider)
    invalid_only = recommend_cost_types(item, None, invalid_provider)

    assert abstained.recommendations == ()
    assert abstained.abstain_reason == "Descriptions conflict."
    assert invalid_only.recommendations == ()
    assert "No valid active Cost Type" in invalid_only.abstain_reason
    assert cost_type.active


@pytest.mark.django_db
@override_settings(LLM_COST_TYPE_RECOMMENDATIONS_ENABLED=False)
def test_feature_flag_disabled_preserves_manual_classification_page(
    monkeypatch,
) -> None:
    item = create_open_item()
    client = create_transaction_client()
    recommend = Mock()
    monkeypatch.setattr("transactions.views.recommend_cost_types", recommend)

    response = client.get(reverse("open-item-detail", args=[item.pk]))

    assert response.status_code == 200
    assert "AI Cost Type Recommendation" not in response.content.decode()
    recommend.assert_not_called()


@pytest.mark.django_db
@override_settings(LLM_COST_TYPE_RECOMMENDATIONS_ENABLED=True)
def test_enabled_feature_prefills_selection_without_writing_decisions_or_audit(
    monkeypatch,
) -> None:
    item = create_open_item()
    first = CostType.objects.create(code="FIRST", name="First choice")
    second = CostType.objects.create(code="SECOND", name="Second choice")
    result = RecommendationResult(
        recommendations=(
            recommendation("FIRST", "First explanation."),
            recommendation("SECOND", "Alternative explanation."),
        ),
        abstain_reason="",
    )
    recommend = Mock(return_value=result)
    monkeypatch.setattr("transactions.views.recommend_cost_types", recommend)
    client = create_transaction_client()
    decisions_before = ClassificationDecision.objects.count()
    audit_before = AuditEvent.objects.count()

    response = client.get(reverse("open-item-detail", args=[item.pk]))

    assert response.status_code == 200
    assert "AI Cost Type Recommendation" in response.content.decode()
    assert "First explanation." in response.content.decode()
    assert "Alternative explanation." in response.content.decode()
    assert (
        response.context["classification_form"].initial["final_cost_type"]
        == first.pk
    )
    assert ClassificationDecision.objects.count() == decisions_before
    assert AuditEvent.objects.count() == audit_before
    recommend.assert_called_once()
    assert second.active


@pytest.mark.django_db
@override_settings(LLM_COST_TYPE_RECOMMENDATIONS_ENABLED=True)
def test_existing_human_final_type_is_not_recommended_over_or_replaced(
    monkeypatch,
) -> None:
    item = create_open_item()
    human_type = CostType.objects.create(code="HUMAN", name="Human selection")
    decision = save_final_classification(
        item,
        human_type,
        "Human decision.",
    )
    recommend = Mock()
    monkeypatch.setattr("transactions.views.recommend_cost_types", recommend)

    response = create_transaction_client().get(
        reverse("open-item-detail", args=[item.pk])
    )

    assert response.status_code == 200
    assert response.context["current_decision"] == decision
    assert response.context["classification_form"].initial["final_cost_type"] == (
        human_type.pk
    )
    assert "AI Cost Type Recommendation" not in response.content.decode()
    recommend.assert_not_called()


@pytest.mark.django_db
@override_settings(LLM_COST_TYPE_RECOMMENDATIONS_ENABLED=True)
def test_unavailable_ai_keeps_manual_classification_and_post_does_not_call_api(
    monkeypatch,
) -> None:
    item = create_open_item()
    manual_type = CostType.objects.create(code="MANUAL", name="Manual type")
    recommend = Mock(side_effect=RecommendationUnavailable("timeout"))
    monkeypatch.setattr("transactions.views.recommend_cost_types", recommend)
    client = create_transaction_client()

    get_response = client.get(reverse("open-item-detail", args=[item.pk]))

    assert get_response.status_code == 200
    assert "temporarily unavailable" in get_response.content.decode()
    post_response = client.post(
        reverse("open-item-detail", args=[item.pk]),
        {
            "final_cost_type": manual_type.pk,
            "classification_reason": "Confirmed manually.",
            "recommendation_signature": "",
        },
    )

    assert post_response.status_code == 302
    assert ClassificationDecision.objects.get(
        open_item=item,
        is_current=True,
    ).final_cost_type == manual_type
    recommend.assert_called_once()


@pytest.mark.django_db
def test_untrusted_recommendation_token_cannot_set_suggested_cost_type(
    monkeypatch,
) -> None:
    item = create_open_item()
    human_type = CostType.objects.create(code="HUMAN", name="Human selection")
    forged_type = CostType.objects.create(code="FORGED", name="Forged suggestion")
    recommend = Mock()
    monkeypatch.setattr("transactions.views.recommend_cost_types", recommend)

    response = create_transaction_client().post(
        reverse("open-item-detail", args=[item.pk]),
        {
            "final_cost_type": human_type.pk,
            "classification_reason": "Confirmed manually.",
            "recommendation_signature": (
                f'{{"open_item_id": {item.pk}, '
                f'"cost_type_code": "{forged_type.code}"}}'
            ),
        },
    )

    decision = ClassificationDecision.objects.get(open_item=item, is_current=True)
    assert response.status_code == 302
    assert decision.final_cost_type == human_type
    assert decision.suggested_cost_type is None
    recommend.assert_not_called()


@pytest.mark.django_db
@override_settings(LLM_COST_TYPE_RECOMMENDATIONS_ENABLED=True)
def test_signed_recommendation_is_revalidated_as_active_on_submit(
    monkeypatch,
) -> None:
    item = create_open_item()
    suggested = CostType.objects.create(code="SUGGESTED", name="AI suggestion")
    human_type = CostType.objects.create(code="HUMAN", name="Human selection")
    result = RecommendationResult(
        recommendations=(recommendation("SUGGESTED"),),
        abstain_reason="",
    )
    recommend = Mock(return_value=result)
    monkeypatch.setattr("transactions.views.recommend_cost_types", recommend)
    client = create_transaction_client()

    get_response = client.get(reverse("open-item-detail", args=[item.pk]))
    signature = get_response.context["classification_form"][
        "recommendation_signature"
    ].value()
    suggested.active = False
    suggested.save(update_fields=["active"])

    post_response = client.post(
        reverse("open-item-detail", args=[item.pk]),
        {
            "final_cost_type": human_type.pk,
            "classification_reason": "Confirmed manually.",
            "recommendation_signature": signature,
        },
    )

    decision = ClassificationDecision.objects.get(open_item=item, is_current=True)
    assert post_response.status_code == 302
    assert decision.final_cost_type == human_type
    assert decision.suggested_cost_type is None
    recommend.assert_called_once()


@pytest.mark.django_db
@override_settings(LLM_COST_TYPE_RECOMMENDATIONS_ENABLED=True)
def test_human_override_saves_final_type_and_signed_suggestion_for_wht(
    monkeypatch,
) -> None:
    item = create_open_item()
    suggested = CostType.objects.create(code="SUGGESTED", name="AI suggestion")
    human_choice = CostType.objects.create(code="HUMAN", name="Human choice")
    result = RecommendationResult(
        recommendations=(recommendation("SUGGESTED"),),
        abstain_reason="",
    )
    recommend = Mock(return_value=result)
    monkeypatch.setattr("transactions.views.recommend_cost_types", recommend)
    client = create_transaction_client()

    get_response = client.get(reverse("open-item-detail", args=[item.pk]))
    signature = get_response.context["classification_form"][
        "recommendation_signature"
    ].value()
    post_response = client.post(
        reverse("open-item-detail", args=[item.pk]),
        {
            "final_cost_type": human_choice.pk,
            "classification_reason": "Human chose a different Cost Type.",
            "recommendation_signature": signature,
        },
    )

    decision = ClassificationDecision.objects.get(open_item=item, is_current=True)
    assert post_response.status_code == 302
    assert decision.final_cost_type == human_choice
    assert decision.suggested_cost_type == suggested
    assert decision.classification_method == ClassificationDecision.Method.HUMAN
    recommend.assert_called_once()

    supplier_line = (
        OpenItem.objects.exclude(vendor_number="")
        .filter(
            entity_code=item.entity_code,
            accounting_document_number=item.accounting_document_number,
        )
        .first()
    )
    assert supplier_line is not None
    country = "GB"
    SupplierCountry.objects.create(
        supplier_id=supplier_line.vendor_number,
        country_code=country,
        supplier_name="Synthetic supplier",
    )
    mapping = WHTMapping.objects.create(
        supplier=supplier_line.vendor_number,
        supplier_name="Synthetic supplier",
        cost_category=human_choice,
        wht_rate="0.050000",
        country_code=country,
        source_title="Synthetic test mapping",
        version=1,
        status=WHTMapping.Status.APPROVED,
    )

    determination = determine_wht(item, decision)

    assert determination.matched_mapping == mapping
    assert determination.final_cost_type_snapshot == human_choice.code
    assert determination.final_cost_type_snapshot != suggested.code


@pytest.mark.django_db
def test_active_cost_type_catalogue_and_context_are_the_only_provider_inputs() -> None:
    item = create_open_item()
    active = CostType.objects.create(
        code="ACTIVE",
        name="Active",
        description="Synthetic active description",
    )
    CostType.objects.create(
        code="INACTIVE",
        name="Inactive",
        description="Must not be sent",
        active=False,
    )
    provider = StaticProvider(
        CostTypeRecommendationResponse(
            abstain=True,
            abstain_reason="No evidence.",
            recommendations=[],
        )
    )

    recommend_cost_types(item, None, provider)

    assert [option.code for option in provider.catalogue] == [active.code]
    context_fields = set(provider.context.__dataclass_fields__)
    assert context_fields == {
        "gl_account",
        "gl_account_description",
        "accounting_description",
        "assignment",
        "material_description",
        "material_data_short_text",
        "material_group",
        "purchasing_document_type",
    }
