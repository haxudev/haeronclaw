from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


class EmlAttachment(BaseModel):
    path: str = Field(description="Absolute path to an existing file to attach")
    filename: Optional[str] = Field(default=None, description="Attachment filename override")
    mime_type: Optional[str] = Field(default=None, description="MIME type override (e.g. 'application/pdf')")


class CreateEmlParams(BaseModel):
    output_path: str = Field(
        description="Absolute output path under /tmp (e.g. '/tmp/message.eml' or '/tmp/req_x/message.eml')"
    )
    from_addr: str = Field(description="From email address")
    to_addrs: list[str] = Field(description="Recipient email addresses")
    subject: str = Field(description="Email subject")
    body_text: str = Field(description="Plain-text body")
    attachments: list[EmlAttachment] = Field(default_factory=list, description="Optional file attachments")


async def create_eml(params: CreateEmlParams) -> str:
    """Create an RFC 5322 .eml file at the given /tmp path (does NOT send it).

    This tool only writes a .eml file to disk for offline use or download.
    It does NOT send the email.  To actually send an email, use the m365_cli
    tool with 'mail send' instead.

    Returns the saved file path.
    """

    import mimetypes
    from email.message import EmailMessage

    out = Path(params.output_path)
    if not str(out).startswith("/tmp/"):
        raise ValueError("output_path must be an absolute path under /tmp")
    out.parent.mkdir(parents=True, exist_ok=True)

    msg = EmailMessage()
    msg["From"] = params.from_addr
    msg["To"] = ", ".join([a.strip() for a in params.to_addrs if a.strip()])
    msg["Subject"] = params.subject
    msg.set_content(params.body_text)

    for att in params.attachments:
        p = Path(att.path)
        data = p.read_bytes()

        guessed = att.mime_type or (mimetypes.guess_type(p.name)[0] if p.name else None)
        maintype, subtype = (guessed.split("/", 1) if guessed and "/" in guessed else ("application", "octet-stream"))

        filename = att.filename or p.name or "attachment"
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)

    out.write_bytes(msg.as_bytes(policy=msg.policy.clone(linesep="\r\n")))
    return str(out)
