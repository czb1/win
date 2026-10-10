#!/usr/bin/env python3
"""Extract a complete issue window and tasks.txt from one downloaded match .log file.

Standalone script: Python 3.11+ standard library only; no other scripts needed.
"""
import argparse
import base64
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
import re
import sys

# BEGIN EMBEDDED LOG CRYPTO
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
# END EMBEDDED LOG CRYPTO

PREFIX = "FWLOG "
# BEGIN EMBEDDED LOG DISPLAY
"""Compact records for every log sink and export.

This module is embedded in the standalone extractor by sync_log_crypto.py.
"""

TASK_CATEGORIES = {"evolution", "long_context", "reasoning"}
TASK_ACTIONS = {"acceptTask", "submitAnswer", "summonTreasure"}
TASK_PAYLOAD_EVENTS = {"sent_prompt", "sent_executeCmd", "received_llmResp", "received_lastCmdResult"}
HIDDEN_FIELDS = {"run", "request_id", "level", "logger", "timestamp", "source", "session",
                 "record_id", "sequence", "schema_version"}


def is_task(record):
    return record.get("category") in TASK_CATEGORIES or str(record.get("event", "")).startswith("task_")


def task_category(record):
    category = record.get("category")
    if category in TASK_CATEGORIES:
        return category
    if str(record.get("event", "")).startswith("task_"):
        return "evolution"
    return None


def task_related(record):
    if task_category(record) or record.get("event") in TASK_PAYLOAD_EVENTS:
        return True
    data = record.get("data")
    if not isinstance(data, dict):
        return False
    if record.get("event") == "unit_decision" and data.get("role_type") == "pioneer":
        return True
    commands = data.get("commands", data.get("roleCommandMap", {}))
    if isinstance(commands, dict) and any(isinstance(command, dict) and command.get("action") in TASK_ACTIONS
                                          for command in commands.values()):
        return True
    action = data.get("command")
    if isinstance(action, dict) and action.get("action") in TASK_ACTIONS:
        return True
    if record.get("event") == "turn_response" and (data.get("prompt_chars") or data.get("execute_chars")):
        return True
    if record.get("event") == "previous_feedback":
        if record.get("task_id") or data.get("lastSummonTreasureResult") not in (None, "", 0):
            return True
        actions = data.get("actions")
        if isinstance(actions, list) and any(isinstance(item, dict) and isinstance(item.get("command"), dict)
                                            and item["command"].get("action") in TASK_ACTIONS for item in actions):
            return True
    return False


def compact_record(record, keep_session_data=False):
    """Keep useful fields without altering the source or application payloads."""
    result = {key: value for key, value in record.items()
              if key not in HIDDEN_FIELDS and value is not None and value != ""}
    if not task_related(record):
        result.pop("task_id", None)
        result.pop("task_type", None)
    for key in ("task_id", "task_type"):
        if isinstance(result.get(key), str) and not result[key].strip():
            result.pop(key)
    if result.get("message") == result.get("event"):
        result.pop("message", None)
    data = result.get("data")
    if isinstance(data, dict) and record.get("event") == "session_started" and not keep_session_data:
        # Writers retain configuration once per scene for the extractor's meta.json.
        data = {key: data[key] for key in ("version", "reason") if data.get(key) not in (None, "")}
        result["data"] = data
    if isinstance(data, dict) and record.get("event") == "previous_feedback":
        data = {key: value for key, value in data.items() if key != "previous_request_id"}
        result["data"] = data
    if isinstance(data, dict) and record.get("event") in ("unit_decision", "task_state"):
        data = {key: value for key, value in data.items() if value is not None and value not in ({}, [])}
        result["data"] = data
    if data in ({}, []):
        result.pop("data", None)
    return result
# END EMBEDDED LOG DISPLAY


