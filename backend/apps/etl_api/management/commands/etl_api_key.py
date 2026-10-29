from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.etl_api.keys import create_client
from apps.etl_api.models import ApiClient


class Command(BaseCommand):
    help = "Manage ETL API keys (create / revoke / list)."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", required=True)
        c = sub.add_parser("create", help="Create a key (printed once).")
        c.add_argument("--name", required=True)
        c.add_argument("--rate", type=int, default=120)
        r = sub.add_parser("revoke", help="Revoke a key by prefix.")
        r.add_argument("prefix")
        sub.add_parser("list", help="List keys.")

    def handle(self, *args, **opts):
        action = opts["action"]
        if action == "create":
            row, key = create_client(opts["name"], rate_limit_per_min=opts["rate"])
            self.stdout.write(f"Created client {row.name} ({row.key_prefix}).")
            self.stdout.write("Store this key now; it is not shown again:")
            self.stdout.write(key)
        elif action == "revoke":
            row = ApiClient.objects.filter(key_prefix=opts["prefix"]).first()
            if row is None:
                raise CommandError(f"No key with prefix {opts['prefix']}")
            if row.revoked_at is None:
                row.revoked_at = timezone.now()
                row.save(update_fields=["revoked_at"])
            self.stdout.write(f"Revoked {row.key_prefix}.")
        else:
            for c in ApiClient.objects.order_by("created_at"):
                self.stdout.write(
                    "\t".join(
                        [
                            c.key_prefix,
                            c.name,
                            str(c.rate_limit_per_min),
                            str(c.created_at),
                            str(c.last_used_at or "-"),
                            str(c.revoked_at or "-"),
                        ]
                    )
                )
