from django import forms

from reference_data.models import CostType


class ClassificationDecisionForm(forms.Form):
    final_cost_type = forms.ModelChoiceField(
        queryset=CostType.objects.none(),
        empty_label="Select a Cost Type",
    )
    classification_reason = forms.CharField(
        widget=forms.Textarea,
        min_length=1,
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fields["final_cost_type"].queryset = CostType.objects.filter(active=True)


class ReviewActionForm(forms.Form):
    comments = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    override_reason = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )


class ReviewDecisionForm(ReviewActionForm):
    final_cost_type = forms.ModelChoiceField(
        queryset=CostType.objects.none(),
        required=False,
        empty_label="Keep classifier decision",
    )
    def __init__(self, *args, initial_cost_type=None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fields["final_cost_type"].queryset = CostType.objects.filter(active=True)
        if initial_cost_type is not None:
            self.fields["final_cost_type"].initial = initial_cost_type


class LineFlagForm(forms.Form):
    reason = forms.CharField(
        min_length=1,
        widget=forms.Textarea(attrs={"rows": 3}),
        label="Reason for Administrator review",
    )


class WHTExceptionResolutionForm(forms.Form):
    treatment = forms.ChoiceField(
        choices=(("RATE", "Apply one-off WHT rate"), ("NO_WHT", "Explicit No WHT")),
    )
    manual_rate = forms.DecimalField(
        required=False,
        min_value=0,
        max_digits=7,
        decimal_places=6,
        label="One-off WHT rate",
    )
    reason = forms.CharField(
        min_length=1,
        widget=forms.Textarea(attrs={"rows": 3}),
        label="Mandatory resolution reason",
    )

    def clean(self):
        cleaned_data = super().clean()
        treatment = cleaned_data.get("treatment")
        manual_rate = cleaned_data.get("manual_rate")
        if treatment == "RATE" and manual_rate is None:
            self.add_error("manual_rate", "Enter a rate for this treatment.")
        if treatment == "RATE" and manual_rate == 0:
            self.add_error(
                "manual_rate",
                "Use Explicit No WHT instead of a zero one-off rate.",
            )
        if treatment == "NO_WHT" and manual_rate is not None:
            self.add_error(
                "manual_rate",
                "A manual rate is not used for an explicit No WHT decision.",
            )
        return cleaned_data


class LineFlagResolutionForm(forms.Form):
    action = forms.ChoiceField(
        choices=(("CLEAR", "Clear flag"), ("EXCLUDE", "Exclude line")),
    )
    decision = forms.CharField(
        min_length=1,
        widget=forms.Textarea(attrs={"rows": 3}),
        label="Mandatory administrative decision",
    )


class LineExclusionForm(forms.Form):
    reason = forms.CharField(
        min_length=1,
        widget=forms.Textarea(attrs={"rows": 3}),
        label="Mandatory exclusion reason",
    )
