/* 默认矿工：一个 Web Worker，按服务器下发的作业搜索 nonce。

   这是留给同学们用 AI 动手改进的起点，写法刻意直白、节奏刻意放慢：
   每算一个 nonce 都重新拼一遍 84 字节区块头、重新算一次梅克尔根，
   每算出一个哈希都转成 BigInt 再和份额目标比较，每 400 个 nonce 才汇报一次，
   汇报完还要空等 10 毫秒才接着算下一批。
   优化方向见 docs/lab.md，服务器会逐份验证哈希，所以无法靠改数字作弊。 */
importScripts("crypto.js");

const BATCH_SIZE = 400;
/* 刻意留出的等待：一批算完先歇一会儿再算下一批，实时算力上就少掉一大截。 */
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

/* 一个 Worker 里只留一条搜索循环：换作业时先清掉上一批的定时器。
   少了这一步，作业每刷新一次就多出一条循环并行抢算力，算力读数会一直往上飘。 */
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
  const end = nonce + BATCH_SIZE;
  for (; nonce < end; nonce += 1) {
    const digest = SimCrypto.sha256d(headerBytes(nonce));
    const text = SimCrypto.bytesToHex(digest);
    const value = BigInt("0x" + text);
    produced += 1;
    if (value <= shareTarget) {
      self.postMessage({
        type: "share",
        nonce: nonce,
        hash: text,
        reaches_block: value <= blockTarget,
      });
    }
  }
  if (nonce >= NONCE_LIMIT) {
    nonce = 0;
  }
  /* 上报增量而不是累计值：换作业时重置计数不会让控制台的算力读数跳变。 */
  self.postMessage({ type: "progress", hashes: produced - reported });
  reported = produced;
  scheduleBatch(BATCH_PAUSE_MS);
}
