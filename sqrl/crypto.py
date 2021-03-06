
from sqrl import rng
import pysodium as na
from pysodium import sodium
import ctypes
from time import process_time

from cryptography.hazmat.primitives.ciphers import (
    Cipher, algorithms, modes
)

from cryptography.hazmat.backends import default_backend


from sqrl import (
    TAG_BYTES,
    KEY_BYTES,
    GCMIV_BYTES,
    SCRYPT_SALT_BYTES,
    NULLIV
)


def encrypt(key, iv, plaintext, associated_data):
    '''encrypt plaintext and mac with associated_data under key and iv

    uses AES-256-GCM
    '''
    # Construct an AES-GCM Cipher object with the given key and a
    # randomly generated parse_rescue_block.
    encryptor = Cipher(
        algorithms.AES(key),
        modes.GCM(iv),
        backend=default_backend()
    ).encryptor()

    # associated_data will be authenticated but not encrypted,
    # it must also be passed in on decryption.
    encryptor.authenticate_additional_data(associated_data)

    # Encrypt the plaintext and get the associated ciphertext.
    # GCM does not require padding.
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()

    return (ciphertext, encryptor.tag)


def decrypt(key, iv, ciphertext, associated_data, tag):
    '''decrypt ciphertext and mac with associated_data under key and iv, compare mac with tag

    uses AES-256-GCM
    '''
    # Construct a Cipher object, with the key, iv, and additionally the
    # GCM tag used for authenticating the message.
    decryptor = Cipher(
        algorithms.AES(key),
        modes.GCM(iv, tag),
        backend=default_backend()
    ).decryptor()

    # We put associated_data back in or the tag will fail to verify
    # when we finalize the decryptor.
    decryptor.authenticate_additional_data(associated_data)

    # Decryption gs us the authenticated plaintext.
    # If the tag does not match an InvalidTag exception will be raised.
    return decryptor.update(ciphertext) + decryptor.finalize()

def sha256sum(b, bytes=32):
    out = ctypes.create_string_buffer(32)
    na.sodium.crypto_hash_sha256(out, b, ctypes.c_size_t(len(b)))
    return out.raw[:bytes]


class Nonce:
    '''not threadsafe
    Because we are hashing the counter, and truncating the hash, we cannot
    guarantee the full period, so you should ensure that you rotate keys
    frequently.

    If an attacker cannot benefit from information leaked by a sequential
    counter, use that instead of this.
    '''
    __slots__ = ('_bytes', '_count', '_prefix')

    def __init__(self, bytes=na.crypto_secretbox_NONCEBYTES, start=1, prefix=None):
        self._bytes = bytes
        self._count = start
        self._prefix = rng.randombytes(16) if prefix is None else prefix

    def __next__(self):
        self._count, c = self._count + 1, self._count
        res = sha256sum(
            self._prefix + c.to_bytes(self._bytes, 'little'), self._bytes)
        return res

    def __getstate__(self):
        return self._bytes, self._count, self._prefix

    def __setstate__(self, s):
        self.__init__(*s)


def enhash(data, iterations=16):
    '''process the data through 16 rounds of pbkdf2_sha256

    This is intended for deriving secondary keys from a high-entropy master key.
    '''
    assert len(data) == KEY_BYTES
    ld = ctypes.c_ulonglong(KEY_BYTES)
    u = ctypes.create_string_buffer(KEY_BYTES).raw
    sodium.crypto_hash_sha256(u, data, ld)
    acc = int.from_bytes(u, 'little')
    for i in range(1, iterations):
        sodium.crypto_hash_sha256(u, u, ld)
        acc ^= int.from_bytes(u, 'little')
    return acc.to_bytes(32, 'little')

_kdf = sodium.crypto_pwhash_scryptsalsa208sha256_ll


def enscrypt(passwd, salt, logN, iterations, seconds=0):
    '''stretch the password into a high-entropy key

    This is a memory-hard and time-consuming KDF.

    If you are trying to match an exisiting key, leave seconds at 0.
    If you want the derivation to consume a certain amount of time, set seconds to that value.
    The function will terminate when both the minimum iterations and minimum time have been satisfied.

    returns a tuple: (iterations, time_consumed, derived_key)
    '''
    pwlen = ctypes.c_size_t(len(passwd))
    saltlen = ctypes.c_size_t(len(salt))
    N = ctypes.c_uint64(1 << logN)
    r = ctypes.c_uint32(256)
    p = ctypes.c_uint32(1)
    outlen = ctypes.c_size_t(KEY_BYTES)
    out = ctypes.create_string_buffer(KEY_BYTES).raw
    _kdf(
        passwd, pwlen,
        salt, saltlen,
        N, r, p,
        out, outlen,
    )
    acc = int.from_bytes(out, 'little')
    i = 1
    start = process_time()
    end = start + seconds
    while i < iterations or process_time() < end:
        _kdf(
            passwd, pwlen,
            out, outlen,
            N, r, p,
            out, outlen,
        )
        acc ^= int.from_bytes(out, 'little')
        i += 1
    return i, process_time() - start, acc.to_bytes(KEY_BYTES, 'little')

import random
import os


class KeyGen:

    def __init__(self, seed=None):
        '''Gather all of the key generation into one place.

        Use a seed for deterministic results (testing).
        Omit the seed for high-entropy keys.
        '''
        if seed:
            self._rng = random.Random(seed)
        else:
            self._rng = random.SystemRandom()

    def randbytes(self, count):
        return self._rng.getrandbits(count*8).to_bytes(count,'little')

    def rescue_code(self):
        '''generate a random password of 24 decimal digits'''
        r = self._rng.randrange
        return '-'.join(str(r(10000)) for _ in range(6))

    def identity_unlock_key(self):
        '''generate random IdentityUnlockKey'''
        return self.randbytes(KEY_BYTES)

    def identity_master_key(self, iuk):
        '''generate IdentityMasterKey from IdentityUnlockKey'''
        return enhash(iuk)

    def identity_lock_key(self, iuk):
        '''generate IdentityLockKey from IdentityUnlockKey'''
        return crypto_sign_seed_keypair(iuk)[0]

    def public_key(self, sk):
        '''generate public key for the given secret key'''
        return crypto_sign_seed_keypair(sk)[0]

    def local_key(self, mk):
        '''generate a LocalKey from IdentityMasterKey'''
        return enhash(mk)

    def random_lock_key(self):
        '''generate RandomLockKey'''
        return self.randbytes(KEY_BYTES)

    def verify_unlock_key(self, ilk, rlk):
        '''generate VerifyUnlockKey from IdentityLockKey and RandomLockKey

        SignPublic(DHKA(IdentityLock,RandomLock))
        '''
        return crypto_sign_seed_keypair(rlk)[0]

    def server_unlock_key(self, rlk):
        '''generate ServerUnlockKey from RandomLockKey'''
        return crypto_sign_seed_keypair(rlk)[0]

    def unlock_request_signing_key(self, suk, iuk):
        '''generate UnlockRequestSigningKey from ServerUnlockKey and IdentityUnlockKey'''


def sign(message, sk, pk):  # =>signature
    return crypto_sign_detached(message, sk + pk)


def verify(sig, msg, pk):
    crypto_sign_verify_detached(message, pk)
