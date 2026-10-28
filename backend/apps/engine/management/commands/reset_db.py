from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS, connections


class Command(BaseCommand):
    help = (
        "Flush every table in the database, then run VACUUM ANALYZE to reclaim "
        "space and refresh planner statistics."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--database",
            default=DEFAULT_DB_ALIAS,
            choices=tuple(connections),
            help='Database alias to reset (default: "default").',
        )
        parser.add_argument(
            "--noinput",
            "--no-input",
            action="store_false",
            dest="interactive",
            help="Skip the confirmation prompt.",
        )

    def handle(self, *args, **options):
        alias = options["database"]
        connection = connections[alias]

        if connection.vendor != "postgresql":
            raise CommandError("VACUUM ANALYZE requires a PostgreSQL database.")

        if options["interactive"]:
            confirm = input(
                'This will IRREVERSIBLY DESTROY all data in the "%s" database.\n'
                "Type 'yes' to continue, or 'no' to cancel: " % connection.settings_dict["NAME"]
            )
            if confirm != "yes":
                self.stdout.write("Reset cancelled.")
                return

        self.stdout.write("Flushing database ...")
        call_command(
            "flush",
            interactive=False,
            verbosity=options["verbosity"],
            database=alias,
        )

        self.stdout.write("Running VACUUM ANALYZE ...")
        with connection.cursor() as cursor:
            cursor.execute("VACUUM ANALYZE")

        self.stdout.write(self.style.SUCCESS(f'Reset database "{alias}" and vacuum-analyzed it.'))
