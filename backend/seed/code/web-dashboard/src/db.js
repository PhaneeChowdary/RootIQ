// Database pool for the dashboard API. Its size comes from config/<environment>.yaml . Db.pool.max_size.
const { Pool } = require("pg");
const config = require("./config");

const pool = new Pool({
  host: config.db.host,
  database: config.db.name,
  max: config.db.pool.max_size,
  connectionTimeoutMillis: config.db.pool.acquire_timeout_ms,
});

pool.on("acquire", () => {
  if (pool.waitingCount > 10) {
    console.warn(`[db] pool exhausted size=${pool.totalCount} waiting=${pool.waitingCount}`);
  }
});

module.exports = { pool };
