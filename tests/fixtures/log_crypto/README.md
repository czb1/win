# Log crypto test vectors

`rsa-test-key.json` is an intentionally public, disposable 2048-bit RSA test
private key. Never configure production logs with this key or its public half.
It exists only to keep the standard-library test suite fast and deterministic.

`oaep-known-answer.json` was verified with the system OpenSSL `pkeyutl` in both
directions using OAEP SHA-256, MGF1 SHA-256 and label `FWLOG-KEY-v1`:
the seeded Python ciphertext decrypts in OpenSSL, and the independently
generated OpenSSL ciphertext decrypts in Python. OpenSSL is not an application
dependency or required by the test suite.

ChaCha20, Poly1305 and combined AEAD vectors in `test_log_crypto.py` are from
RFC 8439 sections 2.3.2, 2.5.2 and 2.8.2 respectively.
