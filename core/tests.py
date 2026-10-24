import pytest
from django.contrib.auth.models import User
from django.urls import reverse


@pytest.mark.django_db
def test_home_page(client) -> None:
    response = client.get(reverse("home"))

    assert response.status_code == 302
    assert response.url == reverse("dashboard")


@pytest.mark.django_db
def test_authenticated_user_can_sign_out(client) -> None:
    user = User.objects.create_user(username="logout-user", password="test-password")
    client.force_login(user)

    response = client.post(reverse("logout"))

    assert response.status_code == 302
    assert response.url == reverse("home")
    assert "_auth_user_id" not in client.session


@pytest.mark.django_db
def test_successful_sign_in_redirects_to_dashboard(client) -> None:
    User.objects.create_user(username="login-user", password="test-password")

    response = client.post(
        reverse("login"),
        {"username": "login-user", "password": "test-password"},
    )

    assert response.status_code == 302
    assert response.url == reverse("dashboard")
