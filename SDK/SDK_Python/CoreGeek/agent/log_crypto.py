"""Zero-dependency log encryption (RFC 8439 and RFC 8017).

This module is also embedded verbatim in tools/extract_logs.py. Python integer
operations are not constant-time; this implementation is for offline log
recovery, not an exposed private-key decryption service. No private key ships
with the agent. Tests include published vectors and independent interoperability.
"""
import base64
from collections import OrderedDict
from hashlib import sha256
from hmac import compare_digest
import json
from math import gcd, lcm
import os
from pathlib import Path
import secrets
import struct
from threading import Lock
import zlib

ENCRYPTED_PREFIX = "FWENC "
CRYPTO_VERSION = 1
KEY_FORMAT = "fwlog-rsa-v1"
OAEP_LABEL = b"FWLOG-KEY-v1"
MAX_LOG_BYTES = 16 * 1024 * 1024
MAX_PACKET_BYTES = MAX_LOG_BYTES + 65536
CHUNK_BYTES = 4200
_MASK32 = (1 << 32) - 1


class LogCryptoError(ValueError):
    """Malformed keys, packets or failed authentication; never includes secrets."""


class MissingLogKey(LogCryptoError):
    """An encrypted log requires a matching local private key."""


def _quarter_round(state, a, b, c, d):
    for shift in (16, 12, 8, 7):
        state[a] = (state[a] + state[b]) & _MASK32
        value = state[d] ^ state[a]
        state[d] = ((value << shift) | (value >> (32 - shift))) & _MASK32
        a, b, c, d = c, d, a, b


def chacha20_block(key, nonce, counter):
    if len(key) != 32 or len(nonce) != 12 or type(counter) is not int or not 0 <= counter <= _MASK32:
        raise LogCryptoError("invalid ChaCha20 key, nonce or counter")
    initial = list(struct.unpack("<4I", b"expand 32-byte k"))
    initial += list(struct.unpack("<8I", key)) + [counter] + list(struct.unpack("<3I", nonce))
    state = initial.copy()
    for _ in range(10):
        for indices in ((0, 4, 8, 12), (1, 5, 9, 13), (2, 6, 10, 14), (3, 7, 11, 15),
                        (0, 5, 10, 15), (1, 6, 11, 12), (2, 7, 8, 13), (3, 4, 9, 14)):
            _quarter_round(state, *indices)
    return struct.pack("<16I", *((a + b) & _MASK32 for a, b in zip(state, initial)))


