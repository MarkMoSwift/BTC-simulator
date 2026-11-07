"""节点地址的校验和传输地址转换。"""
import ipaddress
from urllib.parse import urlsplit


def peer_source(host: str, port: int) -> str:
    """格式化连接来源，将映射 IPv4 恢复为普通 IPv4。"""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return f"{host}:{port}"
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return f"{ip.ipv4_mapped}:{port}"
    return f"[{ip}]:{port}" if ip.version == 6 else f"{ip}:{port}"


def peer_url(address: str) -> str:
    value = address.strip().rstrip("/")
    if "://" not in value:
        try:
            ip = ipaddress.ip_address(value)
        except ValueError:
            parsed = urlsplit("//" + value)
            ip = ipaddress.ip_address(parsed.hostname or "")
            port = parsed.port or 8000
            if parsed.path or parsed.query or parsed.fragment or parsed.username is not None or parsed.password is not None:
                raise ValueError("节点地址不合法")
            if parsed.port == 0:
                raise ValueError("端口不能为 0")
        else:
            port = 8000
        host = f"[{ip}]" if ip.version == 6 else str(ip)
        value = f"http://{host}:{port}"
    parsed = urlsplit(value)
    port = parsed.port
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or parsed.username is not None or parsed.password is not None or port == 0):
        raise ValueError("节点地址须为 IP、IP:端口或 HTTP/HTTPS 地址")
    return value
