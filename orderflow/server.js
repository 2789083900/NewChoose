'use strict';

const fs = require('fs');
const http = require('http');
const path = require('path');
const { URL } = require('url');
const mysql = require('mysql2/promise');
const WebSocket = require('ws');

const ROOT = __dirname;
const CONFIG_PATH = path.join(ROOT, 'config.json');
const SCHEMA_PATH = path.join(ROOT, 'schema.sql');
const DEFAULT_CONFIG = {
  host: '127.0.0.1',
  port: 8787,
  symbols: ['BTCUSDT', 'ETHUSDT', 'SOLUSDT'],
  database: {
    host: '127.0.0.1',
    port: 3306,
    user: 'coinpulse',
    password: 'change-me',
    database: 'coinpulse'
  },
  largeTradeUsd: 100000,
  retentionDays: 60,
  persistClosedMinutesOnly: true
};

function loadConfig() {
  try {
    return { ...DEFAULT_CONFIG, ...JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8')) };
  } catch {
    return DEFAULT_CONFIG;
  }
}

const config = loadConfig();
const symbols = [...new Set((config.symbols || []).map((s) => String(s).toUpperCase()).filter((s) => /^[A-Z0-9]{3,20}$/.test(s)))];
const state = new Map();
let pool = null;
let dbReady = false;
let lastDbError = null;
let cleanupTimer = null;
let stopping = false;

function stateFor(symbol) {
  if (!state.has(symbol)) {
    state.set(symbol, {
      symbol,
      connected: false,
      reconnects: 0,
      trades: 0,
      current: null,
      recent: [],
      cvd: 0,
      lastTradeAt: null,
      lastMessageAt: null,
      lastError: null
    });
  }
  return state.get(symbol);
}

function minuteStart(timestamp) {
  return Math.floor(timestamp / 60000) * 60000;
}

function createBucket(symbol, start) {
  return {
    symbol,
    bucketStart: start,
    buyVolume: 0,
    sellVolume: 0,
    delta: 0,
    cvd: 0,
    tradeCount: 0,
    largeBuyCount: 0,
    largeSellCount: 0,
    largeBuyVolume: 0,
    largeSellVolume: 0
  };
}

function ingestTrade(symbol, trade) {
  const item = stateFor(symbol);
  const start = minuteStart(trade.time);
  if (!item.current || item.current.bucketStart !== start) {
    if (item.current) {
      item.recent.push(item.current);
      item.recent = item.recent.slice(-240);
      void persistBucket(item.current).catch(recordDbError);
    }
    item.current = createBucket(symbol, start);
  }
  const bucket = item.current;
  const isBuy = !trade.isBuyerMaker;
  const large = trade.value >= Number(config.largeTradeUsd || 100000);
  if (isBuy) {
    bucket.buyVolume += trade.qty;
    if (large) {
      bucket.largeBuyCount += 1;
      bucket.largeBuyVolume += trade.qty;
    }
  } else {
    bucket.sellVolume += trade.qty;
    if (large) {
      bucket.largeSellCount += 1;
      bucket.largeSellVolume += trade.qty;
    }
  }
  bucket.delta = bucket.buyVolume - bucket.sellVolume;
  bucket.tradeCount += 1;
  item.cvd += isBuy ? trade.qty : -trade.qty;
  bucket.cvd = item.cvd;
  item.trades += 1;
  item.lastTradeAt = trade.time;
  item.lastMessageAt = Date.now();
}

function normalizeTrade(raw) {
  const price = Number(raw.p);
  const qty = Number(raw.q);
  const time = Number(raw.T || raw.E);
  if (![price, qty, time].every(Number.isFinite) || price <= 0 || qty <= 0) return null;
  return {
    price,
    qty,
    value: price * qty,
    time,
    isBuyerMaker: raw.m === true || raw.m === 1
  };
}

function serializeBucket(bucket) {
  const start = bucket.bucketStart ?? bucket.time;
  return {
    ...bucket,
    time: start,
    timestamp: new Date(start).toISOString()
  };
}

async function initDatabase() {
  try {
    const adminConfig = { ...config.database };
    delete adminConfig.database;
    const adminPool = mysql.createPool({
      ...adminConfig,
      waitForConnections: true,
      connectionLimit: 4,
      decimalNumbers: true
    });
    const schema = fs.readFileSync(SCHEMA_PATH, 'utf8');
    const statements = schema.split(';').map((statement) => statement.trim()).filter(Boolean);
    for (const statement of statements) await adminPool.query(statement);
    await adminPool.end();
    pool = mysql.createPool({
      ...config.database,
      waitForConnections: true,
      connectionLimit: 4,
      decimalNumbers: true
    });
    dbReady = true;
    lastDbError = null;
    for (const symbol of symbols) await restoreSymbolState(symbol);
    await cleanupOldRows();
    cleanupTimer = setInterval(() => void cleanupOldRows().catch(recordDbError), 6 * 60 * 60 * 1000);
  } catch (error) {
    recordDbError(error);
    console.error(`MySQL unavailable: ${lastDbError}`);
  }
}

async function persistBucket(bucket) {
  if (!dbReady || !pool || (config.persistClosedMinutesOnly && bucket.bucketStart >= minuteStart(Date.now()))) return;
  await pool.query(
    `INSERT INTO orderflow_1m
      (symbol, bucket_start, buy_volume, sell_volume, delta, cvd, trade_count,
       large_buy_count, large_sell_count, large_buy_volume, large_sell_volume)
     VALUES (?, FROM_UNIXTIME(? / 1000), ?, ?, ?, ?, ?, ?, ?, ?, ?)
     ON DUPLICATE KEY UPDATE
       buy_volume = VALUES(buy_volume), sell_volume = VALUES(sell_volume),
       delta = VALUES(delta), cvd = VALUES(cvd), trade_count = VALUES(trade_count),
       large_buy_count = VALUES(large_buy_count), large_sell_count = VALUES(large_sell_count),
       large_buy_volume = VALUES(large_buy_volume), large_sell_volume = VALUES(large_sell_volume)`,
    [bucket.symbol, bucket.bucketStart, bucket.buyVolume, bucket.sellVolume, bucket.delta, bucket.cvd,
      bucket.tradeCount, bucket.largeBuyCount, bucket.largeSellCount, bucket.largeBuyVolume, bucket.largeSellVolume]
  );
}

async function cleanupOldRows() {
  if (!dbReady || !pool) return;
  const days = Math.max(1, Number(config.retentionDays || 60));
  const cutoff = new Date(Date.now() - days * 86400000);
  await pool.query('DELETE FROM orderflow_1m WHERE bucket_start < ?', [cutoff]);
}

function recordDbError(error) {
  lastDbError = error && error.message ? error.message : String(error);
  dbReady = false;
}

async function retryDatabase() {
  if (dbReady || stopping) return;
  await initDatabase();
}

function connectSymbol(symbol) {
  const item = stateFor(symbol);
  const stream = `${symbol.toLowerCase()}@aggTrade`;
  const socket = new WebSocket(`wss://fstream.binance.com/ws/${stream}`);
  item.socket = socket;
  socket.on('open', () => {
    item.connected = true;
    item.lastError = null;
  });
  socket.on('message', (payload) => {
    item.lastMessageAt = Date.now();
    try {
      const trade = normalizeTrade(JSON.parse(payload.toString()));
      if (trade) ingestTrade(symbol, trade);
    } catch (error) {
      item.lastError = error.message;
    }
  });
  socket.on('error', (error) => {
    item.lastError = error.message;
  });
  socket.on('close', () => {
    item.connected = false;
    if (stopping) return;
    item.reconnects += 1;
    setTimeout(() => connectSymbol(symbol), 2000);
  });
}

async function restoreSymbolState(symbol) {
  if (!dbReady || !pool) return;
  const item = stateFor(symbol);
  const rows = await loadRecent(symbol, 240);
  item.recent = rows;
  const last = rows[rows.length - 1];
  if (last && Number.isFinite(Number(last.cvd))) item.cvd = Number(last.cvd);
}

async function loadRecent(symbol, limit) {
  if (!dbReady || !pool) return [];
  const safeLimit = Math.min(240, Math.max(1, Number(limit) || 120));
  const [rows] = await pool.query(
    `SELECT symbol, UNIX_TIMESTAMP(bucket_start) * 1000 AS time,
      buy_volume AS buyVolume, sell_volume AS sellVolume, delta, cvd, trade_count AS tradeCount,
      large_buy_count AS largeBuyCount, large_sell_count AS largeSellCount,
      large_buy_volume AS largeBuyVolume, large_sell_volume AS largeSellVolume
     FROM orderflow_1m WHERE symbol = ? ORDER BY bucket_start DESC LIMIT ?`,
    [symbol, safeLimit]
  );
  return rows.reverse();
}

function snapshot(item) {
  return {
    symbol: item.symbol,
    connected: item.connected,
    reconnects: item.reconnects,
    trades: item.trades,
    cvd: item.cvd,
    lastTradeAt: item.lastTradeAt,
    lastMessageAt: item.lastMessageAt,
    lastError: item.lastError,
    current: item.current ? serializeBucket(item.current) : null,
    recent: item.recent.map(serializeBucket),
    databaseReady: dbReady,
    databaseError: lastDbError
  };
}

async function handleApi(request, response, url) {
  response.setHeader('Access-Control-Allow-Origin', '*');
  response.setHeader('Cache-Control', 'no-store');
  const symbol = String(url.searchParams.get('symbol') || symbols[0] || '').toUpperCase();
  if (!symbols.includes(symbol)) {
    response.writeHead(400, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify({ error: 'Unsupported symbol' }));
    return;
  }
  if (url.pathname === '/api/orderflow/status') {
    response.writeHead(200, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify(snapshot(stateFor(symbol))));
    return;
  }
  if (url.pathname === '/api/orderflow/1m') {
    try {
      const rows = await loadRecent(symbol, url.searchParams.get('limit'));
      const item = stateFor(symbol);
      if (item.current) rows.push(serializeBucket(item.current));
      response.writeHead(200, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify({ symbol, rows: rows.slice(-240), databaseReady: dbReady, databaseError: lastDbError }));
    } catch (error) {
      response.writeHead(500, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify({ error: error.message }));
    }
    return;
  }
  response.writeHead(404, { 'Content-Type': 'application/json' });
  response.end(JSON.stringify({ error: 'Not found' }));
}

const server = http.createServer((request, response) => {
  const url = new URL(request.url, `http://${request.headers.host || 'localhost'}`);
  if (url.pathname.startsWith('/api/orderflow/')) {
    void handleApi(request, response, url);
    return;
  }
  response.writeHead(200, { 'Content-Type': 'text/plain; charset=utf-8' });
  response.end('CoinPulse order-flow service is running.');
});

server.listen(Number(config.port || 8787), config.host || '127.0.0.1', () => {
  console.log(`CoinPulse order-flow service: http://${config.host || '127.0.0.1'}:${config.port || 8787}`);
  void initDatabase();
  setInterval(() => void retryDatabase(), 30000);
  for (const symbol of symbols) connectSymbol(symbol);
});

function shutdown() {
  stopping = true;
  if (cleanupTimer) clearInterval(cleanupTimer);
  for (const item of state.values()) if (item.socket) item.socket.close();
  if (pool) void pool.end();
  server.close(() => process.exit(0));
}

process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
