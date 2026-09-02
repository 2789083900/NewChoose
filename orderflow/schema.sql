CREATE DATABASE IF NOT EXISTS coinpulse CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE coinpulse;

CREATE TABLE IF NOT EXISTS orderflow_1m (
  symbol VARCHAR(20) NOT NULL,
  bucket_start DATETIME(3) NOT NULL,
  buy_volume DECIMAL(36, 12) NOT NULL DEFAULT 0,
  sell_volume DECIMAL(36, 12) NOT NULL DEFAULT 0,
  delta DECIMAL(36, 12) NOT NULL DEFAULT 0,
  cvd DECIMAL(36, 12) NOT NULL DEFAULT 0,
  trade_count INT UNSIGNED NOT NULL DEFAULT 0,
  large_buy_count INT UNSIGNED NOT NULL DEFAULT 0,
  large_sell_count INT UNSIGNED NOT NULL DEFAULT 0,
  large_buy_volume DECIMAL(36, 12) NOT NULL DEFAULT 0,
  large_sell_volume DECIMAL(36, 12) NOT NULL DEFAULT 0,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (symbol, bucket_start),
  KEY idx_orderflow_time (bucket_start),
  KEY idx_orderflow_symbol_time (symbol, bucket_start)
) ENGINE=InnoDB;