def _chacha20_xor(key, nonce, data):
    if len(data) > 64 * _MASK32:
        raise LogCryptoError("ChaCha20 counter would overflow")
    output = bytearray(len(data))
    for offset in range(0, len(data), 64):
        block = chacha20_block(key, nonce, offset // 64 + 1)
        output[offset:offset + 64] = bytes(a ^ b for a, b in zip(data[offset:offset + 64], block))
    return bytes(output)


def poly1305_mac(data, key):
    if len(key) != 32:
        raise LogCryptoError("invalid Poly1305 key")
    r = int.from_bytes(key[:16], "little") & 0x0ffffffc0ffffffc0ffffffc0fffffff
    s = int.from_bytes(key[16:], "little")
    accumulator = 0
    for offset in range(0, len(data), 16):
        value = int.from_bytes(data[offset:offset + 16] + b"\x01", "little")
        accumulator = (accumulator + value) * r % ((1 << 130) - 5)
    return ((accumulator + s) % (1 << 128)).to_bytes(16, "little")


def _authentication_tag(key, nonce, aad, ciphertext):
    data = aad + b"\x00" * (-len(aad) % 16)
    data += ciphertext + b"\x00" * (-len(ciphertext) % 16)
    data += struct.pack("<QQ", len(aad), len(ciphertext))
    return poly1305_mac(data, chacha20_block(key, nonce, 0)[:32])


def aead_encrypt(key, nonce, plaintext, aad=b""):
    ciphertext = _chacha20_xor(key, nonce, plaintext)
    return ciphertext + _authentication_tag(key, nonce, aad, ciphertext)


def aead_decrypt(key, nonce, sealed, aad=b""):
    if len(sealed) < 16:
        raise LogCryptoError("encrypted record is shorter than its authentication tag")
    ciphertext, tag = sealed[:-16], sealed[-16:]
    if not compare_digest(tag, _authentication_tag(key, nonce, aad, ciphertext)):
        raise LogCryptoError("encrypted record authentication failed")
    # Do not expose plaintext before the entire tag has been verified.
    return _chacha20_xor(key, nonce, ciphertext)


def _xor(left, right):
    return bytes(a ^ b for a, b in zip(left, right))


def _mgf1(seed, length):
    return b"".join(sha256(seed + i.to_bytes(4, "big")).digest()
                    for i in range((length + 31) // 32))[:length]


def _oaep_encode(message, size, seed, label=OAEP_LABEL):
    if len(seed) != 32 or len(message) > size - 66:
        raise LogCryptoError("invalid RSA-OAEP message or seed length")
    data = sha256(label).digest() + b"\x00" * (size - len(message) - 66) + b"\x01" + message
    masked_data = _xor(data, _mgf1(seed, size - 33))
    return b"\x00" + _xor(seed, _mgf1(masked_data, 32)) + masked_data


def _oaep_decode(encoded, label=OAEP_LABEL):
    if len(encoded) < 66:
        raise LogCryptoError("RSA-OAEP authentication failed")
    seed = _xor(encoded[1:33], _mgf1(encoded[33:], 32))
    data = _xor(encoded[33:], _mgf1(seed, len(encoded) - 33))
    padding, separator, message = data[32:].partition(b"\x01")
    valid = compare_digest(data[:32], sha256(label).digest())
    if encoded[0] != 0 or not valid or not separator or any(padding):
        raise LogCryptoError("RSA-OAEP authentication failed")
    return message


def canonical_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def public_key_id(key):
    return sha256(canonical_json({"n": format(key["n"], "x"), "e": key["e"]}).encode("ascii")).hexdigest()


def _validate_key(key, private=False):
    n, e = key.get("n"), key.get("e")
    if type(n) is not int or type(e) is not int or not 2048 <= n.bit_length() <= 4096 or not n & 1 or e != 65537:
        raise LogCryptoError("RSA keys require a 2048-4096 bit odd modulus and exponent 65537")
    if private:
        d, p, q = (key.get(name) for name in ("d", "p", "q"))
        if any(type(v) is not int or not 1 < v < n for v in (d, p, q)):
            raise LogCryptoError("invalid RSA private key")
        if p == q or not p & 1 or not q & 1 or p * q != n or e * d % lcm(p - 1, q - 1) != 1:
            raise LogCryptoError("inconsistent RSA private key")
    return key


def load_log_key(path, private=False):
    try:
        with Path(path).open("r", encoding="utf-8") as source:
            text = source.read(16385)
        if len(text) > 16384:
            raise LogCryptoError("RSA key file is too large")
        value = json.loads(text)
        expected = "private" if private else "public"
        if not isinstance(value, dict) or value.get("format") != KEY_FORMAT or value.get("kind") != expected:
            raise LogCryptoError("expected a FWLOG " + expected + " key file")
        key = {"e": value["e"]}
        for name in (("n", "d", "p", "q") if private else ("n",)):
            number = value[name]
            if not isinstance(number, str) or not 1 <= len(number) <= 1024 or any(c not in "0123456789abcdef" for c in number):
                raise LogCryptoError("invalid RSA key encoding")
            key[name] = int(number, 16)
        return _validate_key(key, private)
    except (OSError, KeyError, TypeError, ValueError) as error:
        if isinstance(error, LogCryptoError):
            raise
        raise LogCryptoError("cannot read a valid FWLOG key file") from None


def rsa_oaep_encrypt(key, message, seed=None):
    size = (key["n"].bit_length() + 7) // 8
    encoded = _oaep_encode(message, size, secrets.token_bytes(32) if seed is None else seed)
    return pow(int.from_bytes(encoded, "big"), key["e"], key["n"]).to_bytes(size, "big")


def rsa_oaep_decrypt(key, ciphertext):
    n, e, d, p, q = (key[name] for name in ("n", "e", "d", "p", "q"))
    size = (n.bit_length() + 7) // 8
    value = int.from_bytes(ciphertext, "big")
    if len(ciphertext) != size or value >= n:
        raise LogCryptoError("RSA-OAEP authentication failed")
    # Blind the private operation and verify the CRT result before OAEP decoding.
    while True:
        blind = secrets.randbelow(n - 2) + 2
        if gcd(blind, n) == 1:
            break
    blinded = value * pow(blind, e, n) % n
    left, right = pow(blinded, d % (p - 1), p), pow(blinded, d % (q - 1), q)
    message = (right + q * ((left - right) * pow(q, -1, p) % p)) * pow(blind, -1, n) % n
    if pow(message, e, n) != value:
        raise LogCryptoError("RSA-OAEP authentication failed")
    return _oaep_decode(message.to_bytes(size, "big"))


_SMALL_PRIMES = (3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59,
                 61, 67, 71, 73, 79, 83, 89, 97, 101, 103, 107, 109, 113)


def _probable_prime(value):
    if value < 3 or not value & 1 or any(value % p == 0 for p in _SMALL_PRIMES):
        return value in _SMALL_PRIMES
    exponent, shifts = value - 1, 0
    while not exponent & 1:
        exponent >>= 1
        shifts += 1
    # 64 independent Miller-Rabin witnesses: false-positive bound <= 2^-128.
    for _ in range(64):
        base = secrets.randbelow(value - 3) + 2
        witness = pow(base, exponent, value)
        if witness in (1, value - 1):
            continue
        for _ in range(shifts - 1):
            witness = witness * witness % value
            if witness == value - 1:
                break
        else:
            return False
    return True


def generate_rsa_key(bits=3072):
    if type(bits) is not int or bits not in (2048, 3072, 4096):
        raise LogCryptoError("RSA key size must be 2048, 3072 or 4096")
    def prime():
        while True:
            candidate = secrets.randbits(bits // 2) | (3 << (bits // 2 - 2)) | 1
            if gcd(candidate - 1, 65537) == 1 and _probable_prime(candidate):
                return candidate
    p, q = prime(), prime()
    while p == q or abs(p - q).bit_length() < bits // 2 - 100:
        q = prime()
    return _validate_key({"n": p * q, "e": 65537, "d": pow(65537, -1, lcm(p - 1, q - 1)),
                          "p": p, "q": q}, private=True)


def write_keypair(private_path, public_path, bits=3072):
    private_path, public_path = Path(private_path), Path(public_path)
    if private_path.resolve() == public_path.resolve() or any(p.exists() for p in (private_path, public_path)):
        raise LogCryptoError("key outputs must be different new files; existing keys are never overwritten")
    if "coregeek" in (part.lower() for part in private_path.resolve().parts):
        raise LogCryptoError("keep the private key outside the uploaded CoreGeek directory")
    key = generate_rsa_key(bits)
    created = []
    try:
        for path, private in ((private_path, True), (public_path, False)):
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            value = {"format": KEY_FORMAT, "kind": "private" if private else "public", "e": key["e"]}
            value.update({name: format(key[name], "x") for name in (("n", "d", "p", "q") if private else ("n",))})
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600 if private else 0o644)
            created.append(path)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                output.write(canonical_json(value) + "\n")
    except OSError:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return public_key_id(key)


def _b64(data):
    return base64.b64encode(data).decode("ascii")


def packet_header(packet):
    names = ("fwlog_encrypted", "algorithm", "compression", "key_id", "wrapped_key", "nonce")
    try:
        header = {name: packet[name] for name in names}
        if type(header["fwlog_encrypted"]) is not int or header["fwlog_encrypted"] != CRYPTO_VERSION:
            raise LogCryptoError("unsupported encrypted log version")
        if header["algorithm"] != "RSA-OAEP-SHA256+ChaCha20-Poly1305" or header["compression"] != "zlib":
            raise LogCryptoError("unsupported encrypted log algorithm")
        if any(not isinstance(header[name], str) for name in names[1:]):
            raise LogCryptoError("invalid encrypted log header")
        if len(header["key_id"]) != 64 or any(c not in "0123456789abcdef" for c in header["key_id"]):
            raise LogCryptoError("invalid log key identity")
        if not 344 <= len(header["wrapped_key"]) <= 684 or len(header["nonce"]) != 16:
            raise LogCryptoError("invalid encrypted log key or nonce encoding")
        wrapped = base64.b64decode(header["wrapped_key"], validate=True)
        nonce = base64.b64decode(header["nonce"], validate=True)
        if not 256 <= len(wrapped) <= 512 or len(nonce) != 12:
            raise LogCryptoError("invalid encrypted log key or nonce")
        return header
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, LogCryptoError):
            raise
        raise LogCryptoError("invalid encrypted log header") from None


class LogEncryptor:
    def __init__(self, public_key):
        _validate_key(public_key)
        self._key = secrets.token_bytes(32)
        self._wrapped_key = _b64(rsa_oaep_encrypt(public_key, self._key))
        self.key_id = public_key_id(public_key)
        self._counter = 0
        self._lock = Lock()

    def seal(self, raw):
        if len(raw) > MAX_LOG_BYTES:
            raise LogCryptoError("log record exceeds encryption size limit")
        with self._lock:
            if self._counter >= 1 << 96:
                raise LogCryptoError("log encryption nonce space exhausted")
            nonce = self._counter.to_bytes(12, "big")
            self._counter += 1
        header = {"fwlog_encrypted": CRYPTO_VERSION, "algorithm": "RSA-OAEP-SHA256+ChaCha20-Poly1305",
                  "compression": "zlib", "key_id": self.key_id, "wrapped_key": self._wrapped_key,
                  "nonce": _b64(nonce)}
        sealed = aead_encrypt(self._key, nonce, zlib.compress(raw, level=3), canonical_json(header).encode("ascii"))
        return {**header, "ciphertext": _b64(sealed)}


def packet_lines(packet, wire=True):
    header = packet_header(packet)
    sealed = base64.b64decode(packet["ciphertext"], validate=True)
    digest = sha256(sealed).hexdigest()
    total = (len(sealed) + CHUNK_BYTES - 1) // CHUNK_BYTES
    for index in range(total):
        piece = sealed[index * CHUNK_BYTES:(index + 1) * CHUNK_BYTES]
        value = packet if total == 1 else {**header, "fragment": {
            "index": index, "total": total, "sha256": digest, "content": _b64(piece)}}
        yield (ENCRYPTED_PREFIX if wire else "") + canonical_json(value)


class LogDecryptor:
    def __init__(self, keys=()):
        self._keys = {}
        for key in keys:
            _validate_key(key, private=True)
            self._keys[public_key_id(key)] = key
        self._cache = OrderedDict()

    def require_key(self, packet):
        header = packet_header(packet)
        key = self._keys.get(header["key_id"])
        if key is None:
            raise MissingLogKey("encrypted logs require --private-key with the matching FWLOG private key")
        return header, key

    def open(self, packet):
        header, key = self.require_key(packet)
        identity = (header["key_id"], header["wrapped_key"])
        try:
            if identity not in self._cache:
                recovered = rsa_oaep_decrypt(key, base64.b64decode(header["wrapped_key"], validate=True))
                if len(recovered) != 32:
                    raise LogCryptoError("invalid recovered log key")
                self._cache[identity] = recovered
            self._cache.move_to_end(identity)
            while len(self._cache) > 128:
                self._cache.popitem(last=False)
            encoded = packet["ciphertext"]
            if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_PACKET_BYTES + 2) // 3):
                raise LogCryptoError("encrypted log record exceeds size limit")
            sealed = base64.b64decode(encoded, validate=True)
            if len(sealed) > MAX_PACKET_BYTES:
                raise LogCryptoError("encrypted log record exceeds size limit")
            compressed = aead_decrypt(self._cache[identity], base64.b64decode(header["nonce"], validate=True),
                                      sealed, canonical_json(header).encode("ascii"))
            inflater = zlib.decompressobj()
            raw = inflater.decompress(compressed, MAX_LOG_BYTES + 1)
            if len(raw) > MAX_LOG_BYTES or inflater.unconsumed_tail or inflater.unused_data or not inflater.eof:
                raise LogCryptoError("invalid or oversized compressed log record")
            record = json.loads(raw.decode("utf-8"))
            if not isinstance(record, dict):
                raise LogCryptoError("decrypted log record must be a JSON object")
            return record
        except (ValueError, TypeError, KeyError, zlib.error, UnicodeError) as error:
            if isinstance(error, LogCryptoError):
                raise
            raise LogCryptoError("invalid encrypted log record") from None
