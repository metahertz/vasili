"""Unit tests for portal_forms — form parsing, classification, and auto-fill."""

import pytest

from portal_forms import (
    PortalFormParser, FormField, FormData,
    classify_field, autofill_form, parse_and_fill,
    DEFAULT_IDENTITY,
)


# --- Sample portal HTML ---

HOTEL_PORTAL = """
<html><body>
<h1>Welcome to Grand Hotel WiFi</h1>
<form action="/guest/connect" method="POST">
  <input type="hidden" name="csrf_token" value="abc123xyz">
  <input type="hidden" name="session_id" value="sess_456">
  <input type="text" name="guest_name" placeholder="Your Full Name" required>
  <input type="email" name="email_address" placeholder="Email" required>
  <input type="text" name="room_number" placeholder="Room Number">
  <input type="checkbox" name="terms" value="agreed"> I accept the terms
  <input type="checkbox" name="marketing" value="yes"> Send me offers
  <button type="submit">Connect</button>
</form>
</body></html>
"""

AIRPORT_SPLASH = """
<html><body>
<form action="https://portal.airport.com/accept" method="POST">
  <input type="hidden" name="token" value="t0k3n">
  <input type="checkbox" name="accept_terms" value="1" required>
  <input type="submit" name="submit" value="Connect to WiFi">
</form>
</body></html>
"""

MARKETING_PORTAL = """
<html><body>
<form action="/api/register" method="post">
  <input type="hidden" name="_xsrf" value="csrf789">
  <input type="email" name="user_email" placeholder="Enter your email" required>
  <input type="text" name="first_name" placeholder="First Name">
  <input type="text" name="last_name" placeholder="Last Name">
  <input type="text" name="zipcode" placeholder="ZIP Code">
  <select name="country">
    <option value="US">United States</option>
    <option value="UK">United Kingdom</option>
  </select>
  <input type="checkbox" name="newsletter_optin" value="on"> Subscribe
  <input type="checkbox" name="agree_terms" value="1" required> Terms
  <input type="submit" value="Get Online">
</form>
</body></html>
"""

MULTI_FORM_PAGE = """
<html><body>
<form action="/search" method="GET">
  <input type="text" name="q" placeholder="Search...">
</form>
<form action="/portal/submit" method="POST">
  <input type="hidden" name="tk" value="secret">
  <input type="email" name="email" required>
  <input type="checkbox" name="tos" value="1">
  <button type="submit">Go Online</button>
</form>
</body></html>
"""

NO_FORM_PAGE = "<html><body><h1>Connected!</h1></body></html>"

PASSWORD_PORTAL = """
<form action="/login" method="POST">
  <input type="text" name="username" placeholder="Username">
  <input type="password" name="password" placeholder="Password">
  <input type="submit" value="Login">
</form>
"""


@pytest.mark.unit
class TestPortalFormParser:
    def test_hotel_portal(self):
        parser = PortalFormParser()
        forms = parser.parse(HOTEL_PORTAL, 'https://hotel.example.com')

        assert len(forms) == 1
        form = forms[0]
        assert form.action == 'https://hotel.example.com/guest/connect'
        assert form.method == 'POST'
        assert len(form.fields) >= 6

        names = {f.name for f in form.fields}
        assert 'csrf_token' in names
        assert 'guest_name' in names
        assert 'email_address' in names
        assert 'room_number' in names
        assert 'terms' in names

    def test_airport_splash(self):
        parser = PortalFormParser()
        forms = parser.parse(AIRPORT_SPLASH)

        assert len(forms) == 1
        assert forms[0].action == 'https://portal.airport.com/accept'
        assert any(f.name == 'token' and f.field_type == 'hidden' for f in forms[0].fields)

    def test_marketing_portal_with_select(self):
        parser = PortalFormParser()
        forms = parser.parse(MARKETING_PORTAL)

        assert len(forms) == 1
        select_fields = [f for f in forms[0].fields if f.tag == 'select']
        assert len(select_fields) == 1
        assert select_fields[0].name == 'country'
        assert 'US' in select_fields[0].options

    def test_multi_form_page(self):
        parser = PortalFormParser()
        forms = parser.parse(MULTI_FORM_PAGE)
        assert len(forms) == 2

    def test_no_form(self):
        parser = PortalFormParser()
        forms = parser.parse(NO_FORM_PAGE)
        assert len(forms) == 0

    def test_relative_action_resolved(self):
        parser = PortalFormParser()
        forms = parser.parse(
            '<form action="/submit"><input name="x"></form>',
            'https://portal.example.com/page'
        )
        assert forms[0].action == 'https://portal.example.com/submit'

    def test_default_method_is_post(self):
        parser = PortalFormParser()
        forms = parser.parse('<form action="/x"><input name="y"></form>')
        assert forms[0].method == 'POST'


