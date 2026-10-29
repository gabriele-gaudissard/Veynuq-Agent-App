import datetime
import http.server
import json
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest

from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from veyq.tls import diagnostic


class TLSTests(unittest.TestCase):
    def test_native_tls_keeps_verification_and_hostname_checks(self):
        now=datetime.datetime.now(datetime.timezone.utc)
        ca_key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        ca_name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Veynuq controlled test CA')])
        ca=(x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now-datetime.timedelta(days=1)).not_valid_after(now+datetime.timedelta(days=2))
            .add_extension(x509.BasicConstraints(ca=True,path_length=0),critical=True).sign(ca_key,hashes.SHA256()))
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        cert=(x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'localhost')])).issuer_name(ca_name)
              .public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(hours=1)).not_valid_after(now+datetime.timedelta(days=1))
              .add_extension(x509.BasicConstraints(ca=False,path_length=None),critical=True).add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]),critical=False).sign(ca_key,hashes.SHA256()))
        class Handler(http.server.BaseHTTPRequestHandler):
            def handle(self):
                try:super().handle()
                except (ConnectionError,ssl.SSLError):pass  # Expected rejected TLS clients.
            def do_GET(self):
                self.send_response(200);self.end_headers();self.wfile.write(b'verified')
            def log_message(self,*args):pass
        with tempfile.TemporaryDirectory(prefix='veynuq-tls-') as directory:
            root=Path(directory);ca_file=root/'ca.pem';chain=root/'chain.pem';private=root/'key.pem'
            ca_file.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
            chain.write_bytes(cert.public_bytes(serialization.Encoding.PEM)+ca.public_bytes(serialization.Encoding.PEM))
            private.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
            context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(chain,private)
            server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler);server.socket=context.wrap_socket(server.socket,server_side=True)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            code='''from veyq.tls import initialize
initialize()
import ssl,sys,requests,json
if sys.platform=='win32':assert ssl.SSLContext.__module__.startswith('truststore')
session=requests.Session();session.trust_env=False
try:
 response=session.get(sys.argv[1],verify=True if sys.argv[2]=='system' else sys.argv[2],timeout=5)
 print(json.dumps({'ok':True,'text':response.text}))
except requests.exceptions.SSLError:print(json.dumps({'ok':False,'error':'certificate verification rejected'}))
finally:session.close()
'''
            try:
                for host,trust,expected in [('localhost','system',False),('localhost',str(ca_file),True),('127.0.0.1',str(ca_file),False)]:
                    with self.subTest(host=host,trust=trust):
                        result=subprocess.run([sys.executable,'-c',code,f'https://{host}:{server.server_port}/',trust],cwd=Path(__file__).resolve().parent.parent,capture_output=True,text=True,timeout=20,check=True)
                        actual=json.loads(result.stdout);self.assertEqual(actual['ok'],expected,actual)
                        if expected:self.assertEqual(actual['text'],'verified')
            finally:server.shutdown();server.server_close();thread.join(3)

    def test_bootstrap_initializes_tls_before_http_imports_and_error_is_actionable(self):
        source=(Path(__file__).resolve().parent.parent/'app.py').read_text(encoding='utf-8')
        self.assertLess(source.index('initialize()'),source.index('from veyq.desktop import main'))
        detail=diagnostic(RuntimeError('[SSL: CERTIFICATE_VERIFY_FAILED] self-signed certificate in certificate chain'))
        self.assertIn('verification remains enabled',detail);self.assertIn('Windows',detail)
        self.assertEqual(diagnostic(RuntimeError('unrelated failure')),'unrelated failure')


if __name__=='__main__':unittest.main()
