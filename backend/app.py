"""DeltaFStation 应用入口。

四个页面：首页 / 数据管理 / 每日打分 / 回测中心。
所有接口挂在 /api 下，统一 {ok, data} 响应格式（见 backend/api/__init__.py）。

启动：python run.py
"""
from __future__ import annotations

import logging
from pathlib import Path

from flask import Flask, render_template, request
from flask_cors import CORS

from backend.api import fail, ok
from backend.api.backtest_api import backtest_bp
from backend.api.data_api import data_bp
from backend.api.job_api import job_bp
from backend.api.score_api import score_bp
from backend.core.config import get_config
from backend.core.lake import get_lake
from backend.core.meta_db import get_meta_db
from backend.core.snapshots import get_score_store

BASE_DIR = Path(__file__).resolve().parents[1]

logger = logging.getLogger(__name__)


def _health_payload() -> dict:
    """汇总数据与打分状态，供首页与健康检查使用。"""
    cfg = get_config()
    lake = get_lake()
    store = get_score_store()

    datasets = {}
    for name in ("bars", "valuation", "adj_factor", "fundamental", "universe", "calendar"):
        datasets[name] = {
            "exists": lake.exists(name),
            "latest_date": lake.latest_date(name) if name in ("bars", "valuation") else None,
        }

    universe_count = 0
    if lake.exists("universe"):
        try:
            universe_count = int(len(lake.read("universe", columns=["code"])))
        except Exception:
            universe_count = 0

    return {
        "version": cfg.version,
        "datasets": datasets,
        "universe_count": universe_count,
        "score": {
            "versions": store.versions(),
            "latest_date": store.latest_date(),
        },
        "schedule": {
            "update_time": cfg.get("schedule.update_time"),
            "enabled": bool(cfg.get("schedule.enabled")),
        },
    }


def create_app() -> Flask:
    cfg = get_config()
    cfg.paths.ensure()
    get_meta_db()  # 确保 SQLite 表已创建

    app = Flask(
        __name__,
        template_folder=str(BASE_DIR / "frontend" / "templates"),
        static_folder=str(BASE_DIR / "frontend" / "static"),
    )
    app.config["DELTAF_CONFIG"] = cfg
    app.json.ensure_ascii = False
    CORS(app)

    logging.basicConfig(
        level=logging.DEBUG if cfg.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # ---------- API ----------
    app.register_blueprint(data_bp, url_prefix="/api")
    app.register_blueprint(score_bp, url_prefix="/api")
    app.register_blueprint(backtest_bp, url_prefix="/api")
    app.register_blueprint(job_bp, url_prefix="/api")

    @app.get("/api/health")
    def health():
        try:
            return ok(_health_payload())
        except Exception as exc:  # pragma: no cover
            logger.exception("health check failed")
            return fail("health_error", str(exc), 500)

    # ---------- 页面 ----------
    @app.get("/")
    def page_index():
        return render_template("index.html", active="index", version=cfg.version)

    @app.get("/data")
    def page_data():
        return render_template("data.html", active="data", version=cfg.version)

    @app.get("/scores")
    def page_scores():
        return render_template("scores.html", active="scores", version=cfg.version)

    @app.get("/backtest")
    def page_backtest():
        return render_template("backtest.html", active="backtest", version=cfg.version)

    # ---------- 错误处理 ----------
    @app.errorhandler(404)
    def not_found(error):
        if request.path.startswith("/api/"):
            return fail("not_found", f"接口不存在: {request.path}", 404)
        return render_template("index.html", active="index", version=cfg.version), 404

    @app.errorhandler(500)
    def internal_error(error):
        logger.exception("unhandled error: %s", error)
        return fail("internal_error", "服务端错误，请查看日志", 500)

    return app


if __name__ == "__main__":
    app = create_app()
    conf = get_config()
    app.run(host=conf.host, port=conf.port, debug=conf.debug)