@dataclass
class ReadReport:
    records: int = 0
    ignored_lines: int = 0
    invalid_lines: int = 0
    duplicate_records: int = 0
    incomplete_records: int = 0
    encrypted_records: int = 0
    crypto_errors: int = 0
    problems: list = field(default_factory=list)
    sequences: dict = field(default_factory=dict)

    def problem(self, line, reason):
        if len(self.problems) < 100:
            self.problems.append({"line": line, "reason": reason})

    def summary(self):
        gaps = []
        for run, numbers in self.sequences.items():
            ordered = sorted(numbers)
            gaps.extend({"run": run, "from": a + 1, "to": b - 1}
                        for a, b in zip(ordered, ordered[1:]) if b > a + 1)
        return {"records": self.records, "ignored_lines": self.ignored_lines,
                "invalid_lines": self.invalid_lines, "duplicate_records": self.duplicate_records,
                "incomplete_records": self.incomplete_records, "sequence_gaps": gaps[:100],
                "encrypted_records": self.encrypted_records, "crypto_errors": self.crypto_errors,
                "problems": self.problems,
                "note": "Gaps describe this input; platform truncation and manually cut excerpts may cause them."}


class LogRecord(dict):
    """Reader-only associations never become fields in exported JSON records."""
    def __init__(self, record, metadata=None):
        super().__init__(record)
        self.metadata = metadata or {}


def record_value(record, key):
    return record[key] if key in record else getattr(record, "metadata", {}).get(key)


def record_level(record):
    event = record.get("event")
    if event == "critical":
        return "CRITICAL"
    if record.get("exception") or event in {"error", "turn_invalid", "decision_failed", "empty_response_fallback", "logging_failed"}:
        return "ERROR"
    if event in {"warning", "budget_reached", "client_disconnected", "lock_busy", "log_integrity", "judger_errors"}:
        return "WARNING"
    return "DEBUG" if event == "debug" else "INFO"


class ReaderContext:
    """Infer local scenes/requests from boundaries instead of repeating UUIDs."""
    def __init__(self):
        self.states = OrderedDict()
        self.scenes = Counter()

    def attach(self, record, run):
        key = (run, record.get("team"), record.get("side"))
        state = self.states.setdefault(key, {"session": "scene-0", "round": None,
                                            "request": 0, "active": False, "started": False})
        self.states.move_to_end(key)
        if len(self.states) > 256:
            self.states.popitem(last=False)
        event, number = record.get("event"), record.get("round")
        if event == "session_started":
            self.scenes[run] += 1
            state["session"] = "scene-" + str(self.scenes[run])
        if (event == "request_received" or number is not None and number != state["round"]
                or event in {"turn_started", "turn_snapshot"} and not state["active"]
                or event == "turn_started" and state["started"]):
            state.update(request=state["request"] + 1, active=True, started=False, round=number)
        if event == "turn_started":
            state["started"] = True
        metadata = {"run": run, "session": state["session"],
                    "request_id": state["request"], "level": record_level(record)}
        if event in {"turn_response", "http_response", "client_disconnected", "empty_response_fallback"}:
            state["active"] = state["started"] = False
        return LogRecord(record, metadata)


