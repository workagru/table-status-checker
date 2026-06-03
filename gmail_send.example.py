#!/usr/bin/env python3
"""Send a file (zipped) as an email attachment via Gmail SMTP."""

import argparse
import getpass
import os
import smtplib
import sys
import tempfile
import zipfile
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def zip_file(file_path: str) -> str:
    base = os.path.basename(file_path)
    zip_name = os.path.splitext(base)[0] + ".zip"
    zip_path = os.path.join(tempfile.gettempdir(), zip_name)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(file_path, arcname=base)
    print(f"Zipped: {file_path} -> {zip_path} ({os.path.getsize(zip_path)} bytes)")
    return zip_path


def send_email(
    sender: str,
    app_password: str,
    recipient: str,
    subject: str,
    body: str,
    attachment_path: str,
):
    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    with open(attachment_path, "rb") as f:
        part = MIMEBase("application", "zip")
        part.set_payload(f.read())
    encoders.encode_base64(part)
    part.add_header(
        "Content-Disposition",
        f"attachment; filename={os.path.basename(attachment_path)}",
    )
    msg.attach(part)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(sender, app_password)
        server.sendmail(sender, recipient, msg.as_string())

    print(f"Sent to {recipient}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("file", help="Path to the file to send")
    p.add_argument("--to", required=True, help="Recipient email address")
    p.add_argument("--from", dest="sender", help="Your Gmail address (prompted if omitted)")
    p.add_argument("--subject", default="", help="Email subject (default: filename)")
    p.add_argument("--body", default="See attached file.", help="Email body text")
    args = p.parse_args()

    if not os.path.isfile(args.file):
        sys.exit(f"File not found: {args.file}")

    sender = args.sender or "aleksandr.gruzdev@databorn.ai"
    # Copy this file to gmail_send.py (gitignored) and paste your Gmail
    # app password here. Get one at https://myaccount.google.com/apppasswords
    app_password = "PUT-GMAIL-APP-PASSWORD-HERE"

    if not args.subject:
        args.subject = os.path.basename(args.file)

    zip_path = zip_file(args.file)
    try:
        send_email(sender, app_password, args.to, args.subject, args.body, zip_path)
    finally:
        os.remove(zip_path)


if __name__ == "__main__":
    main()
