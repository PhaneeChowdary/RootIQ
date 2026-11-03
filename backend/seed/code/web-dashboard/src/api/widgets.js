// GET /api/widgets: the widgets on the dashboard home page. Each request needs one database connection.
const { pool } = require("../db");

async function listWidgets(req, res) {
  const started = Date.now();
  try {
    const { rows } = await pool.query("SELECT id, kind, title, settings FROM widgets WHERE owner_id = $1", [req.user.id]);
    res.json(rows);
    console.info(`[api] GET /api/widgets 200 ${Date.now() - started}ms`);
  } catch (err) {
    if (err.message.includes("timeout")) {
      console.error(`[api] GET /api/widgets 500 timeout acquiring db connection after ${Date.now() - started}ms`);
    }
    res.status(500).json({ error: "temporarily unavailable" });
  }
}

module.exports = { listWidgets };
