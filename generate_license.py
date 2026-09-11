#!/usr/bin/env python3
import argparse
import sys
from license_service import generate_production_license, verify_license

def main():
    parser = argparse.ArgumentParser(description="DataKarkhana / MarketingOstad Desktop Production Key Generator")
    parser.add_argument("--customer", "-c", required=True, help="Customer name or organization")
    parser.add_argument("--email", "-e", default="", help="Customer email address (optional)")
    parser.add_argument("--days", "-d", type=int, default=30, help="Number of validity days (default: 30)")
    parser.add_argument("--expires", default=None, help="Specific expiration date (YYYY-MM-DD or ISO)")
    parser.add_argument("--key", "-k", default=None, help="Custom production key format (optional)")
    parser.add_argument("--plan", "-p", default="pro", help="Plan tier (default: pro)")

    args = parser.parse_args()

    license_info = generate_production_license(
        customer_name=args.customer,
        customer_email=args.email,
        expiry_days=args.days,
        expires_at_str=args.expires,
        custom_key=args.key,
        plan_tier=args.plan
    )

    print("\n" + "=" * 60)
    print("      DATAKARKHANA DESKTOP PRODUCTION LICENSE GENERATED     ")
    print("=" * 60)
    print(f"Customer Name   : {license_info['customer_name']}")
    if license_info['customer_email']:
        print(f"Customer Email  : {license_info['customer_email']}")
    print(f"Plan Tier       : {license_info['plan_tier'].upper()}")
    print(f"Production Key  : {license_info['production_key']}")
    print(f"Expiration Date : {license_info['expires_at']}")
    print(f"Days Remaining  : {license_info['days_remaining']} days")
    print("-" * 60)
    print("Full License Token (to distribute to customer):")
    print(license_info['license_token'])
    print("=" * 60)
    print("\nCustomer Instructions:")
    print(f"1. Open DataKarkhana Desktop App.")
    print(f"2. Paste Production Key: {license_info['production_key']}")
    print(f"   (or Full Token: {license_info['license_token']})")
    print(f"3. Click 'Activate License'.\n")

if __name__ == "__main__":
    main()
