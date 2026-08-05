from __future__ import annotations

from io import BytesIO

import segno


def mobile_operator_url(public_url: str, capability: str) -> str:
    return f"{public_url}/#{capability}"


def render_pairing_svg(public_url: str, capability: str) -> bytes:
    stream = BytesIO()
    qr = segno.make(
        mobile_operator_url(public_url, capability),
        error="m",
        micro=False,
        boost_error=False,
    )
    qr.save(
        stream,
        kind="svg",
        scale=6,
        xmldecl=False,
        nl=False,
    )
    return stream.getvalue()
