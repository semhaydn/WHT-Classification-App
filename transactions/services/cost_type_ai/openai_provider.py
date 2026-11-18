from collections.abc import Sequence

from django.conf import settings
from openai import OpenAI, OpenAIError
from pydantic import ValidationError

from .prompts import SYSTEM_INSTRUCTIONS, build_recommendation_input
from .provider import (
    ClassificationContext,
    CostTypeOption,
    RecommendationUnavailable,
)
from .schemas import CostTypeRecommendationResponse


class OpenAICostTypeRecommendationProvider:
    def __init__(self, client: OpenAI, model: str) -> None:
        self.client = client
        self.model = model

    @classmethod
    def from_settings(cls) -> "OpenAICostTypeRecommendationProvider":
        if not settings.OPENAI_API_KEY:
            raise RecommendationUnavailable(
                "The OpenAI API key is not configured."
            )
        if not settings.OPENAI_COST_TYPE_MODEL:
            raise RecommendationUnavailable(
                "The OpenAI Cost Type model is not configured."
            )
        client = OpenAI(
            api_key=settings.OPENAI_API_KEY,
            timeout=8.0,
            max_retries=1,
        )
        return cls(client=client, model=settings.OPENAI_COST_TYPE_MODEL)

    def recommend(
        self,
        context: ClassificationContext,
        catalogue: Sequence[CostTypeOption],
    ) -> CostTypeRecommendationResponse:
        try:
            response = self.client.responses.parse(
                model=self.model,
                input=[
                    {"role": "system", "content": SYSTEM_INSTRUCTIONS},
                    {
                        "role": "user",
                        "content": build_recommendation_input(context, catalogue),
                    },
                ],
                text_format=CostTypeRecommendationResponse,
                store=False,
            )
        except (OpenAIError, ValidationError) as error:
            raise RecommendationUnavailable(
                "The OpenAI recommendation request failed."
            ) from error

        if response.status != "completed" or response.output_parsed is None:
            raise RecommendationUnavailable(
                "The OpenAI service returned an incomplete or refused response."
            )
        return response.output_parsed
