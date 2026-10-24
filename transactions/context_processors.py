from .permissions import has_role


def import_center_access(request):
    return {
        "can_access_import_center": (
            request.user.is_authenticated
            and has_role(request.user, "Administrator")
        )
    }


def exceptions_access(request):
    can_administer = (
        request.user.is_authenticated
        and has_role(request.user, "Administrator")
    )
    return {
        "can_access_exceptions": can_administer,
        "can_access_django_admin": can_administer and request.user.is_staff,
    }
