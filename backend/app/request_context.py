from ipaddress import IPv6Address, ip_address

from fastapi import Request


def client_ip(request: Request) -> str | None:
    # Uvicorn resolves forwarded addresses only for its configured trusted proxies.
    if not request.client:
        return None
    try:
        address = ip_address(request.client.host.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(address, IPv6Address) and address.ipv4_mapped:
        return str(address.ipv4_mapped)
    return str(address)
