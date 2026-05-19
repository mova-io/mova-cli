# Security and Compliance

## Certifications

We hold a current SOC 2 Type II report, audited Q4 of the prior
year and covering 12 months of operations. ISO 27001 audit is in
progress with an ETA of Q4. A HIPAA Business Associate Agreement is
available on the enterprise tier in the US region only.

## Data residency

We operate in three regions: US (us-east-1, Virginia), EU
(eu-west-1, Ireland), and APAC (ap-southeast-2, Sydney). Customer
data does not cross regional boundaries unless the customer
explicitly opts into multi-region replication (an enterprise-tier
feature). Region is selected at workspace creation; changing
regions later requires a managed migration through solutions
engineering (typically a 2-week engagement).

## Encryption

All data is encrypted at rest using AES-256 (managed by the cloud
provider's KMS) and in transit using TLS 1.3. Customer-supplied
encryption keys (CMEK / BYOK) are available on the enterprise tier
through Azure Key Vault integration.

## IP allowlisting

IP allowlisting is available on the enterprise tier. Accepts CIDR
ranges (e.g. 10.0.0.0/16) and single IPs, up to 100 entries per
tenant. The customer's admin must add their current IP first or
they'll lock themselves out — we require 2 entries minimum on
first enable.

## GDPR and CCPA

Data subject access requests are handled via the Subject Data
Export API endpoint. The export returns a ZIP within 24h via a
signed S3 URL. Right-to-erasure requests are irreversible and
purge backup copies after the 30-day retention window.
