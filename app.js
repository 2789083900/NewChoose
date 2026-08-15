'use strict';

const $ = (id) => document.getElementById(id);

const DEFAULT_WATCHLIST = [
  'BTCUSDT',
  'ETHUSDT',
  'SOLUSDT',
  'BNBUSDT',
  'XRPUSDT',
  'ADAUSDT',
  'DOGEUSDT',
  'LINKUSDT',
  'AVAXUSDT',
  'TONUSDT',
  'SUIUSDT',
  'PEPEUSDT'
];

const TOKEN_META = {
  BTCUSDT: { base: 'BTC', quote: 'USDT', icon: '₿', name: 'Bitcoin' },
  ETHUSDT: { base: 'ETH', quote: 'USDT', icon: 'Ξ', name: 'Ethereum' },
  SOLUSDT: { base: 'SOL', quote: 'USDT', icon: 'S', name: 'Solana' },
  BNBUSDT: { base: 'BNB', quote: 'USDT', icon: 'B', name: 'BNB' },
  XRPUSDT: { base: 'XRP', quote: 'USDT', icon: 'X', name: 'XRP' },
  ADAUSDT: { base: 'ADA', quote: 'USDT', icon: 'A', name: 'Cardano' },
  DOGEUSDT: { base: 'DOGE', quote: 'USDT', icon: 'Ð', name: 'Dogecoin' },
  LINKUSDT: { base: 'LINK', quote: 'USDT', icon: 'L', name: 'Chainlink' },
  AVAXUSDT: { base: 'AVAX', quote: 'USDT', icon: 'A', name: 'Avalanche' },
  TONUSDT: { base: 'TON', quote: 'USDT', icon: 'T', name: 'Toncoin' },
  SUIUSDT: { base: 'SUI', quote: 'USDT', icon: 'S', name: 'Sui' },
  PEPEUSDT: { base: 'PEPE', quote: 'USDT', icon: 'P', name: 'Pepe' },
  TRXUSDT: { base: 'TRX', quote: 'USDT', icon: 'T', name: 'TRON' },
  DOTUSDT: { base: 'DOT', quote: 'USDT', icon: 'D', name: 'Polkadot' },
  LTCUSDT: { base: 'LTC', quote: 'USDT', icon: 'Ł', name: 'Litecoin' },
  SHIBUSDT: { base: 'SHIB', quote: 'USDT', icon: 'S', name: 'Shiba Inu' },
  APTUSDT: { base: 'APT', quote: 'USDT', icon: 'A', name: 'Aptos' },
  ARBUSDT: { base: 'ARB', quote: 'USDT', icon: 'A', name: 'Arbitrum' },
  OPUSDT: { base: 'OP', quote: 'USDT', icon: 'O', name: 'Optimism' },
  INJUSDT: { base: 'INJ', quote: 'USDT', icon: 'I', name: 'Injective' }
};

const INTERVAL_BARS = { '15m': 96, '1h': 24, '4h': 6, '1d': 1 };

const PROVIDERS = [
  {
    key: 'binance-data',
    label: 'Binance',
    get: async (symbol, interval, limit = 320) => {
      const rows = await fetchJSON(
        `https://data-api.binance.vision/api/v3/klines?symbol=${encodeURIComponent(symbol)}&interval=${encodeURIComponent(interval)}&limit=${limit}`
      );
      return rows.map((r) => ({
        time: +r[0],
        open: +r[1],
        high: +r[2],
        low: +r[3],
        close: +r[4],
        volume: +r[5]
      }));
    }
  },
  {
    key: 'binance-api',
    label: 'Binance',
    get: async (symbol, interval, limit = 320) => {
      const rows = await fetchJSON(
        `https://api.binance.com/api/v3/klines?symbol=${encodeURIComponent(symbol)}&interval=${encodeURIComponent(interval)}&limit=${limit}`
      );
      return rows.map((r) => ({
        time: +r[0],
        open: +r[1],
        high: +r[2],
        low: +r[3],
        close: +r[4],
        volume: +r[5]
      }));
    }
  },
  {
    key: 'bybit',
    label: 'Bybit',
    get: async (symbol, interval, limit = 320) => {
      const map = { '15m': '15', '1h': '60', '4h': '240', '1d': 'D' };
      const data = await fetchJSON(
        `https://api.bybit.com/v5/market/kline?category=spot&symbol=${encodeURIComponent(symbol)}&interval=${map[interval]}&limit=${limit}`
      );
      if (data.retCode !== 0) {
        throw new Error(data.retMsg || 'Bybit error');
      }
      const list = Array.isArray(data.result && data.result.list) ? data.result.list : [];
      return list
        .map((r) => ({
          time: +r[0] * 1000,
          open: +r[1],
          high: +r[2],
          low: +r[3],
          close: +r[4],
          volume: +r[5]
        }))
        .reverse();
    }
  },
  {
    key: 'okx',
    label: 'OKX',
    get: async (symbol, interval, limit = 320) => {
      const map = { '15m': '15m', '1h': '1H', '4h': '4H', '1d': '1D' };
      const instId = symbol.replace('USDT', '-USDT');
      const data = await fetchJSON(
        `https://www.okx.com/api/v5/market/candles?instId=${encodeURIComponent(instId)}&bar=${map[interval]}&limit=${limit}`
      );
      if (data.code !== '0') {
        throw new Error(data.msg || 'OKX error');
      }
      const list = Array.isArray(data.data) ? data.data : [];
      return list
        .map((r) => ({
          time: +r[0],
          open: +r[1],
          high: +r[2],
          low: +r[3],
          close: +r[4],
          volume: +r[5]
        }))
        .reverse();
    }
  }
];

const state = {
  symbol: 'BTCUSDT',
  interval: '4h',
  data: null,
  indicators: null,
  analysis: null,
  watchlist: normalizeWatchlist(readJSON('coinpulse.watchlist', DEFAULT_WATCHLIST)),
  scanResults: {},
  scanLabels: {},
  provider: '',
  auto: true,
  hasLoaded: false,
  mainLoading: false,
  scanning: false,
  smartMoney: null,
  smartLoading: false,
  micro: null,
  microLoading: false,
  backtest: null,
  log: readJSON('coinpulse.log', []),
  lastDetailLabel: {},
  toastTimer: null,
  notify: readJSON('coinpulse.notify', false),
  audioCtx: null,
  deferredPrompt: null
};

function readJSON(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : fallback;
  } catch {
    return fallback;
  }
}

function writeJSON(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // storage can be unavailable in private mode; ignore
  }
}

function normalizeWatchlist(list) {
  if (!Array.isArray(list)) {
    return DEFAULT_WATCHLIST.slice();
  }
  const seen = new Set();
  const out = [];
  for (const item of list) {
    const symbol = String(item || '').trim().toUpperCase();
    if (/^[A-Z0-9]{3,20}$/.test(symbol) && !seen.has(symbol)) {
      seen.add(symbol);
      out.push(symbol);
    }
  }
  return out.length ? out : DEFAULT_WATCHLIST.slice();
}

async function fetchJSON(url, timeoutMs = 9000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      signal: controller.signal,
      headers: { Accept: 'application/json' }
    });
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

async function fetchKlinesWithFallback(symbol, interval, limit = 320) {
  const errors = [];
  for (const provider of PROVIDERS) {
    try {
      const klines = await provider.get(symbol, interval, limit);
      if (Array.isArray(klines) && klines.length > 50) {
        return { klines, provider: provider.label };
      }
      throw new Error('数据不足');
    } catch (err) {
      errors.push(`${provider.key}: ${err.message}`);
    }
  }
  throw new Error(errors.join(' / '));
}

async function fetchKlinesForBacktest(symbol, interval, total = 2000) {
  try {
    const rows = [];
    let cursor = Date.now();
    const pageSize = Math.min(1000, total);
    while (rows.length < total && cursor > 0) {
      const page = await fetchJSON(
        `https://data-api.binance.vision/api/v3/klines?symbol=${encodeURIComponent(symbol)}&interval=${encodeURIComponent(interval)}&limit=${pageSize}&endTime=${cursor}`
      );
      if (!Array.isArray(page) || !page.length) break;
      rows.unshift(...page);
      if (page.length < pageSize) break;
      cursor = page[0][0] - 1;
    }
    if (rows.length < 80) {
      throw new Error('历史数据不足');
    }
    const parsed = rows
      .map((r) => ({
        time: +r[0],
        open: +r[1],
        high: +r[2],
        low: +r[3],
        close: +r[4],
        volume: +r[5]
      }))
      .sort((a, b) => a.time - b.time);
    const deduped = [];
    for (const k of parsed) {
      if (!deduped.length || deduped[deduped.length - 1].time !== k.time) {
        deduped.push(k);
      }
    }
    return { klines: deduped.slice(-total), provider: 'Binance' };
  } catch (err) {
    return fetchKlinesWithFallback(symbol, interval, Math.min(total, 1000));
  }
}

function ema(values, period) {
  const result = new Array(values.length).fill(null);
  if (values.length < period) {
    return result;
  }
  let sum = 0;
  for (let i = 0; i < period; i += 1) {
    sum += values[i];
  }
  let prev = sum / period;
  result[period - 1] = prev;
  const multiplier = 2 / (period + 1);
  for (let i = period; i < values.length; i += 1) {
    prev = (values[i] - prev) * multiplier + prev;
    result[i] = prev;
  }
  return result;
}

function calcRSI(closes, period = 14) {
  const rsi = new Array(closes.length).fill(null);
  if (closes.length <= period) {
    return rsi;
  }
  let gainSum = 0;
  let lossSum = 0;
  for (let i = 1; i <= period; i += 1) {
    const change = closes[i] - closes[i - 1];
    gainSum += Math.max(change, 0);
    lossSum += Math.max(-change, 0);
  }
  let avgGain = gainSum / period;
  let avgLoss = lossSum / period;
  rsi[period] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);
  for (let i = period + 1; i < closes.length; i += 1) {
    const change = closes[i] - closes[i - 1];
    avgGain = (avgGain * (period - 1) + Math.max(change, 0)) / period;
    avgLoss = (avgLoss * (period - 1) + Math.max(-change, 0)) / period;
    rsi[i] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);
  }
  return rsi;
}

function calcKDJ(klines, period = 9) {
  const length = klines.length;
  const K = new Array(length).fill(null);
  const D = new Array(length).fill(null);
  const J = new Array(length).fill(null);
  let prevK = 50;
  let prevD = 50;
  for (let i = 0; i < length; i += 1) {
    const from = Math.max(0, i - period + 1);
    let high = -Infinity;
    let low = Infinity;
    for (let q = from; q <= i; q += 1) {
      high = Math.max(high, klines[q].high);
      low = Math.min(low, klines[q].low);
    }
    const rsv = high === low ? 50 : ((klines[i].close - low) / (high - low)) * 100;
    prevK = (2 / 3) * prevK + (1 / 3) * rsv;
    prevD = (2 / 3) * prevD + (1 / 3) * prevK;
    K[i] = prevK;
    D[i] = prevD;
    J[i] = 3 * prevK - 2 * prevD;
  }
  return { K, D, J };
}

function calcATR(klines, period = 14) {
  if (klines.length < period + 1) return null;
  const trs = [];
  for (let i = 1; i < klines.length; i += 1) {
    const prevClose = klines[i - 1].close;
    trs.push(Math.max(
      klines[i].high - klines[i].low,
      Math.abs(klines[i].high - prevClose),
      Math.abs(klines[i].low - prevClose)
    ));
  }
  const tail = trs.slice(-period);
  return tail.reduce((sum, value) => sum + value, 0) / tail.length;
}

function calcATRSeries(klines, period = 14) {
  const atr = new Array(klines.length).fill(null);
  if (klines.length <= period) return atr;
  const trs = [];
  for (let i = 1; i < klines.length; i += 1) {
    const prevClose = klines[i - 1].close;
    trs.push(Math.max(
      klines[i].high - klines[i].low,
      Math.abs(klines[i].high - prevClose),
      Math.abs(klines[i].low - prevClose)
    ));
  }
  const first = trs.slice(0, period).reduce((sum, value) => sum + value, 0) / period;
  atr[period] = first;
  for (let i = period + 1; i < klines.length; i += 1) {
    atr[i] = (atr[i - 1] * (period - 1) + trs[i - 1]) / period;
  }
  return atr;
}

function calcADX(klines, period = 14) {
  const length = klines.length;
  const adx = new Array(length).fill(null);
  const plusDi = new Array(length).fill(null);
  const minusDi = new Array(length).fill(null);
  if (length <= period) return { adx, plusDi, minusDi };

  const trs = new Array(length).fill(0);
  const plusDms = new Array(length).fill(0);
  const minusDms = new Array(length).fill(0);
  for (let i = 1; i < length; i += 1) {
    const high = klines[i].high;
    const low = klines[i].low;
    const prevClose = klines[i - 1].close;
    const prevHigh = klines[i - 1].high;
    const prevLow = klines[i - 1].low;
    const upMove = high - prevHigh;
    const downMove = prevLow - low;
    plusDms[i] = upMove > downMove && upMove > 0 ? upMove : 0;
    minusDms[i] = downMove > upMove && downMove > 0 ? downMove : 0;
    trs[i] = Math.max(high - low, Math.abs(high - prevClose), Math.abs(low - prevClose));
  }

  let trSum = 0;
  let plusSum = 0;
  let minusSum = 0;
  for (let i = 1; i <= period; i += 1) {
    trSum += trs[i];
    plusSum += plusDms[i];
    minusSum += minusDms[i];
  }
  let prevTr = trSum / period;
  let prevPlus = plusSum / period;
  let prevMinus = minusSum / period;
  plusDi[period] = prevTr > 0 ? (prevPlus / prevTr) * 100 : 0;
  minusDi[period] = prevTr > 0 ? (prevMinus / prevTr) * 100 : 0;
  const firstSum = prevPlus + prevMinus;
  let prevDx = firstSum > 0 ? (Math.abs(prevPlus - prevMinus) / firstSum) * 100 : 0;
  adx[period] = prevDx;

  for (let i = period + 1; i < length; i += 1) {
    prevTr = (prevTr * (period - 1) + trs[i]) / period;
    prevPlus = (prevPlus * (period - 1) + plusDms[i]) / period;
    prevMinus = (prevMinus * (period - 1) + minusDms[i]) / period;
    plusDi[i] = prevTr > 0 ? (prevPlus / prevTr) * 100 : 0;
    minusDi[i] = prevTr > 0 ? (prevMinus / prevTr) * 100 : 0;
    const sum = prevPlus + prevMinus;
    const dx = sum > 0 ? (Math.abs(prevPlus - prevMinus) / sum) * 100 : 0;
    prevDx = (prevDx * (period - 1) + dx) / period;
    adx[i] = prevDx;
  }
  return { adx, plusDi, minusDi };
}

function calcStochRSI(closes, rsiPeriod = 14, stochPeriod = 14) {
  const rsi = calcRSI(closes, rsiPeriod);
  const length = closes.length;
  const K = new Array(length).fill(null);
  const D = new Array(length).fill(null);
  const first = rsiPeriod + stochPeriod - 1;
  for (let i = first; i < length; i += 1) {
    let min = Infinity;
    let max = -Infinity;
    for (let q = i - stochPeriod + 1; q <= i; q += 1) {
      if (rsi[q] == null) continue;
      min = Math.min(min, rsi[q]);
      max = Math.max(max, rsi[q]);
    }
    if (Number.isFinite(min) && max > min) {
      K[i] = ((rsi[i] - min) / (max - min)) * 100;
    } else {
      K[i] = 50;
    }
  }
  for (let i = 0; i < length; i += 1) {
    if (K[i] == null) continue;
    let sum = 0;
    let count = 0;
    for (let q = Math.max(0, i - 2); q <= i; q += 1) {
      if (K[q] != null) {
        sum += K[q];
        count += 1;
      }
    }
    if (count >= 3) D[i] = sum / count;
  }
  return { K, D };
}

