"""DNS choices shared by the standalone CLI and optional desktop app."""
from ipaddress import IPv4Address

KAERU_DNS = '178.62.43.212'
DNS_PRESETS = (
    ('Windows DNS', 'system', 'Uses your PC’s resolver. No service override.'),
    ('Kaeru WFC / Wiimmfi', KAERU_DNS, 'DS gateway to Wiimmfi. Start here for Pokémon.'),
    ('WiiLink WFC', '5.161.56.11', 'Separate WFC network. Game support varies.'),
    ('AltWFC', '172.104.88.237', 'Separate WFC network. Game support varies.'),
    ('WiiLink DNS', '167.235.229.36', 'Different from WiiLink WFC. DS download DNS failed the latest check.'),
)


def normalize_dns(value):
    """Accept the Windows resolver or a single usable IPv4 DNS address."""
    if not isinstance(value, str):
        raise ValueError('Enter an IPv4 DNS address, such as 178.62.43.212.')
    value = value.strip()
    if value == 'system':
        return value
    try:
        address = IPv4Address(value)
    except ValueError as exc:
        raise ValueError('Enter a complete IPv4 DNS address, such as 178.62.43.212.') from exc
    if address.is_unspecified or address.is_multicast or int(address) == 0xffffffff:
        raise ValueError('Use a unicast DNS address; unspecified and broadcast addresses are not supported.')
    return str(address)
