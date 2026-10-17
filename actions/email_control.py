import imaplib
import smtplib
import email
from email.header import decode_header
import json
import os
import sys
import time
import webbrowser
from pathlib import Path

try:
    import mss
    import mss.tools
    _MSS = True
except ImportError:
    _MSS = False

from core.gemini import text as gemini_text, SMART

def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

def _load_config() -> dict:
    config_file = _get_base_dir() / "config" / "api_keys.json"
    if not config_file.exists():
        return {}
    try:
        return json.loads(config_file.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _decode_mime_header(header_value: str) -> str:
    if not header_value:
        return ""
    decoded_parts = decode_header(header_value)
    result = ""
    for part, encoding in decoded_parts:
        if isinstance(part, bytes):
            try:
                result += part.decode(encoding or "utf-8", errors="replace")
            except Exception:
                result += part.decode("latin-1", errors="replace")
        else:
            result += str(part)
    return result

def _capture_screen_b64() -> str:
    if not _MSS:
        return ""
    import base64
    import tempfile
    with mss.mss() as sct:
        monitor = sct.monitors[1]
        sct_img = sct.grab(monitor)
        temp_file = tempfile.mktemp(suffix=".png")
        mss.tools.to_png(sct_img.rgb, sct_img.size, output=temp_file)
        with open(temp_file, "rb") as f:
            b64_data = base64.b64encode(f.read()).decode("utf-8")
        try:
            os.remove(temp_file)
        except OSError:
            pass
        return b64_data

def check_email_imap(cfg: dict, limit: int = 5) -> str | None:
    email_addr = cfg.get("email_address") or cfg.get("email_user") or cfg.get("gmail_address")
    email_pass = cfg.get("email_password") or cfg.get("email_app_password") or cfg.get("gmail_app_password")
    imap_server = cfg.get("imap_server", "imap.gmail.com")
    imap_port = int(cfg.get("imap_port", 993))

    if not (email_addr and email_pass):
        return None

    try:
        mail = imaplib.IMAP4_SSL(imap_server, imap_port)
        mail.login(email_addr, email_pass)
        mail.select("INBOX")

        status, messages = mail.search(None, 'UNSEEN')
        msg_ids = messages[0].split()
        if not msg_ids:
            status, messages = mail.search(None, 'ALL')
            msg_ids = messages[0].split()

        msg_ids = msg_ids[-limit:]
        msg_ids.reverse()

        email_list = []
        for msg_id in msg_ids:
            res, msg_data = mail.fetch(msg_id, "(RFC822)")
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    subject = _decode_mime_header(msg.get("Subject", "No Subject"))
                    from_sender = _decode_mime_header(msg.get("From", "Unknown Sender"))
                    date_sent = msg.get("Date", "")

                    body_snippet = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            ctype = part.get_content_type()
                            cdisp = str(part.get("Content-Disposition"))
                            if ctype == "text/plain" and "attachment" not in cdisp:
                                try:
                                    body_snippet = part.get_payload(decode=True).decode("utf-8", errors="replace")[:200]
                                    break
                                except Exception:
                                    pass
                    else:
                        try:
                            body_snippet = msg.get_payload(decode=True).decode("utf-8", errors="replace")[:200]
                        except Exception:
                            pass

                    clean_snippet = " ".join(body_snippet.split())
                    email_list.append(
                        f"• From: {from_sender}\n  Subject: {subject}\n  Date: {date_sent}\n  Snippet: {clean_snippet}"
                    )

        mail.close()
        mail.logout()

        if email_list:
            return f"Found {len(email_list)} recent email(s):\n\n" + "\n\n".join(email_list)
        return "No unread or recent emails found in inbox."
    except Exception as e:
        print(f"[EmailControl] IMAP error: {e}")
        return None

def email_control_handler(parameters: dict, player=None, speak=None, response=None, session_memory=None) -> str:
    params = parameters or {}
    action = params.get("action", "check_inbox").lower().strip()
    limit = int(params.get("limit", 5))
    recipient = params.get("recipient", "").strip()
    subject = params.get("subject", "No Subject").strip()
    body = params.get("body", "").strip()

    cfg = _load_config()

    if action in ("check_inbox", "read_emails", "check_email"):
        # First try direct IMAP check
        imap_res = check_email_imap(cfg, limit=limit)
        if imap_res:
            if player:
                try:
                    player.show_content("EMAIL INBOX", imap_res)
                except Exception:
                    pass
            return imap_res

        # Fallback: Open Gmail in browser and capture screenshot with Vision
        webbrowser.open("https://mail.google.com")
        time.sleep(4.0)

        b64_img = _capture_screen_b64()
        if b64_img:
            prompt = (
                "Here is the user's opened email inbox. "
                "Read the latest unread and recent emails shown on screen. "
                "List sender name, email subject, and brief topic for each unread email. "
                "Summarize clearly."
            )
            contents = [
                {"role": "user", "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": "image/png", "data": b64_img}}
                ]}
            ]
            try:
                vision_analysis = gemini_text(contents, tier=SMART)
            except Exception as ve:
                print(f"[EmailControl] Vision call failed: {ve}")
                vision_analysis = ""

            # An empty reply means every model failed — fall through to the
            # browser-only message instead of showing an empty "summary".
            if vision_analysis:
                if player:
                    try:
                        player.show_content("EMAIL INBOX (VISION)", vision_analysis)
                    except Exception:
                        pass
                return f"Opened Gmail in browser. Inbox summary from screen:\n\n{vision_analysis}"

        return (
            "Opened Gmail (https://mail.google.com) in browser. "
            "To enable instant background email checking without browser, add "
            "'email_address' and 'email_app_password' to config/api_keys.json."
        )

    elif action in ("open_email", "open_gmail"):
        webbrowser.open("https://mail.google.com")
        return "Opened Gmail in browser."

    elif action == "send_email":
        if not recipient:
            return "Please specify recipient email address."

        email_addr = cfg.get("email_address") or cfg.get("gmail_address")
        email_pass = cfg.get("email_password") or cfg.get("email_app_password")

        if email_addr and email_pass:
            try:
                smtp_server = cfg.get("smtp_server", "smtp.gmail.com")
                smtp_port = int(cfg.get("smtp_port", 465))
                server = smtplib.SMTP_SSL(smtp_server, smtp_port)
                server.login(email_addr, email_pass)
                msg_text = f"From: {email_addr}\nTo: {recipient}\nSubject: {subject}\n\n{body}"
                server.sendmail(email_addr, [recipient], msg_text.encode("utf-8"))
                server.quit()
                return f"Email sent successfully to {recipient} with subject '{subject}'."
            except Exception as se:
                print(f"[EmailControl] SMTP failed ({se}) — using web compose fallback.")

        from urllib.parse import quote
        compose_url = f"https://mail.google.com/mail/?view=cm&fs=1&to={quote(recipient)}&su={quote(subject)}&body={quote(body)}"
        webbrowser.open(compose_url)
        return f"Opened Gmail compose window to send email to {recipient}."

    else:
        return f"Unknown action: {action}"

TOOL = {
    "name": "email_control",
    "description": (
        "Check unread emails, read inbox, summarize latest email messages, open Gmail, "
        "or send emails. Use when user asks to check email, read unread messages, "
        "or send an email."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "check_inbox | read_emails | open_gmail | send_email"
            },
            "limit": {
                "type": "INTEGER",
                "description": "Number of recent emails to read (default: 5)"
            },
            "recipient": {
                "type": "STRING",
                "description": "Recipient email address for send_email"
            },
            "subject": {
                "type": "STRING",
                "description": "Email subject for send_email"
            },
            "body": {
                "type": "STRING",
                "description": "Email body content for send_email"
            }
        },
        "required": ["action"]
    },
    "handler": email_control_handler
}