function calcBB(closes, period = 20, multiplier = 2) {
  const upper = new Array(closes.length).fill(null);
  const middle = new Array(closes.length).fill(null);
  const lower = new Array(closes.length).fill(null);
  const width = new Array(closes.length).fill(null);
  const percentB = new Array(closes.length).fill(null);
  for (let i = period - 1; i < closes.length; i += 1) {
    const slice = closes.slice(i - period + 1, i + 1);
    const mean = slice.reduce((sum, v) => sum + v, 0) / period;
    const variance = slice.reduce((sum, v) => sum + (v - mean) ** 2, 0) / period;
    const sd = Math.sqrt(variance);
    middle[i] = mean;
    upper[i] = mean + multiplier * sd;
    lower[i] = mean - multiplier * sd;
    width[i] = middle[i] ? ((upper[i] - lower[i]) / middle[i]) * 100 : 0;
    percentB[i] = upper[i] - lower[i] > 0 ? (closes[i] - lower[i]) / (upper[i] - lower[i]) : 0.5;
  }
  return { upper, middle, lower, width, percentB };
}

function computeIndicators(klines) {
  const closes = klines.map((k) => k.close);
  const ema12 = ema(closes, 12);
  const ema26 = ema(closes, 26);
  const dif = new Array(closes.length).fill(null);
  for (let i = 25; i < closes.length; i += 1) {
    dif[i] = ema12[i] - ema26[i];
  }
  const dea = new Array(closes.length).fill(null);
  const hist = new Array(closes.length).fill(null);
  const deaSlice = ema(dif.slice(25), 9);
  for (let i = 0; i < deaSlice.length; i += 1) {
    dea[25 + i] = deaSlice[i];
    hist[25 + i] = (dif[25 + i] - deaSlice[i]) * 2;
  }
  return {
    ema20: ema(closes, 20),
    ema50: ema(closes, 50),
    ema100: ema(closes, 100),
    ema200: ema(closes, 200),
    closes,
    macd: { dif, dea, hist },
    rsi: calcRSI(closes, 14),
    kdj: calcKDJ(klines, 9),
    adx: calcADX(klines, 14),
    stochRsi: calcStochRSI(closes, 14, 14),
    bb: calcBB(closes, 20, 2)
  };
}

function analyzeIndicators(klines, indicators) {
  return analyzeAt(indicators, klines.length - 1);
}

function analyzeAt(indicators, last) {
  const { macd, kdj, rsi, adx, stochRsi, bb } = indicators;
  const close = indicators.closes ? indicators.closes[last] : null;
  const prev = Math.max(0, last - 1);
  const dif = macd.dif[last];
  const dea = macd.dea[last];
  const hist = macd.hist[last];
  const k = kdj.K[last];
  const d = kdj.D[last];
  const j = kdj.J[last];
  const rsiCur = rsi[last] == null ? 50 : rsi[last];
  const rsiPrev = prev > 0 && rsi[prev] != null ? rsi[prev] : rsiCur;
  const hasPrev = prev > 0 && macd.dif[prev] != null && macd.dea[prev] != null;

  let score = 0;
  const reasons = [];
  const macdCross = { golden: false, dead: false };
  const kdjCross = { golden: false, dead: false };

  if (hasPrev) {
    if (macd.dif[prev] <= macd.dea[prev] && dif > dea) {
      score += 2.5;
      macdCross.golden = true;
      reasons.push('MACD金叉');
    } else if (macd.dif[prev] >= macd.dea[prev] && dif < dea) {
      score -= 2.5;
      macdCross.dead = true;
      reasons.push('MACD死叉');
    }

    if (kdj.K[prev] <= kdj.D[prev] && k > d) {
      score += 1.5;
      kdjCross.golden = true;
      reasons.push('KDJ金叉');
    } else if (kdj.K[prev] >= kdj.D[prev] && k < d) {
      score -= 1.5;
      kdjCross.dead = true;
      reasons.push('KDJ死叉');
    }

    score += hist > macd.hist[prev] ? 0.5 : -0.5;

    if (rsiPrev < 50 && rsiCur >= 50) {
      score += 1;
      reasons.push('RSI上穿50');
    } else if (rsiPrev > 50 && rsiCur <= 50) {
      score -= 1;
      reasons.push('RSI下穿50');
    }
  }

  score += dif > dea ? 1 : -1;
  score += k > d ? 0.5 : -0.5;
  score += rsiCur > 50 ? 0.5 : -0.5;

  if (j < 20) {
    score += 1;
    reasons.push('KDJ超卖');
  } else if (j > 80) {
    score -= 1;
    reasons.push('KDJ超买');
  }

  if (rsiCur < 30) {
    score += 1;
    reasons.push('RSI超卖');
  } else if (rsiCur > 70) {
    score -= 1;
    reasons.push('RSI超买');
  }

  const stK = stochRsi ? stochRsi.K[last] : null;
  const stD = stochRsi ? stochRsi.D[last] : null;
  const stKPrev = prev > 0 && stochRsi ? stochRsi.K[prev] : null;
  const stDPrev = prev > 0 && stochRsi ? stochRsi.D[prev] : null;
  if (stK != null && stKPrev != null && stDPrev != null) {
    if (stKPrev <= stDPrev && stK > stD) {
      score += 1;
      reasons.push('StochRSI金叉');
    } else if (stKPrev >= stDPrev && stK < stD) {
      score -= 1;
      reasons.push('StochRSI死叉');
    }
  }
  if (stK != null) {
    if (stK < 20) {
      score += 1;
      reasons.push('StochRSI超卖');
    } else if (stK > 80) {
      score -= 1;
      reasons.push('StochRSI超买');
    }
  }

  const adxCur = adx ? adx.adx[last] : null;
  const plusDi = adx ? adx.plusDi[last] : null;
  const minusDi = adx ? adx.minusDi[last] : null;
  if (adxCur != null && plusDi != null && minusDi != null && adxCur >= 25) {
    if (plusDi > minusDi) {
      score += 1;
      reasons.push('ADX多头趋势');
    } else if (minusDi > plusDi) {
      score -= 1;
      reasons.push('ADX空头趋势');
    }
  }

  const bbUpper = bb.upper[last];
  const bbLower = bb.lower[last];
  const bbMiddle = bb.middle[last];
  const bbWidth = bb.width[last];
  let bbText = '--';
  if (close != null && bbUpper != null && bbLower != null && bbMiddle != null) {
    if (close > bbUpper) {
      score += 1;
      reasons.push('BB突破上轨');
      bbText = '突破上轨';
    } else if (close < bbLower) {
      score -= 1;
      reasons.push('BB跌破下轨');
      bbText = '跌破下轨';
    } else {
      bbText = close > bbMiddle ? '中轨上方' : close < bbMiddle ? '中轨下方' : '中轨';
    }
    score += close > bbMiddle ? 0.5 : -0.5;
    let widthSum = 0;
    let widthCount = 0;
    for (let q = last - 20; q < last; q += 1) {
      if (bb.width[q] != null) {
        widthSum += bb.width[q];
        widthCount += 1;
      }
    }
    if (widthCount && bbWidth != null && bbWidth < (widthSum / widthCount) * 0.9) {
      reasons.push('BB收口');
    }
  }

  const rounded = Math.round(score * 10) / 10;
  const label = rounded >= 4 ? '强烈看多' : rounded >= 2 ? '偏多' : rounded <= -4 ? '强烈看空' : rounded <= -2 ? '偏空' : '震荡观望';
  const signalClass = rounded >= 2 ? 'bull' : rounded <= -2 ? 'bear' : 'flat';
const reason = reasons.length ? [...new Set(reasons)].slice(0, 4).join(' · ') : '指标信号暂未共振';

  return {
    score: rounded,
    label,
    signalClass,
    reason,
    macd: macdSummary(macd, last, prev, macdCross),
    kdj: kdjSummary(kdj, last, prev, kdjCross, j),
    rsi: rsiSummary(rsiCur),
    adx: adxSummary(adxCur, plusDi, minusDi),
    stochRsi: stochRsiSummary(stK, stD),
    bb: bbText
  };
}

function macdSummary(macd, last, prev, cross) {
  if (cross.golden) return '金叉';
  if (cross.dead) return '死叉';
  if (macd.dif[last] > macd.dea[last]) return '多头';
  if (macd.dif[last] < macd.dea[last]) return '空头';
  return '平衡';
}

function kdjSummary(kdj, last, prev, cross, j) {
  if (cross.golden) return '金叉';
  if (cross.dead) return '死叉';
  if (j < 20) return '超卖';
  if (j > 80) return '超买';
  if (kdj.K[last] > kdj.D[last]) return '多头';
  if (kdj.K[last] < kdj.D[last]) return '空头';
  return '平衡';
}

function rsiSummary(value) {
  if (value < 30) return '超卖';
  if (value > 70) return '超买';
  if (value > 50) return '偏强';
  if (value < 50) return '偏弱';
  return '中性';
}

function adxSummary(value, plusDi, minusDi) {
  if (value == null || plusDi == null || minusDi == null) return '--';
  if (value >= 25) return plusDi > minusDi ? '强趋势·多头' : '强趋势·空头';
  return '弱趋势';
}

function stochRsiSummary(k, d) {
  if (k == null) return '--';
  if (k > 80) return '超买';
  if (k < 20) return '超卖';
  if (k > d) return '多头';
  if (k < d) return '空头';
  return '中性';
}

function getMeta(symbol) {
  const known = TOKEN_META[symbol] || {};
  const base = known.base || symbol.replace(/USDT$|USDC$|FDUSD$|BUSD$/, '') || symbol.slice(0, 3);
  const quote = known.quote || 'USDT';
  return {
    base,
    quote,
    icon: known.icon || base.slice(0, 1).toUpperCase(),
    name: known.name || base
  };
}

function getChange(klines, interval) {
  const bars = INTERVAL_BARS[interval] || 24;
  const n = Math.min(bars, klines.length - 1);
  const prevClose = klines[klines.length - 1 - n] ? klines[klines.length - 1 - n].close : klines[0].close;
  const lastClose = klines[klines.length - 1].close;
  return (lastClose / prevClose - 1) * 100;
}

function lookbackSlice(klines, interval) {
  const bars = INTERVAL_BARS[interval] || 24;
  const n = Math.max(1, Math.min(bars, klines.length));
  return klines.slice(-n);
}

function formatPrice(value) {
  if (value == null || Number.isNaN(value)) return '--';
  const abs = Math.abs(value);
  if (abs >= 1000) {
    return value.toLocaleString('en-US', { minimumFractionDigits: 0, maximumFractionDigits: 2 });
  }
  if (abs >= 1) {
    return value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 4 });
  }
  return value.toLocaleString('en-US', { minimumFractionDigits: 4, maximumFractionDigits: 8 });
}

function formatAxis(value) {
  if (value == null || Number.isNaN(value)) return '--';
  const abs = Math.abs(value);
  if (abs >= 1e6) return `${(value / 1e6).toFixed(2)}M`;
  if (abs >= 1000) return value.toLocaleString('en-US', { maximumFractionDigits: 0 });
  if (abs >= 100) return value.toFixed(0);
  if (abs >= 1) return value.toFixed(1);
  return value.toFixed(4);
}

function formatVolume(value) {
  if (value == null || Number.isNaN(value)) return '--';
  const abs = Math.abs(value);
  if (abs >= 1e9) return `${(value / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `${(value / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `${(value / 1e3).toFixed(1)}K`;
  return value.toFixed(2);
}

function formatUsd(value) {
  if (value == null || Number.isNaN(value)) return '--';
  const abs = Math.abs(value);
  if (abs >= 1e9) return `$${(value / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `$${(value / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `$${(value / 1e3).toFixed(1)}K`;
  return `$${value.toFixed(0)}`;
}

function formatQuantity(value) {
  if (value == null || Number.isNaN(value)) return '--';
  if (value >= 1000) return value.toFixed(0);
  if (value >= 1) return value.toFixed(3);
  return value.toFixed(6);
}

function formatPct(value) {
  if (value == null || Number.isNaN(value)) return '--';
  return `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`;
}

function formatNumber(value) {
  if (value == null || Number.isNaN(value)) return '--';
  return Number(value).toFixed(4);
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
  }[c]));
}

function setConn(status, provider = '') {
  const dotClass = status === 'ok' ? 'ok' : status === 'err' ? 'err' : '';
  const text = status === 'ok' ? `已连接 · ${provider || ''}` : status === 'loading' ? '连接中' : '连接失败';
  $('connDot').className = `dot ${dotClass}`;
  $('connText').textContent = text;
  $('sideDot').className = `dot ${dotClass}`;
  $('sideConnText').textContent = status === 'ok' ? '已连接' : status === 'loading' ? '连接中' : '连接失败';
}

function setLastUpdated() {
  if (state.data) {
    const now = new Date().toLocaleTimeString('zh-CN', { hour12: false });
    $('lastUpdateText').textContent = `更新 ${now}`;
  } else {
    $('lastUpdateText').textContent = '等待数据';
  }
}

function showToast(message) {
  const toast = $('toast');
  toast.textContent = message;
  toast.classList.add('show');
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => toast.classList.remove('show'), 2600);
}

function setChip(id, text, cls = 'flat') {
  const el = $(id);
  if (el) {
    el.textContent = text;
    el.className = `signal-chip ${cls}`;
  }
}

function chipClassForIndicator(text) {
  if (!text) return 'flat';
  if (/金叉|多头|超卖|上穿|偏强/.test(text)) return 'bull';
  if (/死叉|空头|超买|下穿|偏弱/.test(text)) return 'bear';
  return 'flat';
}

function setupCanvas(canvas) {
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(rect.width, 40);
  const height = Math.max(rect.height, 40);
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(width * dpr);
  canvas.height = Math.round(height * dpr);
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  return { ctx, width, height };
}

function drawEmptyChart(ctx, width, height, text = '等待数据') {
  ctx.fillStyle = '#5d6c77';
  ctx.font = '13px "Segoe UI", "Microsoft YaHei", sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(text, width / 2, height / 2);
}

function strokeLine(ctx, points, xFn, yFn, color, width = 1.4, dash = []) {
  ctx.save();
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.setLineDash(dash);
  ctx.beginPath();
  let started = false;
  for (let i = 0; i < points.length; i += 1) {
    const value = points[i];
    if (value == null) {
      started = false;
      continue;
    }
    const x = xFn(i);
    const y = yFn(value);
    if (!started) {
      ctx.moveTo(x, y);
      started = true;
    } else {
      ctx.lineTo(x, y);
    }
  }
  ctx.stroke();
  ctx.restore();
}

function drawMarker(ctx, x, y, direction, label) {
  const color = direction === 'up' ? '#2dd4a7' : '#fb5f74';
  ctx.save();
  ctx.fillStyle = color;
  ctx.beginPath();
  if (direction === 'up') {
    ctx.moveTo(x, y);
    ctx.lineTo(x - 5, y - 8);
    ctx.lineTo(x + 5, y - 8);
  } else {
    ctx.moveTo(x, y);
    ctx.lineTo(x - 5, y + 8);
    ctx.lineTo(x + 5, y + 8);
  }
  ctx.closePath();
  ctx.fill();
  ctx.font = '9px "Cascadia Mono", Consolas, monospace';
  ctx.textAlign = 'center';
  ctx.fillText(label, x, direction === 'up' ? y - 11 : y + 12);
  ctx.restore();
}

