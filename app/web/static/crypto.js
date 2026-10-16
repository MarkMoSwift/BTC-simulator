/* 浏览器与 Web Worker 共用的密码学工具：SHA-256、P-256 签名、地址推导。

   浏览器只在 https 或 localhost 下才会提供 crypto.subtle，而课堂上学生用的是
   局域网 http 地址，所以这里自带一份纯 JavaScript 实现，任何环境都能用。
   私钥只保存在学生自己的浏览器里，服务器永远拿不到它。 */
(function (global) {
  "use strict";

  const SHA256_K = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ]);

  const P = 0xffffffff00000001000000000000000000000000ffffffffffffffffffffffffn;
  const A = P - 3n;
  const N = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551n;
  const G = [
    0x6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296n,
    0x4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5n,
  ];

  function rotateRight(value, bits) {
    return ((value >>> bits) | (value << (32 - bits))) >>> 0;
  }

  function sha256(bytes) {
    const state = new Uint32Array([
      0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
      0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
    ]);
    const paddedLength = Math.ceil((bytes.length + 9) / 64) * 64;
    const block = new Uint8Array(paddedLength);
    block.set(bytes);
    block[bytes.length] = 0x80;
    const view = new DataView(block.buffer);
    const bitLength = bytes.length * 8;
    view.setUint32(paddedLength - 8, Math.floor(bitLength / 4294967296), false);
    view.setUint32(paddedLength - 4, bitLength >>> 0, false);

    const schedule = new Uint32Array(64);
    for (let offset = 0; offset < paddedLength; offset += 64) {
      for (let index = 0; index < 16; index += 1) {
        schedule[index] = view.getUint32(offset + index * 4, false);
      }
      for (let index = 16; index < 64; index += 1) {
        const w15 = schedule[index - 15];
        const w2 = schedule[index - 2];
        const s0 = rotateRight(w15, 7) ^ rotateRight(w15, 18) ^ (w15 >>> 3);
        const s1 = rotateRight(w2, 17) ^ rotateRight(w2, 19) ^ (w2 >>> 10);
        schedule[index] = (schedule[index - 16] + s0 + schedule[index - 7] + s1) >>> 0;
      }

      let [a, b, c, d, e, f, g, h] = state;
      for (let index = 0; index < 64; index += 1) {
        const sum1 = rotateRight(e, 6) ^ rotateRight(e, 11) ^ rotateRight(e, 25);
        const choose = (e & f) ^ (~e & g);
        const temp1 = (h + sum1 + choose + SHA256_K[index] + schedule[index]) >>> 0;
        const sum0 = rotateRight(a, 2) ^ rotateRight(a, 13) ^ rotateRight(a, 22);
        const majority = (a & b) ^ (a & c) ^ (b & c);
        const temp2 = (sum0 + majority) >>> 0;
        h = g;
        g = f;
        f = e;
        e = (d + temp1) >>> 0;
        d = c;
        c = b;
        b = a;
        a = (temp1 + temp2) >>> 0;
      }
      state[0] = (state[0] + a) >>> 0;
      state[1] = (state[1] + b) >>> 0;
      state[2] = (state[2] + c) >>> 0;
      state[3] = (state[3] + d) >>> 0;
      state[4] = (state[4] + e) >>> 0;
      state[5] = (state[5] + f) >>> 0;
      state[6] = (state[6] + g) >>> 0;
      state[7] = (state[7] + h) >>> 0;
    }

    const digest = new Uint8Array(32);
    const digestView = new DataView(digest.buffer);
    for (let index = 0; index < 8; index += 1) {
      digestView.setUint32(index * 4, state[index], false);
    }
    return digest;
  }

  function sha256d(bytes) {
    return sha256(sha256(bytes));
  }

  function bytesToHex(bytes) {
    let text = "";
    for (let index = 0; index < bytes.length; index += 1) {
      text += bytes[index].toString(16).padStart(2, "0");
    }
    return text;
  }

  function hexToBytes(hex) {
    const out = new Uint8Array(hex.length / 2);
    for (let index = 0; index < out.length; index += 1) {
      out[index] = parseInt(hex.substr(index * 2, 2), 16);
    }
    return out;
  }

  function utf8Bytes(text) {
    return new TextEncoder().encode(text);
  }

  function toUint32LE(value) {
    const out = new Uint8Array(4);
    new DataView(out.buffer).setUint32(0, value >>> 0, true);
    return out;
  }

  function concatBytes(parts) {
    const total = parts.reduce((sum, part) => sum + part.length, 0);
    const out = new Uint8Array(total);
    let offset = 0;
    for (const part of parts) {
      out.set(part, offset);
      offset += part.length;
    }
    return out;
  }

  function randomBytes(length) {
    const out = new Uint8Array(length);
    global.crypto.getRandomValues(out);
    return out;
  }

  function modulo(value, modulus) {
    const result = value % modulus;
    return result < 0n ? result + modulus : result;
  }

  function power(base, exponent, modulus) {
    let result = 1n;
    let factor = modulo(base, modulus);
    let remaining = exponent;
    while (remaining > 0n) {
      if (remaining & 1n) {
        result = (result * factor) % modulus;
      }
      factor = (factor * factor) % modulus;
      remaining >>= 1n;
    }
    return result;
  }

  function inverse(value, modulus) {
    return power(value, modulus - 2n, modulus);
  }

  function pointDouble(point) {
    const [x, y] = point;
    if (y === 0n) {
      return null;
    }
    const slope = modulo((3n * x * x + A) * inverse(2n * y, P), P);
    const nextX = modulo(slope * slope - 2n * x, P);
    return [nextX, modulo(slope * (x - nextX) - y, P)];
  }

  function pointAdd(left, right) {
    if (left === null) {
      return right;
    }
    if (right === null) {
      return left;
    }
    const [x1, y1] = left;
    const [x2, y2] = right;
    if (x1 === x2) {
      return modulo(y1 + y2, P) === 0n ? null : pointDouble(left);
    }
    const slope = modulo((y2 - y1) * inverse(x2 - x1, P), P);
    const nextX = modulo(slope * slope - x1 - x2, P);
    return [nextX, modulo(slope * (x1 - nextX) - y1, P)];
  }

  function pointMultiply(point, scalar) {
    let result = null;
    let addend = point;
    let remaining = scalar;
    while (remaining > 0n) {
      if (remaining & 1n) {
        result = pointAdd(result, addend);
      }
      addend = pointDouble(addend);
      remaining >>= 1n;
    }
    return result;
  }

  function bigIntToBytes(value, length) {
    const out = new Uint8Array(length);
    let remaining = value;
    for (let index = length - 1; index >= 0; index -= 1) {
      out[index] = Number(remaining & 0xffn);
      remaining >>= 8n;
    }
    return out;
  }

  function bytesToBigInt(bytes) {
    let value = 0n;
    for (let index = 0; index < bytes.length; index += 1) {
      value = (value << 8n) | BigInt(bytes[index]);
    }
    return value;
  }

  function generatePrivateKey() {
    for (;;) {
      const candidate = bytesToBigInt(randomBytes(32));
      if (candidate > 0n && candidate < N) {
        return bytesToHex(bigIntToBytes(candidate, 32));
      }
    }
  }

  function randomHex(length) {
    return bytesToHex(randomBytes(Math.ceil(length / 2))).slice(0, length);
  }

  function publicKeyFromPrivate(privateKeyHex) {
    const point = pointMultiply(G, bytesToBigInt(hexToBytes(privateKeyHex)) % N);
    return bytesToHex(concatBytes([bigIntToBytes(point[0], 32), bigIntToBytes(point[1], 32)]));
  }

  function addressFromPublicKey(publicKeyHex) {
    return "sim" + bytesToHex(sha256(hexToBytes(publicKeyHex))).slice(0, 40);
  }

  function sign(privateKeyHex, message) {
    const secret = bytesToBigInt(hexToBytes(privateKeyHex)) % N;
    const digest = bytesToBigInt(sha256(utf8Bytes(message)));
    for (;;) {
      const nonce = bytesToBigInt(randomBytes(32));
      if (nonce === 0n || nonce >= N) {
        continue;
      }
      const point = pointMultiply(G, nonce);
      const r = modulo(point[0], N);
      if (r === 0n) {
        continue;
      }
      const s = modulo(inverse(nonce, N) * (digest + r * secret), N);
      if (s === 0n) {
        continue;
      }
      return bytesToHex(concatBytes([bigIntToBytes(r, 32), bigIntToBytes(s, 32)]));
    }
  }

  global.SimCrypto = {
    sha256,
    sha256d,
    bytesToHex,
    hexToBytes,
    utf8Bytes,
    toUint32LE,
    concatBytes,
    generatePrivateKey,
    randomHex,
    publicKeyFromPrivate,
    addressFromPublicKey,
    sign,
  };
})(typeof self !== "undefined" ? self : this);
