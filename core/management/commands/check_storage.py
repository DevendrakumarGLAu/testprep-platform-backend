import base64
import urllib.error
import urllib.request

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage, storages
from django.core.management.base import BaseCommand, CommandError

from core.media import unique_upload_path

# A 1 x 1 pixel PNG.
PIXEL = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==")


class Command(BaseCommand):
    help = (
        "Checks where question images are stored: saves a tiny test picture, reads it back, opens its public address like a student's "
        "browser would, and removes it. Run it after setting the SUPABASE_S3_* settings to be sure uploads will work."
    )

    def handle(self, *args, **options):
        real = storages["default"]  # default_storage itself is only a stand-in for this
        backend = f"{type(real).__module__}.{type(real).__name__}"
        self.stdout.write(f"Storage: {backend}")
        if not backend.startswith("storages."):
            self.stdout.write("Pictures are kept in the local media folder (set SUPABASE_S3_ENDPOINT to use Supabase).")
        name = None
        try:
            name = default_storage.save(unique_upload_path("healthcheck", "check.png"), ContentFile(PIXEL))
            self.stdout.write(f"Saved: {name}")
            with default_storage.open(name) as f:
                if f.read() != PIXEL:
                    raise CommandError("The file read back is not the one that was saved.")
            self.stdout.write("Read back: ok")
            url = default_storage.url(name)
            self.stdout.write(f"Public address: {url}")
            if url.startswith("http"):
                try:
                    with urllib.request.urlopen(url, timeout=20) as r:
                        kind = r.headers.get("Content-Type", "")
                        if r.status != 200 or not kind.startswith("image/"):
                            raise CommandError(f"The address answered {r.status} ({kind}). Is the bucket public?")
                except urllib.error.HTTPError as e:
                    raise CommandError(f"The public address answered {e.code}. Make the bucket public in Supabase (Storage > bucket > Public).")
                except urllib.error.URLError as e:
                    raise CommandError(f"Could not open the public address: {e.reason}")
                self.stdout.write("Opened like a browser: ok")
        except CommandError:
            raise
        except Exception as e:  # noqa: BLE001 - any storage error is worth showing as it is
            raise CommandError(f"Could not save to the storage: {type(e).__name__}: {e}")
        finally:
            if name:
                try:
                    default_storage.delete(name)
                    self.stdout.write("Removed the test file.")
                except Exception:  # noqa: BLE001
                    self.stdout.write(self.style.WARNING(f"Could not remove the test file {name}; delete it by hand."))
        self.stdout.write(self.style.SUCCESS("Storage works."))