function drawPriceChart() {
  const canvas = $('priceChart');
  const { ctx, width, height } = setupCanvas(canvas);
  const data = state.data;
  if (!data || data.length < 2 || !state.indicators) {
    drawEmptyChart(ctx, width, height, state.symbol ? '正在加载行情' : '等待数据');
    return;
  }

  const visibleCount = Math.min(160, data.length);
  const start = data.length - visibleCount;
  const padL = 8;
  const padR = 58;
  const padT = 12;
  const padB = 38;
  const plotW = Math.max(1, width - padL - padR);
  const plotH = Math.max(1, height - padT - padB);
  const volH = Math.min(70, plotH * 0.22);
  const priceH = Math.max(80, plotH - volH - 18);

  let min = Infinity;
  let max = -Infinity;
  let maxVol = 0;
  const bb = state.indicators.bb;
  for (let i = start; i < data.length; i += 1) {
    min = Math.min(min, data[i].low);
    max = Math.max(max, data[i].high);
    maxVol = Math.max(maxVol, data[i].volume);
    if (bb.upper[i] != null) {
      min = Math.min(min, bb.lower[i]);
      max = Math.max(max, bb.upper[i]);
    }
  }
  const range = Math.max(max - min, max * 0.001);
  const yMin = min - range * 0.08;
  const yMax = max + range * 0.08;
  const xStep = plotW / visibleCount;
  const candleW = Math.max(1, Math.min(14, xStep * 0.62));
  const xFor = (i) => padL + (i - start + 0.5) * xStep;
  const yFor = (v) => padT + ((yMax - v) / (yMax - yMin)) * priceH;

  ctx.font = '11px "Cascadia Mono", Consolas, monospace';
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  for (let i = 0; i <= 4; i += 1) {
    const ratio = i / 4;
    const y = padT + priceH * ratio;
    const value = yMax - (yMax - yMin) * ratio;
    ctx.strokeStyle = 'rgba(38, 50, 59, 0.5)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(padL + plotW, y);
    ctx.stroke();
    ctx.fillStyle = '#5d6c77';
    ctx.fillText(formatAxis(value), width - 4, y);
  }

  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  for (let i = 0; i <= 4; i += 1) {
    const idx = start + Math.round((i * (visibleCount - 1)) / 4);
    const date = new Date(data[idx].time);
    const label = `${date.getMonth() + 1}/${date.getDate()} ${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
    ctx.fillStyle = '#5d6c77';
    ctx.fillText(label, xFor(idx), padT + priceH + volH + 12);
  }

  const volTop = padT + priceH + 8;
  for (let i = start; i < data.length; i += 1) {
    const bar = data[i];
    const up = bar.close >= bar.open;
    const volHpx = Math.max(1, (bar.volume / (maxVol || 1)) * volH);
    ctx.fillStyle = up ? 'rgba(45, 212, 167, 0.24)' : 'rgba(251, 95, 116, 0.24)';
    ctx.fillRect(xFor(i) - candleW / 2, volTop + volH - volHpx, candleW, volHpx);
  }

  const bbUpper = bb.upper.slice(start);
  const bbMiddle = bb.middle.slice(start);
  const bbLower = bb.lower.slice(start);
  ctx.save();
  ctx.beginPath();
  let bandStarted = false;
  for (let i = 0; i < bbUpper.length; i += 1) {
    if (bbUpper[i] == null) {
      bandStarted = false;
      continue;
    }
    const x = padL + (i + 0.5) * xStep;
    const y = yFor(bbUpper[i]);
    if (!bandStarted) {
      ctx.moveTo(x, y);
      bandStarted = true;
    } else {
      ctx.lineTo(x, y);
    }
  }
  for (let i = bbLower.length - 1; i >= 0; i -= 1) {
    if (bbLower[i] == null) continue;
    ctx.lineTo(padL + (i + 0.5) * xStep, yFor(bbLower[i]));
  }
  ctx.closePath();
  ctx.fillStyle = 'rgba(240, 171, 252, 0.06)';
  ctx.fill();
  ctx.restore();
  strokeLine(ctx, bbUpper, (i) => padL + (i + 0.5) * xStep, yFor, '#e879f9', 1.1, [3, 3]);
  strokeLine(ctx, bbMiddle, (i) => padL + (i + 0.5) * xStep, yFor, '#e879f9', 1.2);
  strokeLine(ctx, bbLower, (i) => padL + (i + 0.5) * xStep, yFor, '#e879f9', 1.1, [3, 3]);

  for (let i = start; i < data.length; i += 1) {
    const bar = data[i];
    const up = bar.close >= bar.open;
    const color = up ? '#2dd4a7' : '#fb5f74';
    const x = xFor(i);
    ctx.strokeStyle = color;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x, yFor(bar.high));
    ctx.lineTo(x, yFor(bar.low));
    ctx.stroke();
    ctx.fillStyle = color;
    const bodyTop = yFor(Math.max(bar.open, bar.close));
    const bodyBottom = yFor(Math.min(bar.open, bar.close));
    ctx.fillRect(x - candleW / 2, bodyTop, candleW, Math.max(1, bodyBottom - bodyTop));
  }

  const { ema20, ema50, ema100, ema200 } = state.indicators;
  strokeLine(ctx, ema20.slice(start), (i) => padL + (i + 0.5) * xStep, yFor, '#42c8e6', 1.5);
  strokeLine(ctx, ema50.slice(start), (i) => padL + (i + 0.5) * xStep, yFor, '#f7b955', 1.5);
  strokeLine(ctx, ema100.slice(start), (i) => padL + (i + 0.5) * xStep, yFor, '#a78bfa', 1.4);
  strokeLine(ctx, ema200.slice(start), (i) => padL + (i + 0.5) * xStep, yFor, '#94a3b8', 1.4);

  const markerStart = Math.max(start, data.length - 55);
  for (let i = markerStart + 1; i < data.length; i += 1) {
    const local = i - start;
    const x = xFor(i);
    const macd = state.indicators.macd;
    const kdj = state.indicators.kdj;
    if (macd.dif[i - 1] <= macd.dea[i - 1] && macd.dif[i] > macd.dea[i]) {
      drawMarker(ctx, x, yFor(data[i].low) + 6, 'up', 'M');
    }
    if (macd.dif[i - 1] >= macd.dea[i - 1] && macd.dif[i] < macd.dea[i]) {
      drawMarker(ctx, x, yFor(data[i].high) - 6, 'down', 'M');
    }
    if (kdj.K[i - 1] <= kdj.D[i - 1] && kdj.K[i] > kdj.D[i]) {
      drawMarker(ctx, x, yFor(data[i].low) + 18, 'up', 'K');
    }
    if (kdj.K[i - 1] >= kdj.D[i - 1] && kdj.K[i] < kdj.D[i]) {
      drawMarker(ctx, x, yFor(data[i].high) - 18, 'down', 'K');
    }
    if (bb.upper[i - 1] != null && data[i - 1].close <= bb.upper[i - 1] && data[i].close > bb.upper[i]) {
      drawMarker(ctx, x, yFor(data[i].low) + 30, 'up', 'B');
    }
    if (bb.lower[i - 1] != null && data[i - 1].close >= bb.lower[i - 1] && data[i].close < bb.lower[i]) {
      drawMarker(ctx, x, yFor(data[i].high) - 30, 'down', 'B');
    }
  }

  const lastClose = data[data.length - 1].close;
  ctx.setLineDash([4, 4]);
  ctx.strokeStyle = 'rgba(232, 238, 242, 0.24)';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(padL, yFor(lastClose));
  ctx.lineTo(padL + plotW, yFor(lastClose));
  ctx.stroke();
  ctx.setLineDash([]);
}

function drawMacdChart() {
  const canvas = $('macdChart');
  const { ctx, width, height } = setupCanvas(canvas);
  if (!state.indicators) {
    drawEmptyChart(ctx, width, height, 'MACD');
    return;
  }
  const m = state.indicators.macd;
  const length = m.dif.length;
  const start = Math.max(0, length - 120);
  const padL = 8;
  const padR = 36;
  const padT = 10;
  const padB = 18;
  const plotW = Math.max(1, width - padL - padR);
  const plotH = Math.max(1, height - padT - padB);
  let min = Infinity;
  let max = -Infinity;
  for (let i = start; i < length; i += 1) {
    for (const value of [m.hist[i], m.dif[i], m.dea[i]]) {
      if (value != null) {
        min = Math.min(min, value);
        max = Math.max(max, value);
      }
    }
  }
  if (!Number.isFinite(min) || !Number.isFinite(max)) {
    drawEmptyChart(ctx, width, height, 'MACD');
    return;
  }
  const range = max - min || 1;
  const yMin = min - range * 0.12;
  const yMax = max + range * 0.12;
  const xStep = plotW / (length - start);
  const xFor = (i) => padL + (i - start + 0.5) * xStep;
  const yFor = (v) => padT + ((yMax - v) / (yMax - yMin)) * plotH;
  const zeroY = yFor(0);

  ctx.font = '11px "Cascadia Mono", Consolas, monospace';
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  for (const y of [padT, zeroY, padT + plotH]) {
    ctx.strokeStyle = 'rgba(38, 50, 59, 0.5)';
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(padL + plotW, y);
    ctx.stroke();
  }
  ctx.fillStyle = '#5d6c77';
  ctx.fillText(formatAxis(yMax), width - 2, padT + 8);
  ctx.fillText('0', width - 2, zeroY);
  ctx.fillText(formatAxis(yMin), width - 2, padT + plotH);

  for (let i = start; i < length; i += 1) {
    const value = m.hist[i];
    if (value == null) continue;
    ctx.fillStyle = value >= 0 ? 'rgba(45, 212, 167, 0.55)' : 'rgba(251, 95, 116, 0.55)';
    const x = xFor(i);
    const barW = Math.max(1, xStep * 0.55);
    ctx.fillRect(x - barW / 2, Math.min(zeroY, yFor(value)), barW, Math.max(1, Math.abs(yFor(value) - zeroY)));
  }

  strokeLine(ctx, m.dif.slice(start), (i) => padL + (i + 0.5) * xStep, yFor, '#42c8e6', 1.5);
  strokeLine(ctx, m.dea.slice(start), (i) => padL + (i + 0.5) * xStep, yFor, '#f7b955', 1.5);
}

function drawKdjChart() {
  const canvas = $('kdjChart');
  const { ctx, width, height } = setupCanvas(canvas);
  if (!state.indicators) {
    drawEmptyChart(ctx, width, height, 'KDJ');
    return;
  }
  const k = state.indicators.kdj;
  const length = k.K.length;
  const start = Math.max(0, length - 120);
  const padL = 8;
  const padR = 36;
  const padT = 10;
  const padB = 18;
  const plotW = Math.max(1, width - padL - padR);
  const plotH = Math.max(1, height - padT - padB);
  let min = Infinity;
  let max = -Infinity;
  for (let i = start; i < length; i += 1) {
    for (const value of [k.K[i], k.D[i], k.J[i]]) {
      if (value != null) {
        min = Math.min(min, value);
        max = Math.max(max, value);
      }
    }
  }
  if (!Number.isFinite(min) || !Number.isFinite(max)) {
    drawEmptyChart(ctx, width, height, 'KDJ');
    return;
  }
  const range = max - min || 1;
  const yMin = min - range * 0.1;
  const yMax = max + range * 0.1;
  const xStep = plotW / (length - start);
  const xFor = (i) => padL + (i - start + 0.5) * xStep;
  const yFor = (v) => padT + ((yMax - v) / (yMax - yMin)) * plotH;

  ctx.font = '11px "Cascadia Mono", Consolas, monospace';
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  for (const level of [20, 80]) {
    ctx.strokeStyle = 'rgba(247, 185, 85, 0.28)';
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(padL, yFor(level));
    ctx.lineTo(padL + plotW, yFor(level));
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = '#5d6c77';
    ctx.fillText(String(level), width - 2, yFor(level));
  }

  strokeLine(ctx, k.K.slice(start), (i) => padL + (i + 0.5) * xStep, yFor, '#42c8e6', 1.5);
  strokeLine(ctx, k.D.slice(start), (i) => padL + (i + 0.5) * xStep, yFor, '#f7b955', 1.5);
  strokeLine(ctx, k.J.slice(start), (i) => padL + (i + 0.5) * xStep, yFor, '#fb5f74', 1.4);
}

function drawRsiChart() {
  const canvas = $('rsiChart');
  const { ctx, width, height } = setupCanvas(canvas);
  if (!state.indicators) {
    drawEmptyChart(ctx, width, height, 'RSI');
    return;
  }
  const rsi = state.indicators.rsi;
  const length = rsi.length;
  const start = Math.max(0, length - 120);
  const padL = 8;
  const padR = 36;
  const padT = 10;
  const padB = 18;
  const plotW = Math.max(1, width - padL - padR);
  const plotH = Math.max(1, height - padT - padB);
  const xStep = plotW / (length - start);
  const xFor = (i) => padL + (i - start + 0.5) * xStep;
  const yFor = (v) => padT + ((100 - v) / 100) * plotH;

  ctx.font = '11px "Cascadia Mono", Consolas, monospace';
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  for (const level of [30, 50, 70]) {
    ctx.strokeStyle = level === 50 ? 'rgba(38, 50, 59, 0.7)' : 'rgba(247, 185, 85, 0.3)';
    ctx.setLineDash(level === 50 ? [] : [4, 4]);
    ctx.beginPath();
    ctx.moveTo(padL, yFor(level));
    ctx.lineTo(padL + plotW, yFor(level));
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = '#5d6c77';
    ctx.fillText(String(level), width - 2, yFor(level));
  }

  const values = rsi.slice(start);
  ctx.save();
  ctx.beginPath();
  let started = false;
  for (let i = 0; i < values.length; i += 1) {
    const value = values[i];
    if (value == null) {
      started = false;
      continue;
    }
    const x = xFor(i + start);
    const y = yFor(value);
    if (!started) {
      ctx.moveTo(x, y);
      started = true;
    } else {
      ctx.lineTo(x, y);
    }
  }
  ctx.lineTo(padL + plotW, padT + plotH);
  ctx.lineTo(padL, padT + plotH);
  ctx.closePath();
  ctx.fillStyle = 'rgba(66, 200, 230, 0.07)';
  ctx.fill();
  ctx.restore();

  strokeLine(ctx, rsi.slice(start), (i) => padL + (i + 0.5) * xStep, yFor, '#42c8e6', 1.6);
}

function drawAdxChart() {
  const canvas = $('adxChart');
  const { ctx, width, height } = setupCanvas(canvas);
  if (!state.indicators || !state.indicators.adx) {
    drawEmptyChart(ctx, width, height, 'ADX');
    return;
  }
  const adx = state.indicators.adx;
  const length = adx.adx.length;
  const start = Math.max(0, length - 120);
  const padL = 8;
  const padR = 36;
  const padT = 10;
  const padB = 18;
  const plotW = Math.max(1, width - padL - padR);
  const plotH = Math.max(1, height - padT - padB);
  const xStep = plotW / (length - start);
  const xFor = (i) => padL + (i + 0.5) * xStep;
  const yFor = (v) => padT + ((100 - v) / 100) * plotH;

  ctx.font = '11px "Cascadia Mono", Consolas, monospace';
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  for (const level of [25, 50]) {
    ctx.strokeStyle = level === 25 ? 'rgba(247, 185, 85, 0.35)' : 'rgba(38, 50, 59, 0.5)';
    ctx.setLineDash(level === 25 ? [4, 4] : []);
    ctx.beginPath();
    ctx.moveTo(padL, yFor(level));
    ctx.lineTo(padL + plotW, yFor(level));
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = '#5d6c77';
    ctx.fillText(String(level), width - 2, yFor(level));
  }

  strokeLine(ctx, adx.adx.slice(start), xFor, yFor, '#42c8e6', 1.7);
  strokeLine(ctx, adx.plusDi.slice(start), xFor, yFor, '#2dd4a7', 1.2);
  strokeLine(ctx, adx.minusDi.slice(start), xFor, yFor, '#fb5f74', 1.2);
}

function drawStochRsiChart() {
  const canvas = $('stochRsiChart');
  const { ctx, width, height } = setupCanvas(canvas);
  if (!state.indicators || !state.indicators.stochRsi) {
    drawEmptyChart(ctx, width, height, 'StochRSI');
    return;
  }
  const sr = state.indicators.stochRsi;
  const length = sr.K.length;
  const start = Math.max(0, length - 120);
  const padL = 8;
  const padR = 36;
  const padT = 10;
  const padB = 18;
  const plotW = Math.max(1, width - padL - padR);
  const plotH = Math.max(1, height - padT - padB);
  const xStep = plotW / (length - start);
  const xFor = (i) => padL + (i + 0.5) * xStep;
  const yFor = (v) => padT + ((100 - v) / 100) * plotH;

  ctx.font = '11px "Cascadia Mono", Consolas, monospace';
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  for (const level of [20, 80]) {
    ctx.strokeStyle = 'rgba(247, 185, 85, 0.28)';
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(padL, yFor(level));
    ctx.lineTo(padL + plotW, yFor(level));
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = '#5d6c77';
    ctx.fillText(String(level), width - 2, yFor(level));
  }

  strokeLine(ctx, sr.K.slice(start), xFor, yFor, '#42c8e6', 1.6);
  strokeLine(ctx, sr.D.slice(start), xFor, yFor, '#f7b955', 1.6);
}

function drawSparkline(canvas, closes) {
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(rect.width, 2);
  const height = Math.max(rect.height, 2);
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(width * dpr);
  canvas.height = Math.round(height * dpr);
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  const values = closes.slice(-48);
  if (values.length < 2) return;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || max * 0.01 || 1;
  const up = values[values.length - 1] >= values[0];
  const pad = 2;
  const xFor = (i) => pad + ((width - pad * 2) * i) / (values.length - 1);
  const yFor = (v) => pad + (height - pad * 2) * (1 - (v - min) / range);
  const gradient = ctx.createLinearGradient(0, 0, 0, height);
  gradient.addColorStop(0, up ? 'rgba(45, 212, 167, 0.22)' : 'rgba(251, 95, 116, 0.22)');
  gradient.addColorStop(1, 'rgba(13, 18, 22, 0)');
  ctx.beginPath();
  ctx.moveTo(xFor(0), yFor(values[0]));
  for (let i = 1; i < values.length; i += 1) {
    ctx.lineTo(xFor(i), yFor(values[i]));
  }
  ctx.lineTo(xFor(values.length - 1), height - pad);
  ctx.lineTo(xFor(0), height - pad);
  ctx.closePath();
  ctx.fillStyle = gradient;
  ctx.fill();
  ctx.strokeStyle = up ? '#2dd4a7' : '#fb5f74';
  ctx.lineWidth = 1.4;
  ctx.beginPath();
  for (let i = 0; i < values.length; i += 1) {
    if (i === 0) ctx.moveTo(xFor(i), yFor(values[i]));
    else ctx.lineTo(xFor(i), yFor(values[i]));
  }
  ctx.stroke();
}

function renderMain() {
  const meta = getMeta(state.symbol);
  $('assetIcon').textContent = meta.icon;
  $('symbolTitle').textContent = state.symbol;
  $('assetSubtitle').textContent = `${meta.name} / ${meta.quote}`;
  $('chartTitle').textContent = `${meta.base} 价格走势`;
  document.title = `${state.symbol} · CoinPulse`;

  if (!state.data || !state.indicators || !state.analysis) {
    $('priceDisplay').textContent = '--';
    $('changeDisplay').textContent = '--';
    $('changeDisplay').className = 'change flat';
    $('chartSubtitle').textContent = `${state.interval.toUpperCase()} · 等待数据`;
    const banner = $('signalBanner');
    banner.className = 'signal-banner flat';
    $('bannerIcon').textContent = '→';
    $('bannerLabel').textContent = '加载中';
    $('bannerScore').textContent = '--';
    $('bannerReason').textContent = '正在获取行情';
    $('bannerMacd').textContent = '--';
    $('bannerKdj').textContent = '--';
    $('bannerRsi').textContent = '--';
    $('bannerBb').textContent = '--';
    setChip('macdChip', '--', 'flat');
    setChip('kdjChip', '--', 'flat');
    setChip('rsiChip', '--', 'flat');
    setChip('adxChip', '--', 'flat');
    setChip('stochRsiChip', '--', 'flat');
    $('statsGrid').innerHTML = '<div class="stat-cell"><span>数据</span><strong>加载中</strong></div>';
    renderStrategy();
    return;
  }

  const klines = state.data;
  const analysis = state.analysis;
  const indicators = state.indicators;
  const lastIndex = klines.length - 1;
  const last = klines[lastIndex];
  const change = getChange(klines, state.interval);
  const changeClass = change > 0.05 ? 'up' : change < -0.05 ? 'down' : 'flat';

  $('priceDisplay').textContent = formatPrice(last.close);
  $('changeDisplay').textContent = formatPct(change);
  $('changeDisplay').className = `change ${changeClass}`;
  $('chartSubtitle').textContent = `${state.interval.toUpperCase()} · ${klines.length}根K线 · ${state.provider || '实时数据'}`;

  const banner = $('signalBanner');
  banner.className = `signal-banner ${analysis.signalClass}`;
  $('bannerIcon').textContent = analysis.signalClass === 'bull' ? '↗' : analysis.signalClass === 'bear' ? '↘' : '→';
  $('bannerLabel').textContent = analysis.label;
  $('bannerScore').textContent = `${analysis.score > 0 ? '+' : ''}${analysis.score}`;
  $('bannerReason').textContent = analysis.reason;
  $('bannerMacd').textContent = analysis.macd;
  $('bannerKdj').textContent = analysis.kdj;
  $('bannerRsi').textContent = analysis.rsi;
  $('bannerBb').textContent = analysis.bb || '--';

  setChip('macdChip', analysis.macd, chipClassForIndicator(analysis.macd));
  setChip('kdjChip', analysis.kdj, chipClassForIndicator(analysis.kdj));
  setChip('rsiChip', analysis.rsi, chipClassForIndicator(analysis.rsi));
  setChip('adxChip', analysis.adx, chipClassForIndicator(analysis.adx));
  setChip('stochRsiChip', analysis.stochRsi, chipClassForIndicator(analysis.stochRsi));

  const look = lookbackSlice(klines, state.interval);
  const high = Math.max(...look.map((k) => k.high));
  const low = Math.min(...look.map((k) => k.low));
  const volume = look.reduce((sum, k) => sum + k.volume, 0);
  const prevClose = look[0].close;
  const amplitude = ((high - low) / prevClose) * 100;
  const cells = [
    ['最新价', formatPrice(last.close), ''],
    ['阶段涨跌', formatPct(change), changeClass],
    ['区间最高', formatPrice(high), 'up'],
    ['区间最低', formatPrice(low), 'down'],
    ['成交量', formatVolume(volume), ''],
    ['区间振幅', `${amplitude.toFixed(2)}%`, ''],
    ['EMA 20', formatNumber(indicators.ema20[lastIndex]), ''],
    ['EMA 50', formatNumber(indicators.ema50[lastIndex]), ''],
    ['EMA 100', formatNumber(indicators.ema100[lastIndex]), ''],
    ['EMA 200', formatNumber(indicators.ema200[lastIndex]), ''],
    ['BB上轨', formatPrice(indicators.bb.upper[lastIndex]), ''],
    ['BB中轨', formatPrice(indicators.bb.middle[lastIndex]), ''],
    ['BB下轨', formatPrice(indicators.bb.lower[lastIndex]), ''],
    ['BB带宽', `${Number(indicators.bb.width[lastIndex] || 0).toFixed(2)}%`, '']
  ];
  $('statsGrid').innerHTML = cells
    .map(([label, value, cls]) => `<div class="stat-cell"><span>${label}</span><strong class="${cls}">${value}</strong></div>`)
    .join('');

  $('macdStats').innerHTML = [
    ['DIF', indicators.macd.dif[lastIndex]],
    ['DEA', indicators.macd.dea[lastIndex]],
    ['HIST', indicators.macd.hist[lastIndex]]
  ]
    .map(([label, value]) => `<div><span>${label}</span><strong>${formatNumber(value)}</strong></div>`)
    .join('');
  $('kdjStats').innerHTML = [
    ['K', indicators.kdj.K[lastIndex]],
    ['D', indicators.kdj.D[lastIndex]],
    ['J', indicators.kdj.J[lastIndex]]
  ]
    .map(([label, value]) => `<div><span>${label}</span><strong>${formatNumber(value)}</strong></div>`)
    .join('');
  $('rsiStats').innerHTML = [
    ['RSI14', indicators.rsi[lastIndex]],
    ['50线', '50.00'],
    ['区间', '30 / 70']
  ]
    .map(([label, value]) => `<div><span>${label}</span><strong>${typeof value === 'number' ? formatNumber(value) : value}</strong></div>`)
    .join('');
  $('adxStats').innerHTML = [
    ['ADX14', indicators.adx.adx[lastIndex]],
    ['+DI', indicators.adx.plusDi[lastIndex]],
    ['-DI', indicators.adx.minusDi[lastIndex]]
  ]
    .map(([label, value]) => `<div><span>${label}</span><strong>${formatNumber(value)}</strong></div>`)
    .join('');
  $('stochRsiStats').innerHTML = [
    ['K', indicators.stochRsi.K[lastIndex]],
    ['D', indicators.stochRsi.D[lastIndex]],
    ['区间', '20 / 80']
  ]
    .map(([label, value]) => `<div><span>${label}</span><strong>${typeof value === 'number' ? formatNumber(value) : value}</strong></div>`)
    .join('');
  renderStrategy();
}

function renderStrategy() {
  const tag = $('strategyTag');
  const metaEl = $('strategyMeta');
  const planEl = $('strategyPlan');
  const dirEl = $('strategyDirection');
  const entryEl = $('strategyEntry');
  const stopEl = $('strategyStop');
  const targetEl = $('strategyTarget');
  const sizeEl = $('strategySize');

  setChip('strategyTag', '--', 'flat');
  metaEl.textContent = `${state.symbol} · ${state.interval.toUpperCase()} · 等待行情`;
  planEl.textContent = '正在生成策略，等待行情数据加载。';
  dirEl.textContent = '--';
  entryEl.textContent = '--';
  stopEl.textContent = '--';
  targetEl.textContent = '--';
  sizeEl.textContent = '--';
  [dirEl, entryEl, stopEl, targetEl, sizeEl].forEach((el) => {
    el.className = '';
  });

  if (!state.data || !state.analysis || !state.indicators) return;

  const meta = getMeta(state.symbol);
  const klines = state.data;
  const analysis = state.analysis;
  const indicators = state.indicators;
  const lastIndex = klines.length - 1;
  const last = klines[lastIndex];
  const look = lookbackSlice(klines, state.interval);
  const recentHigh = Math.max(...look.map((k) => k.high));
  const recentLow = Math.min(...look.map((k) => k.low));
  const atr = calcATR(klines, 14) || last.close * 0.008;
  const ema20 = indicators.ema20[lastIndex];
  const ema50 = indicators.ema50[lastIndex];
  const ema100 = indicators.ema100[lastIndex];
  const ema200 = indicators.ema200[lastIndex];
  const hasAllEma = [ema20, ema50, ema100, ema200].every(Number.isFinite);
  const bullAlign = hasAllEma && ema20 > ema50 && ema50 > ema100 && ema100 > ema200;
  const bearAlign = hasAllEma && ema20 < ema50 && ema50 < ema100 && ema100 < ema200;
  const emaState = bullAlign ? 'EMA 多头排列' : bearAlign ? 'EMA 空头排列' : 'EMA 均线纠缠';
  const volumeSpike = getVolumeSpike();
  const sm = state.smartMoney;
  const smRatio = sm && Number.isFinite(sm.netRatio) ? sm.netRatio : null;
  const smAgrees = smRatio != null && ((analysis.signalClass === 'bull' && smRatio >= 5) || (analysis.signalClass === 'bear' && smRatio <= -5));
  const smOpposes = smRatio != null && ((analysis.signalClass === 'bull' && smRatio <= -5) || (analysis.signalClass === 'bear' && smRatio >= 5));

  setChip('strategyTag', analysis.label, analysis.signalClass);
  metaEl.textContent = `${meta.base} · ${state.interval.toUpperCase()} · ATR ${formatPrice(atr)} · ${emaState}`;

  const direction = analysis.signalClass === 'bull' ? '做多' : analysis.signalClass === 'bear' ? '做空' : '观望';
  dirEl.textContent = direction;
  dirEl.className = analysis.signalClass;

  // 高胜率反转策略：优先展示反转信号（超卖抄底/超买逃顶）
  const reversal = detectHighWinReversal(klines, indicators, lastIndex);
  if (reversal.direction) {
    const grade = reversal.points >= 6 ? '★★★★★ 极高胜率' : reversal.points >= 5 ? '★★★★ 高胜率' : reversal.points >= 4 ? '★★★ 较高胜率' : '★★ 中胜率';
    const isLong = reversal.direction === 'long';
    dirEl.textContent = isLong ? '抄底做多' : '逃顶做空';
    dirEl.className = isLong ? 'bull' : 'bear';
    setChip('strategyTag', `反转 ${reversal.points} 共振`, isLong ? 'bull' : 'bear');
    metaEl.textContent = `${meta.base} · ${state.interval.toUpperCase()} · 高胜率反转策略 · ${grade}`;
    const risk = Math.max(atr * 1.5, atr * 1.5);
    const stop = isLong ? last.close - risk : last.close + risk;
    const target = isLong ? last.close + risk * 2.5 : last.close - risk * 2.5;
    entryEl.textContent = isLong ? `超卖反弹 ${formatPrice(last.close)} 附近` : `超买回落 ${formatPrice(last.close)} 附近`;
    entryEl.className = dirEl.className;
    stopEl.textContent = formatPrice(stop);
    stopEl.className = dirEl.className;
    targetEl.textContent = formatPrice(target);
    targetEl.className = dirEl.className;
    sizeEl.textContent = reversal.points >= 5 ? '风险 1.5%-2%' : '风险 1%-1.5%';
    sizeEl.className = dirEl.className;
    planEl.textContent = `${meta.base} 出现高胜率反转信号（${reversal.points} 项共振：${reversal.reasons.join('、')}）。建议${isLong ? '超卖后分批做多' : '超买后分批做空'}，止损按 1.5 倍 ATR，目标按 2.5 倍盈亏比设置。信号需严格止损，反转失败立即离场。`;
    return;
  }

  if (analysis.signalClass === 'flat') {
    planEl.textContent = `${meta.base} 当前信号不明确，建议空仓等待。等 MACD/KDJ 金叉或死叉共振、RSI 明确穿越 50 后再入场，避免无信号反复交易。`;
    entryEl.textContent = '等待确认';
    stopEl.textContent = '--';
    targetEl.textContent = '--';
    sizeEl.textContent = '观望';
    return;
  }

  const isBull = analysis.signalClass === 'bull';
  const entryRef = Number.isFinite(ema20) ? ema20 : last.close;
  const risk = Math.max(
    atr * 1.5,
    (isBull ? Math.max(0, last.close - recentLow) : Math.max(0, recentHigh - last.close)) * 0.6
  );
  const stop = isBull ? last.close - risk : last.close + risk;
  const target = isBull ? last.close + risk * 2 : last.close - risk * 2;

  entryEl.textContent = isBull
    ? `回踩 ${formatPrice(entryRef)} 附近分批`
    : `反弹 ${formatPrice(entryRef)} 附近分批`;
  entryEl.className = analysis.signalClass;
  stopEl.textContent = formatPrice(stop);
  stopEl.className = analysis.signalClass;
  targetEl.textContent = formatPrice(target);
  targetEl.className = analysis.signalClass;

  const absScore = Math.abs(analysis.score);
  let sizeText = absScore >= 4 ? '风险 1.5%-2%' : absScore >= 3 ? '风险 1%-1.5%' : '风险 ≤1%';
  if (smOpposes || (isBull && !bullAlign) || (!isBull && !bearAlign)) sizeText = '轻仓 0.5%-1%';
  sizeEl.textContent = sizeText;
  sizeEl.className = analysis.signalClass;

  const stopText = isBull ? '跌破止损位立即离场' : '突破止损位立即离场';
  let plan;
  if (isBull) {
    if (bullAlign) {
      plan = `${meta.base} 当前为${analysis.label}信号（${analysis.score}）。均线多头排列（EMA20>50>100>200），建议顺势做多：价格回踩 EMA20 附近分批建仓，${stopText}，目标按 2 倍风险收益比设置。`;
    } else if (bearAlign) {
      plan = `${meta.base} 当前指标偏多，但均线仍为空头排列，属于逆势信号，建议放弃或轻仓，等 EMA20 上穿 EMA50 后再做多。`;
    } else {
      plan = `${meta.base} 当前为${analysis.label}信号（${analysis.score}），但均线方向尚未统一，建议轻仓试探，等 EMA20 与 EMA50 方向一致后加仓。`;
    }
  } else {
    if (bearAlign) {
      plan = `${meta.base} 当前为${analysis.label}信号（${analysis.score}）。均线空头排列（EMA20<50<100<200），建议顺势做空：价格反弹至 EMA20 附近分批做空，${stopText}，目标按 2 倍风险收益比设置。`;
    } else if (bullAlign) {
      plan = `${meta.base} 当前指标偏空，但均线仍为多头排列，属于逆势信号，建议放弃或轻仓，等 EMA20 下穿 EMA50 后再做空。`;
    } else {
      plan = `${meta.base} 当前为${analysis.label}信号（${analysis.score}），但均线方向尚未统一，建议轻仓试探，等 EMA20 与 EMA50 方向一致后加仓。`;
    }
  }
  if (volumeSpike != null && volumeSpike >= 1.5) {
    plan += ' 当前成交量放大，信号可信度提升。';
  }
  if (smAgrees) {
    plan += ' 聪明钱方向同步，可正常执行。';
  } else if (smOpposes) {
    plan += ' 但聪明钱方向相反，建议降低仓位并严格执行止损。';
  }
  planEl.textContent = plan;
}

function renderWatchlist() {
  const list = $('watchlist');
  list.innerHTML = '';
  for (const symbol of state.watchlist) {
    const meta = getMeta(symbol);
    const snap = state.scanResults[symbol];
    const item = document.createElement('button');
    item.className = `watch-item${symbol === state.symbol ? ' active' : ''}`;
    item.dataset.symbol = symbol;
    item.innerHTML = `
      <span class="watch-icon">${escapeHtml(meta.icon)}</span>
      <span class="watch-name"><span class="base">${escapeHtml(meta.base)}</span><span class="quote">${escapeHtml(meta.quote)}</span></span>
      <span class="signal-chip ${snap ? snap.analysis.signalClass : 'flat'}">${snap ? escapeHtml(snap.analysis.label) : '加载中'}</span>
      <span class="watch-price">${snap ? formatPrice(snap.last) : '--'}</span>
      <span class="watch-change ${snap ? (snap.change > 0.05 ? 'up' : snap.change < -0.05 ? 'down' : 'flat') : 'flat'}">${snap ? formatPct(snap.change) : '--'}</span>
      <canvas class="watch-spark"></canvas>
    `;
    list.appendChild(item);
    if (snap) {
      requestAnimationFrame(() => drawSparkline(item.querySelector('.watch-spark'), snap.closes));
    }
  }
}

function renderOverview() {
  const results = Object.values(state.scanResults).filter((r) => r.analysis);
  const buy = results.filter((r) => r.analysis.signalClass === 'bull').length;
  const sell = results.filter((r) => r.analysis.signalClass === 'bear').length;
  const flat = results.filter((r) => r.analysis.signalClass === 'flat').length;
  $('marketCount').textContent = String(state.watchlist.length);
  $('overviewStats').innerHTML = `
    <div class="overview-stat"><span>看多</span><strong class="bull">${buy}</strong></div>
    <div class="overview-stat"><span>看空</span><strong class="bear">${sell}</strong></div>
    <div class="overview-stat"><span>观望</span><strong class="flat">${flat}</strong></div>
  `;
}

function renderSignalLog() {
  const el = $('signalLog');
  if (!state.log.length) {
    el.innerHTML = '<div class="empty-log">暂无信号</div>';
    return;
  }
  el.innerHTML = state.log
    .map((item) => {
      const date = new Date(item.time);
      const time = `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}:${String(date.getSeconds()).padStart(2, '0')}`;
      return `
        <div class="log-item">
          <span class="log-time">${time}</span>
          <div class="log-main">
            <div class="log-symbol">${escapeHtml(item.symbol)} · ${escapeHtml(item.interval || '')}</div>
            <div class="log-reason">${escapeHtml(item.reason || '')}</div>
          </div>
          <span class="signal-chip ${item.signalClass || 'flat'}">${escapeHtml(item.label)}</span>
        </div>
      `;
    })
    .join('');
}

function addLog(symbol, analysis, interval) {
  state.log.unshift({
    time: Date.now(),
    symbol,
    interval,
    label: analysis.label,
    reason: analysis.reason,
    signalClass: analysis.signalClass
  });
  if (state.log.length > 40) {
    state.log.length = 40;
  }
  writeJSON('coinpulse.log', state.log);
  renderSignalLog();
  alertSignal(symbol, analysis, interval);
}

function ensureAudio() {
  if (!state.audioCtx) {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return null;
    state.audioCtx = new Ctx();
  }
  if (state.audioCtx.state === 'suspended') {
    state.audioCtx.resume();
  }
  return state.audioCtx;
}

function playAlertSound() {
  const ctx = ensureAudio();
  if (!ctx) return;
  const now = ctx.currentTime;
  [[880, 0], [660, 0.18]].forEach(([freq, offset]) => {
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'sine';
    osc.frequency.value = freq;
    gain.gain.setValueAtTime(0.0001, now + offset);
    gain.gain.exponentialRampToValueAtTime(0.18, now + offset + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, now + offset + 0.22);
    osc.connect(gain).connect(ctx.destination);
    osc.start(now + offset);
    osc.stop(now + offset + 0.24);
  });
}

function browserNotify(title, body) {
  if (!state.notify || !('Notification' in window) || Notification.permission !== 'granted') return;
  try {
    new Notification(title, { body, tag: 'coinpulse-signal' });
  } catch {
    // some mobile browsers reject notifications without a service worker
  }
}

function alertSignal(symbol, analysis, interval) {
  if (!state.notify) return;
  const direction = analysis.signalClass === 'bull' ? '看多' : '看空';
  const body = `${interval} · ${analysis.label}（${analysis.score}）· ${analysis.reason}`;
  playAlertSound();
  if (navigator.vibrate) {
    try {
      navigator.vibrate(analysis.signalClass === 'bull' ? [160, 80, 160] : [160, 160, 160]);
    } catch {
      // vibration is optional
    }
  }
  browserNotify(`CoinPulse ${direction}信号 ${symbol}`, body);
  showToast(`${symbol} ${analysis.label}：${analysis.reason}`);
}

function setNotifyButton() {
  const btn = $('notifyBtn');
  btn.classList.toggle('on', state.notify);
  btn.title = state.notify ? '关闭信号提醒' : '开启信号提醒';
  const icon = btn.querySelector('i');
  if (icon) {
    icon.dataset.lucide = state.notify ? 'bell-ring' : 'bell';
  }
  if (window.lucide && lucide.createIcons) {
    try {
      lucide.createIcons();
    } catch {
      // icons are decorative; ignore failures
    }
  }
}

async function fetchSmartMoney(symbol) {
  let trades = [];
  let error = '';
  let threshold = 5000;
  try {
    const rows = await fetchJSON(
      `https://data-api.binance.vision/api/v3/aggTrades?symbol=${encodeURIComponent(symbol)}&limit=1000`
    );
    if (!Array.isArray(rows)) {
      throw new Error('响应格式错误');
    }
    const parsed = rows
      .map((trade) => ({
        time: +trade.T,
        price: +trade.p,
        qty: +trade.q,
        value: +trade.p * +trade.q,
        isBuyerMaker: trade.m === true || trade.m === 1
      }))
      .filter((t) => Number.isFinite(t.value));
    const values = parsed.map((t) => t.value).sort((a, b) => a - b);
    const p90 = values.length ? values[Math.floor(values.length * 0.9)] : 0;
    threshold = Math.max(1000, p90);
    trades = parsed.filter((t) => t.value >= threshold);
  } catch (err) {
    error = err.message;
  }

  const buyCount = trades.filter((t) => !t.isBuyerMaker).length;
  const sellCount = trades.length - buyCount;
  const buyValue = trades.filter((t) => !t.isBuyerMaker).reduce((sum, t) => sum + t.value, 0);
  const sellValue = trades.filter((t) => t.isBuyerMaker).reduce((sum, t) => sum + t.value, 0);
  const totalValue = buyValue + sellValue;
  const netValue = buyValue - sellValue;
  const netRatio = totalValue > 0 ? (netValue / totalValue) * 100 : 0;
  const largest = trades.length ? trades.reduce((a, b) => (b.value > a.value ? b : a)) : null;

  return {
    threshold,
    error: error || null,
    trades: [...trades].reverse().slice(0, 12),
    totalValue,
    buyValue,
    sellValue,
    buyCount,
    sellCount,
    netValue,
    netRatio,
    largest,
    updatedAt: Date.now()
  };
}

