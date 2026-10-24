from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect


def home(request: HttpRequest) -> HttpResponse:
    return redirect("dashboard")
