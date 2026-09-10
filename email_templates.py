"""
MarketingOstad — Dedicated Email Templates Module
Stores all HTML templates for transactional emails (Verification, Welcome, Password Reset, etc.)
"""

def get_verification_email_html(full_name: str, verify_link: str) -> str:
    """
    Generates a high-end, responsive HTML email template for account verification.
    """
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Verify Your MarketingOstad Account</title>
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
                ⚡ MARKETINGOSTAD PLATFORM
              </div>
              <h1 style="margin: 0; color: #ffffff; font-size: 26px; font-weight: 800; letter-spacing: -0.5px;">
                Verify Your Email Address
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
                Thank you for creating an account with <strong style="color: #38bdf8;">MarketingOstad</strong>. To activate your account and access BDT datasets, custom scrapers, and marketing tools, please confirm your email address below.
              </p>

              <!-- CTA Button Box -->
              <table width="100%" border="0" cellspacing="0" cellpadding="0" style="margin: 35px 0;">
                <tr>
                  <td align="center">
                    <a href="{verify_link}" target="_blank" style="display: inline-block; background: linear-gradient(135deg, #22c55e 0%, #16a34a 100%); color: #ffffff; font-weight: 700; font-size: 16px; text-decoration: none; padding: 16px 36px; border-radius: 8px; box-shadow: 0 4px 15px rgba(34, 197, 94, 0.4); text-transform: uppercase; letter-spacing: 0.5px;">
                      ✅ Verify Email Address
                    </a>
                  </td>
                </tr>
              </table>

              <p style="font-size: 13px; color: #64748b; margin-top: 25px;">
                If the button above does not work, copy and paste this link into your web browser:
              </p>
              <div style="background-color: #030712; padding: 12px 16px; border-radius: 6px; border: 1px solid #1e293b; word-break: break-all; margin-bottom: 25px;">
                <a href="{verify_link}" style="color: #38bdf8; font-size: 13px; text-decoration: none;">{verify_link}</a>
              </div>

              <!-- Security Callout Box -->
              <table width="100%" border="0" cellspacing="0" cellpadding="0" style="background-color: rgba(234, 179, 8, 0.06); border-left: 3px solid #eab308; border-radius: 4px; padding: 12px 16px; margin-bottom: 10px;">
                <tr>
                  <td style="font-size: 12px; color: #e2e8f0;">
                    🔒 <strong>Security Notice:</strong> This link is unique to your account. If you did not register for a MarketingOstad account, no action is required and you can safely ignore this email.
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