function getVolumeSpike() {
  if (!state.data || state.data.length < 22) return null;
  const volumes = state.data.map((k) => k.volume);
  const last = volumes[volumes.length - 1];
  const previous = volumes.slice(-21, -1);
  const avg = previous.reduce((sum, v) => sum + v, 0) / previous.length || 1;
  return last / avg;
}

function renderSmartMoney() {
  const sm = state.smartMoney;
  const meta = getMeta(state.symbol);
  if (!sm || !sm.trades) {
    setChip('smartChip', '--', 'flat');
    $('smartSubtitle').textContent = '大额成交 · 资金流向';
    $('smartNetFlow').textContent = '--';
    $('smartNetFlow').className = '';
    $('smartRatio').textContent = '--';
    $('smartRatio').className = 'smart-ratio flat';
    $('smartGaugeFill').style.width = '50%';
    $('smartStats').innerHTML = '';
    $('smartWindow').textContent = '--';
    $('largeTrades').innerHTML = '<div class="empty-smart">等待大额成交数据</div>';
    return;
  }

  const hasTrades = sm.totalValue > 0;
  const flowClass = hasTrades ? (sm.netRatio >= 20 ? 'bull' : sm.netRatio <= -20 ? 'bear' : 'flat') : 'flat';
  const flowLabel = hasTrades
    ? sm.netRatio >= 20
      ? '大额净流入'
      : sm.netRatio <= -20
        ? '大额净流出'
        : '流向均衡'
    : '暂无大单数据';

  setChip('smartChip', flowLabel, flowClass);
  $('smartSubtitle').textContent = `大单阈值 ≥ ${formatUsd(sm.threshold)} · ${state.interval.toUpperCase()}`;
  $('smartNetFlow').textContent = hasTrades ? `${sm.netValue >= 0 ? '+' : '-'}${formatUsd(Math.abs(sm.netValue))}` : '--';
  $('smartNetFlow').className = hasTrades ? (sm.netValue >= 0 ? 'up' : 'down') : '';
  $('smartRatio').textContent = hasTrades ? `${sm.netRatio >= 0 ? '+' : ''}${sm.netRatio.toFixed(0)}%` : '--';
  $('smartRatio').className = `smart-ratio ${flowClass}`;
  $('smartGaugeFill').style.width = `${hasTrades ? Math.max(0, Math.min(100, 50 + sm.netRatio / 2)) : 50}%`;

  const spike = getVolumeSpike();
  const spikeClass = spike != null && spike >= 1.5 ? 'up' : '';
  const spikeText = spike != null ? `${spike.toFixed(2)}x` : '--';
  const largestText = sm.largest ? formatUsd(sm.largest.value) : '--';
  $('smartStats').innerHTML = [
    ['大单买入', `${sm.buyCount}笔 · ${formatUsd(sm.buyValue)}`, 'up'],
    ['大单卖出', `${sm.sellCount}笔 · ${formatUsd(sm.sellValue)}`, 'down'],
    ['大单总金额', formatUsd(sm.totalValue), ''],
    ['最大单笔', largestText, ''],
    ['近20根量比', spikeText, spikeClass],
    ['监控周期', `${sm.trades.length}笔 · ${new Date(sm.updatedAt).toLocaleTimeString('zh-CN', { hour12: false })}`, '']
  ]
    .map(([label, value, cls]) => `<div class="smart-stat"><span>${label}</span><strong class="${cls}">${value}</strong></div>`)
    .join('');

  $('smartWindow').textContent = hasTrades ? `${sm.trades.length}笔大额成交` : '近1000笔成交中无大单';
  if (!hasTrades) {
    $('largeTrades').innerHTML = sm.error
      ? '<div class="empty-smart">大额成交数据暂不可用</div>'
      : '<div class="empty-smart">近1000笔成交未达到大单阈值</div>';
    return;
  }

  $('largeTrades').innerHTML = sm.trades
    .slice(0, 10)
    .map((trade) => {
      const buy = !trade.isBuyerMaker;
      const time = new Date(trade.time).toLocaleTimeString('zh-CN', { hour12: false });
      return `
        <div class="trade-row">
          <span class="trade-time">${time}</span>
          <span class="trade-side ${buy ? 'buy' : 'sell'}">${buy ? '买' : '卖'}</span>
          <span class="trade-price">${formatPrice(trade.price)}</span>
          <span class="trade-qty">${formatQuantity(trade.qty)} ${escapeHtml(meta.base)}</span>
          <span class="trade-value ${buy ? 'buy' : 'sell'}">${formatUsd(trade.value)}</span>
        </div>
      `;
    })
    .join('');
}