@pytest.mark.unit
class TestFieldClassifier:
    def _field(self, **kwargs):
        defaults = dict(
            tag='input', name='', field_type='text', value='',
            placeholder='', required=False, field_id='',
        )
        defaults.update(kwargs)
        return FormField(**defaults)

    def test_email_by_type(self):
        assert classify_field(self._field(field_type='email', name='x')) == 'email'

    def test_email_by_name(self):
        assert classify_field(self._field(name='user_email')) == 'email'

    def test_hidden(self):
        assert classify_field(self._field(field_type='hidden', name='csrf')) == 'hidden'

    def test_submit(self):
        assert classify_field(self._field(field_type='submit', name='go')) == 'submit'

    def test_terms_checkbox(self):
        assert classify_field(self._field(
            field_type='checkbox', name='accept_terms'
        )) == 'terms_checkbox'

    def test_marketing_checkbox(self):
        assert classify_field(self._field(
            field_type='checkbox', name='newsletter_optin'
        )) == 'marketing_optin'

    def test_first_name(self):
        assert classify_field(self._field(name='first_name')) == 'first_name'
        assert classify_field(self._field(name='fname')) == 'first_name'

    def test_last_name(self):
        assert classify_field(self._field(name='last_name')) == 'last_name'
        assert classify_field(self._field(name='surname')) == 'last_name'

    def test_name_generic(self):
        assert classify_field(self._field(name='name')) == 'name'
        assert classify_field(self._field(name='full_name')) == 'name'

    def test_phone(self):
        assert classify_field(self._field(name='phone')) == 'phone'
        assert classify_field(self._field(name='mobile_number')) == 'phone'

    def test_zip(self):
        assert classify_field(self._field(name='zipcode')) == 'zip'
        assert classify_field(self._field(name='postal_code')) == 'zip'

    def test_room(self):
        assert classify_field(self._field(name='room_number')) == 'room_number'

    def test_password_skipped(self):
        assert classify_field(self._field(field_type='password', name='pw')) == 'password'

    def test_unknown(self):
        assert classify_field(self._field(name='xyzzy_field')) == 'unknown'

    def test_placeholder_used(self):
        assert classify_field(self._field(
            name='field1', placeholder='Enter your email'
        )) == 'email'


@pytest.mark.unit
class TestAutoFill:
    def test_hotel_form(self):
        parser = PortalFormParser()
        forms = parser.parse(HOTEL_PORTAL, 'https://hotel.example.com')
        filled = autofill_form(forms[0])

        assert filled['csrf_token'] == 'abc123xyz'  # Hidden preserved
        assert filled['session_id'] == 'sess_456'
        assert '@' in filled['email_address']  # Email filled
        assert filled['terms'] == 'agreed'  # Checkbox checked
        assert filled['room_number']  # Room filled

    def test_marketing_portal(self):
        parser = PortalFormParser()
        forms = parser.parse(MARKETING_PORTAL)
        filled = autofill_form(forms[0])

        assert filled['_xsrf'] == 'csrf789'  # CSRF preserved
        assert '@' in filled['user_email']
        assert filled['first_name']  # Name filled
        assert filled['last_name']
        assert filled['agree_terms'] == '1'
        assert filled['newsletter_optin'] == 'on'
        assert filled['country'] == 'US'  # First select option

    def test_custom_identity(self):
        parser = PortalFormParser()
        forms = parser.parse(MARKETING_PORTAL)
        filled = autofill_form(forms[0], identity={
            'email': 'me@test.com',
            'first_name': 'Alice',
            'last_name': 'Smith',
        })

        assert filled['user_email'] == 'me@test.com'
        assert filled['first_name'] == 'Alice'
        assert filled['last_name'] == 'Smith'

    def test_password_fields_skipped(self):
        parser = PortalFormParser()
        forms = parser.parse(PASSWORD_PORTAL)
        filled = autofill_form(forms[0])

        assert 'password' not in filled


@pytest.mark.unit
class TestParseAndFill:
    def test_multi_form_best_first(self):
        results = parse_and_fill(MULTI_FORM_PAGE, 'https://example.com')

        # The portal form (with email + tos) should rank higher than search form
        assert len(results) == 2
        best_form, best_data = results[0]
        assert 'email' in best_data or 'tos' in best_data

    def test_no_forms(self):
        results = parse_and_fill(NO_FORM_PAGE, '')
        assert results == []
