"""Captive portal form parsing, field classification, and auto-fill.

Uses only Python stdlib (html.parser) — no external dependencies.
Designed for headless operation on a Raspberry Pi.
"""

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urljoin

from logging_config import get_logger

logger = get_logger('portal_forms')


@dataclass
class FormField:
    """A single form field extracted from HTML."""
    tag: str  # 'input', 'select', 'textarea'
    name: str
    field_type: str  # HTML type attribute (text, email, hidden, checkbox, etc.)
    value: str  # Pre-filled value from HTML
    placeholder: str
    required: bool
    field_id: str
    options: list[str] = field(default_factory=list)  # For <select>


@dataclass
class FormData:
    """A parsed HTML form."""
    action: str
    method: str  # GET or POST
    fields: list[FormField]
    enctype: str = 'application/x-www-form-urlencoded'


class PortalFormParser(HTMLParser):
    """Extract forms and their fields from portal HTML pages."""

    def __init__(self):
        super().__init__()
        self.forms: list[FormData] = []
        self._current_form: Optional[FormData] = None
        self._in_select = False
        self._current_select_name = ''
        self._current_select_options: list[str] = []
        self._current_textarea_name = ''
        self._in_textarea = False

    def parse(self, html: str, base_url: str = '') -> list[FormData]:
        """Parse HTML and return all forms found."""
        self.forms = []
        self._base_url = base_url
        self.feed(html)
        # Close any unclosed form
        if self._current_form:
            self.forms.append(self._current_form)
            self._current_form = None
        return self.forms

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        tag_lower = tag.lower()

        if tag_lower == 'form':
            action = attr.get('action', '')
            if action and self._base_url and not action.startswith('http'):
                action = urljoin(self._base_url, action)
            self._current_form = FormData(
                action=action,
                method=attr.get('method', 'POST').upper(),
                fields=[],
                enctype=attr.get('enctype', 'application/x-www-form-urlencoded'),
            )

        elif tag_lower == 'input' and self._current_form is not None:
            name = attr.get('name', '')
            if not name:
                # Submit buttons without name are still useful
                if attr.get('type', '').lower() == 'submit':
                    name = '__submit__'
                else:
                    return
            self._current_form.fields.append(FormField(
                tag='input',
                name=name,
                field_type=attr.get('type', 'text').lower(),
                value=attr.get('value', ''),
                placeholder=attr.get('placeholder', ''),
                required='required' in attr,
                field_id=attr.get('id', ''),
            ))

        elif tag_lower == 'select' and self._current_form is not None:
            self._in_select = True
            self._current_select_name = attr.get('name', '')
            self._current_select_options = []

        elif tag_lower == 'option' and self._in_select:
            val = attr.get('value', '')
            if val:
                self._current_select_options.append(val)

        elif tag_lower == 'textarea' and self._current_form is not None:
            self._in_textarea = True
            self._current_textarea_name = attr.get('name', '')

        elif tag_lower == 'button' and self._current_form is not None:
            # Treat submit buttons as fields
            btn_type = attr.get('type', 'submit').lower()
            if btn_type == 'submit':
                name = attr.get('name', '__submit__')
                self._current_form.fields.append(FormField(
                    tag='button',
                    name=name,
                    field_type='submit',
                    value=attr.get('value', ''),
                    placeholder='',
                    required=False,
                    field_id=attr.get('id', ''),
                ))

    def handle_endtag(self, tag):
        tag_lower = tag.lower()

        if tag_lower == 'form' and self._current_form is not None:
            self.forms.append(self._current_form)
            self._current_form = None

        elif tag_lower == 'select' and self._in_select:
            if self._current_form and self._current_select_name:
                self._current_form.fields.append(FormField(
                    tag='select',
                    name=self._current_select_name,
                    field_type='select',
                    value=self._current_select_options[0] if self._current_select_options else '',
                    placeholder='',
                    required=False,
                    field_id='',
                    options=self._current_select_options,
                ))
            self._in_select = False

        elif tag_lower == 'textarea' and self._in_textarea:
            if self._current_form and self._current_textarea_name:
                self._current_form.fields.append(FormField(
                    tag='textarea',
                    name=self._current_textarea_name,
                    field_type='textarea',
                    value='',
                    placeholder='',
                    required=False,
                    field_id='',
                ))
            self._in_textarea = False

    def handle_data(self, data):
        pass  # We don't need text content


# --- Field Classification ---

