/* Password-protected wallet files stay within the browser. */
(function (global) {
  "use strict";
  const ITERATIONS = 600000;
  const encoder = new TextEncoder();

  function cryptoApi() {
    if (!global.crypto?.subtle) {
      throw new Error("加密钱包备份需要 HTTPS 或 localhost，请在可信的安全入口操作");
    }
    return global.crypto.subtle;
  }

  function encode(bytes) {
    return btoa(String.fromCharCode(...bytes));
  }

  function decode(value, length) {
    if (typeof value !== "string" || value.length > 1024) throw new Error("备份格式无效");
    const bytes = Uint8Array.from(atob(value), (character) => character.charCodeAt(0));
    if (bytes.length !== length || encode(bytes) !== value) throw new Error("备份格式无效");
    return bytes;
  }

  async function keyFor(password, salt) {
    if (typeof password !== "string" || password.length < 8 || password.length > 1024) {
      throw new Error("备份密码须为 8 至 1024 个字符");
    }
    const subtle = cryptoApi();
    const material = await subtle.importKey("raw", encoder.encode(password), "PBKDF2", false, ["deriveKey"]);
    return subtle.deriveKey(
      {name: "PBKDF2", salt, iterations: ITERATIONS, hash: "SHA-256"}, material,
      {name: "AES-GCM", length: 256}, false, ["encrypt", "decrypt"],
    );
  }

  function validatePrivate(value) {
    if (typeof value !== "string" || !/^[0-9a-f]{64}$/.test(value)) throw new Error("钱包私钥无效");
    const order = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551n;
    if (BigInt("0x" + value) === 0n || BigInt("0x" + value) >= order) throw new Error("钱包私钥无效");
    return value;
  }

  async function encrypt(privateKey, password) {
    validatePrivate(privateKey);
    const salt = global.crypto.getRandomValues(new Uint8Array(16));
    const iv = global.crypto.getRandomValues(new Uint8Array(12));
    const key = await keyFor(password, salt);
    const ciphertext = await cryptoApi().encrypt(
      {name: "AES-GCM", iv, additionalData: encoder.encode("btc-sim-wallet-v1")},
      key, encoder.encode(privateKey),
    );
    return {format: "btc-sim-wallet-v1", salt: encode(salt), iv: encode(iv),
            ciphertext: encode(new Uint8Array(ciphertext))};
  }

  async function decrypt(backup, password) {
    if (!backup || backup.format !== "btc-sim-wallet-v1") throw new Error("钱包备份版本无效");
    const salt = decode(backup.salt, 16);
    const iv = decode(backup.iv, 12);
    const ciphertext = decode(backup.ciphertext, 80);
    const key = await keyFor(password, salt);
    let plaintext;
    try {
      plaintext = await cryptoApi().decrypt(
        {name: "AES-GCM", iv, additionalData: encoder.encode("btc-sim-wallet-v1")}, key, ciphertext,
      );
    } catch {
      throw new Error("密码错误或钱包备份已损坏");
    }
    return validatePrivate(new TextDecoder("utf-8", {fatal: true}).decode(plaintext));
  }
  global.SimWalletBackup = {encrypt, decrypt};
})(window);