async function loadSmartMoney() {
  if (state.smartLoading) return;
  state.smartLoading = true;
  try {
    state.smartMoney = await fetchSmartMoney(state.symbol);
  } catch (err) {
    state.smartMoney = {
      threshold: 1000,
      error: err.message,
      trades: [],
      totalValue: 0,
      buyValue: 0,
      sellValue: 0,
      buyCount: 0,
      sellCount: 0,
      netValue: 0,
      netRatio: 0,
      largest: null,
      updatedAt: Date.now()
    };
  } finally {
    state.smartLoading = false;
    renderSmartMoney();
    renderStrategy();
  }
}

async function fetchMicroSignal(symbol) {
  const [klineResult, tradeResult] = await Promise.allSettled([
    fetchKlinesForBacktest(symbol, '15m', 60),
    fetchJSON(`https://data-api.binance.vision/api/v3/aggTrades?symbol=${encodeURIComponent(symbol)}&limit=1000`)
  ]);
  const klines = klineResult.status === 'fulfilled' ? klineResult.value.klines : null;
  const rows = tradeResult.status === 'fulfilled' ? tradeResult.value : null;
  if (!Array.isArray(rows) || !rows.length) {
    throw new Error('微观成交数据不可用');
  }

  const trades = rows
    .map((trade) => ({
      time: +trade.T,
      price: +trade.p,
      qty: +trade.q,
      value: +trade.p * +trade.q,
      isBuyerMaker: trade.m === true || trade.m === 1
    }))
    .filter((t) => Number.isFinite(t.value));
  const totalValue = trades.reduce((sum, t) => sum + t.value, 0);
  const buyValue = trades.filter((t) => !t.isBuyerMaker).reduce((sum, t) => sum + t.value, 0);
  const sellValue = trades.filter((t) => t.isBuyerMaker).reduce((sum, t) => sum + t.value, 0);
  const buyRatio = totalValue > 0 ? (buyValue / totalValue) * 100 : 50;

  const values = trades.map((t) => t.value).sort((a, b) => a - b);
  const p90 = values.length ? values[Math.floor(values.length * 0.9)] : 0;
  const threshold = Math.max(1000, p90);
  const largeTrades = trades.filter((t) => t.value >= threshold);
  const largeBuy = largeTrades.filter((t) => !t.isBuyerMaker).reduce((sum, t) => sum + t.value, 0);
  const largeSell = largeTrades.filter((t) => t.isBuyerMaker).reduce((sum, t) => sum + t.value, 0);
  const largeTotal = largeBuy + largeSell;
  const largeNetValue = largeBuy - largeSell;
  const largeNetRatio = largeTotal > 0 ? (largeNetValue / largeTotal) * 100 : 0;

  const notional = trades.reduce((sum, t) => sum + t.price * t.qty, 0);
  const quantity = trades.reduce((sum, t) => sum + t.qty, 0);
  const lastPrice = trades[trades.length - 1].price;
  const vwap = quantity > 0 ? notional / quantity : lastPrice;
  const vwapPosition = vwap > 0 ? ((lastPrice / vwap) - 1) * 100 : 0;

  let volumeSpike = null;
  if (klines && klines.length >= 22) {
    const volumes = klines.map((k) => k.volume);
    const lastVolume = volumes[volumes.length - 1];
    const previous = volumes.slice(-21, -1);
    const avg = previous.reduce((sum, v) => sum + v, 0) / previous.length || 1;
    volumeSpike = lastVolume / avg;
  }

  const older = trades[Math.max(0, trades.length - 31)].price;
  const momentum = older > 0 ? ((lastPrice / older) - 1) * 100 : 0;

  return {
    buyRatio,
    sellRatio: 100 - buyRatio,
    buyValue,
    sellValue,
    largeNetRatio,
    largeNetValue,
    vwapPosition,
    volumeSpike,
    momentum,
    tradesCount: trades.length,
    threshold,
    updatedAt: Date.now()
  };
}

