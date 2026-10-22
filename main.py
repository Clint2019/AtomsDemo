"""Entry point: configuration, composition, serving only. No domain logic."""
from atoms_demo.config import load_settings
from atoms_demo.bootstrap import compose_application
from atoms_demo.api.server import serve


def main() -> None:
    settings = load_settings()
    application = compose_application(settings)
    serve(application, settings)


if __name__ == "__main__":
    main()
