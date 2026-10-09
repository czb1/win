"""Published vectors, independent RSA fixtures, packet integrity and key handling."""
import ast
import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "SDK/SDK_Python/CoreGeek"))
sys.path.insert(0, str(ROOT / "tools"))
from agent import log_crypto as crypto
import extract_logs as extractor
from sync_log_crypto import main as sync
from check_log_key_packaging import private_keys

FIXTURES = ROOT / "tests/fixtures/log_crypto"


class LogCryptoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = crypto.load_log_key(FIXTURES / "rsa-test-key.json", private=True)

    def test_rfc8439_chacha20_block(self):
        expected = bytes.fromhex(
            "10f1e7e4d13b5915500fdd1fa32071c4c7d1f4c733c068030422aa9ac3d46c4e"
            "d2826446079faa0914c2d705d98b02a2b5129cd1de164eb9cbd083e8a2503c4e")
        for module in (crypto, extractor):
            self.assertEqual(module.chacha20_block(bytes(range(32)),
                             bytes.fromhex("000000090000004a00000000"), 1), expected)

    def test_rfc8439_poly1305(self):
        key = bytes.fromhex("85d6be7857556d337f4452fe42d506a80103808afb0db2fd4abff6af4149f51b")
        for module in (crypto, extractor):
            self.assertEqual(module.poly1305_mac(b"Cryptographic Forum Research Group", key).hex(),
                             "a8061dc1305136c6c22b8baf0c0127a9")

    def test_rfc8439_aead_encrypt_and_decrypt(self):
        key = bytes(range(0x80, 0xa0))
        nonce = bytes.fromhex("070000004041424344454647")
        aad = bytes.fromhex("50515253c0c1c2c3c4c5c6c7")
        plain = b"Ladies and Gentlemen of the class of '99: If I could offer you only one tip for the future, sunscreen would be it."
        expected = bytes.fromhex(
            "d31a8d34648e60db7b86afbc53ef7ec2a4aded51296e08fea9e2b5a736ee62d6"
            "3dbea45e8ca9671282fafb69da92728b1a71de0a9e060b2905d6a5b67ecd3b36"
            "92ddbd7f2d778b8c9803aee328091b58fab324e4fad675945585808b4831d7bc"
            "3ff4def08e4b7a9de576d26586cec64b61161ae10b594f09e26a7e902ecbd0600691")
        for module in (crypto, extractor):
            self.assertEqual(module.aead_encrypt(key, nonce, plain, aad), expected)
            self.assertEqual(module.aead_decrypt(key, nonce, expected, aad), plain)
            for wrong_nonce, sealed, wrong_aad in ((nonce, expected[:-1] + b"\x00", aad),
                                                   (b"\x00" * 12, expected, aad),
                                                   (nonce, expected, aad + b"x")):
                with self.assertRaises(module.LogCryptoError):
                    module.aead_decrypt(key, wrong_nonce, sealed, wrong_aad)

    def test_rfc8439_empty_and_padding_boundaries(self):
        for size in (0, 1, 15, 16, 17, 63, 64, 65):
            key, nonce = bytes(range(32)), bytes(range(12))
            plain, aad = bytes(range(size)), b"a" * (size % 17)
            sealed = crypto.aead_encrypt(key, nonce, plain, aad)
            self.assertEqual(extractor.aead_decrypt(key, nonce, sealed, aad), plain)
        with self.assertRaises(crypto.LogCryptoError):
            crypto.chacha20_block(bytes(32), bytes(12), 1 << 32)

    def test_rsa_oaep_independent_openssl_vectors(self):
        vector = json.loads((FIXTURES / "oaep-known-answer.json").read_text())
        plain, seed = bytes.fromhex(vector["plaintext_hex"]), bytes.fromhex(vector["seed_hex"])
        for module in (crypto, extractor):
            self.assertEqual(module.rsa_oaep_encrypt(self.key, plain, seed).hex(), vector["expected_ciphertext_hex"])
            self.assertEqual(module.rsa_oaep_decrypt(self.key, bytes.fromhex(vector["openssl_ciphertext_hex"])), plain)
            with self.assertRaises(module.LogCryptoError):
                module.rsa_oaep_decrypt(self.key, bytes(256))

    def test_nonce_uniqueness_across_threads_and_reconfiguration(self):
        writer = crypto.LogEncryptor(self.key)
        with ThreadPoolExecutor(max_workers=4) as pool:
            packets = list(pool.map(writer.seal, [b"{}"] * 64))
        self.assertEqual(len({p["nonce"] for p in packets}), 64)
        following = crypto.LogEncryptor(self.key).seal(b"{}")
        self.assertNotEqual(packets[0]["wrapped_key"], following["wrapped_key"])

    def test_packet_headers_are_authenticated(self):
        reader = extractor.LogDecryptor([self.key])
        packet = crypto.LogEncryptor(self.key).seal('{"secret":"完整题目"}'.encode())
        self.assertEqual(reader.open(packet), {"secret": "完整题目"})
        with self.assertRaises(extractor.LogCryptoError):
            reader.open({**packet, "nonce": base64.b64encode(bytes([1]) * 12).decode()})
        with self.assertRaises(extractor.MissingLogKey):
            extractor.LogDecryptor().open(packet)

    def test_decompression_is_bounded_even_after_valid_authentication(self):
        writer = crypto.LogEncryptor(self.key)
        packet = writer.seal(b"{}")
        header = crypto.packet_header(packet)
        compressed = zlib.compress(b"x" * (crypto.MAX_LOG_BYTES + 1))
        sealed = crypto.aead_encrypt(writer._key, base64.b64decode(packet["nonce"]), compressed,
                                     crypto.canonical_json(header).encode())
        with self.assertRaises(extractor.LogCryptoError):
            extractor.LogDecryptor([self.key]).open({**header, "ciphertext": base64.b64encode(sealed).decode()})

    def test_generated_key_files_are_exclusive_and_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private, public = root / "local.json", root / "CoreGeek/log-public.json"
            with patch.object(crypto, "generate_rsa_key", return_value=self.key):
                crypto.write_keypair(private, public)
                self.assertEqual(crypto.load_log_key(private, True), self.key)
                self.assertNotIn("d", json.loads(public.read_text()))
                if sys.platform != "win32":
                    self.assertEqual(private.stat().st_mode & 0o777, 0o600)
                before = private.read_bytes()
                with self.assertRaises(crypto.LogCryptoError):
                    crypto.write_keypair(private, public)
                self.assertEqual(private.read_bytes(), before)
                with self.assertRaises(crypto.LogCryptoError):
                    crypto.write_keypair(root / "CoreGeek/local.json", root / "other.json")
                self.assertEqual(list(private_keys(root / "CoreGeek")), [])
                (root / "CoreGeek/accidental.json").write_bytes(private.read_bytes())
                self.assertEqual(len(list(private_keys(root / "CoreGeek"))), 1)

    def test_codec_is_embedded_verbatim_and_imports_only_standard_library(self):
        self.assertEqual(sync(["--check"]), 0)
        source = ROOT / "SDK/SDK_Python/CoreGeek/agent/log_crypto.py"
        for node in ast.walk(ast.parse(source.read_text())):
            names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module] if isinstance(node, ast.ImportFrom) else []
            for name in names:
                self.assertIn(name.split(".")[0], sys.stdlib_module_names)