# Patterns for field type detection (name, id, placeholder checked)
_FIELD_PATTERNS = [
    ('email', re.compile(r'e[-_]?mail|correo', re.I)),
    ('first_name', re.compile(r'first[-_]?name|fname|given[-_]?name|prenom', re.I)),
    ('last_name', re.compile(r'last[-_]?name|lname|surname|family[-_]?name', re.I)),
    ('name', re.compile(r'\bname\b|full[-_]?name|your[-_]?name|nombre', re.I)),
    ('phone', re.compile(r'phone|mobile|tel|cellphone|numero', re.I)),
    ('zip', re.compile(r'zip|postal|postcode|plz', re.I)),
    ('room_number', re.compile(r'room|habitacion|zimmer|chambre', re.I)),
    ('company', re.compile(r'company|organization|empresa|firma', re.I)),
    ('country', re.compile(r'country|pais|land|pays', re.I)),
]

_TERMS_PATTERN = re.compile(r'terms|agree|accept|condition|tos|privacy|consent|gdpr', re.I)
_MARKETING_PATTERN = re.compile(r'market|opt[-_]?in|newsletter|promo|subscribe|news', re.I)


def classify_field(f: FormField) -> str:
    """Classify a form field into a semantic category.

    Returns one of: email, name, first_name, last_name, phone, zip,
    room_number, company, country, terms_checkbox, marketing_optin,
    hidden, submit, password, unknown
    """
    ft = f.field_type.lower()
    hints = f'{f.name} {f.field_id} {f.placeholder}'.lower()

    # HTML type-based shortcuts
    if ft == 'email':
        return 'email'
    if ft == 'hidden':
        return 'hidden'
    if ft == 'submit':
        return 'submit'
    if ft == 'password':
        return 'password'
    if f.tag == 'button' and ft == 'submit':
        return 'submit'

    # Checkbox classification
    if ft == 'checkbox':
        if _TERMS_PATTERN.search(hints):
            return 'terms_checkbox'
        if _MARKETING_PATTERN.search(hints):
            return 'marketing_optin'
        return 'checkbox'

    # Radio buttons — just preserve value
    if ft == 'radio':
        return 'radio'

    # Text/tel/etc — classify by name patterns
    for category, pattern in _FIELD_PATTERNS:
        if pattern.search(hints):
            return category

    return 'unknown'


# --- Auto-Fill ---

DEFAULT_IDENTITY = {
    'email': 'traveler@vasili.local',
    'name': 'J. Traveler',
    'first_name': 'J.',
    'last_name': 'Traveler',
    'phone': '',
    'zip': '10001',
    'room_number': '101',
    'company': '',
    'country': 'US',
}


def autofill_form(form: FormData, identity: dict = None) -> dict[str, str]:
    """Build POST data for a form by auto-filling fields.

    Args:
        form: Parsed FormData
        identity: Override identity values (merged over defaults)

    Returns:
        Dict of field_name -> value for POST submission
    """
    ident = dict(DEFAULT_IDENTITY)
    if identity:
        ident.update({k: v for k, v in identity.items() if v})

    data = {}

    for f in form.fields:
        category = classify_field(f)
        logger.debug(f'Field "{f.name}" type={f.field_type} -> {category}')

        if category == 'hidden':
            # Preserve server-set value (CSRF tokens, session IDs)
            data[f.name] = f.value
        elif category == 'submit':
            # Include submit button value if it has a name
            if f.name and f.name != '__submit__':
                data[f.name] = f.value or '1'
        elif category == 'terms_checkbox' or category == 'marketing_optin':
            data[f.name] = f.value or 'on'
        elif category == 'checkbox':
            # Generic checkbox — check it
            data[f.name] = f.value or 'on'
        elif category == 'radio':
            # Use pre-selected value or first option
            if f.name not in data:
                data[f.name] = f.value
        elif category == 'password':
            # Skip password fields — we don't have credentials
            continue
        elif category in ident:
            data[f.name] = ident[category]
        elif f.tag == 'select' and f.options:
            # Pick first non-empty option
            data[f.name] = f.options[0] if f.options else ''
        else:
            # Unknown field — include empty to avoid missing-field errors
            data[f.name] = f.value or ''

    return data


def parse_and_fill(html: str, base_url: str, identity: dict = None) -> list[tuple[FormData, dict]]:
    """Parse HTML and return auto-filled form data for all forms found.

    Returns list of (FormData, filled_data) tuples, best candidate first.
    Forms with more fillable fields are ranked higher.
    """
    parser = PortalFormParser()
    forms = parser.parse(html, base_url)

    results = []
    for form in forms:
        filled = autofill_form(form, identity)
        # Score: more classified fields = more likely the main form
        score = sum(1 for f in form.fields if classify_field(f) != 'unknown')
        results.append((form, filled, score))

    # Sort by score descending (most recognized fields first)
    results.sort(key=lambda x: x[2], reverse=True)
    return [(form, filled) for form, filled, _ in results]