function renderMicroSignal() {
  const micro = state.micro;
  const meta = getMeta(state.symbol);
  if (!micro) {
    setChip('microChip', '--', 'flat');
    $('microSubtitle').textContent = '主动买卖 · 大单流向 · VWAP';
    $('microScore').textContent = '--';
    $('microScore').className = '';
    $('microGaugeFill').style.width = '50%';
    $('microStats').innerHTML = '';
    $('microReasons').innerHTML = '<div class="empty-smart">等待成交流数据</div>';
    return;
  }
  if (micro.error) {
    setChip('microChip', '不可用', 'flat');
    $('microSubtitle').textContent = `${meta.base} · 微观数据暂不可用`;
    $('microScore').textContent = '--';
    $('microGaugeFill').style.width = '50%';
    $('microStats').innerHTML = '';
    $('microReasons').innerHTML = '<div class="empty-smart">成交流数据暂不可用</div>';
    return;
  }

  let score = 0;
  const reasons = [];
  if (micro.buyRatio >= 55) {
    score += 1.5;
    reasons.push({ text: '主动买盘占优', cls: 'bull' });
  } else if (micro.buyRatio >= 52) {
    score += 0.75;
    reasons.push({ text: '买盘略占优', cls: 'bull' });
  } else if (micro.buyRatio <= 45) {
    score -= 1.5;
    reasons.push({ text: '主动卖盘占优', cls: 'bear' });
  } else if (micro.buyRatio <= 48) {
    score -= 0.75;
    reasons.push({ text: '卖盘略占优', cls: 'bear' });
  }
  if (micro.largeNetRatio >= 20) {
    score += 1.5;
    reasons.push({ text: '大单净流入', cls: 'bull' });
  } else if (micro.largeNetRatio <= -20) {
    score -= 1.5;
    reasons.push({ text: '大单净流出', cls: 'bear' });
  } else if (micro.largeNetRatio >= 10) {
    score += 0.75;
  } else if (micro.largeNetRatio <= -10) {
    score -= 0.75;
  }
  if (micro.vwapPosition >= 0.1) {
    score += 1;
    reasons.push({ text: '价格高于VWAP', cls: 'bull' });
  } else if (micro.vwapPosition <= -0.1) {
    score -= 1;
    reasons.push({ text: '价格低于VWAP', cls: 'bear' });
  }
  if (micro.volumeSpike != null) {
    if (micro.volumeSpike >= 1.5) {
      score += 0.75;
      reasons.push({ text: '15m 放量', cls: 'bull' });
    } else if (micro.volumeSpike <= 0.6) {
      score -= 0.5;
      reasons.push({ text: '15m 缩量', cls: 'flat' });
    }
  }
  if (micro.momentum >= 0.15) {
    score += 0.75;
    reasons.push({ text: '短线动量向上', cls: 'bull' });
  } else if (micro.momentum <= -0.15) {
    score -= 0.75;
    reasons.push({ text: '短线动量向下', cls: 'bear' });
  }

  const rounded = Math.round(score * 10) / 10;
  const label = rounded >= 3 ? '强烈做多' : rounded >= 1.5 ? '偏多' : rounded <= -3 ? '强烈做空' : rounded <= -1.5 ? '偏空' : '中性';
  const signalClass = rounded >= 1.5 ? 'bull' : rounded <= -1.5 ? 'bear' : 'flat';
  setChip('microChip', label, signalClass);
  const time = new Date(micro.updatedAt).toLocaleTimeString('zh-CN', { hour12: false });
  $('microSubtitle').textContent = `${meta.base} · 15m · ${time} · 近1000笔成交`;
  $('microScore').textContent = `${rounded > 0 ? '+' : ''}${rounded}`;
  $('microScore').className = signalClass === 'bull' ? 'up' : signalClass === 'bear' ? 'down' : '';
  $('microGaugeFill').style.width = `${Math.max(0, Math.min(100, 50 + rounded * 8))}%`;
  $('microStats').innerHTML = [
    ['主动买入', `${micro.buyRatio.toFixed(1)}%`, micro.buyRatio >= 52 ? 'up' : 'down'],
    ['主动卖出', `${micro.sellRatio.toFixed(1)}%`, micro.sellRatio >= 52 ? 'down' : 'up'],
    ['大单净流向', formatUsd(micro.largeNetValue), micro.largeNetValue >= 0 ? 'up' : 'down'],
    ['VWAP位置', `${micro.vwapPosition >= 0 ? '+' : ''}${micro.vwapPosition.toFixed(2)}%`, micro.vwapPosition >= 0 ? 'up' : 'down'],
    ['15m量比', micro.volumeSpike != null ? `${micro.volumeSpike.toFixed(2)}x` : '--', micro.volumeSpike != null && micro.volumeSpike >= 1.5 ? 'up' : ''],
    ['成交样本', `${micro.tradesCount}笔`, '']
  ]
    .map(([label, value, cls]) => `<div class="micro-stat"><span>${label}</span><strong class="${cls}">${value}</strong></div>`)
    .join('');
  $('microReasons').innerHTML = reasons.length
    ? reasons.slice(0, 4).map((r) => `<div class="micro-reason ${r.cls}">${r.text}</div>`).join('')
    : '<div class="micro-reason flat">多空力量接近，等待明确方向</div>';
}

async function loadMicroSignal() {
  if (state.microLoading) return;
  state.microLoading = true;
  try {
    state.micro = await fetchMicroSignal(state.symbol);
  } catch (err) {
    state.micro = { error: err.message };
  } finally {
    state.microLoading = false;
    renderMicroSignal();
  }
}

function trendLong(indicators, index) {
  const close = indicators.closes[index];
  const ema20 = indicators.ema20[index];
  const ema50 = indicators.ema50[index];
  return close != null && ema20 != null && ema50 != null && close > ema50 && ema20 > ema50;
}

function trendShort(indicators, index) {
  const close = indicators.closes[index];
  const ema20 = indicators.ema20[index];
  const ema50 = indicators.ema50[index];
  return close != null && ema20 != null && ema50 != null && close < ema50 && ema20 < ema50;
}

function volumeConfirm(klines, index, minRatio = 1.1) {
  const from = Math.max(0, index - 20);
  const volumes = [];
  for (let i = from; i < index; i += 1) {
    volumes.push(klines[i].volume);
  }
  if (volumes.length < 5) return true;
  const avg = volumes.reduce((sum, v) => sum + v, 0) / volumes.length;
  return avg > 0 && klines[index].volume >= avg * minRatio;
}

function backtestDual(klines, indicators, threshold = 4, feeRate = 0.001, capital = 10000) {
  const start = 34;
  let longEquity = capital / 2;
  let shortEquity = capital / 2;
  let longPos = null;
  let shortPos = null;
  const curve = [];
  const trades = [];
  let peak = capital;
  let maxDrawdown = 0;
  const firstTime = klines[start].time;

  for (let i = start; i < klines.length; i += 1) {
    const signal = analyzeAt(indicators, i);
    const price = klines[i].close;

    if (!longPos && signal.score >= threshold && trendLong(indicators, i)) {
      longEquity *= 1 - feeRate;
      longPos = {
        entryPrice: price,
        entryTime: klines[i].time,
        entryIndex: i,
        entryEquity: longEquity
      };
    }
    if (!shortPos && signal.score <= -threshold && trendShort(indicators, i)) {
      shortEquity *= 1 - feeRate;
      shortPos = {
        entryPrice: price,
        entryTime: klines[i].time,
        entryIndex: i,
        entryEquity: shortEquity
      };
    }

    if (longPos && (signal.score <= -threshold || !trendLong(indicators, i))) {
      const ratio = price / longPos.entryPrice;
      const netReturn = (1 - feeRate) * (1 - feeRate) * ratio - 1;
      longEquity = longPos.entryEquity * ratio * (1 - feeRate);
      trades.push({
        side: 'long',
        entryTime: longPos.entryTime,
        exitTime: klines[i].time,
        entryPrice: longPos.entryPrice,
        exitPrice: price,
        netReturn,
        bars: i - longPos.entryIndex,
        exitReason: signal.score <= -threshold ? 'signal' : 'trend',
        closed: true
      });
      longPos = null;
    }

    if (shortPos && (signal.score >= threshold || !trendShort(indicators, i))) {
      const ratio = price / shortPos.entryPrice;
      const netReturn = (1 - feeRate) * (1 - feeRate) * Math.max(0, 2 - ratio) - 1;
      shortEquity = Math.max(0, shortPos.entryEquity * (2 - ratio)) * (1 - feeRate);
      trades.push({
        side: 'short',
        entryTime: shortPos.entryTime,
        exitTime: klines[i].time,
        entryPrice: shortPos.entryPrice,
        exitPrice: price,
        netReturn,
        bars: i - shortPos.entryIndex,
        exitReason: signal.score >= threshold ? 'signal' : 'trend',
        closed: true
      });
      shortPos = null;
    }

    const currentLong = longPos ? longPos.entryEquity * (price / longPos.entryPrice) : longEquity;
    const currentShort = shortPos ? Math.max(0, shortPos.entryEquity * (2 - price / shortPos.entryPrice)) : shortEquity;
    const currentEquity = currentLong + currentShort;
    curve.push({ time: klines[i].time, equity: currentEquity });
    if (currentEquity > peak) peak = currentEquity;
    const drawdown = peak > 0 ? (currentEquity - peak) / peak : 0;
    if (drawdown < maxDrawdown) maxDrawdown = drawdown;
  }

  if (longPos) {
    const lastIndex = klines.length - 1;
    const lastPrice = klines[lastIndex].close;
    const ratio = lastPrice / longPos.entryPrice;
    const netReturn = (1 - feeRate) * (1 - feeRate) * ratio - 1;
    longEquity = longPos.entryEquity * ratio * (1 - feeRate);
    trades.push({
      side: 'long',
      entryTime: longPos.entryTime,
      exitTime: klines[lastIndex].time,
      entryPrice: longPos.entryPrice,
      exitPrice: lastPrice,
      netReturn,
      bars: lastIndex - longPos.entryIndex,
      exitReason: 'open',
      closed: false
    });
  }
  if (shortPos) {
    const lastIndex = klines.length - 1;
    const lastPrice = klines[lastIndex].close;
    const ratio = lastPrice / shortPos.entryPrice;
    const netReturn = (1 - feeRate) * (1 - feeRate) * Math.max(0, 2 - ratio) - 1;
    shortEquity = Math.max(0, shortPos.entryEquity * (2 - ratio)) * (1 - feeRate);
    trades.push({
      side: 'short',
      entryTime: shortPos.entryTime,
      exitTime: klines[lastIndex].time,
      entryPrice: shortPos.entryPrice,
      exitPrice: lastPrice,
      netReturn,
      bars: lastIndex - shortPos.entryIndex,
      exitReason: 'open',
      closed: false
    });
  }

  const firstClose = klines[start].close;
  const lastClose = klines[klines.length - 1].close;
  const buyHoldCurve = klines.slice(start).map((k) => ({
    time: k.time,
    equity: capital * (1 - feeRate) * (k.close / firstClose)
  }));
  const totalReturn = ((longEquity + shortEquity) / capital - 1) * 100;
  const longReturn = (longEquity / (capital / 2) - 1) * 100;
  const shortReturn = (shortEquity / (capital / 2) - 1) * 100;
  const buyHoldReturn = ((1 - feeRate) * (lastClose / firstClose) - 1) * 100;
  const closedTrades = trades.filter((t) => t.closed);
  const wins = closedTrades.filter((t) => t.netReturn > 0);
  const losses = closedTrades.filter((t) => t.netReturn <= 0);
  const grossProfit = wins.reduce((sum, t) => sum + t.netReturn, 0);
  const grossLoss = Math.abs(losses.reduce((sum, t) => sum + t.netReturn, 0));
  const winRate = closedTrades.length ? (wins.length / closedTrades.length) * 100 : 0;
  const profitFactor = grossLoss > 0 ? grossProfit / grossLoss : grossProfit > 0 ? 99 : 0;
  const avgNet = closedTrades.length ? (closedTrades.reduce((sum, t) => sum + t.netReturn, 0) / closedTrades.length) * 100 : 0;
  const lastTime = klines[klines.length - 1].time;
  const days = Math.max(1, (lastTime - firstTime) / 86400000);
  const annualized = (Math.pow((longEquity + shortEquity) / capital, 365 / days) - 1) * 100;

  return {
    curve,
    buyHoldCurve,
    trades,
    totalReturn,
    longReturn,
    shortReturn,
    buyHoldReturn,
    maxDrawdown: maxDrawdown * 100,
    winRate,
    closedTrades: closedTrades.length,
    openTrades: trades.length - closedTrades.length,
    profitFactor,
    avgNet,
    annualized,
    mode: 'dual',
    firstTime,
    lastTime
  };
}

function detectHighWinReversal(klines, indicators, i) {
  // 高胜率反转信号：极值 + 反转确认 + 多条件共振（与 Python reversal_scanner 一致）
  const closes = indicators.closes;
  const prev = Math.max(0, i - 1);
  const kline = klines[i];
  const prevK = klines[prev];
  const rsi = indicators.rsi[i];
  const rsiPrev = indicators.rsi[prev];
  const j = indicators.kdj.J[i];
  const jPrev = indicators.kdj.J[prev];
  const k = indicators.kdj.K[i];
  const d = indicators.kdj.D[i];
  const kPrev = indicators.kdj.K[prev];
  const dPrev = indicators.kdj.D[prev];
  const stK = indicators.stochRsi.K[i];
  const stD = indicators.stochRsi.D[i];
  const stKPrev = indicators.stochRsi.K[prev];
  const stDPrev = indicators.stochRsi.D[prev];
  const ema20 = indicators.ema20[i];
  const price = closes[i];
  const bias = ema20 ? ((price - ema20) / ema20) * 100 : 0;

  let longPoints = 0;
  let shortPoints = 0;
  const longReasons = [];
  const shortReasons = [];

  // --- 极值区 ---
  if (rsi != null && rsi < 30) { longPoints += 1; longReasons.push(`RSI超卖(${rsi.toFixed(0)})`); }
  if (rsi != null && rsi < 20) { longPoints += 1; longReasons.push(`RSI深度超卖(${rsi.toFixed(0)})`); }
  if (j != null && j < 20) { longPoints += 1; longReasons.push(`KDJ-J超卖(${j.toFixed(0)})`); }
  if (j != null && j < 5) { longPoints += 1; longReasons.push(`KDJ-J深度超卖(${j.toFixed(0)})`); }
  if (stK != null && stK < 20) { longPoints += 1; longReasons.push(`StochRSI超卖(${stK.toFixed(0)})`); }
  if (bias < -5) { longPoints += 1; longReasons.push(`乖离率${bias.toFixed(1)}%（偏离过大）`); }

  if (rsi != null && rsi > 70) { shortPoints += 1; shortReasons.push(`RSI超买(${rsi.toFixed(0)})`); }
  if (rsi != null && rsi > 80) { shortPoints += 1; shortReasons.push(`RSI深度超买(${rsi.toFixed(0)})`); }
  if (j != null && j > 80) { shortPoints += 1; shortReasons.push(`KDJ-J超买(${j.toFixed(0)})`); }
  if (j != null && j > 95) { shortPoints += 1; shortReasons.push(`KDJ-J深度超买(${j.toFixed(0)})`); }
  if (stK != null && stK > 80) { shortPoints += 1; shortReasons.push(`StochRSI超买(${stK.toFixed(0)})`); }
  if (bias > 5) { shortPoints += 1; shortReasons.push(`乖离率${bias.toFixed(1)}%（偏离过大）`); }

  // --- 反转确认 ---
  if (kPrev != null && dPrev != null && k != null && d != null) {
    if (kPrev <= dPrev && k > d) {
      longPoints += 1; longReasons.push('KDJ金叉');
      if (jPrev != null && jPrev < 20) { longPoints += 1; longReasons.push('超卖区金叉'); }
    }
    if (kPrev >= dPrev && k < d) {
      shortPoints += 1; shortReasons.push('KDJ死叉');
      if (jPrev != null && jPrev > 80) { shortPoints += 1; shortReasons.push('超买区死叉'); }
    }
  }
  if (stKPrev != null && stDPrev != null && stK != null && stD != null) {
    if (stKPrev <= stDPrev && stK > stD) { longPoints += 1; longReasons.push('StochRSI金叉'); }
    if (stKPrev >= stDPrev && stK < stD) { shortPoints += 1; shortReasons.push('StochRSI死叉'); }
  }
  if (rsiPrev != null && rsi != null) {
    if (rsiPrev < 30 && rsi >= 30) { longPoints += 1; longReasons.push('RSI上穿30'); }
    if (rsiPrev > 70 && rsi <= 70) { shortPoints += 1; shortReasons.push('RSI下穿70'); }
  }

  // 吞没 K 线
  const body = Math.abs(kline.close - kline.open);
  const prevBody = Math.abs(prevK.close - prevK.open);
  if (body > prevBody * 1.2 && body > 0) {
    if (kline.close > kline.open && prevK.close < prevK.open) { longPoints += 1; longReasons.push('阳线吞没'); }
    if (kline.close < kline.open && prevK.close > prevK.open) { shortPoints += 1; shortReasons.push('阴线吞没'); }
  }
  // 长下影
  const lowRange = kline.high - kline.low;
  if (lowRange > 0) {
    const lowerWick = Math.min(kline.open, kline.close) - kline.low;
    if (lowerWick / lowRange > 0.5 && lowerWick > body * 0.8) { longPoints += 1; longReasons.push('长下影线'); }
  }
  // 放量
  const vols = klines.slice(Math.max(0, i - 5), i).map((x) => x.volume);
  const avgVol = vols.length ? vols.reduce((a, b) => a + b, 0) / vols.length : 0;
  if (avgVol > 0 && kline.volume > avgVol * 1.5) {
    if (longPoints > 0) { longPoints += 1; longReasons.push('放量'); }
    if (shortPoints > 0) { shortPoints += 1; shortReasons.push('放量'); }
  }

  if (longPoints >= 3 && longPoints >= shortPoints) {
    return { direction: 'long', points: longPoints, reasons: longReasons };
  }
  if (shortPoints >= 3 && shortPoints > longPoints) {
    return { direction: 'short', points: shortPoints, reasons: shortReasons };
  }
  return { direction: null, points: Math.max(longPoints, shortPoints), reasons: longPoints >= shortPoints ? longReasons : shortReasons };
}