def events(path, report=None, warn=True, decryptor=None):
    report = report if report is not None else ReadReport()
    decryptor = decryptor if decryptor is not None else LogDecryptor()
    pending, emitted = OrderedDict(), OrderedDict()
    pending_bytes = 0
    context = ReaderContext()
    decoder = json.JSONDecoder()
    with Path(path).open("rb") as source:
        prefix = source.read(4)
    encoding = "utf-16" if prefix.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    with Path(path).open(encoding=encoding, errors="replace") as source:
        for line_no, line in enumerate(source, 1):
            line = re.sub(r"\x1b\[[0-9;]*[mK]", "", line).strip()
            if not line:
                continue
            encrypted_line = False
            fragment_identity = None
            header = None
            if line.startswith("{"):
                raw = line
            elif ENCRYPTED_PREFIX in line and (PREFIX not in line or line.index(ENCRYPTED_PREFIX) < line.index(PREFIX)):
                raw = line.split(ENCRYPTED_PREFIX, 1)[1]
                encrypted_line = True
            elif PREFIX in line:
                raw = line.split(PREFIX, 1)[1]
            else:
                report.ignored_lines += 1
                if Path(path).suffix == ".jsonl":
                    report.invalid_lines += 1
                    report.problem(line_no, "invalid JSONL line")
                    if warn and report.invalid_lines <= 5:
                        print(f"跳过无效日志：{path}:{line_no}", file=sys.stderr)
                continue
            try:
                record, end = decoder.raw_decode(raw)
                if raw[end:].strip() or not isinstance(record, dict):
                    raise ValueError("expected one JSON object")
                encrypted_line = encrypted_line or "fwlog_encrypted" in record
                if encrypted_line:
                    header, _ = decryptor.require_key(record)
                    encrypted_fragment = record.get("fragment")
                    if encrypted_fragment is not None:
                        if not isinstance(encrypted_fragment, dict):
                            raise LogCryptoError("invalid encrypted fragment")
                        index, total = encrypted_fragment["index"], encrypted_fragment["total"]
                        if type(index) is not int or type(total) is not int or not 0 <= index < total <= 4096:
                            raise LogCryptoError("invalid encrypted fragment count")
                        content, digest = encrypted_fragment["content"], encrypted_fragment["sha256"]
                        if not isinstance(content, str) or len(content) > 4 * ((CHUNK_BYTES + 2) // 3):
                            raise LogCryptoError("encrypted fragment exceeds size limit")
                        if not isinstance(digest, str) or len(digest) != 64:
                            raise LogCryptoError("invalid encrypted fragment checksum")
                        part = base64.b64decode(content, validate=True)
                        if not part or len(part) > CHUNK_BYTES:
                            raise LogCryptoError("invalid encrypted fragment length")
                        identity = ("encrypted", header["key_id"], header["wrapped_key"], header["nonce"])
                        state = pending.setdefault(identity, {"parts": {}, "total": total, "sha256": digest,
                                                             "line": line_no, "header": header})
                        if state["total"] != total or state["sha256"] != digest or state["header"] != header:
                            raise LogCryptoError("encrypted fragment metadata mismatch")
                        if index in state["parts"] and state["parts"][index] != part:
                            raise LogCryptoError("conflicting duplicate encrypted fragment")
                        if index not in state["parts"]:
                            state["parts"][index] = part
                            pending_bytes += len(part)
                        while len(pending) > 128 or pending_bytes > 32 * 1024 * 1024:
                            _, removed = pending.popitem(last=False)
                            pending_bytes -= sum(map(len, removed["parts"].values()))
                            report.incomplete_records += 1
                            report.problem(removed["line"], "fragment buffer limit; record unavailable")
                        if identity not in pending or len(state["parts"]) != total:
                            continue
                        sealed = b"".join(state["parts"][i] for i in range(total))
                        pending_bytes -= len(sealed)
                        del pending[identity]
                        if sha256(sealed).hexdigest() != digest:
                            raise LogCryptoError("encrypted fragment checksum mismatch")
                        record = {**header, "ciphertext": base64.b64encode(sealed).decode("ascii")}
                    record = decryptor.open(record)
                    report.encrypted_records += 1
                fragment = record.get("fragment")
                if fragment is not None:
                    if not isinstance(fragment, dict):
                        raise ValueError("invalid fragment")
                    index, total = fragment["index"], fragment["total"]
                    if type(index) is not int or type(total) is not int or not 0 <= index < total <= 4096:
                        raise ValueError("invalid fragment count")
                    if fragment.get("encoding") != "base64":
                        raise ValueError("unsupported fragment encoding")
                    identity = (record.get("run"), record.get("record_id", fragment.get("id")))
                    if not isinstance(identity[1], str):
                        raise ValueError("missing fragment identity")
                    part = base64.b64decode(fragment["content"], validate=True)
                    state = pending.setdefault(identity, {"parts": {}, "total": total,
                                               "sha256": fragment["sha256"], "line": line_no})
                    if state["total"] != total or state["sha256"] != fragment["sha256"]:
                        raise ValueError("fragment metadata mismatch")
                    if index in state["parts"] and state["parts"][index] != part:
                        raise ValueError("conflicting duplicate fragment")
                    if index not in state["parts"]:
                        state["parts"][index] = part
                        pending_bytes += len(part)
                    while len(pending) > 128 or pending_bytes > 32 * 1024 * 1024:
                        _, removed = pending.popitem(last=False)
                        pending_bytes -= sum(map(len, removed["parts"].values()))
                        report.incomplete_records += 1
                        report.problem(removed["line"], "fragment buffer limit; record unavailable")
                    if identity not in pending or len(state["parts"]) != total:
                        continue
                    encoded = b"".join(state["parts"][i] for i in range(total))
                    pending_bytes -= len(encoded)
                    del pending[identity]
                    if sha256(encoded).hexdigest() != state["sha256"]:
                        raise ValueError("fragment checksum mismatch")
                    record = json.loads(encoded.decode("utf-8"))
                    if not isinstance(record, dict) or (record.get("record_id") is not None
                            and (record.get("run"), record.get("record_id")) != identity):
                        raise ValueError("restored record identity mismatch")
                    fragment_identity = identity
                for key in ("run", "request_id", "session", "team", "record_id", "task_id", "category", "event"):
                    if record.get(key) is not None and type(record[key]) not in (str, int):
                        raise ValueError("invalid scalar identity: " + key)
            except MissingLogKey:
                # An encrypted packet must never be silently treated as legacy data.
                raise
            except (ValueError, KeyError, TypeError, UnicodeError) as error:
                report.invalid_lines += 1
                if encrypted_line:
                    report.crypto_errors += 1
                report.problem(line_no, str(error))
                if warn and report.invalid_lines <= 5:
                    print(f"跳过无效日志：{path}:{line_no} ({error})", file=sys.stderr)
                continue
            packet_identity = (("encrypted", header["key_id"], header["wrapped_key"], header["nonce"])
                               if header is not None else None)
            identity = fragment_identity or ((record.get("run"), record["record_id"])
                                            if record.get("record_id") is not None else packet_identity)
            if identity is not None:
                if identity in emitted:
                    report.duplicate_records += 1
                    continue
                emitted[identity] = True
                if len(emitted) > 4096:
                    emitted.popitem(last=False)
            run = (sha256((header["key_id"] + header["wrapped_key"]).encode()).hexdigest()[:16]
                   if header is not None else "plaintext")
            sequence = (int.from_bytes(base64.b64decode(header["nonce"]), "big") + 1
                        if header is not None else None)
            record = context.attach(record, run)
            sequence = record.get("sequence", sequence)
            if type(sequence) is int:
                report.sequences.setdefault(record_value(record, "run"), set()).add(sequence)
            report.records += 1
            yield record
    for state in pending.values():
        report.incomplete_records += 1
        report.problem(state["line"], "missing fragments; record unavailable")
    if warn and report.incomplete_records:
        print(f"日志缺少分段：{report.incomplete_records} 条记录无法恢复", file=sys.stderr)


def scope(record):
    return record_value(record, "run"), record_value(record, "session"), record.get("team")


def payload_key(record):
    data = record.get("data")
    if isinstance(data, dict) and isinstance(data.get("payload_id"), str):
        return (*scope(record), record.get("category"), data["payload_id"])
    return None


def matches_mode(record, mode):
    if mode == "evolution":
        return task_category(record) == "evolution"
    if mode == "long-context":
        return task_category(record) == "long_context"
    if mode == "non-task":
        return not task_related(record)
    return True


def anchors(record, args):
    number = record.get("round")
    if type(number) is not int or not matches_mode(record, args.mode):
        return False
    for name in ("session", "team", "run", "day", "phase", "task_id"):
        expected = getattr(args, name)
        if expected is not None and record_value(record, name) != expected:
            return False
    if args.from_round is not None and number < args.from_round:
        return False
    if args.to_round is not None and number > args.to_round:
        return False
    if args.unit_id is not None and str(record.get("unit_id")) != args.unit_id:
        return False
    return True


def selected_record(record, args, interval):
    number = record.get("round")
    if interval is None or type(number) is not int or not interval[0] - args.context <= number <= interval[1] + args.context:
        return False
    if not matches_mode(record, args.mode):
        return False
    if args.mode != "all":
        # Context must not cross an explicit day/phase/task boundary in filtered modes.
        for name in ("day", "phase", "task_id"):
            expected = getattr(args, name)
            if expected is not None and record.get(name) != expected:
                return False
    return True


def dumps(record):
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path, nargs="?", help="Downloaded .log file or legacy events.jsonl")
    parser.add_argument("--private-key", type=Path, action="append", default=[],
                        help="Local FWLOG private key JSON; repeat for logs encrypted with different public keys")
    parser.add_argument("--generate-keys", type=Path, metavar="PRIVATE_JSON", help="Generate a new local private key")
    parser.add_argument("--public-key-out", type=Path, metavar="PUBLIC_JSON", help="Public key output for --generate-keys")
    parser.add_argument("--key-bits", type=int, choices=(2048, 3072, 4096), default=3072)
    parser.add_argument("--mode", choices=("all", "evolution", "long-context", "non-task"), default="all",
                        help="all=全部；evolution=仅自进化；long-context=仅长上下文；non-task=仅普通日志")
    parser.add_argument("--out", type=Path, default=Path("issue"))
    parser.add_argument("--from-round", type=int)
    parser.add_argument("--to-round", type=int)
    parser.add_argument("--context", type=int, default=5, help="Neighboring game rounds")
    parser.add_argument("--day", type=int)
    parser.add_argument("--phase", choices=("day", "night"))
    for name in ("session", "team", "run", "task-id", "unit-id"):
        parser.add_argument("--" + name)
    parser.add_argument("--split", action="store_true", help="Also write snapshots, decisions, feedback and errors JSONL")
    parser.add_argument("--list", action="store_true", help="List sessions and tasks without writing files")
    args = parser.parse_args(argv)
    if args.generate_keys:
        if args.log is not None or args.private_key or args.public_key_out is None:
            parser.error("--generate-keys requires --public-key-out and cannot be combined with log extraction")
        try:
            identity = write_keypair(args.generate_keys, args.public_key_out, args.key_bits)
        except (LogCryptoError, OSError) as error:
            parser.exit(2, f"无法生成密钥：{error}\n")
        print(f"已生成密钥：私钥 {args.generate_keys}（仅保留在本地），公钥 {args.public_key_out}；key_id={identity}")
        return 0
    if args.log is None or args.public_key_out is not None:
        parser.error("provide a log file, or use --generate-keys with --public-key-out")
    try:
        decryptor = LogDecryptor(load_log_key(path, private=True) for path in args.private_key)
    except LogCryptoError as error:
        parser.exit(2, f"无法读取解密私钥：{error}\n")
    if args.day is not None and args.day < 1:
        parser.error("--day must be at least 1")
    if args.context < 0 or (args.from_round is not None and args.to_round is not None and args.from_round > args.to_round):
        parser.error("invalid round range or negative context")
    report = ReadReport()
    bounds, definitions, metadata, groups = {}, {}, {}, Counter()
    for record in events(args.log, report, decryptor=decryptor):
        identity = scope(record)
        number = record.get("round")
        if record.get("event") == "session_started":
            metadata[identity] = record
        key = payload_key(record)
        if key and "content" in record.get("data", {}):
            definitions.setdefault(key, True)
        if anchors(record, args):
            groups[(identity, None if args.mode == "non-task" else record.get("task_id"))] += 1
            lower, upper = bounds.get(identity, (number, number))
            bounds[identity] = min(lower, number), max(upper, number)
    if args.list:
        print("运行\t场次\t队伍\t任务编号\t记录数")
        for (identity, task), total in sorted(groups.items(), key=lambda row: str(row[0])):
            print("\t".join(str(value or "-") for value in (*identity, task, total)))
        return 2 if report.crypto_errors else 0
    if not bounds:
        print("没有匹配的回合；使用 --list 检查场次与任务编号。", file=sys.stderr)
        return 1
    outputs = {"issue.txt", "tasks.txt", "meta.json", ".records.tmp", "turns.jsonl", "decisions.jsonl", "feedback.jsonl", "errors.jsonl"}
    if args.log.resolve() in {(args.out / name).resolve() for name in outputs}:
        parser.error("output would overwrite input log")
    args.out.mkdir(parents=True, exist_ok=True)
    requested, available, task_requests = set(), set(), set()
    selected_count = 0
    unfinished = set()
    temporary = args.out / ".records.tmp"
    try:
        with temporary.open("w", encoding="utf-8") as target:
            for record in events(args.log, warn=False, decryptor=decryptor):
                identity = scope(record)
                interval = bounds.get(identity)
                if not selected_record(record, args, interval):
                    continue
                if record.get("event") == "session_started":
                    continue
                target.write(dumps({"record": record, "metadata": record.metadata}) + "\n")
                selected_count += 1
                key = payload_key(record)
                if key:
                    requested.add(key)
                    if "content" in record.get("data", {}):
                        available.add(key)
                request = (*identity, record_value(record, "request_id"))
                if record.get("event") == "turn_started":
                    unfinished.add(request)
                elif record.get("event") == "turn_response":
                    unfinished.discard(request)
                if is_task(record) and (args.task_id is None or record.get("task_id") == args.task_id):
                    task_requests.add(request)
        missing = requested - available
        absent = [list(key) for key in missing if key not in definitions]
        meta = {"source": str(args.log), "filters": {name: getattr(args, name) for name in
                ("mode", "from_round", "to_round", "context", "team", "session", "run", "day", "phase", "task_id", "unit_id")},
                "windows": [{"run": key[0], "session": key[1], "team": key[2],
                             "from_round": value[0] - args.context, "to_round": value[1] + args.context}
                            for key, value in bounds.items()],
                "selected_records": selected_count, "read_report": report.summary(),
                "missing_payloads": absent, "requests_without_response": [list(key) for key in unfinished],
                "missing_session_metadata": [list(key) for key in bounds if key not in metadata]}
        meta["session_metadata"] = [{"run": key[0], "session": key[1], **metadata[key]}
                                    for key in bounds if key in metadata]
        files = {"issue": (args.out / "issue.txt").open("w", encoding="utf-8"),
                 "tasks": (args.out / "tasks.txt").open("w", encoding="utf-8")}
        if args.split:
            files.update({name: (args.out / (name + ".jsonl")).open("w", encoding="utf-8")
                          for name in ("turns", "decisions", "feedback", "errors")})
        counts = Counter(task_records=0)
        def write(record):
            encoded = dumps(compact_record(record))
            files["issue"].write("FWLOG " + encoded + "\n")
            request = (*scope(record), record_value(record, "request_id"))
            task = is_task(record) and (args.task_id is None or record.get("task_id") == args.task_id)
            task = task or record.get("event") in ("session_started", "log_integrity")
            task = task or record.get("included_as") == "payload_dependency" and is_task(record)
            task = task or request in task_requests and (record.get("event") in ("previous_feedback", "turn_response")
                     or record.get("event") == "unit_decision" and isinstance(record.get("data"), dict) and record["data"].get("role_type") == "pioneer"
                     or record_value(record, "level") in ("WARNING", "ERROR", "CRITICAL"))
            if task and args.mode != "non-task":
                files["tasks"].write("FWLOG " + encoded + "\n")
                counts["task_records"] += 1
            for name, category in (("turns", "snapshot"), ("decisions", "decision"), ("feedback", "feedback")):
                if name in files and record.get("category") == category:
                    files[name].write(encoded + "\n")
            if "errors" in files and (record_value(record, "level") in ("WARNING", "ERROR", "CRITICAL")
                                       or record.get("event") in ("judger_errors", "log_integrity")):
                files["errors"].write(encoded + "\n")
        try:
            if args.mode == "all":
                issues = {key: value for key, value in {
                    "invalid_lines": report.invalid_lines, "incomplete_records": report.incomplete_records,
                    "crypto_errors": report.crypto_errors, "missing_payloads": len(absent),
                    "unfinished_requests": len(unfinished)}.items() if value}
                if issues:
                    write({"event": "log_integrity", "category": "runtime", "level": "WARNING",
                           "data": {**issues, "details": "meta.json"}})
                for identity in bounds:
                    if identity in metadata:
                        write(LogRecord({**metadata[identity], "included_as": "session_metadata"}, metadata[identity].metadata))
            supplied = set()
            for record in events(args.log, warn=False, decryptor=decryptor):
                key = payload_key(record)
                if key in missing and key not in supplied and "content" in record.get("data", {}):
                    write(LogRecord({**record, "included_as": "payload_dependency"}, record.metadata))
                    supplied.add(key)
            with temporary.open(encoding="utf-8") as selected:
                for line in selected:
                    saved = json.loads(line)
                    write(LogRecord(saved["record"], saved["metadata"]))
        finally:
            for target in files.values():
                target.close()
        meta.update(counts)
        (args.out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    finally:
        temporary.unlink(missing_ok=True)
    print(f"已导出 {selected_count} 条区间记录、{counts['task_records']} 条任务相关记录：{args.out}")
    if report.crypto_errors:
        print("部分加密记录损坏或认证失败；已导出可恢复记录，详情见 meta.json。", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except LogCryptoError as error:
        print(f"无法解密日志：{error}", file=sys.stderr)
        sys.exit(2)
    except OSError as error:
        print(f"无法处理日志：{error}", file=sys.stderr)
        sys.exit(1)

