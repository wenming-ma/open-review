"""Bootstrap runtime environment before importing the rest of the app."""

from __future__ import annotations

import os


def bootstrap_runtime_environment() -> None:
    # Fix proxy: .bashrc sets all_proxy=socks5h://... which breaks localhost connections.
    socks_proxy = os.environ.pop("all_proxy", None)
    os.environ.pop("ALL_PROXY", None)
    if socks_proxy:
        proxy = socks_proxy.replace("socks5h://", "socks5://")
        os.environ.setdefault("HTTPS_PROXY", proxy)
    os.environ["NO_PROXY"] = "localhost,127.0.0.1,::1,.local"
    os.environ["no_proxy"] = "localhost,127.0.0.1,::1,.local"


bootstrap_runtime_environment()
