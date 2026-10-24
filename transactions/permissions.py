from collections.abc import Callable
from functools import wraps

from django.http import HttpRequest, HttpResponse, HttpResponseForbidden


TRANSACTION_USER_ROLE = "Transaction User"
LEGACY_TRANSACTION_USER_ROLES = frozenset({"Classifier", "Reviewer"})
TRANSACTION_USER_ROLES = LEGACY_TRANSACTION_USER_ROLES | {
    TRANSACTION_USER_ROLE
}


def has_role(user, *roles: str) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser:
        return True

    requested_roles = set(roles)
    if requested_roles & TRANSACTION_USER_ROLES:
        requested_roles.update(TRANSACTION_USER_ROLES)
    return user.groups.filter(name__in=requested_roles).exists()


def role_required(*roles: str) -> Callable:
    def decorator(view: Callable) -> Callable:
        @wraps(view)
        def wrapped(request: HttpRequest, *args, **kwargs) -> HttpResponse:
            if not request.user.is_authenticated:
                from django.contrib.auth.views import redirect_to_login

                return redirect_to_login(request.get_full_path())
            if not has_role(request.user, *roles):
                return HttpResponseForbidden("You are not authorized for this action.")
            return view(request, *args, **kwargs)

        return wrapped

    return decorator