function backtestRevertDual(klines, indicators, threshold = 4, feeRate = 0.001, capital = 10000) {
  // 高胜率反转主策略回测（极值+共振+形态，多条件共振才入场）
  const start = 34;
  let longEquity = capital / 2;
  let shortEquity = capital / 2;
  let longPos = null;
  let shortPos = null;
  const curve = [];
  const trades = [];
  let peak = capital;
  let maxDrawdown = 0;
  const firstTime = klines[start].time;
  const atrSeries = calcATRSeries(klines, 14);

  const canLong = (i) => {
    const r = detectHighWinReversal(klines, indicators, i);
    if (r.direction !== 'long' || r.points < 3) return false;
    // 强趋势过滤：ADX 空头趋势且强劲时不抄底（避免接飞刀）
    const adxCur = indicators.adx ? indicators.adx.adx[i] : null;
    const plusDi = indicators.adx ? indicators.adx.plusDi[i] : null;
    const minusDi = indicators.adx ? indicators.adx.minusDi[i] : null;
    if (adxCur != null && plusDi != null && minusDi != null && adxCur >= 30 && minusDi > plusDi) return false;
    return true;
  };
  const canShort = (i) => {
    const r = detectHighWinReversal(klines, indicators, i);
    if (r.direction !== 'short' || r.points < 3) return false;
    const adxCur = indicators.adx ? indicators.adx.adx[i] : null;
    const plusDi = indicators.adx ? indicators.adx.plusDi[i] : null;
    const minusDi = indicators.adx ? indicators.adx.minusDi[i] : null;
    if (adxCur != null && plusDi != null && minusDi != null && adxCur >= 30 && plusDi > minusDi) return false;
    return true;
  };

  const closeLong = (i, pos, price, reason) => {
    const ratio = price / pos.entryPrice;
    const netReturn = (1 - feeRate) * (1 - feeRate) * ratio - 1;
    longEquity = pos.entryEquity * ratio * (1 - feeRate);
    trades.push({
      side: 'long',
      entryTime: pos.entryTime,
      exitTime: klines[i].time,
      entryPrice: pos.entryPrice,
      exitPrice: price,
      netReturn,
      bars: i - pos.entryIndex,
      exitReason: reason,
      closed: true
    });
  };

  const closeShort = (i, pos, price, reason) => {
    const ratio = price / pos.entryPrice;
    const netReturn = (1 - feeRate) * (1 - feeRate) * Math.max(0, 2 - ratio) - 1;
    shortEquity = Math.max(0, pos.entryEquity * (2 - ratio)) * (1 - feeRate);
    trades.push({
      side: 'short',
      entryTime: pos.entryTime,
      exitTime: klines[i].time,
      entryPrice: pos.entryPrice,
      exitPrice: price,
      netReturn,
      bars: i - pos.entryIndex,
      exitReason: reason,
      closed: true
    });
  };

  for (let i = start; i < klines.length; i += 1) {
    const signal = analyzeAt(indicators, i);
    const price = klines[i].close;

    if (!longPos && canLong(i)) {
      longEquity *= 1 - feeRate;
      const atr = atrSeries[i] || price * 0.01;
      longPos = {
        entryPrice: price,
        entryTime: klines[i].time,
        entryIndex: i,
        entryEquity: longEquity,
        stopPct: Math.max(0.02, (1.8 * atr) / price),
        targetPct: Math.max(0.04, (2.5 * atr) / price)
      };
    }
    if (!shortPos && canShort(i)) {
      shortEquity *= 1 - feeRate;
      const atr = atrSeries[i] || price * 0.01;
      shortPos = {
        entryPrice: price,
        entryTime: klines[i].time,
        entryIndex: i,
        entryEquity: shortEquity,
        stopPct: Math.max(0.02, (1.8 * atr) / price),
        targetPct: Math.max(0.04, (2.5 * atr) / price)
      };
    }

    if (longPos) {
      const stopPrice = longPos.entryPrice * (1 - longPos.stopPct);
      if (klines[i].low <= stopPrice) {
        closeLong(i, longPos, stopPrice, 'stop');
        longPos = null;
      } else if (klines[i].high >= longPos.entryPrice * (1 + longPos.targetPct)) {
        closeLong(i, longPos, longPos.entryPrice * (1 + longPos.targetPct), 'target');
        longPos = null;
      } else if (indicators.closes[i] != null && indicators.bb.middle[i] != null && indicators.closes[i] >= indicators.bb.middle[i]) {
        closeLong(i, longPos, price, 'mid');
        longPos = null;
      } else if (indicators.rsi[i] != null && indicators.rsi[i] >= 55) {
        closeLong(i, longPos, price, 'rsi');
        longPos = null;
      } else if (signal.score >= threshold) {
        closeLong(i, longPos, price, 'signal');
        longPos = null;
      }
    }

    if (shortPos) {
      const stopPrice = shortPos.entryPrice * (1 + shortPos.stopPct);
      if (klines[i].high >= stopPrice) {
        closeShort(i, shortPos, stopPrice, 'stop');
        shortPos = null;
      } else if (klines[i].low <= shortPos.entryPrice * (1 - shortPos.targetPct)) {
        closeShort(i, shortPos, shortPos.entryPrice * (1 - shortPos.targetPct), 'target');
        shortPos = null;
      } else if (indicators.closes[i] != null && indicators.bb.middle[i] != null && indicators.closes[i] <= indicators.bb.middle[i]) {
        closeShort(i, shortPos, price, 'mid');
        shortPos = null;
      } else if (indicators.rsi[i] != null && indicators.rsi[i] <= 45) {
        closeShort(i, shortPos, price, 'rsi');
        shortPos = null;
      } else if (signal.score <= -threshold) {
        closeShort(i, shortPos, price, 'signal');
        shortPos = null;
      }
    }

    const currentLong = longPos ? longPos.entryEquity * (price / longPos.entryPrice) : longEquity;
    const currentShort = shortPos ? Math.max(0, shortPos.entryEquity * (2 - price / shortPos.entryPrice)) : shortEquity;
    const currentEquity = currentLong + currentShort;
    curve.push({ time: klines[i].time, equity: currentEquity });
    if (currentEquity > peak) peak = currentEquity;
    const drawdown = peak > 0 ? (currentEquity - peak) / peak : 0;
    if (drawdown < maxDrawdown) maxDrawdown = drawdown;
  }

  if (longPos) {
    const lastIndex = klines.length - 1;
    const lastPrice = klines[lastIndex].close;
    closeLong(lastIndex, longPos, lastPrice, 'open');
    trades[trades.length - 1].closed = false;
  }
  if (shortPos) {
    const lastIndex = klines.length - 1;
    const lastPrice = klines[lastIndex].close;
    closeShort(lastIndex, shortPos, lastPrice, 'open');
    trades[trades.length - 1].closed = false;
  }

  const firstClose = klines[start].close;
  const lastClose = klines[klines.length - 1].close;
  const buyHoldCurve = klines.slice(start).map((k) => ({
    time: k.time,
    equity: capital * (1 - feeRate) * (k.close / firstClose)
  }));
  const totalReturn = ((longEquity + shortEquity) / capital - 1) * 100;
  const longReturn = (longEquity / (capital / 2) - 1) * 100;
  const shortReturn = (shortEquity / (capital / 2) - 1) * 100;
  const buyHoldReturn = ((1 - feeRate) * (lastClose / firstClose) - 1) * 100;
  const closedTrades = trades.filter((t) => t.closed);
  const wins = closedTrades.filter((t) => t.netReturn > 0);
  const losses = closedTrades.filter((t) => t.netReturn <= 0);
  const grossProfit = wins.reduce((sum, t) => sum + t.netReturn, 0);
  const grossLoss = Math.abs(losses.reduce((sum, t) => sum + t.netReturn, 0));
  const winRate = closedTrades.length ? (wins.length / closedTrades.length) * 100 : 0;
  const profitFactor = grossLoss > 0 ? grossProfit / grossLoss : grossProfit > 0 ? 99 : 0;
  const avgWin = wins.length ? (grossProfit / wins.length) * 100 : 0;
  const avgLoss = losses.length ? (-grossLoss / losses.length) * 100 : 0;
  const avgNet = closedTrades.length ? (closedTrades.reduce((sum, t) => sum + t.netReturn, 0) / closedTrades.length) * 100 : 0;
  const lastTime = klines[klines.length - 1].time;
  const days = Math.max(1, (lastTime - firstTime) / 86400000);
  const annualized = (Math.pow((longEquity + shortEquity) / capital, 365 / days) - 1) * 100;

  return {
    curve,
    buyHoldCurve,
    trades,
    totalReturn,
    longReturn,
    shortReturn,
    buyHoldReturn,
    maxDrawdown: maxDrawdown * 100,
    winRate,
    closedTrades: closedTrades.length,
    openTrades: trades.length - closedTrades.length,
    profitFactor,
    avgWin,
    avgLoss,
    avgNet,
    annualized,
    mode: 'revert',
    firstTime,
    lastTime
  };
}

function backtest(klines, indicators, threshold = 4, feeRate = 0.001, capital = 10000, mode = 'trend') {
  if (mode === 'dual') {
    return backtestDual(klines, indicators, threshold, feeRate, capital);
  }
  if (mode === 'revert') {
    return backtestRevertDual(klines, indicators, threshold, feeRate, capital);
  }
  const start = 34;
  let equity = capital;
  let inPosition = false;
  let entryPrice = 0;
  let entryTime = 0;
  let entryIndex = 0;
  let entryEquity = 0;
  let stopPct = 0;
  let targetPct = 0;
  const curve = [];
  const trades = [];
  let peak = capital;
  let maxDrawdown = 0;
  const firstTime = klines[start].time;
  const atrSeries = calcATRSeries(klines, 14);

  for (let i = start; i < klines.length; i += 1) {
    const signal = analyzeAt(indicators, i);
    const price = klines[i].close;
    const canEnter = mode === 'score'
      ? signal.score >= threshold
      : mode === 'revert'
        ? signal.score <= -threshold &&
          indicators.bb.percentB[i] != null &&
          indicators.bb.percentB[i] <= 0.2 &&
          indicators.rsi[i] != null &&
          indicators.rsi[i] <= 40
        : signal.score >= threshold && trendLong(indicators, i) && (mode === 'trend' || volumeConfirm(klines, i));
    if (!inPosition && canEnter) {
      inPosition = true;
      entryPrice = price;
      entryTime = klines[i].time;
      entryIndex = i;
      entryEquity = equity * (1 - feeRate);
      const atr = atrSeries[i] || price * 0.01;
      const isFast = mode === 'fast';
      const isRevert = mode === 'revert';
      stopPct = Math.max(isFast || isRevert ? 0.02 : 0.03, ((isFast ? 1.2 : 1.8) * atr) / price);
      targetPct = Math.max(isFast ? 0.02 : 0.06, ((isFast ? 1.5 : 3.2) * atr) / price);
    }

    let exitPrice = null;
    let exitReason = '';
    if (inPosition && mode !== 'revert' && signal.score <= -threshold) {
      exitPrice = price;
      exitReason = 'signal';
    }
    if (inPosition && mode === 'revert') {
      const stopPrice = entryPrice * (1 - stopPct);
      if (klines[i].low <= stopPrice) {
        exitPrice = stopPrice;
        exitReason = 'stop';
      } else if (indicators.closes[i] != null && indicators.bb.middle[i] != null && indicators.closes[i] >= indicators.bb.middle[i]) {
        exitPrice = price;
        exitReason = 'mid';
      } else if (indicators.rsi[i] != null && indicators.rsi[i] >= 55) {
        exitPrice = price;
        exitReason = 'rsi';
      } else if (signal.score >= threshold) {
        exitPrice = price;
        exitReason = 'signal';
      }
    }
    if (inPosition && (mode === 'trendRisk' || mode === 'fast')) {
      const stopPrice = entryPrice * (1 - stopPct);
      const targetPrice = entryPrice * (1 + targetPct);
      if (klines[i].low <= stopPrice) {
        exitPrice = stopPrice;
        exitReason = 'stop';
      } else if (klines[i].high >= targetPrice) {
        exitPrice = targetPrice;
        exitReason = 'target';
      } else if (indicators.closes[i] != null && indicators.ema20[i] != null && indicators.closes[i] < indicators.ema20[i]) {
        exitPrice = price;
        exitReason = 'trend';
      }
    }

    if (inPosition && exitPrice != null) {
      const grossReturn = exitPrice / entryPrice - 1;
      const netReturn = (1 - feeRate) * (1 - feeRate) * (exitPrice / entryPrice) - 1;
      equity *= (1 - feeRate) * (1 - feeRate) * (exitPrice / entryPrice);
      trades.push({
        entryTime,
        exitTime: klines[i].time,
        entryPrice,
        exitPrice,
        grossReturn,
        netReturn,
        bars: i - entryIndex,
        exitReason,
        closed: true
      });
      inPosition = false;
    }

    let currentEquity = equity;
    if (inPosition) {
      currentEquity = entryEquity * (price / entryPrice);
    }
    curve.push({ time: klines[i].time, equity: currentEquity });
    if (currentEquity > peak) peak = currentEquity;
    const drawdown = peak > 0 ? (currentEquity - peak) / peak : 0;
    if (drawdown < maxDrawdown) maxDrawdown = drawdown;
  }

  if (inPosition) {
    const lastIndex = klines.length - 1;
    const lastPrice = klines[lastIndex].close;
    const grossReturn = lastPrice / entryPrice - 1;
    const netReturn = (1 - feeRate) * (1 - feeRate) * (lastPrice / entryPrice) - 1;
    trades.push({
      entryTime,
      exitTime: klines[lastIndex].time,
      entryPrice,
      exitPrice: lastPrice,
      grossReturn,
      netReturn,
      bars: lastIndex - entryIndex,
      exitReason: 'open',
      closed: false
    });
    equity = entryEquity * (lastPrice / entryPrice) * (1 - feeRate);
  }

  const firstClose = klines[start].close;
  const lastClose = klines[klines.length - 1].close;
  const buyHoldCurve = klines.slice(start).map((k) => ({
    time: k.time,
    equity: capital * (1 - feeRate) * (k.close / firstClose)
  }));
  const totalReturn = (equity / capital - 1) * 100;
  const buyHoldReturn = ((1 - feeRate) * (lastClose / firstClose) - 1) * 100;
  const closedTrades = trades.filter((t) => t.closed);
  const wins = closedTrades.filter((t) => t.netReturn > 0);
  const losses = closedTrades.filter((t) => t.netReturn <= 0);
  const grossProfit = wins.reduce((sum, t) => sum + t.netReturn, 0);
  const grossLoss = Math.abs(losses.reduce((sum, t) => sum + t.netReturn, 0));
  const winRate = closedTrades.length ? (wins.length / closedTrades.length) * 100 : 0;
  const profitFactor = grossLoss > 0 ? grossProfit / grossLoss : grossProfit > 0 ? 99 : 0;
  const avgWin = wins.length ? (grossProfit / wins.length) * 100 : 0;
  const avgLoss = losses.length ? (-grossLoss / losses.length) * 100 : 0;
  const avgNet = closedTrades.length ? (closedTrades.reduce((sum, t) => sum + t.netReturn, 0) / closedTrades.length) * 100 : 0;
  const lastTime = klines[klines.length - 1].time;
  const days = Math.max(1, (lastTime - firstTime) / 86400000);
  const annualized = (Math.pow(equity / capital, 365 / days) - 1) * 100;

  return {
    curve,
    buyHoldCurve,
    trades,
    totalReturn,
    buyHoldReturn,
    maxDrawdown: maxDrawdown * 100,
    winRate,
    closedTrades: closedTrades.length,
    openTrades: trades.length - closedTrades.length,
    profitFactor,
    avgWin,
    avgLoss,
    avgNet,
    annualized,
    mode,
    firstTime,
    lastTime
  };
}

