"""Read-only adapter around the official py-clob-client.

The client is constructed with the public CLOB host only. This build never
passes a key, signer, or API credential into it, and it never calls an
order-entry method.
"""

from __future__ import annotations

CLOB_HOST = "https://clob.polymarket.com"


def build_readonly_client(host: str = CLOB_HOST, client_cls: object | None = None):
    if client_cls is None:
        from py_clob_client.client import ClobClient

        client_cls = ClobClient
    return client_cls(host)


class PyClobBooks:
    def __init__(self, client: object) -> None:
        self.client = client

    def order_book(self, token_id: str):
        return self.client.get_order_book(token_id)
