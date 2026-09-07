"""Transactional email rendering."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent

_ENV = Environment(loader=FileSystemLoader(_PACKAGE_ROOT / "mail_templates"), autoescape=True)


def render(template_name: str, **context: object) -> str:
    """Render one transactional email body.

    Preconditions:
        * ``template_name`` names a template under the mail template directory.
    Postconditions:
        * Returns the rendered body as a string.
    """
    assert template_name, "template_name is required"
    return _ENV.get_template(template_name).render(**context)
