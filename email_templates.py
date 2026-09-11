"""
MarketingOstad — Dedicated Email Templates Module
Stores all HTML templates for transactional emails (Verification, Welcome, Password Reset, etc.)
"""

def get_verification_email_html(full_name: str, otp_code: str, verify_link: str = "") -> str:
    """
    Generates a high-end, responsive HTML email template for account verification via 6-digit OTP only.
    No localhost or external links are included.
    """
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Your DataKarkhana Verification Code</title>
</head>
<body style="margin:0; padding:0; background-color:#030712; font-family:'Segoe UI', Roboto, Helvetica, Arial, sans-serif; -webkit-font-smoothing:antialiased;">
  <table width="100%" border="0" cellspacing="0" cellpadding="0" style="background-color:#030712; padding: 40px 10px;">
    <tr>
      <td align="center">
        <table width="100%" border="0" cellspacing="0" cellpadding="0" style="max-width: 600px; background-color:#0b0f19; border: 1px solid rgba(56, 189, 248, 0.2); border-radius: 12px; overflow: hidden; box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);">
          
          <!-- Header Banner -->
          <tr>
            <td align="center" style="padding: 35px 30px 25px 30px; background: linear-gradient(135deg, #0f172a 0%, #1e1b4b 100%); border-bottom: 1px solid rgba(255,255,255,0.08);">
              <div style="display:inline-block; padding: 8px 16px; background: rgba(6, 182, 212, 0.1); border: 1px solid rgba(6, 182, 212, 0.3); border-radius: 20px; color: #38bdf8; font-size: 12px; font-weight: bold; letter-spacing: 2px; text-transform: uppercase; margin-bottom: 12px;">
                ⚡ DATAKARKHANA PLATFORM
              </div>
              <h1 style="margin: 0; color: #ffffff; font-size: 26px; font-weight: 800; letter-spacing: -0.5px;">
                Your Verification Code
              </h1>
            </td>
          </tr>

          <!-- Main Content Body -->
          <tr>
            <td style="padding: 40px 35px; color: #94a3b8; font-size: 15px; line-height: 1.6;">
              <p style="margin-top: 0; color: #f1f5f9; font-size: 18px; font-weight: 600;">
                Hello {full_name}, 👋
              </p>
              <p style="margin-bottom: 25px; color: #cbd5e1;">
                Thank you for creating an account with <strong style="color: #38bdf8;">DataKarkhana</strong>. To verify your email address and activate your desktop workspace, enter the following 6-digit verification code into the application:
              </p>

              <!-- OTP Code Display Box -->
              <table width="100%" border="0" cellspacing="0" cellpadding="0" style="margin: 30px 0;">
                <tr>
                  <td align="center">
                    <div style="background-color: #030712; padding: 24px 36px; border-radius: 12px; border: 2px dashed rgba(56, 189, 248, 0.5); display: inline-block;">
                      <div style="font-size: 11px; color: #94a3b8; text-transform: uppercase; letter-spacing: 2px; margin-bottom: 8px; font-weight: bold;">
                        Verification Code (OTP)
                      </div>
                      <div style="font-size: 42px; font-weight: 900; letter-spacing: 12px; color: #38bdf8; font-family: 'Courier New', Courier, monospace;">
                        {otp_code}
                      </div>
                      <div style="font-size: 11px; color: #64748b; margin-top: 8px;">
                        Valid for 15 minutes • Enter this code in the app to continue
                      </div>
                    </div>
                  </td>
                </tr>
              </table>

              <!-- Security Callout Box -->
              <table width="100%" border="0" cellspacing="0" cellpadding="0" style="background-color: rgba(234, 179, 8, 0.06); border-left: 3px solid #eab308; border-radius: 4px; padding: 12px 16px; margin-top: 25px; margin-bottom: 10px;">
                <tr>
                  <td style="font-size: 12px; color: #e2e8f0;">
                    🔒 <strong>Security Notice:</strong> Do not share this code with anyone. If you did not request this verification code, please ignore this email.
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td align="center" style="padding: 25px 30px; background-color: #04060b; border-top: 1px solid #1e293b; color: #64748b; font-size: 12px; line-height: 1.5;">
              <p style="margin: 0 0 6px 0; color: #94a3b8; font-weight: 600;">MarketingOstad — Monospace Cyber Data Service</p>
              <p style="margin: 0 0 6px 0;">Dhaka, Bangladesh • Support Email: <a href="mailto:asifdev777@gmail.com" style="color: #38bdf8; text-decoration: none;">asifdev777@gmail.com</a></p>
              <p style="margin: 0; font-size: 11px; color: #475569;">© 2026 MarketingOstad. All rights reserved.</p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def get_license_key_email_html(customer_name: str, production_key: str, plan_name: str, credits: int, expires_at: str) -> str:
    """
    Generates a premium HTML email template for delivering a production license key
    to the customer after Super Admin approves their payment.
    """
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Your DataKarkhana Production Key</title>
</head>
<body style="margin:0; padding:0; background-color:#030712; font-family:'Segoe UI', Roboto, Helvetica, Arial, sans-serif; -webkit-font-smoothing:antialiased;">
  <table width="100%" border="0" cellspacing="0" cellpadding="0" style="background-color:#030712; padding: 40px 10px;">
    <tr>
      <td align="center">
        <table width="100%" border="0" cellspacing="0" cellpadding="0" style="max-width: 600px; background-color:#0b0f19; border: 1px solid rgba(56, 189, 248, 0.2); border-radius: 12px; overflow: hidden; box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);">

          <!-- Header Banner -->
          <tr>
            <td align="center" style="padding: 35px 30px 25px 30px; background: linear-gradient(135deg, #0f172a 0%, #064e3b 100%); border-bottom: 1px solid rgba(255,255,255,0.08);">
              <div style="display:inline-block; padding: 8px 16px; background: rgba(16, 185, 129, 0.1); border: 1px solid rgba(16, 185, 129, 0.3); border-radius: 20px; color: #34d399; font-size: 12px; font-weight: bold; letter-spacing: 2px; text-transform: uppercase; margin-bottom: 12px;">
                🔑 PRODUCTION LICENSE KEY
              </div>
              <h1 style="margin: 0; color: #ffffff; font-size: 26px; font-weight: 800; letter-spacing: -0.5px;">
                Your Payment Has Been Approved!
              </h1>
            </td>
          </tr>

          <!-- Main Content Body -->
          <tr>
            <td style="padding: 40px 35px; color: #94a3b8; font-size: 15px; line-height: 1.6;">
              <p style="margin-top: 0; color: #f1f5f9; font-size: 18px; font-weight: 600;">
                Hello {customer_name}, 🎉
              </p>
              <p style="margin-bottom: 25px; color: #cbd5e1;">
                Great news! Your payment for the <strong style="color: #38bdf8;">{plan_name}</strong> plan has been verified and approved. Your production license key is ready — use it in the app to activate <strong style="color: #34d399;">{credits} credits</strong>.
              </p>

              <!-- Production Key Display Box -->
              <table width="100%" border="0" cellspacing="0" cellpadding="0" style="margin: 25px 0;">
                <tr>
                  <td style="background: linear-gradient(135deg, rgba(16, 185, 129, 0.08), rgba(6, 182, 212, 0.08)); border: 2px solid rgba(16, 185, 129, 0.3); border-radius: 10px; padding: 20px; text-align: center;">
                    <p style="margin: 0 0 8px 0; font-size: 11px; color: #64748b; text-transform: uppercase; letter-spacing: 2px; font-weight: 600;">Your Production Key</p>
                    <p style="margin: 0; font-family: 'Courier New', Courier, monospace; font-size: 24px; font-weight: 900; color: #34d399; letter-spacing: 3px;">
                      {production_key}
                    </p>
                  </td>
                </tr>
              </table>

              <!-- Plan Details -->
              <table width="100%" border="0" cellspacing="0" cellpadding="0" style="margin: 20px 0; background-color: rgba(255,255,255,0.03); border: 1px solid #1e293b; border-radius: 8px;">
                <tr>
                  <td style="padding: 14px 18px; border-bottom: 1px solid #1e293b;">
                    <span style="color: #64748b; font-size: 13px;">Plan:</span>
                    <span style="float: right; color: #f1f5f9; font-weight: 600; font-size: 13px;">{plan_name}</span>
                  </td>
                </tr>
                <tr>
                  <td style="padding: 14px 18px; border-bottom: 1px solid #1e293b;">
                    <span style="color: #64748b; font-size: 13px;">Credits Included:</span>
                    <span style="float: right; color: #34d399; font-weight: 700; font-size: 13px;">{credits} CR</span>
                  </td>
                </tr>
                <tr>
                  <td style="padding: 14px 18px;">
                    <span style="color: #64748b; font-size: 13px;">Valid Until:</span>
                    <span style="float: right; color: #f1f5f9; font-weight: 600; font-size: 13px;">{expires_at}</span>
                  </td>
                </tr>
              </table>

              <!-- How to Activate -->
              <p style="color: #f1f5f9; font-size: 15px; font-weight: 600; margin-top: 30px; margin-bottom: 12px;">
                📋 How to Activate:
              </p>
              <table width="100%" border="0" cellspacing="0" cellpadding="0">
                <tr>
                  <td style="padding: 6px 0; color: #cbd5e1; font-size: 14px;">
                    <strong style="color: #38bdf8;">1.</strong> Open the DataKarkhana Desktop App
                  </td>
                </tr>
                <tr>
                  <td style="padding: 6px 0; color: #cbd5e1; font-size: 14px;">
                    <strong style="color: #38bdf8;">2.</strong> Click the license badge in the header (or open Subscribe panel)
                  </td>
                </tr>
                <tr>
                  <td style="padding: 6px 0; color: #cbd5e1; font-size: 14px;">
                    <strong style="color: #38bdf8;">3.</strong> Paste the Production Key above and click <strong style="color: #34d399;">Activate</strong>
                  </td>
                </tr>
                <tr>
                  <td style="padding: 6px 0; color: #cbd5e1; font-size: 14px;">
                    <strong style="color: #38bdf8;">4.</strong> Your <strong style="color: #34d399;">{credits} credits</strong> will be added to your account instantly!
                  </td>
                </tr>
              </table>

              <!-- Security Callout Box -->
              <table width="100%" border="0" cellspacing="0" cellpadding="0" style="background-color: rgba(234, 179, 8, 0.06); border-left: 3px solid #eab308; border-radius: 4px; padding: 12px 16px; margin-top: 25px; margin-bottom: 10px;">
                <tr>
                  <td style="font-size: 12px; color: #e2e8f0;">
                    🔒 <strong>Security Notice:</strong> This key is unique to your account and can only be redeemed once. Do not share it with anyone. If you did not make this purchase, please contact support immediately.
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td align="center" style="padding: 25px 30px; background-color: #04060b; border-top: 1px solid #1e293b; color: #64748b; font-size: 12px; line-height: 1.5;">
              <p style="margin: 0 0 6px 0; color: #94a3b8; font-weight: 600;">DataKarkhana — Monospace Cyber Data Service</p>
              <p style="margin: 0 0 6px 0;">Dhaka, Bangladesh &bull; Support Email: <a href="mailto:asifdev777@gmail.com" style="color: #38bdf8; text-decoration: none;">asifdev777@gmail.com</a></p>
              <p style="margin: 0; font-size: 11px; color: #475569;">&copy; 2026 DataKarkhana. All rights reserved.</p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""
