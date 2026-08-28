from __future__ import annotations

from io import BytesIO

import segno


def mobile_operator_url(public_url: str) -> str:
    return public_url


def render_pairing_svg(public_url: str) -> bytes:
    stream = BytesIO()
    qr = segno.make(
        mobile_operator_url(public_url),
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
