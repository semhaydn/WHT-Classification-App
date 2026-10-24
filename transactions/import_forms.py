from django import forms
from django.conf import settings


class ImportCenterForm(forms.Form):
    material_data_file = forms.FileField(
        required=False,
        label="MaterialData CSV",
        help_text="Purchasing data matched by Purchasing Document + Item.",
        widget=forms.ClearableFileInput(attrs={"accept": ".csv,text/csv"}),
    )
    open_items_file = forms.FileField(
        required=False,
        label="Open Line Items CSV",
        help_text="Accounting lines, including vendor-control and WHT postings.",
        widget=forms.ClearableFileInput(attrs={"accept": ".csv,text/csv"}),
    )

    def clean_material_data_file(self):
        return self._validate_file(self.cleaned_data.get("material_data_file"))

    def clean_open_items_file(self):
        return self._validate_file(self.cleaned_data.get("open_items_file"))

    def clean(self):
        cleaned_data = super().clean()
        if not cleaned_data.get("material_data_file") and not cleaned_data.get(
            "open_items_file"
        ):
            raise forms.ValidationError("Select at least one CSV file to preview.")
        return cleaned_data

    def _validate_file(self, uploaded_file):
        if uploaded_file is None:
            return None
        if not uploaded_file.name.lower().endswith(".csv"):
            raise forms.ValidationError("Upload a .csv file.")
        if uploaded_file.size > settings.WHT_IMPORT_MAX_FILE_SIZE:
            raise forms.ValidationError(
                "File exceeds the configured upload-size limit."
            )
        return uploaded_file
