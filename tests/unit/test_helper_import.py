"""Unit tests for the helper client-config importer.

Covers the parser that turns the helper UI's ``/api/client-config`` block
into per-stage config, and the stage-side read path that now merges stored
overrides over schema defaults.
"""

from vasili import parse_helper_config, HELPER_CONFIG_KEY_STAGE, PipelineStage


# A realistic block as emitted by helper/app/app.py:api_client_config.
SAMPLE_BLOCK = """\
# dns_port_tunnel stage (SSH path)
ssh_server: 203.0.113.10
ssh_user: root
ssh_key_path: /etc/vasili/ssh_client_key

# dns_tunnel stage (iodine)
server_domain: t.example.com
tunnel_password: s3cr3t
tunnel_type: iodine

# DNS delegation required for t.example.com:
#   t.example.com.   NS   ns-vasili.example.com
#   ns-vasili...   A   203.0.113.10

# dns_port_tunnel stage (WireGuard) — fetch wg config from
# /api/wg-client-config and place at /etc/wireguard/wg-vasili-client.conf
wg_config_path: /etc/wireguard/wg-vasili-client.conf

# dns_offload_crack stage
offload_domain: c.example.com
offload_secret: cracksecret
"""


def test_parse_routes_keys_to_stages():
    by_stage, unknown = parse_helper_config(SAMPLE_BLOCK)
    assert by_stage['dns_port_tunnel'] == {
        'ssh_server': '203.0.113.10',
        'ssh_user': 'root',
        'ssh_key_path': '/etc/vasili/ssh_client_key',
        'wg_config_path': '/etc/wireguard/wg-vasili-client.conf',
    }
    assert by_stage['dns_tunnel'] == {
        'server_domain': 't.example.com',
        'tunnel_password': 's3cr3t',
        'tunnel_type': 'iodine',
    }
    assert by_stage['dns_offload_crack'] == {
        'offload_domain': 'c.example.com',
        'offload_secret': 'cracksecret',
    }
    assert unknown == []


def test_parse_ignores_comments_and_blanks():
    by_stage, unknown = parse_helper_config(
        '\n  \n# just a comment\n# another: comment-with-colon\n'
    )
    assert by_stage == {}
    # A pure comment line is skipped before colon-splitting, so it is not
    # mistaken for an unknown key.
    assert unknown == []


def test_parse_reports_unknown_keys():
    by_stage, unknown = parse_helper_config(
        'ssh_server: 1.2.3.4\nbogus_key: whatever\n'
    )
    assert by_stage == {'dns_port_tunnel': {'ssh_server': '1.2.3.4'}}
    assert unknown == ['bogus_key']


def test_parse_values_keep_inner_colons():
    # Split only on the first colon so values with colons survive.
    by_stage, _ = parse_helper_config('ssh_server: host:5353\n')
    assert by_stage['dns_port_tunnel']['ssh_server'] == 'host:5353'


def test_parse_empty_input():
    assert parse_helper_config('') == ({}, [])
    assert parse_helper_config(None) == ({}, [])


def test_key_map_covers_all_helper_keys():
    # Every routed key must map to one of the three tunnel/crack stages.
    assert set(HELPER_CONFIG_KEY_STAGE.values()) == {
        'dns_port_tunnel', 'dns_tunnel', 'dns_offload_crack',
    }


class _FakeStore:
    """Minimal module_config stand-in for the stage read path."""

    def __init__(self, values):
        self._values = values

    def get_config(self, name):
        return dict(self._values.get(name, {}))


class _SchemaStage(PipelineStage):
    name = 'dns_port_tunnel'

    def get_config_schema(self):
        return {
            'ssh_server': {'type': 'str', 'default': ''},
            'ssh_user': {'type': 'str', 'default': 'root'},
        }


def test_stage_config_merges_stored_over_defaults():
    stage = _SchemaStage()
    stage._module_config = _FakeStore({'dns_port_tunnel': {'ssh_server': '203.0.113.10'}})
    cfg = stage._get_stage_config()
    assert cfg['ssh_server'] == '203.0.113.10'  # stored override
    assert cfg['ssh_user'] == 'root'            # untouched default


def test_stage_config_defaults_without_store():
    stage = _SchemaStage()
    cfg = stage._get_stage_config()
    assert cfg == {'ssh_server': '', 'ssh_user': 'root'}


def test_stage_config_explicit_override_short_circuits():
    stage = _SchemaStage()
    stage._module_config = _FakeStore({'dns_port_tunnel': {'ssh_server': 'ignored'}})
    stage._stage_config = {'ssh_server': 'forced'}
    assert stage._get_stage_config() == {'ssh_server': 'forced'}
