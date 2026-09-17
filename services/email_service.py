import os
import json
import time
import secrets
import threading
import urllib.request
import urllib.error

from email_templates import get_verification_email_html, get_license_key_email_html

def generate_otp() -> str:
    return f"{secrets.randbelow(900000) + 100000}"

def send_free_verification_email(recipient_email: str, full_name: str, otp_code: str):
    brevo_api_key = os.getenv("BREVO_API_KEY", "")
    sender_email = os.getenv("SENDER_EMAIL", os.getenv("SUPPORT_EMAIL", os.getenv("SMTP_USER", "asifdev777@gmail.com")))

    html_body = get_verification_email_html(full_name, otp_code)
    last_error = ""

    print(f"[OTP CODE GENERATED] Verification Code for {recipient_email}: {otp_code}")

    if not brevo_api_key:
        print(f"[BREVO NOTICE] BREVO_API_KEY is not set. In local dev mode, OTP is: {otp_code}")
        return True, ""

    # Use Brevo REST API directly
    try:
        url = "https://api.brevo.com/v3/smtp/email"
        payload = {
            "sender": {"name": "DataKarkhana Platform", "email": sender_email},
            "to": [{"email": recipient_email, "name": full_name}],
            "subject": f"Your DataKarkhana Verification Code: {otp_code}",
            "htmlContent": html_body
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "accept": "application/json",
                "api-key": brevo_api_key,
                "content-type": "application/json"
            }
        )
        with urllib.request.urlopen(req) as resp:
            if resp.status in (200, 201):
                print(f"[BREVO API SUCCESS] Verification code {otp_code} sent to {recipient_email} via Brevo API!")
                return True, ""
    except urllib.error.HTTPError as ex:
        err_body = ex.read().decode("utf-8")
        print(f"[BREVO API ERROR] {ex.code}: {err_body}")
        try:
            err_json = json.loads(err_body)
            last_error = err_json.get("message", str(ex))
        except Exception:
            last_error = f"HTTP {ex.code}: {err_body}"
    except Exception as ex:
        print(f"[BREVO API ERROR] {ex}")
        last_error = str(ex)

    # In local development, if Brevo fails, don't block registration
    print(f"[DEV FALLBACK] Brevo error: {last_error}. Local OTP code remains: {otp_code}")
    return True, last_error


def send_license_key_email(recipient_email: str, customer_name: str, production_key: str, plan_name: str, credits: int, expires_at: str):
    """Sends the production license key to the customer via Brevo transactional email after payment approval."""
    brevo_api_key = os.getenv("BREVO_API_KEY", "")
    sender_email = os.getenv("SENDER_EMAIL", os.getenv("SUPPORT_EMAIL", os.getenv("SMTP_USER", "asifdev777@gmail.com")))

    if not brevo_api_key:
        print("[LICENSE EMAIL SKIP] BREVO_API_KEY not configured. License key email not sent.")
        return False, "BREVO_API_KEY is not configured."

    html_body = get_license_key_email_html(customer_name, production_key, plan_name, credits, expires_at)

    try:
        url = "https://api.brevo.com/v3/smtp/email"
        payload = {
            "sender": {"name": "DataKarkhana Platform", "email": sender_email},
            "to": [{"email": recipient_email, "name": customer_name}],
            "subject": f"🔑 Your DataKarkhana Production Key — {plan_name} ({credits} Credits)",
            "htmlContent": html_body
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "accept": "application/json",
                "api-key": brevo_api_key,
                "content-type": "application/json"
            }
        )
        with urllib.request.urlopen(req) as resp:
            if resp.status in (200, 201):
                print(f"[LICENSE EMAIL SUCCESS] License key email sent to {recipient_email}!")
                return True, ""
    except Exception as ex:
        print(f"[LICENSE EMAIL ERROR] {ex}")
        return False, str(ex)

    return False, "Unknown error sending license key email."


def send_custom_notification(recipient_email: str, recipient_phone: str, subject: str, message: str, channel: str):
    """Sends custom notification to user about request status update via Email and/or WhatsApp"""
    sent_email = False
    sent_wa = False

    # 1. Email Notification
    if channel in ("email", "both") and recipient_email:
        try:
            brevo_api_key = os.getenv("BREVO_API_KEY", "")
            smtp_user = os.getenv("SMTP_USER", "asifdev777@gmail.com")

            html_body = f"""
            <div style="font-family: Arial, sans-serif; background: #0f172a; color: #e2e8f0; padding: 30px; border-radius: 10px;">
                <h2 style="color: #06b6d4; margin-top: 0;">MarketingOstad - Dataset Request Update</h2>
                <div style="background: rgba(255,255,255,0.05); padding: 20px; border-radius: 8px; border-left: 4px solid #06b6d4; margin: 20px 0;">
                    <p style="font-size: 1rem; line-height: 1.6; white-space: pre-wrap; margin: 0; color: #f8fafc;">{message}</p>
                </div>
                <p style="font-size: 0.85rem; color: #94a3b8; margin-top: 30px;">
                    Thank you for choosing MarketingOstad Data Platform.<br>
                    Website: <a href="https://yourdatapoint.com" style="color: #06b6d4;">yourdatapoint.com</a>
                </p>
            </div>
            """

            if brevo_api_key:
                try:
                    headers = {
                        "accept": "application/json",
                        "api-key": brevo_api_key,
                        "content-type": "application/json"
                    }
                    payload = {
                        "sender": {"name": "MarketingOstad Team", "email": smtp_user},
                        "to": [{"email": recipient_email}],
                        "subject": subject,
                        "htmlContent": html_body
                    }
                    req = urllib.request.Request("https://api.brevo.com/v3/smtp/email", data=json.dumps(payload).encode("utf-8"), headers=headers)
                    with urllib.request.urlopen(req) as resp:
                        if resp.status in (200, 201):
                            sent_email = True
                except Exception as e:
                    print(f"[BREVO NOTIF ERROR] {e}")

        except Exception as e:
            print(f"[NOTIF EMAIL GENERAL EXCEPTION] {e}")

    # 2. WhatsApp Notification
    if channel in ("whatsapp", "both") and recipient_phone:
        try:
            from senders import format_phone, run_whatsapp_campaign
            clean_p = format_phone(recipient_phone)
            camp_id = f"notif_wa_{int(time.time())}"
            contacts_list = [{"name": "User", "phone": clean_p}]
            t = threading.Thread(target=run_whatsapp_campaign, args=(camp_id, contacts_list, message, f"notif_{clean_p}", 0))
            t.daemon = True
            t.start()
            sent_wa = True
        except Exception as e:
            print(f"[NOTIF WHATSAPP ERROR] {e}")

    return sent_email or sent_wa