function formatDateShort(timestamp) {
  const date = new Date(timestamp);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}

function renderBacktest(result) {
  const summary = $('backtestSummary');
  const tradeList = $('backtestTradeList');
  const tradeCount = $('backtestTradeCount');
  if (!result) {
    summary.innerHTML = '<div class="empty-smart">运行回测后显示结果</div>';
    tradeList.innerHTML = '';
    tradeCount.textContent = '--';
    $('backtestSample').textContent = '等待运行回测';
    return;
  }

  const modeLabel = result.mode === 'dual'
    ? '多空双开'
    : result.mode === 'trendRisk'
      ? '趋势 + ATR风控'
      : result.mode === 'fast'
        ? '高胜率短线'
        : result.mode === 'revert'
          ? '高胜率反转'
      : result.mode === 'trend'
        ? '趋势过滤'
        : '基础信号';
  $('backtestSample').textContent = `${result.symbol || state.symbol} · ${result.interval || state.interval} · ${modeLabel} · ${result.bars || '--'}根K线 · ${formatDateShort(result.firstTime)} 至 ${formatDateShort(result.lastTime)}`;
  $('backtestTradeCount').textContent = `${result.closedTrades}次完成 · ${result.openTrades}笔持仓`;
  summary.innerHTML = [
    ['策略收益', formatPct(result.totalReturn), result.totalReturn >= 0 ? 'up' : 'down'],
    ['买入持有', formatPct(result.buyHoldReturn), result.buyHoldReturn >= 0 ? 'up' : 'down'],
    ['最大回撤', formatPct(result.maxDrawdown), 'down'],
    ['胜率', `${result.winRate.toFixed(1)}%`, result.winRate >= 50 ? 'up' : 'down'],
    ['盈亏比', result.profitFactor.toFixed(2), result.profitFactor >= 1 ? 'up' : 'down'],
    ['年化收益', formatPct(result.annualized), result.annualized >= 0 ? 'up' : 'down'],
    ['平均单笔', formatPct(result.avgNet), result.avgNet >= 0 ? 'up' : 'down'],
    ...(result.longReturn != null ? [
      ['多头收益', formatPct(result.longReturn), result.longReturn >= 0 ? 'up' : 'down'],
      ['空头收益', formatPct(result.shortReturn), result.shortReturn >= 0 ? 'up' : 'down']
    ] : []),
    ['样本K线', `${result.bars || '--'}根`, '']
  ]
    .map(([label, value, cls]) => `<div class="bt-stat"><span>${label}</span><strong class="${cls}">${value}</strong></div>`)
    .join('');

  const recent = result.trades.slice(-8).reverse();
  if (!recent.length) {
    tradeList.innerHTML = '<div class="empty-smart">回测期内没有交易</div>';
    return;
  }
  tradeList.innerHTML = recent
    .map((trade) => {
      const cls = trade.netReturn >= 0 ? 'up' : 'down';
      return `
        <div class="bt-trade-row">
          <span class="trade-time">${trade.side === 'short' ? '空' : '多'} ${formatDateShort(trade.entryTime)}</span>
          <span class="trade-time">${formatDateShort(trade.exitTime)}</span>
          <span class="trade-qty">${trade.bars}根</span>
          <span class="trade-value ${cls}">${trade.netReturn >= 0 ? '+' : ''}${(trade.netReturn * 100).toFixed(2)}%</span>
        </div>
      `;
    })
    .join('');
}

function drawBacktestChart() {
  const canvas = $('backtestChart');
  const { ctx, width, height } = setupCanvas(canvas);
  const result = state.backtest;
  if (!result || !result.curve || !result.curve.length) {
    drawEmptyChart(ctx, width, height, '回测资金曲线');
    return;
  }

  const padL = 12;
  const padR = 58;
  const padT = 24;
  const padB = 24;
  const plotW = Math.max(1, width - padL - padR);
  const plotH = Math.max(1, height - padT - padB);
  const strategy = result.curve.map((p) => p.equity);
  const buyHold = result.buyHoldCurve.map((p) => p.equity);
  let min = Infinity;
  let max = -Infinity;
  for (const value of strategy.concat(buyHold)) {
    min = Math.min(min, value);
    max = Math.max(max, value);
  }
  const range = max - min || max * 0.01 || 1;
  const yMin = min - range * 0.08;
  const yMax = max + range * 0.08;
  const xFor = (i) => padL + (plotW * i) / (strategy.length - 1);
  const yFor = (v) => padT + ((yMax - v) / (yMax - yMin)) * plotH;

  ctx.font = '11px "Cascadia Mono", Consolas, monospace';
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  for (let i = 0; i <= 4; i += 1) {
    const ratio = i / 4;
    const y = padT + plotH * ratio;
    const value = yMax - (yMax - yMin) * ratio;
    ctx.strokeStyle = 'rgba(38, 50, 59, 0.5)';
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(padL + plotW, y);
    ctx.stroke();
    ctx.fillStyle = '#5d6c77';
    ctx.fillText(formatUsd(value), width - 4, y);
  }

  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  for (let i = 0; i <= 4; i += 1) {
    const idx = Math.round((i * (strategy.length - 1)) / 4);
    ctx.fillStyle = '#5d6c77';
    ctx.fillText(formatDateShort(result.curve[idx].time), xFor(idx), padT + plotH + 7);
  }

  ctx.fillStyle = '#42c8e6';
  ctx.fillText('策略', padL, 10);
  ctx.fillStyle = '#f7b955';
  ctx.fillText('买入持有', padL + 52, 10);

  strokeLine(ctx, strategy, xFor, yFor, '#42c8e6', 1.8);
  strokeLine(ctx, buyHold, xFor, yFor, '#f7b955', 1.4, [5, 4]);
}

async function runBacktest() {
  const symbol = state.symbol;
  const interval = $('btInterval').value;
  const threshold = Number($('btThreshold').value);
  const mode = $('btMode').value;
  const bars = Number($('btBars').value);
  const button = $('runBacktestBtn');
  button.disabled = true;
  button.textContent = '回测中';
  try {
    const { klines, provider } = await fetchKlinesForBacktest(symbol, interval, bars);
    if (klines.length < 80) {
      throw new Error('历史数据不足');
    }
    const indicators = computeIndicators(klines);
    const result = backtest(klines, indicators, threshold, 0.001, 10000, mode);
    result.symbol = symbol;
    result.interval = interval;
    result.threshold = threshold;
    result.provider = provider;
    result.mode = mode;
    result.bars = klines.length;
    state.backtest = result;
    renderBacktest(result);
    drawBacktestChart();
  } catch (err) {
    showToast(`回测失败：${err.message}`);
    renderBacktest(null);
    drawBacktestChart();
  } finally {
    button.disabled = false;
    button.textContent = '运行回测';
  }
}

function maybeLogDetail() {
  if (!state.data || !state.analysis) return;
  const key = `${state.symbol}|${state.interval}`;
  const prev = state.lastDetailLabel[key];
  if (prev && prev !== state.analysis.label && Math.abs(state.analysis.score) >= 4) {
    addLog(state.symbol, state.analysis, state.interval);
  }
  state.lastDetailLabel[key] = state.analysis.label;
}

async function loadMain() {
  if (state.mainLoading) return;
  state.mainLoading = true;
  setConn('loading');
  try {
    const { klines, provider } = await fetchKlinesWithFallback(state.symbol, state.interval);
    state.data = klines;
    state.provider = provider;
    state.indicators = computeIndicators(klines);
    state.analysis = analyzeIndicators(klines, state.indicators);
    state.hasLoaded = true;
    renderMain();
    drawCharts();
    renderSmartMoney();
    setConn('ok', provider);
    setLastUpdated();
    maybeLogDetail();
  } catch (err) {
    setConn('err');
    renderMain();
    setLastUpdated();
    if (!state.hasLoaded) {
      showToast(`行情加载失败：${err.message}`);
    }
  } finally {
    state.mainLoading = false;
  }
}

async function scanWatchlist() {
  if (state.scanning || !state.watchlist.length) return;
  state.scanning = true;
  try {
    const settled = await Promise.allSettled(
      state.watchlist.map(async (symbol) => {
        try {
          const { klines } = await fetchKlinesWithFallback(symbol, '4h');
          const indicators = computeIndicators(klines);
          const analysis = analyzeIndicators(klines, indicators);
          return {
            symbol,
            analysis,
            last: klines[klines.length - 1].close,
            change: getChange(klines, '4h'),
            closes: klines.slice(-48).map((k) => k.close)
          };
        } catch (err) {
          return { symbol, error: err.message };
        }
      })
    );
    const next = {};
    for (const result of settled) {
      if (result.status === 'fulfilled' && result.value && !result.value.error) {
        const value = result.value;
        next[value.symbol] = value;
      }
    }
    state.scanResults = next;
    for (const symbol of state.watchlist) {
      const snap = state.scanResults[symbol];
      if (!snap) continue;
      const prev = state.scanLabels[`${symbol}|4h`];
      if (prev && prev !== snap.analysis.label && Math.abs(snap.analysis.score) >= 4) {
        addLog(symbol, snap.analysis, '4h');
      }
      state.scanLabels[`${symbol}|4h`] = snap.analysis.label;
    }
    renderWatchlist();
    renderOverview();
  } finally {
    state.scanning = false;
  }
}

function refreshAll() {
  loadMain();
  scanWatchlist();
  loadSmartMoney();
  loadMicroSignal();
}

function selectSymbol(symbol) {
  if (symbol === state.symbol) return;
  state.symbol = symbol;
  state.data = null;
  state.indicators = null;
  state.analysis = null;
  state.smartMoney = null;
  state.micro = null;
  renderMain();
  renderWatchlist();
  renderSmartMoney();
  renderMicroSignal();
  loadMain();
  loadSmartMoney();
  loadMicroSignal();
}

function drawCharts() {
  drawPriceChart();
  drawMacdChart();
  drawKdjChart();
  drawRsiChart();
  drawAdxChart();
  drawStochRsiChart();
}

function bindEvents() {
  $('refreshBtn').addEventListener('click', refreshAll);

  $('autoRefreshBtn').addEventListener('click', () => {
    state.auto = !state.auto;
    $('autoRefreshBtn').classList.toggle('on', state.auto);
    if (state.auto) startAuto();
    else stopAuto();
  });

  $('notifyBtn').addEventListener('click', async () => {
    if ('Notification' in window && Notification.permission === 'default') {
      const permission = await Notification.requestPermission();
      if (permission === 'denied') {
        showToast('浏览器通知被拒绝，仍可使用声音提醒');
      }
    }
    state.notify = !state.notify;
    writeJSON('coinpulse.notify', state.notify);
    setNotifyButton();
    if (state.notify) {
      ensureAudio();
      showToast('信号提醒已开启');
    } else {
      showToast('信号提醒已关闭');
    }
  });

  $('installBtn').addEventListener('click', async () => {
    if (state.deferredPrompt) {
      state.deferredPrompt.prompt();
      await state.deferredPrompt.userChoice;
      state.deferredPrompt = null;
    } else {
      showToast('请在浏览器菜单中选择“添加到主屏幕”');
    }
  });

  window.addEventListener('beforeinstallprompt', (event) => {
    event.preventDefault();
    state.deferredPrompt = event;
  });

  window.addEventListener('appinstalled', () => {
    state.deferredPrompt = null;
    showToast('CoinPulse 已安装到主屏幕');
  });

  $('tfSwitch').addEventListener('click', (event) => {
    const btn = event.target.closest('button[data-tf]');
    if (!btn) return;
    state.interval = btn.dataset.tf;
    document.querySelectorAll('#tfSwitch button').forEach((item) => item.classList.toggle('active', item === btn));
    loadMain();
  });

  $('watchlist').addEventListener('click', (event) => {
    const item = event.target.closest('.watch-item');
    if (item && item.dataset.symbol) {
      selectSymbol(item.dataset.symbol);
    }
  });

  $('addSymbolBtn').addEventListener('click', () => {
    $('addRow').classList.remove('hidden');
    $('addSymbolBtn').classList.add('hidden');
    $('symbolInput').focus();
  });

  $('cancelAddBtn').addEventListener('click', () => {
    $('addRow').classList.add('hidden');
    $('addSymbolBtn').classList.remove('hidden');
    $('symbolInput').value = '';
  });

  $('confirmAddBtn').addEventListener('click', addSymbol);
  $('symbolInput').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') addSymbol();
    if (event.key === 'Escape') {
      $('addRow').classList.add('hidden');
      $('addSymbolBtn').classList.remove('hidden');
      $('symbolInput').value = '';
    }
  });

  $('clearLogBtn').addEventListener('click', () => {
    state.log = [];
    writeJSON('coinpulse.log', state.log);
    renderSignalLog();
    showToast('已清空信号记录');
  });

  $('runBacktestBtn').addEventListener('click', runBacktest);

  window.addEventListener('resize', () => {
    clearTimeout(window.__resizeTimer);
    window.__resizeTimer = setTimeout(() => {
      drawCharts();
      drawBacktestChart();
    }, 80);
  });

  if (window.ResizeObserver) {
    new ResizeObserver(drawCharts).observe($('chartWrap'));
  }

  if (window.lucide && lucide.createIcons) {
    try {
      lucide.createIcons();
    } catch {
      // icons are decorative; ignore failures
    }
  }
}

function addSymbol() {
  const input = $('symbolInput');
  const raw = input.value.trim().toUpperCase();
  if (!/^[A-Z0-9]{3,20}$/.test(raw)) {
    showToast('请输入有效交易对，例如 BTCUSDT');
    return;
  }
  const symbol = raw.endsWith('USDT') ? raw : `${raw}USDT`;
  if (state.watchlist.includes(symbol)) {
    showToast(`${symbol} 已在自选列表`);
    return;
  }
  state.watchlist.push(symbol);
  writeJSON('coinpulse.watchlist', state.watchlist);
  input.value = '';
  $('addRow').classList.add('hidden');
  $('addSymbolBtn').classList.remove('hidden');
  renderWatchlist();
  renderOverview();
  scanWatchlist();
  selectSymbol(symbol);
}

let autoTimer = null;

function startAuto() {
  stopAuto();
  if (state.auto) {
    autoTimer = setInterval(refreshAll, 30000);
  }
}

function stopAuto() {
  clearInterval(autoTimer);
  autoTimer = null;
}

function init() {
  bindEvents();
  setNotifyButton();
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('sw.js').catch(() => {
        // service worker needs a secure origin; local network may not support it
      });
    });
  }
  renderMain();
  renderWatchlist();
  renderOverview();
  renderSignalLog();
  renderSmartMoney();
  renderMicroSignal();
  renderBacktest(null);
  drawBacktestChart();
  refreshAll();
  startAuto();
}

init();
