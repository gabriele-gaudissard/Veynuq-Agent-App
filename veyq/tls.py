"""Application-entry TLS initialization; never changes the Windows trust store."""
import sys


def initialize():
    # Run before importing requests/urllib3/httpx in each application process.
    # Use Windows CryptoAPI, including roots already approved by Windows/IT,
    # while retaining hostname, chain and certificate validity verification.
    if sys.platform == 'win32':
        import truststore
        truststore.inject_into_ssl()


def diagnostic(error):
    if 'CERTIFICATE_VERIFY_FAILED' in str(error) or 'certificate verify failed' in str(error).lower():
        return ('Secure download failed: the HTTPS certificate chain is not trusted. '
                'Check the Windows date/time and ask your network administrator or HTTPS-filter provider '
                'to verify the certificate trusted by Windows. Certificate verification remains enabled. '
                'Retry setup after correcting the certificate configuration; completed verified downloads are reused.')
    return str(error)
