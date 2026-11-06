
importScripts("crypto.js");

const BATCH_SIZE = 400;

const BATCH_PAUSE_MS = 10;
const NONCE_LIMIT = 4294967296;

let currentJob = null;
let running = false;
let nonce = 0;
let produced = 0;
let reported = 0;
let shareTarget = 0n;
let blockTarget = 0n;
let batchTimer = 0;

self.onmessage = function (event) {
  const message = event.data;
  if (message.type === "job") {
    adoptJob(message.job);
  } else if (message.type === "stop") {
    running = false;
    clearTimeout(batchTimer);
  }
};

function adoptJob(job) {
  currentJob = job;
  nonce = Math.floor(Math.random() * NONCE_LIMIT);
  produced = 0;
  reported = 0;
  shareTarget = BigInt("0x" + job.share_target);
  blockTarget = BigInt("0x" + job.block_target);
  running = true;
  scheduleBatch(0);
}


function scheduleBatch(delay) {
  clearTimeout(batchTimer);
  batchTimer = setTimeout(mineBatch, delay);
}

function merkleRootFromTxIds(txIds) {
  let layer = txIds.map(SimCrypto.hexToBytes);
  if (layer.length === 0) {
    return "0".repeat(64);
  }
  while (layer.length > 1) {
    if (layer.length % 2 === 1) {
      layer.push(layer[layer.length - 1]);
    }
    const next = [];
    for (let index = 0; index < layer.length; index += 2) {
      next.push(SimCrypto.sha256d(SimCrypto.concatBytes([layer[index], layer[index + 1]])));
    }
    layer = next;
  }
  return SimCrypto.bytesToHex(layer[0]);
}

function headerBytes(value) {
  return SimCrypto.concatBytes([
    SimCrypto.toUint32LE(currentJob.version),
    SimCrypto.hexToBytes(currentJob.prev_hash),
    SimCrypto.hexToBytes(merkleRootFromTxIds(currentJob.tx_ids)),
    SimCrypto.toUint32LE(currentJob.timestamp),
    SimCrypto.toUint32LE(currentJob.difficulty),
    SimCrypto.toUint32LE(currentJob.miner_tag),
    SimCrypto.toUint32LE(value),
  ]);
}

function mineBatch() {
  if (!running || currentJob === null) {
    return;
  }
  const end = Math.min(nonce + BATCH_SIZE, NONCE_LIMIT);
  for (; nonce < end; nonce += 1) {
    const digest = SimCrypto.sha256d(headerBytes(nonce));
    const text = SimCrypto.bytesToHex(digest);
    const value = BigInt("0x" + text);
    produced += 1;
    if (value <= shareTarget) {
      self.postMessage({
        type: "share",
        request_id: currentJob.request_id,
        job_id: currentJob.job_id,
        share_bits: currentJob.share_bits,
        nonce: nonce,
        hash: text,
        reaches_block: value <= blockTarget,
      });
    }
  }
  if (nonce >= NONCE_LIMIT) {
    nonce = 0;
  }

  self.postMessage({ type: "progress", request_id: currentJob.request_id, hashes: produced - reported });
  reported = produced;
  scheduleBatch(BATCH_PAUSE_MS);
}
