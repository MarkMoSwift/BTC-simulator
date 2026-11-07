"""为公开入口同时提供 IPv4 和 IPv6 监听。"""
import socket

import uvicorn


def run_server(app, *, host: str, port: int, **options) -> None:
    if host in {"0.0.0.0", "::"} and socket.has_dualstack_ipv6():
        with socket.create_server(("::", port), family=socket.AF_INET6,
                                  dualstack_ipv6=True) as listener:
            server = uvicorn.Server(uvicorn.Config(app, host="::", port=port, **options))
            server.run(sockets=[listener])
    else:
        uvicorn.run(app, host=host, port=port, **options)
