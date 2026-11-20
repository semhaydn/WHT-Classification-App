from django.conf import settings
from django.contrib.staticfiles.management.commands.runserver import (
    Command as StaticfilesRunserverCommand,
)
from django.core.management.base import CommandError


class Command(StaticfilesRunserverCommand):
    def handle(self, *args, **options):
        if (
            settings.DEPLOYMENT_ENVIRONMENT == "development"
            and not settings.DEBUG
            and options["use_static_handler"]
            and not options["insecure_serving"]
        ):
            raise CommandError(
                "DJANGO_DEBUG is False, so runserver will not serve admin static "
                "files. For local development set DJANGO_DEBUG=true, or use "
                "scripts/start_codespaces_preview.sh in Codespaces. To run "
                "intentionally without static files, pass --nostatic."
            )
        return super().handle(*args, **options)
